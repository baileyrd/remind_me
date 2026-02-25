# Phase 8: Performance Improvements - Research

**Researched:** 2026-02-24
**Domain:** Python asyncio concurrency, ONNX batch embedding, SQLite write ordering
**Confidence:** HIGH

## Summary

Phase 8 addresses two distinct performance bottlenecks. First, `remind_me_reindex` (in `tools.py`) calls `embedder.embed_one()` per memory in a sequential loop — one ONNX forward pass per item. The embedder already has a batch `embed(list[str])` method that runs a single ONNX forward pass for multiple inputs simultaneously; reindexing should chunk the `missing` list into batches of 32 and call that API instead, reducing ONNX call count by ~32x. Second, `import_directory` (in `importer.py`) processes files with a plain `for` loop — sequential, one file at a time. This must become a bounded-concurrent async fan-out using `asyncio.Semaphore` + `asyncio.gather`, so files are processed in parallel while capping peak resource use.

Both changes are pure performance refactors. No data is lost, no API surface changes, and the existing test harness (213 tests, `pytest`, `asyncio_mode=auto`) continues to run unchanged. The critical constraint is SQLite write safety: the singleton connection is configured with `check_same_thread=False` and WAL mode, but SQLite itself is not safe for truly concurrent writes from multiple threads. The concurrent import approach must serialize all SQLite writes by routing them through `asyncio.to_thread` tasks (which run in a thread pool) but using a semaphore to keep the concurrency bounded, letting the event loop schedule them without true simultaneous writes.

**Primary recommendation:** Batch ONNX calls in `remind_me_reindex` using the existing `embedder.embed()` list API with `EMBED_BATCH_SIZE = 32`; make `import_directory` an async function that dispatches file processing with `asyncio.gather` guarded by an `asyncio.Semaphore(8)` default.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| PERF-01 | Reindex tool processes embeddings in batches of 32 using `embedder.embed()` list API | `_Embedder.embed(list[str])` already exists in `embeddings.py` and returns `np.ndarray` shaped `(N, dim)`; reindex loop in `tools.py` lines 766-772 calls `embed_one` per item; replacing with batch API is a drop-in refactor |
| PERF-02 | Directory import processes files concurrently with semaphore-bounded parallelism | `import_directory()` in `importer.py` uses `for f in sorted(files)` at line 373; `import_chat_file` is synchronous; wrapping each call in `asyncio.to_thread` and gathering with semaphore gives concurrent I/O + CPU overlap |
</phase_requirements>

## Standard Stack

### Core

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `asyncio` (stdlib) | Python 3.11+ | Concurrency primitive — `Semaphore`, `gather`, `to_thread` | Zero new dependencies; already imported in `tools.py` |
| `numpy` | >=1.24.0 (declared dep) | Holds batched embedding results as `ndarray`; `.tobytes()` per row | Already used in embeddings.py and test harness |

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `itertools` (stdlib) | Python 3.11+ | `batched()` (3.12+) or manual chunking for batch slicing | Use manual slice `texts[i:i+BATCH_SIZE]` for 3.11 compat |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `asyncio.Semaphore` | `concurrent.futures.ThreadPoolExecutor` with `max_workers` | ThreadPoolExecutor works but bypasses the asyncio event loop; Semaphore integrates cleanly with `asyncio.gather` — consistent with Phase 7 pattern of `asyncio.to_thread` |
| Manual batch loop | `itertools.batched()` (3.12+) | `batched()` is cleaner but 3.12+ only; project targets 3.11 (`requires-python = ">=3.11"`); use `range(0, len(missing), BATCH_SIZE)` slice instead |

**Installation:**
No new packages required — all tools are stdlib or already declared dependencies.

## Architecture Patterns

### Recommended Project Structure

No structural changes. All edits are within existing files:

```
remind_me_mcp/
├── tools.py         # remind_me_reindex — batch embed refactor (PERF-01)
└── importer.py      # import_directory — async concurrent refactor (PERF-02)
```

### Pattern 1: Batch ONNX Embeddings in `remind_me_reindex` (PERF-01)

**What:** Replace the per-item `embed_one()` loop with chunked calls to `embedder.embed(texts)`, then insert all resulting byte vectors in one shot per batch.

**When to use:** Any time you have N items to embed and N > 1 — batch throughput on ONNX is dramatically higher than N individual calls because tokenization, padding, and the ONNX graph run once per batch.

**Current code (tools.py lines 765-773):**
```python
created = 0
for mem_id, rowid, content in missing:
    try:
        vec_bytes = await asyncio.to_thread(embedder.embed_one, content[:2000])
        db.execute("INSERT OR REPLACE INTO memories_vec(rowid, embedding) VALUES (?, ?)", (rowid, vec_bytes))
        created += 1
    except (sqlite3.OperationalError, ValueError, TypeError) as e:
        log.warning("Failed to embed %s: %s", mem_id, e)

db.commit()
```

**Target pattern:**
```python
EMBED_BATCH_SIZE = 32

created = 0
for batch_start in range(0, len(missing), EMBED_BATCH_SIZE):
    batch = missing[batch_start : batch_start + EMBED_BATCH_SIZE]
    ids    = [item[0] for item in batch]   # mem_id
    rowids = [item[1] for item in batch]   # rowid
    texts  = [item[2][:2000] for item in batch]  # content, truncated
    try:
        vecs: np.ndarray = await asyncio.to_thread(embedder.embed, texts)
        # vecs shape: (len(batch), 384) — float32
        for i, (mem_id, rowid) in enumerate(zip(ids, rowids)):
            vec_bytes = vecs[i].tobytes()
            db.execute(
                "INSERT OR REPLACE INTO memories_vec(rowid, embedding) VALUES (?, ?)",
                (rowid, vec_bytes),
            )
            created += 1
    except (sqlite3.OperationalError, ValueError, TypeError) as e:
        log.warning("Failed to embed batch starting at %s: %s", ids[0], e)

db.commit()
```

**Key detail:** `embedder.embed(texts)` returns `np.ndarray` of shape `(N, dim)`; `vecs[i].tobytes()` produces the same `struct`-packed float32 bytes as `embed_one` — confirmed in `embeddings.py` line 136 (`embed_one` calls `self.embed([text])[0].tobytes()`). The output is byte-for-byte identical to the current approach.

**Constant placement:** Define `EMBED_BATCH_SIZE = 32` as a module-level constant in `tools.py` (or in `config.py` if callers outside tools need it). Module-level is preferred for testability.

### Pattern 2: Semaphore-Bounded Concurrent File Import (PERF-02)

**What:** Convert `import_directory` from a synchronous sequential loop to an `async` function that dispatches each file to `asyncio.to_thread` and gathers results with a bounded semaphore.

**When to use:** When you have 10+ I/O-bound tasks (file reads + ONNX calls) where sequential blocking wastes wall time. Semaphore prevents unbounded resource use.

**Current code (importer.py lines 372-385):**
```python
results: list[dict[str, Any]] = []
for f in sorted(files):
    try:
        r = import_chat_file(...)
        results.append(r)
    except (...) as e:
        ...
```

**Target pattern:**
```python
import asyncio

IMPORT_CONCURRENCY = 8  # module-level constant; semaphore bound

async def import_directory(...) -> dict[str, Any]:
    """..."""
    root = Path(directory)
    ...  # file discovery unchanged

    sem = asyncio.Semaphore(IMPORT_CONCURRENCY)

    async def _import_one(f: Path) -> dict[str, Any]:
        async with sem:
            try:
                return await asyncio.to_thread(
                    import_chat_file,
                    file_path=str(f),
                    category=category,
                    tags=tags,
                    extract_mode=extract_mode,
                    max_length=max_length,
                )
            except (json.JSONDecodeError, UnicodeDecodeError, FileNotFoundError, OSError) as e:
                log.warning("Failed to import %s: %s", f.name, e)
                return {"status": "error", "file": f.name, "error": str(e)}

    results = await asyncio.gather(*[_import_one(f) for f in sorted(files)])
    ...  # summary aggregation unchanged
```

**Caller impact:** `memory_import_directory` in `tools.py` currently calls `import_directory(...)` synchronously (line 453). After the refactor it must `await import_directory(...)`. Because `memory_import_directory` is already `async`, this is a one-word change: `summary = await import_directory(...)`.

**`import_directory` in `tools.py` `remind_me_import_directory` handler:** The tool handler already is `async def memory_import_directory`. The only required change is adding `await` before the `import_directory()` call.

### Anti-Patterns to Avoid

- **Calling `embedder.embed_one` inside `asyncio.gather`:** Parallelising the ONNX forward pass across threads does not help — ONNX Runtime with `CPUExecutionProvider` uses its own internal threading; multiple simultaneous calls contend on CPU, not run faster. Batch the texts, one `to_thread` call per batch.
- **Unbounded `asyncio.gather` over all files at once:** With hundreds of files, this creates hundreds of concurrent threads/tasks, exhausting the threadpool and possibly the database lock. Always bound concurrency with `asyncio.Semaphore`.
- **Committing inside each loop iteration:** The current reindex code commits after the full loop (`db.commit()` at line 774). Keep that pattern in the batch refactor — do not move `commit()` inside the batch loop, as that generates excessive WAL fsyncs.
- **Making `import_chat_file` async:** It is synchronous and calls `_embed_and_store` directly (synchronous). Keep it synchronous and dispatch it via `asyncio.to_thread`. Changing it to `async` would require deeper refactoring with no benefit given ONNX is blocking CPU work anyway.
- **SQLite concurrent write collisions:** The singleton `_db_connection` (`check_same_thread=False`) is shared. WAL mode allows concurrent readers, but concurrent writers still serialize at the SQLite level. The semaphore bound of 8 is conservative — it is safe because even with 8 concurrent `asyncio.to_thread` tasks, each task's writes are serialized by Python's GIL during the `db.execute()` call.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Batch slicing | Custom chunker | `range(0, N, BATCH_SIZE)` + list slice | Simple, idiomatic, no dependency |
| Concurrency limiting | Custom queue or pool | `asyncio.Semaphore` | Standard, composable, no extra imports |
| Thread dispatch | OS threads directly | `asyncio.to_thread` | Integrates with event loop, exception propagation, already used in the codebase |

**Key insight:** The entire ONNX batch API already exists in `_Embedder.embed(list[str])`. PERF-01 is fundamentally about calling code that already exists — no new compute logic is required.

## Common Pitfalls

### Pitfall 1: Batch error handling loses per-item granularity

**What goes wrong:** If you wrap the entire batch `try/except`, a single bad item (e.g., one memory with a corrupt content string) silently drops the whole batch of 32.

**Why it happens:** The current per-item loop catches per-item errors. Batching moves the exception to the batch level.

**How to avoid:** After the batch `embed()` call succeeds, insert vectors one at a time in an inner loop (as shown in the target pattern above). Only the `embed()` call itself is in the batch try/except — the per-row DB inserts are wrapped individually.

**Warning signs:** `created` count is a multiple of 32 lower than expected.

### Pitfall 2: `asyncio.Semaphore` created outside the event loop (Python 3.9 regression)

**What goes wrong:** Creating `asyncio.Semaphore` at module level or before an event loop is running raises `RuntimeError: no running event loop` in Python 3.10+.

**Why it happens:** Semaphore attaches to the running loop at construction time.

**How to avoid:** Create the semaphore inside the `async` function body (as shown in the target pattern: `sem = asyncio.Semaphore(IMPORT_CONCURRENCY)` inside `import_directory`). The constant `IMPORT_CONCURRENCY` is module-level (integer), the semaphore instance is local.

**Warning signs:** `RuntimeError: no running event loop` on import or first call in tests.

### Pitfall 3: `import_directory` callers in tests are synchronous

**What goes wrong:** `test_importer.py` currently calls `import_directory` synchronously. After making it `async`, the test calls break.

**Why it happens:** Pure unit tests in `test_importer.py` likely call `import_directory` directly. With `asyncio_mode=auto` in `pyproject.toml`, `async` test functions work, but non-async test functions cannot `await`.

**How to avoid:** Check all test calls to `import_directory` and either (a) make those test functions `async` or (b) use `asyncio.run()` if they must stay synchronous. Prefer making them `async` (consistent with `asyncio_mode=auto`).

**Warning signs:** `TypeError: object dict_keys can't be used in 'await' expression` or `coroutine ... was never awaited`.

### Pitfall 4: SQLite rowid lookup inside `asyncio.to_thread` calls for import

**What goes wrong:** `import_chat_file` calls `_get_db()` to get the singleton connection, then reads/writes. With 8 concurrent `to_thread` tasks, all share the same connection. Concurrent reads are fine (WAL), but `INSERT` plus `COMMIT` from two threads simultaneously can cause `database is locked` even with `timeout=10`.

**Why it happens:** WAL allows concurrent readers + one writer, but the writer lock is not re-entrant from different threads. The semaphore bound of 8 reduces but does not eliminate collision risk.

**How to avoid:** The semaphore at 8 is conservative; in practice, most time is spent in file I/O and ONNX (not SQLite). The `busy_timeout=5000` on the connection handles transient locks. Monitor for `sqlite3.OperationalError: database is locked` in test output. If it appears under test concurrency, reduce `IMPORT_CONCURRENCY` to 4 or serialize the `db.commit()` with a threading lock.

**Warning signs:** `sqlite3.OperationalError: database is locked` in test output when running tests with mock embedder and 10+ files.

### Pitfall 5: Test for batch call count requires mock that tracks calls

**What goes wrong:** PERF-01 success criterion is "measurable via reduced call count in tests." The existing `FakeEmbedder` in `conftest.py` does not track call counts. A test asserting batch behavior must use a spy or counter.

**Why it happens:** `FakeEmbedder.embed()` always succeeds silently. There is no assertion surface for "was `embed` called N times with lists of 32?"

**How to avoid:** Use `monkeypatch` + a custom spy wrapper or `unittest.mock.patch` to count `embed()` calls. Example:
```python
call_log: list[list[str]] = []
original_embed = fake_embedder.embed

def spy_embed(texts):
    call_log.append(texts)
    return original_embed(texts)

monkeypatch.setattr(fake_embedder, "embed", spy_embed)
# ... run reindex with 64 items ...
assert len(call_log) == 2  # 64 items / 32 batch = 2 calls
assert all(len(batch) <= 32 for batch in call_log)
```

## Code Examples

Verified patterns from the project codebase:

### Existing `embed()` batch API (embeddings.py lines 88-120)

```python
def embed(self, texts: list[str]) -> np.ndarray:
    """Embed a batch of texts using the ONNX model.
    Returns Float32 numpy array of shape (N, dim), L2-normalised.
    """
    self._ensure_loaded()
    encoded = self._tokenizer.encode_batch(texts)
    input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
    attention_mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
    token_type_ids = np.zeros_like(input_ids)

    outputs = self._session.run(None, {
        "input_ids": input_ids,
        "attention_mask": attention_mask,
        "token_type_ids": token_type_ids,
    })

    embeddings = outputs[0]  # shape: (batch, seq_len, dim)
    mask_expanded = attention_mask[:, :, np.newaxis].astype(np.float32)
    pooled = (embeddings * mask_expanded).sum(axis=1) / mask_expanded.sum(axis=1).clip(min=1e-9)

    norms = np.linalg.norm(pooled, axis=1, keepdims=True).clip(min=1e-9)
    return (pooled / norms).astype(np.float32)
```

### `embed_one` is just `embed([text])[0].tobytes()` (embeddings.py lines 122-137)

```python
def embed_one(self, text: str) -> bytes:
    vec = self.embed([text])[0]
    return vec.tobytes()
```

This confirms `vecs[i].tobytes()` is byte-for-byte equivalent to the old `embed_one(content)` for each item.

### `asyncio.to_thread` pattern already used (tools.py line 768)

```python
vec_bytes = await asyncio.to_thread(embedder.embed_one, content[:2000])
```

Replace with batch variant:
```python
vecs = await asyncio.to_thread(embedder.embed, texts)
```

### Existing async tool handler (`memory_import_directory`, tools.py lines 439-461)

```python
async def memory_import_directory(params: BulkImportDirInput) -> str:
    summary = import_directory(         # <-- becomes: await import_directory(
        directory=params.directory,
        ...
    )
    return json.dumps(summary, indent=2)
```

### `FakeEmbedder.embed()` in conftest.py (lines 122-133) — handles batches already

```python
def embed(self, texts: list[str]) -> np.ndarray:
    rows: list[np.ndarray] = []
    for text in texts:
        seed = hash(text) & 0xFFFFFFFF
        rng = np.random.default_rng(seed=seed)
        vec = rng.standard_normal(384).astype(np.float32)
        norm = np.linalg.norm(vec)
        if norm > 1e-9:
            vec /= norm
        rows.append(vec)
    return np.stack(rows, axis=0)
```

`FakeEmbedder.embed()` already handles variable batch sizes — it will work without modification.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Per-item `embed_one` loop | Batch `embed(list)` every 32 | Phase 8 | ~32x reduction in ONNX call overhead |
| Sequential `for f in files` | `asyncio.gather` + semaphore | Phase 8 | Wall time scales with I/O concurrency, not file count |

**Deprecated/outdated:**
- Nothing in the stdlib or project deps is deprecated for this work. Python 3.12 added `itertools.batched()` but the project targets 3.11 — use slice-based chunking.

## Open Questions

1. **Optimal semaphore bound for `IMPORT_CONCURRENCY`**
   - What we know: Default thread pool size in `asyncio.to_thread` is `min(32, os.cpu_count() + 4)`; 8 concurrent tasks is well within that. SQLite WAL handles concurrent readers but serializes writers.
   - What's unclear: Whether the `busy_timeout=5000` is sufficient under peak concurrent write load, or whether a threading lock on `db.commit()` is needed inside `import_chat_file`.
   - Recommendation: Start with `IMPORT_CONCURRENCY = 8`, run tests with 20+ file directories, watch for `database is locked` errors. Lower to 4 if they appear.

2. **Whether to move `EMBED_BATCH_SIZE` to `config.py`**
   - What we know: `tools.py` is the only current caller of `remind_me_reindex`. Config is the established place for tunable constants.
   - What's unclear: Whether future callers (e.g., a background reindex task) would need the constant.
   - Recommendation: Define `EMBED_BATCH_SIZE = 32` as a module-level constant in `tools.py` for now. Move to `config.py` if a second caller appears.

3. **Test for PERF-02 "faster than sequential" is environment-dependent**
   - What we know: The success criterion says "completing faster than sequential processing on directories with 10+ files." Timing-based tests are flaky in CI (slow runners, cache effects).
   - What's unclear: Whether the test should assert wall-clock time or just structural correctness (gather called, semaphore present).
   - Recommendation: Test structural behavior (results are correct for 10+ files, function is `async`, semaphore is used) rather than wall-clock timing. Wall-clock comparison tests are fragile in CI.

## Sources

### Primary (HIGH confidence)

- Direct codebase inspection (`remind_me_mcp/embeddings.py`, `tools.py`, `importer.py`, `db.py`, `tests/conftest.py`) — all patterns verified from source
- Python 3.11 stdlib (`asyncio.Semaphore`, `asyncio.gather`, `asyncio.to_thread`) — standard APIs, no version uncertainty
- `pyproject.toml` — confirms `requires-python = ">=3.11"`, numpy declared dep, asyncio_mode=auto

### Secondary (MEDIUM confidence)

- ONNX Runtime CPUExecutionProvider threading behavior — internal threading model is not in project's codebase; claim "multiple simultaneous ONNX calls contend on CPU" is based on standard ONNX Runtime behavior as of training. Confidence: MEDIUM — if performance testing shows otherwise, enabling multiple concurrent ONNX calls could be revisited.

### Tertiary (LOW confidence)

- None — all claims either verified from codebase or stdlib docs.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — stdlib only, no new deps
- Architecture: HIGH — patterns derived directly from existing codebase code
- Pitfalls: HIGH for SQLite threading (verified from db.py config); MEDIUM for optimal semaphore bound (requires empirical tuning)

**Research date:** 2026-02-24
**Valid until:** 2026-04-24 (stable stdlib APIs; codebase changes may require revisit)
