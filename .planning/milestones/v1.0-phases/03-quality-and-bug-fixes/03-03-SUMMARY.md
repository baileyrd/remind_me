---
phase: 03-quality-and-bug-fixes
plan: "03"
subsystem: database
tags: [sqlite, asyncio, concurrency, threading, wal, singleton]

# Dependency graph
requires:
  - phase: 03-01
    provides: schema migrations, memory_tags junction table, FTS trigger fixes
  - phase: 03-02
    provides: bug-fixed tool handlers (BUGF-01 embed_pairs, BUGF-02 capture_id index, DATA-02 tag filtering)
provides:
  - SQLite singleton connection with WAL mode, busy_timeout=5000, check_same_thread=False
  - asyncio.to_thread wrapping for all blocking embedding computations in tool handlers
  - _close_db() for clean shutdown, used in app_lifespan
  - 6 async safety and concurrency tests proving thread-safety under concurrent MCP tool calls
affects: [production deployment, multi-process SQLite access, Claude Desktop + Claude Code concurrent use]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - Lazy singleton DB connection via module-level _db_connection variable
    - asyncio.to_thread wrapping for CPU-bound operations in async handlers
    - WAL + busy_timeout pattern for SQLite concurrent reader safety

key-files:
  created:
    - tests/test_async.py
  modified:
    - remind_me_mcp/db.py
    - remind_me_mcp/server.py
    - remind_me_mcp/tools.py

key-decisions:
  - "_db_connection singleton at module level — lazy init avoids opening DB until first call; reset to None on _close_db for testability"
  - "check_same_thread=False required because asyncio.to_thread workers run on thread pool; WAL mode makes this safe (SQLite internal mutex handles serialization)"
  - "Only CPU-bound embedding calls wrapped with asyncio.to_thread — simple DB reads/writes are fast enough inline"
  - "busy_timeout=5000ms — 5 second grace period for lock contention before raising OperationalError, covers multi-process access (Claude Code + Claude Desktop sharing DB)"
  - "spy_to_thread pattern in test_embed_and_store_runs_in_thread — wraps asyncio.to_thread via monkeypatch to record calls without losing actual thread-offload behavior"

patterns-established:
  - "Singleton DB pattern: module-level None guard, global assignment on first open, _close_db for teardown"
  - "asyncio.to_thread wrapping: wrap only the blocking function call, pass db as argument since connection is thread-safe with check_same_thread=False"

requirements-completed: [ASYN-01, ASYN-02, ASYN-03, ASYN-04, ASYN-05]

# Metrics
duration: 4min
completed: 2026-02-24
---

# Phase 3 Plan 03: Async Safety Summary

**SQLite singleton with WAL/busy_timeout + asyncio.to_thread embedding offload making all MCP tool handlers safe for concurrent asyncio.gather calls**

## Performance

- **Duration:** 4 min
- **Started:** 2026-02-24T05:06:04Z
- **Completed:** 2026-02-24T05:09:55Z
- **Tasks:** 2
- **Files modified:** 4

## Accomplishments

- Converted `_get_db` from per-call connection factory to lazy singleton with WAL journal mode, busy_timeout=5000, and check_same_thread=False — enabling safe cross-thread and multi-process access
- Wrapped all blocking embedding computations (`_embed_and_store`, `_semantic_search`, `embedder.embed_one`) in async tool handlers with `asyncio.to_thread` — prevents event loop starvation under concurrent MCP calls
- Added `_close_db()` for clean singleton teardown; updated `app_lifespan` in server.py to call it instead of `db.close()`
- Created 6 async safety tests in `tests/test_async.py` proving singleton identity, WAL mode, busy_timeout, concurrent asyncio.gather success, thread offload, and cross-thread access

## Task Commits

Each task was committed atomically:

1. **Task 1: Convert _get_db to singleton with WAL + busy_timeout, wrap embedding calls** - `a97f80e` (feat)
2. **Task 2: Add async safety and concurrency tests** - `daf0aab` (test)

**Plan metadata:** (docs commit — next)

## Files Created/Modified

- `remind_me_mcp/db.py` - Added `_db_connection` singleton, `_close_db()`, WAL/busy_timeout/check_same_thread=False connection settings, exported `_close_db` in `__all__`
- `remind_me_mcp/server.py` - Import `_close_db`, call it instead of `db.close()` in `app_lifespan`
- `remind_me_mcp/tools.py` - Added `import asyncio`; wrapped `_embed_and_store` calls in `memory_add`, `memory_update`, `remind_me_auto_capture` and `_semantic_search` in `memory_search` and `embedder.embed_one` loop in `remind_me_reindex` with `asyncio.to_thread`
- `tests/test_async.py` - 6 new tests: singleton, WAL mode, busy_timeout, concurrent gather, to_thread spy, cross-thread access

## Decisions Made

- **Singleton guard pattern**: `if _db_connection is not None: return _db_connection` at top of `_get_db` — simple and safe since MCP server is single-process
- **check_same_thread=False rationale**: Required when async handlers run `asyncio.to_thread` workers — thread pool threads are "different threads" from Python's perspective even though only one actually calls the DB at a time in WAL mode
- **Only CPU-bound calls wrapped**: Simple `db.execute()` / `db.commit()` calls are not wrapped — overhead of to_thread would exceed any benefit for microsecond operations
- **spy_to_thread approach**: Monkeypatching `asyncio.to_thread` via the module attribute (`_tools_mod.asyncio.to_thread`) captures calls while still executing the real thread offload, making the test behavior-accurate

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None - all tests passed on first run.

## Next Phase Readiness

- All 5 async safety requirements (ASYN-01 through ASYN-05) are now fulfilled
- Phase 3 Plan 04 (lint/type-check fixes) can proceed; this plan's changes introduce no new linting issues
- DB singleton is reset to None by `_close_db()`, so test isolation via `db_conn` fixture monkeypatching `_get_db` is unaffected

---
*Phase: 03-quality-and-bug-fixes*
*Completed: 2026-02-24*
