---
phase: 08-performance-improvements
plan: 02
subsystem: importer
tags: [concurrency, asyncio, import, sqlite, threading, performance]

# Dependency graph
requires:
  - phase: 08-01
    provides: EMBED_BATCH_SIZE=32 batched reindex loop
  - phase: 07-api-embedding-parity
    provides: asyncio.to_thread pattern for thread-pool dispatch established
provides:
  - Async import_directory with asyncio.gather + asyncio.Semaphore(IMPORT_CONCURRENCY)
  - IMPORT_CONCURRENCY = 8 module-level constant in importer.py
  - threading.Lock (_import_lock) serializing DB writes in import_chat_file
  - Test proving 12-file concurrent import processes all files correctly
affects: [importer, tools, db, tests]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Concurrent file import: asyncio.gather over _import_one coroutines; each acquires asyncio.Semaphore(IMPORT_CONCURRENCY) before dispatching via asyncio.to_thread"
    - "Phase split in import_chat_file: Phase 1 (file I/O + parsing, no lock) then Phase 2 (DB writes, threading.Lock serialized)"
    - "Broaden SQLite exception catch to sqlite3.DatabaseError for thread-safety: OperationalError is a subclass; concurrent thread access can surface as base DatabaseError"

key-files:
  created: []
  modified:
    - remind_me_mcp/importer.py
    - remind_me_mcp/tools.py
    - remind_me_mcp/db.py
    - tests/test_tools.py

key-decisions:
  - "IMPORT_CONCURRENCY = 8 as module-level integer constant; asyncio.Semaphore(IMPORT_CONCURRENCY) created inside async function body (not module level) per Python 3.10+ requirement"
  - "threading.Lock (_import_lock) serializes DB write phase in import_chat_file; file I/O and parsing remain concurrent — lock only guards the DB section"
  - "import_chat_file restructured into Phase 1 (parse, no lock) and Phase 2 (DB writes, locked) to maximize actual I/O concurrency while preventing SQLite InterfaceError"
  - "sqlite3.DatabaseError (parent of OperationalError) now caught in _embed_and_store — concurrent thread access on shared connection can raise base DatabaseError instead of OperationalError"

patterns-established:
  - "Deviation Rule 1: Two concurrent SQLite thread-safety bugs found and auto-fixed during Task 1 verification: (1) DatabaseError from concurrent _embed_and_store, (2) InterfaceError from 12 concurrent workers — both fixed without architectural changes"

requirements-completed: [PERF-02]

# Metrics
duration: 4min
completed: 2026-02-24
---

# Phase 8 Plan 2: Concurrent Directory Import Summary

**Converted import_directory from a sequential sync loop to an async function using asyncio.gather + asyncio.Semaphore(8), enabling concurrent file I/O and CPU overlap; restructured import_chat_file with a threading.Lock to serialize SQLite writes safely across thread-pool workers**

## Performance

- **Duration:** ~4 min
- **Started:** 2026-02-25T00:05:27Z
- **Completed:** 2026-02-25T00:09:24Z
- **Tasks:** 2
- **Files modified:** 4

## Accomplishments

- Added `import asyncio` and `IMPORT_CONCURRENCY = 8` constant to `importer.py`
- Added `import threading` and `_import_lock = threading.Lock()` to `importer.py`
- Converted `import_directory` from `def` to `async def`
- Replaced sequential `for f in sorted(files)` loop with `asyncio.gather` over `_import_one` coroutines
- Each `_import_one` acquires `asyncio.Semaphore(IMPORT_CONCURRENCY)` before dispatching `import_chat_file` via `asyncio.to_thread`
- Restructured `import_chat_file` into Phase 1 (file I/O + parsing, no lock) and Phase 2 (DB writes, `_import_lock` serialized)
- Updated `memory_import_directory` in `tools.py` to `await import_directory()`
- Broadened `_embed_and_store` exception catch from `sqlite3.OperationalError` to `sqlite3.DatabaseError` in `db.py`
- Added `test_import_directory_concurrent`: 12 files, asserts all 12 imported, zero errors
- Full test suite: **215 tests pass** (1 new), zero ruff warnings

## Task Commits

Each task was committed atomically:

1. **Task 1: Convert import_directory to async with semaphore-bounded concurrency** - `cd218aa` (feat)
2. **Task 2: Add concurrent import correctness test with 12 files** - `1949c99` (test)

**Plan metadata:** (docs commit follows)

## Files Created/Modified

- `/home/baileyrd/projects/remind_me/remind_me_mcp/importer.py` - asyncio+threading imports, IMPORT_CONCURRENCY, _import_lock, async import_directory with gather+semaphore, restructured import_chat_file
- `/home/baileyrd/projects/remind_me/remind_me_mcp/tools.py` - await import_directory() in memory_import_directory
- `/home/baileyrd/projects/remind_me/remind_me_mcp/db.py` - broaden sqlite3.OperationalError to sqlite3.DatabaseError in _embed_and_store
- `/home/baileyrd/projects/remind_me/tests/test_tools.py` - test_import_directory_concurrent (12-file correctness test)

## Decisions Made

- `IMPORT_CONCURRENCY = 8` at module level (integer); `asyncio.Semaphore(IMPORT_CONCURRENCY)` created inside the async function body to avoid RuntimeError on Python 3.10+ (Semaphore requires a running event loop)
- `threading.Lock` (not asyncio.Lock) because the lock is acquired inside a synchronous `import_chat_file` running in `asyncio.to_thread` worker threads — asyncio primitives cannot be awaited from non-async context
- Phase split design: file I/O is O(file_size) and slow; parsing is CPU-bound but GIL-limited; only DB writes need serialization — this maximizes the concurrency benefit
- Broadened to `sqlite3.DatabaseError` in `_embed_and_store`: with 12 concurrent threads sharing one SQLite connection, failed writes occasionally surface as base `DatabaseError` (not just `OperationalError` as on single-thread path)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed sqlite3.DatabaseError from concurrent _embed_and_store calls**
- **Found during:** Task 1 verification (test_import_directory)
- **Issue:** Two concurrent `asyncio.to_thread` workers calling `_embed_and_store` on the same SQLite connection raised `sqlite3.DatabaseError: no such table: memories_vec` — parent class not caught by `except sqlite3.OperationalError`
- **Fix:** Changed `_embed_and_store` catch from `sqlite3.OperationalError` to `sqlite3.DatabaseError` in `db.py`
- **Files modified:** `remind_me_mcp/db.py`
- **Commit:** `cd218aa` (Task 1 commit)

**2. [Rule 1 - Bug] Fixed sqlite3.InterfaceError from 12 concurrent workers sharing SQLite connection**
- **Found during:** Task 2 (test_import_directory_concurrent with 12 files)
- **Issue:** With 8 concurrent thread-pool workers (IMPORT_CONCURRENCY=8) all calling `db.execute()` simultaneously on the shared in-memory connection, SQLite raised `InterfaceError: bad parameter or other API misuse`
- **Fix:** Added `_import_lock = threading.Lock()` at module level; restructured `import_chat_file` into Phase 1 (parse, no lock) + Phase 2 (DB writes, `with _import_lock`) to serialize SQLite access while keeping I/O concurrent
- **Files modified:** `remind_me_mcp/importer.py`
- **Commit:** `1949c99` (Task 2 commit)

**3. [Rule 3 - Lint] Fixed UP037 ruff warning in test**
- **Found during:** Task 2 ruff check
- **Issue:** Quoted `"Path"` annotation in `test_import_directory_concurrent` — redundant due to `from __future__ import annotations`
- **Fix:** Removed quotes from `tmp_path: "Path"` → `tmp_path: Path`
- **Files modified:** `tests/test_tools.py`
- **Commit:** `1949c99`

---

**Total deviations:** 3 auto-fixed (2 Rule 1 thread-safety bugs, 1 Rule 3 lint)
**Impact on plan:** The threading lock was essential for correctness; without it the concurrent implementation would be broken in production (not just tests). No scope creep — all fixes directly caused by the concurrency change.

## Issues Encountered

None remaining — all thread-safety issues resolved inline.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- PERF-02 satisfied: directory import processes files concurrently with semaphore-bounded parallelism (IMPORT_CONCURRENCY=8)
- 12-file concurrent import test passes, proving correctness under concurrency
- All 215 tests green with zero regressions and zero ruff warnings
- Phase 8 complete — all v1.1 phases done

## Self-Check: PASSED

- FOUND: `remind_me_mcp/importer.py` (async import_directory, IMPORT_CONCURRENCY, _import_lock)
- FOUND: `remind_me_mcp/tools.py` (await import_directory)
- FOUND: `remind_me_mcp/db.py` (sqlite3.DatabaseError catch)
- FOUND: `tests/test_tools.py` (test_import_directory_concurrent)
- FOUND commit: `cd218aa` (feat: async import_directory with asyncio.gather + Semaphore)
- FOUND commit: `1949c99` (test: add concurrent import correctness test with 12 files)

---
*Phase: 08-performance-improvements*
*Completed: 2026-02-24*
