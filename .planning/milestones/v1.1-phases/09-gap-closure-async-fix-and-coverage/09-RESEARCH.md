# Phase 9: Gap Closure — Async Bug Fix and Coverage Gate — Research

**Researched:** 2026-02-24
**Domain:** Python async/await correctness, pytest-cov coverage enforcement
**Confidence:** HIGH

---

## Summary

Phase 9 closes two precisely-identified gaps from the v1.1 milestone audit. Both gaps have exact
locations in the codebase, require minimal surgical changes, and have no architectural ambiguity.

**Gap 1 (CRITICAL):** `api.py` line 348 calls `import_directory(...)` without `await`.
`import_directory` was converted to `async def` in Phase 8-02 (commit `cd218aa`). The MCP tool
path in `tools.py:455` was correctly updated with `await`. The REST API path in `api_import` was
not. At runtime, `POST /api/import` with a directory path returns a coroutine object instead of
executing the import. Python emits `RuntimeWarning: coroutine 'import_directory' was never
awaited`. The fix is one word: add `await` before `import_directory(...)`.

**Gap 2 (PARTIAL):** `.github/workflows/ci.yml` has `--cov-fail-under=74` but CICD-02 requires
80%. Current measured coverage is 77% (1368 statements, 313 missing = 77.0%). The gate must be
raised to 80, and the coverage must reach 80% before the gate is raised. Adding a test for the
`p.is_dir()` branch in `api_import` (the companion test to the bug fix) will cover api.py lines
348-356 (9 lines). That alone brings coverage to approximately 77.8%. Reaching 80% requires
covering approximately 40 additional lines beyond the directory-import test.

**Primary recommendation:** Fix the `await` in one edit. Add the REST API directory import test as
the companion. Then identify the highest-yield uncovered lines (targeting ~40 more lines) from the
existing modules, add focused tests, then raise the gate to 80.

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| PERF-02 | Directory import processes files concurrently with semaphore-bounded parallelism | The `import_directory` async function exists and works correctly — only the REST API `await` keyword is missing. One-line fix + one companion test fully satisfies the REST API integration leg of PERF-02. |
| CICD-02 | Coverage enforcement gate at 80% minimum via pytest-cov | Gate mechanism is live at 74% in `ci.yml`. Current coverage is 77%. Need to cover ~40 more lines then raise `--cov-fail-under` to 80. |

</phase_requirements>

---

## Standard Stack

### Core

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| pytest | installed (uv) | Test runner | Already in use across 215 tests |
| pytest-cov | installed (uv) | Coverage measurement and gate | Already configured in ci.yml |
| pytest-asyncio | installed (uv, CI) | Async test support | Required for asyncio_mode=auto; already in pyproject.toml |
| starlette TestClient | via starlette dep | Sync wrapper for async route handlers | Used in all existing api tests; handles async routes synchronously |

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `tmp_path` fixture | pytest built-in | Create temporary directories for directory-import tests | The new directory import test needs a real directory with files |
| `monkeypatch` fixture | pytest built-in | Patch `IMPORT_ROOTS` for SEC-02 guard and `_get_db` for importer | Required by the existing `client` fixture (already handled) |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Adding targeted tests for coverage | Excluding modules from coverage (`--omit`) | Exclusion hides real gaps; targeted tests add real quality. Do not omit. |
| Adding tests for easy uncovered lines | Raising gate to current 77% (not 80%) | Requires only changing the YAML number; leaves a permanent gap. Do not do this. |

**Installation:** No new packages needed — all required libraries are already installed.

---

## Architecture Patterns

### Pattern 1: Async route handlers and unawaited coroutines

**What:** `api_import` is an `async def` route handler. When it calls `import_directory(...)` without
`await`, Python creates a coroutine object but does not execute it. The route returns immediately with
the coroutine as the value of `summary`, which then gets JSON-serialised as something like
`<coroutine object import_directory at 0x...>`. The `return _json_ok(summary)` call succeeds (no
exception), which is why tests using file paths never caught this — the `else` branch runs fine.

**Fix:**
```python
# Before (api.py line 348 — BROKEN):
summary = import_directory(
    directory=str(p),
    ...
)

# After (correct):
summary = await import_directory(
    directory=str(p),
    ...
)
```

**Source:** Direct code inspection of `remind_me_mcp/api.py` lines 346-356 and
`remind_me_mcp/importer.py` lines 352-418 (confirmed `async def import_directory`).

### Pattern 2: REST API directory import test structure

**What:** The existing `client` fixture in `tests/test_api.py` already handles all the prerequisites
for a directory import test:
- `monkeypatch` sets `IMPORT_ROOTS` to `[Path.home(), Path("/tmp").resolve()]` so `tmp_path`
  directories pass the SEC-02 guard
- `_get_db` is patched in all modules including `remind_me_mcp.importer` so writes go to the
  isolated test DB
- `TestClient` wraps async route handlers synchronously — no `async def test_...` needed

**Test shape:**
```python
def test_api_import_directory(client: TestClient, db_conn, tmp_path: Path) -> None:
    """POST /api/import with a directory path should execute the import and return a summary."""
    # Create a chat file inside tmp_path (which is under /tmp, inside IMPORT_ROOTS)
    chat_file = tmp_path / "chat.json"
    chat_file.write_text(json.dumps({
        "chat_messages": [
            {"sender": "user", "content": [{"type": "text", "text": "Hello"}]},
            {"sender": "assistant", "content": [{"type": "text", "text": "Hi there."}]},
        ]
    }))

    response = client.post("/api/import", json={"file_path": str(tmp_path)})
    assert response.status_code == 200
    data = response.json()
    assert data["files_processed"] == 1
    assert data["imported"] == 1
    assert data["total_memories_created"] >= 1
```

**Key insight:** `asyncio_mode = "auto"` in `pyproject.toml` means `async def test_...` functions
run automatically. However, since `TestClient` handles async route handlers synchronously, this test
does not need to be `async def` — plain `def` works the same way as the existing import tests.

**Source:** Confirmed in `tests/conftest.py` (client fixture), `tests/test_api.py` (existing file
import tests), `pyproject.toml` (`asyncio_mode = "auto"`).

### Pattern 3: Coverage gap analysis and targeted test additions

**What:** Current coverage is 77.0% (1368 statements, 313 missing). Target is 80% (at most 273
missing — need to cover 40 more lines).

**Measured uncovered lines by module (from `pytest --cov=remind_me_mcp --cov-report=term-missing`):**

| Module | Missing Lines | Count | Testability |
|--------|--------------|-------|-------------|
| `__main__.py` | 17-189 | 94 | LOW — CLI entry point, needs subprocess |
| `pid.py` | 37-54, 67, 82, 97-103, 117-127 | 29 | MEDIUM — PID file management |
| `db.py` | 47-70, 81-83, 287-294, 322, 328, 343-345, 369, 390-392 | 40 | HIGH — DB helpers, some branches |
| `api.py` | 144-146, 168-169, 219-220, 225, 227-228, 275-276, 293-294, 324-325, 348-356, 361-363 | 21 | HIGH — branch coverage |
| `importer.py` | 121-122, 127, 143, 155, 180, 262, 272-274, 279-289, 299, 381, 386, 401-403 | 27 | HIGH — format/mode branches |
| `tools.py` | many | 58 | MEDIUM — MCP tool edge cases |
| `embeddings.py` | 74-86, 142-146, 164-165 | 13 | LOW — ONNX error paths |
| `updater.py` | many | 23 | MEDIUM — HTTP/version paths |
| `server.py` | 44-52 | 6 | LOW — MCP stdio startup |

**The directory import test alone covers api.py lines 348-356 = 9 lines.** That leaves the gap at
304 missing (77.8%). An additional ~31 lines need coverage to reach 80%.

**High-yield targets (testable without subprocess or network):**

1. **api.py lines 361-363** (3 lines): The `except (FileNotFoundError, OSError, ...)` clause in
   `api_import`. Trigger by passing a directory path that exists at check time but is removed before
   `import_directory` runs — or mock `import_directory` to raise `OSError`. However, this is a race
   condition path; a simpler approach is to supply a directory that fails during import.

2. **api.py lines 144-146** (3 lines): The `except (json.JSONDecodeError, TypeError)` clause in
   `api_stats` during tag aggregation. Trigger by inserting a memory row with malformed JSON in the
   `tags` column directly via `db_conn`.

3. **api.py lines 168-169** (2 lines): The `if src := params.get("source")` branch in `api_list`.
   Test with `?source=chat_import` query param.

4. **api.py lines 219-220, 225, 227-228** (5 lines): `api_search` FTS OperationalError path and
   category/tag filter branches. These need FTS-matching memories plus category/tag query params.

5. **api.py lines 275-276, 293-294** (4 lines): `api_update` with `tags` and `metadata` fields.
   Test updating with only `tags` or only `metadata` in the body.

6. **api.py lines 324-325** (2 lines): `api_update` "No fields to update" error path. Send a PUT
   with an empty body `{}`.

7. **importer.py lines 262** (1 line): Unsupported file format path. Pass a `.xyz` file.

8. **importer.py lines 272-274** (3 lines): The multi-conversation JSON path (list where each item
   has `chat_messages`). Provide a JSON array of conversation objects.

9. **importer.py lines 279-289** (11 lines): The JSONL path. Provide a `.jsonl` file with lines.

10. **importer.py lines 180** (1 line): `_filter_messages` with `mode="summaries"`. Call the
    function directly with a message that has "summary" in the role.

11. **importer.py line 381** (1 line): `tags=None` default in `import_directory`. Call with no
    `tags` argument.

12. **db.py lines 287-294** (8 lines): `_row_to_dict` tag/metadata parsing edge cases.

**Practical plan:** The directory import test + approximately 6-8 focused tests covering the above
branches should comfortably reach 80%. The exact number depends on which branches are hit; the
planner should verify coverage after each test file addition.

### Pattern 4: Raising the coverage gate

**What:** After coverage is confirmed at >= 80%, update the single YAML line in ci.yml.

```yaml
# Before:
run: pytest --cov=remind_me_mcp --cov-report=term-missing --cov-fail-under=74

# After:
run: pytest --cov=remind_me_mcp --cov-report=term-missing --cov-fail-under=80
```

Also update the comment at the top of ci.yml from the old coverage gate note.

**Source:** Direct inspection of `.github/workflows/ci.yml` line 32.

### Anti-Patterns to Avoid

- **Raising the gate before coverage reaches 80%:** This causes CI to permanently fail. Always
  verify local coverage first.
- **Using `--omit` to exclude modules:** Hides real gaps. The audit's 77% figure already reflects
  the full module list.
- **Making the directory test `async def`:** The `client` fixture and `TestClient` are synchronous.
  No `async def` needed (and it works either way with `asyncio_mode=auto`, but plain `def` is
  consistent with existing test style).
- **Forgetting the `await` in api.py is inside a try/except:** The except clause
  (`FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError`) correctly wraps the
  awaited call — the fix is only adding `await`, not restructuring the try/except.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Coverage measurement | Custom line counter | pytest-cov (already installed) | Already configured, integrated with pytest |
| Async test support | Manual event loop management | asyncio_mode=auto (already configured) | pyproject.toml already has `asyncio_mode = "auto"` |
| HTTP test client for async handlers | aiohttp test client | starlette TestClient (already used) | TestClient handles async routes synchronously, no new deps |

**Key insight:** Both gaps require only editing existing files. No new dependencies, no new
test infrastructure, no new fixtures beyond what already exists.

---

## Common Pitfalls

### Pitfall 1: Forgetting that `api_import` is inside `_build_api_app()`

**What goes wrong:** New developers search for `def api_import` and miss that it is a nested
function inside `_build_api_app()`. The `await` fix must be applied to the nested function at
line 348, not to some top-level import.

**Why it happens:** `api.py` uses a factory function pattern to keep Starlette imports lazy (MCP
stdio mode compatibility). All route handlers are closures.

**How to avoid:** Read `api.py` from the top — `_build_api_app()` starts around line 60 and all
route handlers are defined inside it.

### Pitfall 2: Coverage gate raised before coverage verified

**What goes wrong:** If `--cov-fail-under=80` is set before tests actually reach 80%, CI will fail
on every commit until fixed. This blocks the team.

**Why it happens:** Optimism about how many lines new tests will cover.

**How to avoid:** Always run `uv run pytest --cov=remind_me_mcp --cov-report=term-missing` locally
and confirm TOTAL coverage >= 80 before editing ci.yml.

**Warning signs:** TOTAL line in coverage output below 80.0%.

### Pitfall 3: The `client` fixture requires both `db_conn` and `monkeypatch` parameters

**What goes wrong:** Writing a test `def test_...(client: TestClient, tmp_path: Path)` works for
most cases. But if the test also directly accesses the database (to verify memories were created),
it needs `db_conn` explicitly in the signature. The `client` fixture already depends on `db_conn`,
so pytest provides the same connection object when it's listed in the test signature.

**Why it happens:** Pytest fixture sharing — the `client` and `db_conn` in the test function resolve
to the same fixture instance.

**How to avoid:** Match the pattern of `test_api_import_file` which includes both `client` and
`db_conn` in the signature.

### Pitfall 4: SEC-02 guard rejects `tmp_path` directories outside `/tmp`

**What goes wrong:** On some systems, `tmp_path` may resolve to a path outside `/tmp` (e.g.,
`/var/folders/...` on macOS). The `client` fixture patches `IMPORT_ROOTS` to include
`[Path.home(), Path("/tmp").resolve()]`. If `tmp_path` resolves outside those roots, the import
returns a 400 error from SEC-02, not a coverage miss.

**Why it happens:** `tmp_path` is OS-dependent.

**How to avoid:** The existing tests work because the system is Linux (WSL2, confirmed in env).
`tmp_path` lives under `/tmp`. No action needed. If portability becomes a concern, patch
`IMPORT_ROOTS` to include `tmp_path.parent` directly in the test.

---

## Code Examples

Verified patterns from direct codebase inspection:

### Bug Fix — Add `await` to `api_import`

```python
# remind_me_mcp/api.py — inside _build_api_app() — api_import function
# Lines 345-356: BEFORE (broken)
try:
    if p.is_dir():
        # Directory import — delegates to shared import_directory() (DRY)
        summary = import_directory(       # <-- missing await
            directory=str(p),
            category=category,
            tags=tags,
            extract_mode=extract_mode,
            max_length=max_length,
            recursive=True,
        )
        return _json_ok(summary)

# AFTER (correct)
try:
    if p.is_dir():
        # Directory import — delegates to shared import_directory() (DRY)
        summary = await import_directory(  # <-- await added
            directory=str(p),
            category=category,
            tags=tags,
            extract_mode=extract_mode,
            max_length=max_length,
            recursive=True,
        )
        return _json_ok(summary)
```

**Source:** `remind_me_mcp/api.py` lines 345-356 (inspected directly).

### Companion Test — REST API directory import

```python
# tests/test_api.py — new test to add after existing import tests
def test_api_import_directory(client: TestClient, db_conn, tmp_path: Path) -> None:
    """POST /api/import with a directory path executes import and returns a summary.

    Exercises the p.is_dir() branch in api_import (previously untested via REST API).
    Requires the await fix in api.py:348 to return a real summary rather than a coroutine.
    """
    # tmp_path is under /tmp which is in the patched IMPORT_ROOTS
    chat_file = tmp_path / "chat.json"
    chat_file.write_text(json.dumps({
        "chat_messages": [
            {"sender": "user", "content": [{"type": "text", "text": "What is Python?"}]},
            {"sender": "assistant", "content": [{"type": "text", "text": "Python is a language."}]},
        ]
    }))

    response = client.post("/api/import", json={"file_path": str(tmp_path)})
    assert response.status_code == 200
    data = response.json()
    assert "files_processed" in data
    assert data["files_processed"] == 1
    assert data["imported"] == 1
    assert data["total_memories_created"] >= 1
```

**Source:** Pattern derived from `test_api_import_file` + audit analysis of the missing branch.

### Coverage Gate Update

```yaml
# .github/workflows/ci.yml — update line 32
# Before:
run: pytest --cov=remind_me_mcp --cov-report=term-missing --cov-fail-under=74

# After:
run: pytest --cov=remind_me_mcp --cov-report=term-missing --cov-fail-under=80
```

Also update the comment at line 1:
```yaml
# Before comment:
# Coverage gate: 74% (measured 76% minus 2% headroom per STATE.md).
# Target is 80% (CICD-02) — will increase as tests are added in Phases 6-8.

# After comment:
# Coverage gate: 80% (CICD-02 requirement satisfied — Phase 9 gap closure).
```

**Source:** `.github/workflows/ci.yml` lines 1-2 and 32 (inspected directly).

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Sequential `import_chat_file` per file | `async import_directory` with `asyncio.Semaphore(8)` | Phase 8-02 | The `async` conversion is what created the missing `await` bug |
| Coverage gate at 74% | Coverage gate at 80% | Phase 9 (this phase) | Satisfies CICD-02 requirement fully |
| `import_directory` called with `await` in MCP only | `import_directory` called with `await` in both MCP and REST API | Phase 9 (this phase) | Closes the integration gap |

**Prior art:** `tools.py:455` shows the correct pattern — `summary = await import_directory(...)`.
This is the reference implementation to match.

---

## Open Questions

1. **Exactly which additional tests will push coverage from ~77.8% to >= 80%?**
   - What we know: The directory import test covers 9 lines. Need ~40 total.
   - What's unclear: Exact line counts per test until tests are written and coverage re-measured.
   - Recommendation: Write the directory import test first, measure, then identify the cheapest
     remaining lines (JSONL importer test and a few api.py branch tests are highest yield).

2. **Will the directory import test exercise importer.py lines 381 and 386?**
   - What we know: `api_import` calls `import_directory(..., recursive=True)`. Line 381 (`tags = []`)
     runs only when `tags=None` is passed. The REST API defaults `tags = body.get("tags", [])`,
     so tags is always a list, never None. Line 386 (non-recursive branch) requires `recursive=False`.
   - Recommendation: The directory test will NOT cover lines 381 or 386. A separate direct call to
     `import_directory` (or a tools test with `recursive=False`) would be needed. These are
     low-priority; the JSONL/multi-conversation format tests cover more lines.

---

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest (with pytest-asyncio, pytest-cov) |
| Config file | `pyproject.toml` — `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest tests/test_api.py -x -q` |
| Full suite command | `uv run pytest --cov=remind_me_mcp --cov-report=term-missing -q` |
| Estimated runtime | ~1-2 seconds (215 tests currently run in 1.24s) |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| PERF-02 | REST API `POST /api/import` with directory path executes import, returns summary | integration | `uv run pytest tests/test_api.py::test_api_import_directory -x` | Wave 0 gap |
| PERF-02 | `api_import` `p.is_dir()` branch is executed | integration | same test | Wave 0 gap |
| CICD-02 | `pytest --cov` total coverage >= 80% | coverage gate | `uv run pytest --cov=remind_me_mcp --cov-report=term-missing -q` | existing |
| CICD-02 | `--cov-fail-under=80` in ci.yml | static | `grep cov-fail-under .github/workflows/ci.yml` | existing (needs edit) |

### Nyquist Sampling Rate

- **Minimum sample interval:** After the `await` fix is committed → run:
  `uv run pytest tests/test_api.py -x -q`
- **After each new test added:** Run `uv run pytest --cov=remind_me_mcp -q` to check running
  coverage total
- **Full suite trigger:** Before raising the gate in ci.yml — run the full suite and confirm TOTAL
  >= 80.0%
- **Phase-complete gate:** Full suite green with `--cov-fail-under=80` before phase is verified
- **Estimated feedback latency per task:** ~1-2 seconds

### Wave 0 Gaps (must be created before implementation)

- [ ] Test `tests/test_api.py::test_api_import_directory` — covers PERF-02 REST API directory branch
- [ ] Additional branch-coverage tests (importer JSONL, api_update edge cases, etc.) —
  collectively needed to push total from ~77.8% to >= 80%

*(Existing test infrastructure is fully sufficient — no new framework, config, or fixture files
needed for Wave 0.)*

---

## Sources

### Primary (HIGH confidence)

- Direct codebase inspection — `remind_me_mcp/api.py` lines 320-363 (api_import function)
- Direct codebase inspection — `remind_me_mcp/importer.py` lines 352-418 (async import_directory)
- Direct codebase inspection — `.github/workflows/ci.yml` lines 1-33
- Live `uv run pytest --cov=remind_me_mcp --cov-report=term-missing -q` output (2026-02-24):
  215 tests, 77% coverage, 1368 statements, 313 missing
- Direct codebase inspection — `tests/test_api.py`, `tests/conftest.py` (fixture patterns)
- Direct codebase inspection — `remind_me_mcp/tools.py:455` (reference `await import_directory`)
- Direct codebase inspection — `.planning/v1.1-MILESTONE-AUDIT.md` (gap analysis)

### Secondary (MEDIUM confidence)

- `.planning/STATE.md` — Phase 8-02 decision log confirms `asyncio.Semaphore` creation inside
  async function body, `threading.Lock` for SQLite write serialization

### Tertiary (LOW confidence)

- None.

---

## Metadata

**Confidence breakdown:**
- Bug location: HIGH — line 348 of api.py confirmed by direct inspection and audit
- Fix correctness: HIGH — `await` is the only change; mirrors tools.py:455 reference pattern
- Coverage math: HIGH — measured from live pytest run (1368 stmts, 313 missing, 77.0%)
- Coverage gap targets: MEDIUM — identified uncovered lines; exact yield per test requires writing
  the tests and re-measuring
- CI gate update: HIGH — single YAML line change, exact syntax confirmed

**Research date:** 2026-02-24
**Valid until:** 2026-03-24 (stable — no external dependencies, pure Python codebase)
