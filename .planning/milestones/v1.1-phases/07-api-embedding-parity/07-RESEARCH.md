# Phase 7: API Embedding Parity - Research

**Researched:** 2026-02-24
**Domain:** REST API embedding integration — bridging Starlette async route handlers with the synchronous ONNX embedding engine
**Confidence:** HIGH

---

## Summary

Phase 7 is a focused, surgical change: two async route handlers in `api.py` (`api_add` and `api_update`) currently insert/update memories in SQLite without generating semantic embeddings. The MCP tool equivalents (`memory_add` and `memory_update` in `tools.py`) already call `await asyncio.to_thread(_embed_and_store, db, mem_id, content)` after every write. The fix is to replicate exactly that call pattern in the two REST handlers.

The codebase already has every piece needed: `_embed_and_store` in `db.py`, `asyncio.to_thread` pattern from `tools.py`, a `FakeEmbedder` / `mock_embedder` fixture in conftest, and — critically — `sqlite_vec` is available in the project's virtual environment, which allows embedding parity tests to run end-to-end without the real ONNX model. The only missing piece is a new `db_conn_with_vec` fixture that loads the sqlite-vec extension into the in-memory test DB so `memories_vec` exists during tests.

The implementation risk is low. Both handlers are already `async def`, so `await asyncio.to_thread(...)` slots in naturally. The embedding call must happen **after** `db.commit()` so `_embed_and_store` can look up the rowid. The update handler must gate the embed call on `"content" in body` (same logic as `memory_update` in tools.py) so tag-only updates do not re-embed unnecessarily.

**Primary recommendation:** Add `await asyncio.to_thread(_embed_and_store, db, mem_id, content)` to `api_add` and `api_update` in `api.py`, mirroring the exact pattern already used in `tools.py`. Write integration tests using a new `db_conn_with_vec` fixture that loads sqlite-vec into the in-memory test database.

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| EMBD-01 | REST API `POST /api/memories` generates semantic embeddings on create (matching MCP tool behavior) | `api_add` is `async def` — `await asyncio.to_thread(_embed_and_store, ...)` can be added after `db.commit()`. `_embed_and_store` already handles missing embedder gracefully (returns `False`). |
| EMBD-02 | REST API `PUT /api/memories/{id}` regenerates semantic embeddings on content update (matching MCP tool behavior) | `api_update` is `async def` — same pattern. Gate on `"content" in body` (not None check; the body dict already filters None fields before this point in `sets` construction, but content may still be present). |
</phase_requirements>

---

## Standard Stack

### Core

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `asyncio.to_thread` | stdlib (Python 3.9+) | Offload blocking `_embed_and_store` to thread pool | Already the pattern in `tools.py`; avoids blocking the Starlette event loop |
| `remind_me_mcp.db._embed_and_store` | project | Generate + upsert float32 embedding into `memories_vec` | The canonical embed-and-store function; handles embedder unavailability gracefully |
| `remind_me_mcp.db._get_embedder` | project | Returns `_Embedder` singleton or `None` | Already imported indirectly via `_embed_and_store` |
| `sqlite_vec` | `>=0.1.0` (in `semantic` extras) | Provides `memories_vec` virtual table | Already in `pyproject.toml [semantic]`; available in project venv |

### Supporting (test only)

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `FakeEmbedder` (conftest) | project | Deterministic fake embedder for tests | All embedding parity tests — avoids loading real ONNX model |
| `sqlite_vec` (in tests) | same | Allows `memories_vec` to exist in `:memory:` test DB | New `db_conn_with_vec` fixture needs this |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `asyncio.to_thread` | Fire-and-forget background task | Background tasks don't guarantee embedding within the same request cycle — violates EMBD-01's "within the same request/response cycle" requirement |
| `asyncio.to_thread` | Direct synchronous call in async handler | Would block the event loop during ONNX inference — not acceptable in async Starlette route |

**Installation:** No new dependencies. `sqlite_vec` is already in `[semantic]` extras. No `pip install` needed.

---

## Architecture Patterns

### Recommended Project Structure

No structural changes needed. All edits are in:
```
remind_me_mcp/
└── api.py        # api_add and api_update get one line each
tests/
├── conftest.py   # new db_conn_with_vec fixture
└── test_api.py   # new embedding parity test cases
```

### Pattern 1: Embedding After Commit (from tools.py)

**What:** After `db.commit()`, call `_embed_and_store` in a thread so the event loop is not blocked.
**When to use:** Any async handler that writes a new memory or updates content.

**Current MCP tool pattern (tools.py lines 107-117):**
```python
# memory_add in tools.py — the reference implementation
db.execute("""INSERT INTO memories ...""", (...))
db.commit()
await asyncio.to_thread(_embed_and_store, db, mem_id, params.content)
```

**Current MCP update pattern (tools.py lines 356-360):**
```python
# memory_update in tools.py — gate on content change
db.execute(f"UPDATE memories SET ...", bindings)
db.commit()
# Re-embed if content changed
if params.content is not None:
    await asyncio.to_thread(_embed_and_store, db, params.memory_id, params.content)
```

**Target api_add pattern (api.py ~line 265):**
```python
# After: db.commit()
# Add:
await asyncio.to_thread(_embed_and_store, db, mem_id, content)
# Then: existing row = db.execute(...).fetchone()
```

**Target api_update pattern (api.py ~line 301):**
```python
# After: db.commit()
# Add (gate on content being updated):
if "content" in body and body["content"] is not None:
    await asyncio.to_thread(_embed_and_store, db, memory_id, body["content"])
# Then: existing updated = db.execute(...).fetchone()
```

### Pattern 2: db_conn_with_vec Test Fixture

**What:** An extended version of `db_conn` that loads the sqlite-vec extension so `memories_vec` is available during tests.
**When to use:** Any test that verifies an embedding row was created in `memories_vec`.

```python
@pytest.fixture()
def db_conn_with_vec(monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    """Like db_conn but with sqlite-vec loaded so memories_vec virtual table exists."""
    import sqlite_vec

    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    _ensure_schema(db)

    import remind_me_mcp.api as _api_mod
    import remind_me_mcp.db as _db_mod
    import remind_me_mcp.importer as _importer_mod
    import remind_me_mcp.tools as _tools_mod

    monkeypatch.setattr(_db_mod, "_get_db", lambda: db)
    monkeypatch.setattr(_api_mod, "_get_db", lambda: db)
    monkeypatch.setattr(_tools_mod, "_get_db", lambda: db)
    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db)

    yield db
    db.close()
```

This was verified working in the project environment (sqlite-vec loads cleanly in `:memory:` and creates `memories_vec` with `_ensure_schema`).

### Pattern 3: Starlette TestClient with Embedding Parity Test

**What:** Tests that use `db_conn_with_vec` + `mock_embedder` to assert a `memories_vec` row exists after POST/PUT.
**When to use:** EMBD-01 and EMBD-02 verification.

```python
def test_api_add_creates_embedding(db_conn_with_vec, mock_embedder, monkeypatch):
    """EMBD-01: POST /api/memories creates a row in memories_vec."""
    import remind_me_mcp.config as _cfg
    from pathlib import Path
    monkeypatch.setattr(_cfg, "IMPORT_ROOTS", [Path.home(), Path("/tmp").resolve()])

    app = _build_api_app()
    client = TestClient(app)

    response = client.post("/api/memories", json={"content": "Embedding parity test"})
    assert response.status_code == 201
    mem_id = response.json()["id"]

    # Verify embedding row exists
    rowid = db_conn_with_vec.execute(
        "SELECT rowid FROM memories WHERE id = ?", (mem_id,)
    ).fetchone()[0]
    vec_row = db_conn_with_vec.execute(
        "SELECT rowid FROM memories_vec WHERE rowid = ?", (rowid,)
    ).fetchone()
    assert vec_row is not None, "POST /api/memories must create a memories_vec row (EMBD-01)"
```

### Anti-Patterns to Avoid

- **Embedding before commit:** `_embed_and_store` does `SELECT rowid FROM memories WHERE id = ?` — if called before commit, the row doesn't exist yet and `_embed_and_store` returns `False` silently.
- **Importing `_embed_and_store` at module level in api.py:** All heavy imports in `api.py` are kept inside `_build_api_app()` to preserve MCP stdio mode compatibility. `_embed_and_store` is already imported at the module level from `remind_me_mcp.db` — this is fine because `db.py` is a lightweight import (no ML deps). Verify the existing import `from remind_me_mcp.db import _get_db, _make_id, _now_iso, _row_to_dict` already covers what's needed; add `_embed_and_store` to this import.
- **Skipping the embed call when embedder is None:** `_embed_and_store` already handles `_get_embedder() is None` — it returns `False` without raising. No guard needed in the caller. Call it unconditionally.
- **Re-embedding for non-content updates:** The update handler should only call `_embed_and_store` when content is in the request body — same gate as `memory_update` in tools.py.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Embedding generation | Custom ONNX inference | `_embed_and_store(db, mem_id, content)` | Already handles rowid lookup, vec bytes packing, upsert, error handling |
| Thread offloading | `threading.Thread`, `concurrent.futures` | `asyncio.to_thread(...)` | Project-standard pattern; integrates with Starlette's async event loop |
| Embedder availability guard | Custom try/except around embedder | Call `_embed_and_store` directly | It already returns `False` if embedder is None or any error occurs |
| Test DB with vec table | Custom sqlite setup per test | New `db_conn_with_vec` fixture in conftest | Reusable fixture; consistent with existing `db_conn` pattern |

**Key insight:** Every infrastructure piece already exists. This phase is two `await asyncio.to_thread(...)` calls in `api.py` plus new tests. No new modules, no new dependencies, no schema changes.

---

## Common Pitfalls

### Pitfall 1: Calling `_embed_and_store` Before `db.commit()`

**What goes wrong:** `_embed_and_store` does `db.execute("SELECT rowid FROM memories WHERE id = ?", (memory_id,))`. If called before the INSERT is committed, the row does not exist, the rowid lookup returns `None`, and the function returns `False` silently — no embedding is stored.
**Why it happens:** Unclear ordering of operations when adding the embed call.
**How to avoid:** Always place the `await asyncio.to_thread(_embed_and_store, ...)` call **after** `db.commit()`, exactly as tools.py does.
**Warning signs:** Tests show `memories_vec` row count = 0 after POST.

### Pitfall 2: `_embed_and_store` Not in `api.py` Module-Level Import

**What goes wrong:** `api.py` imports from `remind_me_mcp.db` at module level (line 21). If `_embed_and_store` is not added to that import, a `NameError` occurs at runtime.
**Why it happens:** Easy to forget to update the import statement.
**How to avoid:** Add `_embed_and_store` to the existing `from remind_me_mcp.db import ...` line at the top of `api.py`.
**Warning signs:** `NameError: name '_embed_and_store' is not defined` in API logs.

### Pitfall 3: Test DB Missing `memories_vec` Table

**What goes wrong:** `db_conn` in conftest opens `:memory:` without loading sqlite-vec, so `memories_vec` does not exist. `_embed_and_store` catches the `sqlite3.OperationalError` and returns `False` — no embedding stored, but no test failure either. Tests that check `memories_vec` row count get a `no such table: memories_vec` error.
**Why it happens:** The existing `db_conn` fixture was designed before embedding parity was needed.
**How to avoid:** Use a new `db_conn_with_vec` fixture that loads sqlite-vec before `_ensure_schema`. Verified working: `sqlite_vec` is installed in the project venv and loads cleanly into `:memory:`.
**Warning signs:** `memories_vec` row count = 0 in tests, or `sqlite3.OperationalError: no such table: memories_vec`.

### Pitfall 4: Test Client Fixture Uses `db_conn`, Not `db_conn_with_vec`

**What goes wrong:** The `client` fixture in test_api.py depends on `db_conn`. New embedding parity tests need `db_conn_with_vec` + `mock_embedder`. Using the wrong fixture means `memories_vec` doesn't exist and the embed call silently fails.
**Why it happens:** Fixture dependency mismatch.
**How to avoid:** New tests should build their own `TestClient` from `_build_api_app()` using `db_conn_with_vec` as the database fixture, or create a `client_with_vec` fixture that mirrors the `client` fixture but uses `db_conn_with_vec`.
**Warning signs:** Embedding tests pass even when embed call is not added (false green).

### Pitfall 5: `asyncio.to_thread` Unavailability in Starlette TestClient

**What goes wrong:** Starlette's `TestClient` runs async handlers in a synchronous test thread using `anyio`. `asyncio.to_thread()` inside route handlers must be awaited and works fine — but if not awaited (e.g., accidentally made a fire-and-forget task), the embedding may not complete before the response is returned.
**Why it happens:** Confusion between `asyncio.create_task` (fire-and-forget) vs `await asyncio.to_thread` (waits for completion).
**How to avoid:** Always `await asyncio.to_thread(...)` — do not wrap in `asyncio.create_task`. This matches tools.py and guarantees the embedding is stored before the handler returns.
**Warning signs:** Intermittent test failures where `memories_vec` row exists sometimes but not others.

---

## Code Examples

Verified patterns from project source:

### MCP Tool Embed-on-Create (tools.py lines 107-118)
```python
# Source: remind_me_mcp/tools.py::memory_add
db.execute(
    """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
    (mem_id, params.content, params.category, json.dumps(params.tags),
     params.source, json.dumps(params.metadata), now, now),
)
db.commit()
await asyncio.to_thread(_embed_and_store, db, mem_id, params.content)
return _maybe_update_notice(f"✓ Memory stored with id `{mem_id}` ...")
```

### MCP Tool Embed-on-Update (tools.py lines 356-361)
```python
# Source: remind_me_mcp/tools.py::memory_update
db.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id = ?", bindings)
db.commit()
# Re-embed if content changed
if params.content is not None:
    await asyncio.to_thread(_embed_and_store, db, params.memory_id, params.content)
return f"✓ Memory `{params.memory_id}` updated."
```

### `_embed_and_store` Signature (db.py lines 305-343)
```python
# Source: remind_me_mcp/db.py::_embed_and_store
def _embed_and_store(db: sqlite3.Connection, memory_id: str, content: str) -> bool:
    """Generate embedding for content and store in the vector table.
    Returns True if stored successfully, False otherwise (embedder None, table missing, etc.)
    """
    embedder = _get_embedder()
    if embedder is None:
        return False
    # ... rowid lookup, embed_one, DELETE + INSERT INTO memories_vec, commit
    return True
```

### sqlite-vec Fixture Loading (verified working 2026-02-24)
```python
# Verified: sqlite_vec.load() works in :memory: connections
import sqlite3
import sqlite_vec

db = sqlite3.connect(":memory:", check_same_thread=False)
db.row_factory = sqlite3.Row
db.enable_load_extension(True)
sqlite_vec.load(db)
db.enable_load_extension(False)
# After _ensure_schema(db): memories_vec virtual table exists
# _embed_and_store returns True with a FakeEmbedder
```

### Verifying memories_vec Row After API Call
```python
# Pattern for EMBD-01 / EMBD-02 test assertions
rowid = db.execute("SELECT rowid FROM memories WHERE id = ?", (mem_id,)).fetchone()[0]
vec_row = db.execute("SELECT rowid FROM memories_vec WHERE rowid = ?", (rowid,)).fetchone()
assert vec_row is not None
```

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| FTS5-only API search (QUAL-05, deferred) | FTS5 API search + semantic search in MCP only | Phase 7 | API add/update will now feed the semantic index; search parity (PERF-03) is future scope |
| api_add: INSERT only | api_add: INSERT + embed | Phase 7 | Memories created via REST API become semantically searchable |
| api_update: UPDATE only | api_update: UPDATE + re-embed on content change | Phase 7 | Updated REST memories have current semantic representation |

**Deprecated/outdated:**
- Nothing deprecated — this is a pure addition.

---

## Open Questions

1. **Should api_update check `"content" in body` or `content := body.get("content")` for the re-embed gate?**
   - What we know: The current `api_update` builds `sets` by iterating `("content", "category", "source")` and checking `if field in body and body[field] is not None`. So by the time we reach the embed gate, we know content was valid if it was in `sets`.
   - What's unclear: Whether to re-examine `body` for the gate or track whether content was in `sets`.
   - Recommendation: Check `if "content" in body and body["content"] is not None` directly — mirrors the clarity of tools.py's `if params.content is not None`. Slightly redundant with `sets` logic but explicit.

2. **What content string to pass to `_embed_and_store` in `api_update`?**
   - What we know: The `api_update` body may contain a new `content` value; after commit, the DB has the updated content.
   - What's unclear: Should we read updated content from `body["content"]` (already validated) or re-fetch from DB?
   - Recommendation: Use `body["content"]` directly — it's already validated non-empty by the `sets` loop. Tools.py uses `params.content` (same approach). Re-fetching would be an unnecessary extra query.

3. **Should the `_embed_and_store` import be at module level or inside `_build_api_app()`?**
   - What we know: All Starlette imports are inside `_build_api_app()` to preserve MCP stdio mode compatibility. But `_embed_and_store` is from `db.py`, which is a lightweight import already imported at module level (`from remind_me_mcp.db import _get_db, _make_id, _now_iso, _row_to_dict`).
   - What's unclear: Whether `db.py`'s `from remind_me_mcp.embeddings import _get_embedder` (line 28) causes any issue in stdio mode.
   - Recommendation: Add `_embed_and_store` to the existing module-level import in `api.py`. The `embeddings.py` module is already imported lazily in the embedder (heavy deps deferred inside `_ensure_loaded()`). This is safe. This is the same approach as `tools.py` which imports `_embed_and_store` at module level.

---

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest (configured in `pyproject.toml [tool.pytest.ini_options]`) |
| Config file | `pyproject.toml` — `testpaths = ["tests"]`, `asyncio_mode = "auto"` |
| Quick run command | `uv run pytest tests/test_api.py -x -q` |
| Full suite command | `uv run pytest --cov=remind_me_mcp --cov-report=term-missing --cov-fail-under=74` |
| Estimated runtime | ~1 second (208 existing tests run in 1.00s) |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| EMBD-01 | POST /api/memories creates a `memories_vec` row within the same request | integration | `uv run pytest tests/test_api.py -k "embedding" -x` | ❌ Wave 0 gap |
| EMBD-01 | POST /api/memories embedding row has correct rowid linking to the memory | integration | `uv run pytest tests/test_api.py -k "embedding" -x` | ❌ Wave 0 gap |
| EMBD-02 | PUT /api/memories/{id} with content update replaces the `memories_vec` row | integration | `uv run pytest tests/test_api.py -k "embedding" -x` | ❌ Wave 0 gap |
| EMBD-02 | PUT /api/memories/{id} without content change does NOT alter `memories_vec` | integration | `uv run pytest tests/test_api.py -k "embedding" -x` | ❌ Wave 0 gap |
| EMBD-01+02 | REST and MCP memories are equally retrievable via `_semantic_search` | integration | `uv run pytest tests/test_api.py -k "embedding" -x` | ❌ Wave 0 gap |

### Nyquist Sampling Rate

- **Minimum sample interval:** After every committed task → run: `uv run pytest tests/test_api.py -x -q`
- **Full suite trigger:** Before merging final task of any plan wave
- **Phase-complete gate:** Full suite green before `/gsd:verify-work` runs
- **Estimated feedback latency per task:** ~1 second

### Wave 0 Gaps (must be created before implementation)

- [ ] `tests/conftest.py` — add `db_conn_with_vec` fixture (loads sqlite-vec into `:memory:` DB; uses existing `db_conn` as reference)
- [ ] `tests/test_api.py` — add EMBD-01 tests: `test_api_add_creates_embedding`, `test_api_add_embedding_rowid_matches_memory`
- [ ] `tests/test_api.py` — add EMBD-02 tests: `test_api_update_content_regenerates_embedding`, `test_api_update_no_content_preserves_embedding`
- [ ] `tests/test_api.py` — add EMBD-01+02 parity test: `test_rest_and_mcp_memories_equally_findable_by_semantic_search`

*(Note: No new test files needed — all new tests go into existing `tests/conftest.py` and `tests/test_api.py`)*

---

## Sources

### Primary (HIGH confidence)

- `remind_me_mcp/api.py` — Verified `api_add` (lines 240-266) and `api_update` (lines 268-304) do not call `_embed_and_store`
- `remind_me_mcp/tools.py` — Verified `memory_add` (line 117) and `memory_update` (lines 359-360) call `await asyncio.to_thread(_embed_and_store, ...)`
- `remind_me_mcp/db.py` — Verified `_embed_and_store` signature, behavior (returns False if embedder None), and that it must be called after commit
- `tests/conftest.py` — Verified `FakeEmbedder`, `mock_embedder`, and `db_conn` fixtures available; sqlite-vec not loaded in `db_conn`
- Live verification (2026-02-24) — `sqlite_vec.load()` works in `:memory:` connection; `_ensure_schema` creates `memories_vec`; `_embed_and_store` returns `True` with FakeEmbedder and vec table present

### Secondary (MEDIUM confidence)

- Python `asyncio.to_thread` stdlib docs — pattern for offloading blocking calls in async context; well-established project pattern

### Tertiary (LOW confidence)

- None

---

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — all code is in the project; no external library research needed
- Architecture: HIGH — reference implementation exists in tools.py; pattern is exact and verified
- Pitfalls: HIGH — all discovered by direct code inspection and live verification
- Test patterns: HIGH — sqlite-vec loading verified working; FakeEmbedder verified; fixture pattern verified

**Research date:** 2026-02-24
**Valid until:** Stable indefinitely — all findings are based on project source code, not external libraries
