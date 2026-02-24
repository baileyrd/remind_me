---
phase: 02-test-infrastructure
plan: "01"
subsystem: testing
tags: [pytest, pytest-asyncio, sqlite, numpy, fixtures, conftest]

# Dependency graph
requires:
  - phase: 01-package-structure
    provides: remind_me_mcp package with db.py (_ensure_schema, _make_id, _now_iso), config.py (DB_PATH, MEMORY_DIR, PID_FILE, IMPORT_LOG), embeddings.py (_get_embedder, _Embedder)
provides:
  - tests/__init__.py — package marker enabling pytest collection
  - tests/conftest.py — shared fixtures: tmp_memory_dir, db_conn, mock_embedder, memory_factory, sample_chat_json, sample_chat_md
  - tests/test_smoke.py — 8 passing smoke tests validating fixture correctness
affects:
  - 02-02 (db layer tests — uses db_conn, memory_factory, mock_embedder)
  - 02-03 (tool handler tests — uses db_conn, memory_factory, mock_embedder)
  - 02-04 (API integration tests — uses db_conn, sample_chat_json, sample_chat_md)

# Tech tracking
tech-stack:
  added:
    - pytest==9.0.2 (installed into .venv via uv pip install)
    - pytest-asyncio==1.3.0 (installed into .venv via uv pip install; asyncio_mode=auto already configured in pyproject.toml)
  patterns:
    - Session-scoped autouse fixture (tmp_memory_dir) uses pytest.MonkeyPatch() directly to avoid fixture scope mismatch
    - In-memory SQLite via sqlite3.connect(":memory:") with _ensure_schema for zero-file-I/O test isolation
    - FakeEmbedder seeded by hash(text) so identical texts always produce identical vectors, no ML model loading
    - memory_factory callable pattern avoids INSERT boilerplate repetition across all test modules

key-files:
  created:
    - tests/__init__.py
    - tests/conftest.py
    - tests/test_smoke.py
  modified: []

key-decisions:
  - "Session-scoped monkeypatch uses pytest.MonkeyPatch() directly — function-scoped monkeypatch fixture cannot be injected into session-scoped fixtures"
  - "FakeEmbedder seeds np.random.default_rng on hash(text) for deterministic per-text vectors without ML model dependency"
  - "db_conn monkeypatches both remind_me_mcp.db._get_db and remind_me_mcp.api._get_db since api.py imports _get_db directly"

patterns-established:
  - "Fixture isolation pattern: session-scope path redirection + function-scope in-memory DB + no file I/O"
  - "Mock embedder pattern: FakeEmbedder class mimics _Embedder interface for drop-in monkeypatching"

requirements-completed: [TEST-04, TEST-05, TEST-06]

# Metrics
duration: 2min
completed: 2026-02-24
---

# Phase 2 Plan 01: Test Infrastructure Fixtures Summary

**Shared pytest fixture suite with in-memory SQLite schema, deterministic FakeEmbedder, and memory row factory — zero file I/O, zero ML model loading**

## Performance

- **Duration:** 2 min
- **Started:** 2026-02-24T03:59:57Z
- **Completed:** 2026-02-24T04:02:34Z
- **Tasks:** 2
- **Files modified:** 3

## Accomplishments
- Session-scoped `tmp_memory_dir` autouse fixture redirects all config paths (MEMORY_DIR, DB_PATH, PID_FILE, IMPORT_LOG) to a temp directory with proper session-lifetime monkeypatching via `pytest.MonkeyPatch()` directly
- Function-scoped `db_conn` creates a fresh `:memory:` SQLite connection with full schema (memories, chat_imports, FTS5, triggers, indexes) for each test and monkeypatches `_get_db` in db.py and api.py
- `FakeEmbedder` yields deterministic (N, 384) float32 L2-normalised vectors seeded on `hash(text)` — same text always returns the same vector, no ML download
- `memory_factory` callable inserts rows with sensible defaults and returns full memory dicts for use in tests
- `sample_chat_json` and `sample_chat_md` fixtures provide temporary Claude export files
- All 8 smoke tests pass confirming fixture correctness

## Task Commits

Each task was committed atomically:

1. **Task 1: Create tests/__init__.py and conftest.py with all shared fixtures** - `b8d3a16` (feat)
2. **Task 2: Validate fixtures with a smoke test** - `947338d` (test)

**Plan metadata:** _(docs commit added after SUMMARY.md)_

## Files Created/Modified
- `tests/__init__.py` — empty package marker enabling pytest collection
- `tests/conftest.py` — all shared fixtures: tmp_memory_dir, db_conn, mock_embedder, memory_factory, sample_chat_json, sample_chat_md
- `tests/test_smoke.py` — 8 smoke tests validating fixture correctness

## Decisions Made
- Session-scoped `tmp_memory_dir` uses `pytest.MonkeyPatch()` directly instead of injecting the function-scoped `monkeypatch` fixture, which would cause a pytest scope mismatch error
- `FakeEmbedder.embed()` seeds on `hash(text) & 0xFFFFFFFF` (positive 32-bit) to ensure determinism and compatibility with `np.random.default_rng` seed constraints
- Both `remind_me_mcp.db._get_db` and `remind_me_mcp.api._get_db` are monkeypatched in `db_conn` because api.py does a direct `from remind_me_mcp.db import _get_db`, creating a separate binding

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Installed pytest and pytest-asyncio**
- **Found during:** Task 1 (creating conftest.py)
- **Issue:** pytest and pytest-asyncio were not installed in the project .venv — `python -m pytest --version` failed with ModuleNotFoundError
- **Fix:** Ran `uv pip install pytest pytest-asyncio` to install pytest==9.0.2 and pytest-asyncio==1.3.0 into .venv
- **Files modified:** None (venv packages only)
- **Verification:** `.venv/bin/python -m pytest --version` succeeds
- **Committed in:** b8d3a16 (noted in commit message, no file change to commit)

**2. [Rule 1 - Bug] Fixed session-scoped fixture scope mismatch**
- **Found during:** Task 1 (writing tmp_memory_dir)
- **Issue:** Plan specified using `monkeypatch` fixture in session-scoped fixture, but pytest's `monkeypatch` is function-scoped and cannot be injected into session-scoped fixtures — would error at runtime
- **Fix:** Changed `tmp_memory_dir` to instantiate `pytest.MonkeyPatch()` directly and call `mp.undo()` in teardown, eliminating the scope dependency entirely
- **Files modified:** tests/conftest.py
- **Verification:** `pytest --collect-only` completes without scope errors; all 8 smoke tests pass
- **Committed in:** b8d3a16

---

**Total deviations:** 2 auto-fixed (1 blocking dependency, 1 bug)
**Impact on plan:** Both fixes necessary for correct operation. No scope creep.

## Issues Encountered
None beyond the deviations above.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- All fixtures ready for use by plans 02-02 through 02-04
- `db_conn`, `mock_embedder`, and `memory_factory` provide complete test isolation
- asyncio_mode=auto already configured in pyproject.toml — async test functions will work without explicit markers
- No blockers

## Self-Check: PASSED

- FOUND: tests/__init__.py
- FOUND: tests/conftest.py
- FOUND: tests/test_smoke.py
- FOUND: .planning/phases/02-test-infrastructure/02-01-SUMMARY.md
- FOUND commit b8d3a16 (feat: tests package with shared fixtures)
- FOUND commit 947338d (test: smoke tests validating all shared fixtures)

---
*Phase: 02-test-infrastructure*
*Completed: 2026-02-24*
