---
phase: 02-test-infrastructure
plan: "03"
subsystem: testing
tags: [pytest, pytest-asyncio, sqlite, fts5, mcp, tools, integration-tests]

# Dependency graph
requires:
  - phase: 02-test-infrastructure
    plan: "01"
    provides: tests/conftest.py with db_conn, mock_embedder, memory_factory, sample_chat_json, sample_chat_md fixtures
  - phase: 01-package-structure
    provides: remind_me_mcp.tools with all 13 handlers, remind_me_mcp.models Pydantic models, remind_me_mcp.db with FTS5 schema
provides:
  - tests/test_tools.py — 39 integration tests covering all 13 MCP tool handlers and 2 resource handlers
affects:
  - 02-04 (API integration tests — same fixture pattern)
  - 03-xx (bug fix phase — test coverage enables safe refactoring)

# Tech tracking
tech-stack:
  added: []
  patterns:
    - Direct binding monkeypatch: tools.py and importer.py use `from remind_me_mcp.db import _get_db` which creates separate bindings; db_conn patches all module-local bindings directly
    - Import round-trip pattern: import chat file then FTS5 search confirms end-to-end visibility
    - Capture ID extraction pattern: regex on tool response string to get capture_id for cross-referencing

key-files:
  created:
    - tests/test_tools.py
  modified:
    - tests/conftest.py

key-decisions:
  - "db_conn fixture must patch remind_me_mcp.tools._get_db and remind_me_mcp.importer._get_db in addition to remind_me_mcp.db._get_db — both modules use direct imports creating separate bindings"
  - "Import dedup test uses same file path twice — hash-based dedup in import_chat_file ensures second import returns status=skipped"
  - "test_reindex_with_embedder verifies graceful handling when memories_vec table is absent (no sqlite-vec in :memory: SQLite)"
  - "server_status test monkeypatches remind_me_mcp.tools.get_server_status (not the pid module) because tools.py imports it directly"

patterns-established:
  - "Tool handler test pattern: create Pydantic input model, await handler, assert response string, query db_conn directly to verify DB state"
  - "Resource handler test pattern: call async resource function directly (no HTTP), assert JSON parsed result"

requirements-completed: [TEST-02, TEST-05, TEST-06]

# Metrics
duration: 4min
completed: 2026-02-24
---

# Phase 2 Plan 03: MCP Tool Handler Integration Tests Summary

**39 async integration tests covering all 13 MCP tool handlers and 2 resource handlers via real FTS5 SQLite — CRUD cycle, import+search round-trip, and auto-capture linking all verified**

## Performance

- **Duration:** 4 min
- **Started:** 2026-02-24T04:05:20Z
- **Completed:** 2026-02-24T04:09:28Z
- **Tasks:** 2
- **Files modified:** 2 (tests/test_tools.py created, tests/conftest.py patched)

## Accomplishments
- 39 async integration tests covering every MCP tool handler: memory_add (3), memory_get (2), memory_search (4), memory_list (4), memory_update (4), memory_delete (2), full CRUD cycle (1), memory_import_chat (4), memory_import_directory (2), memory_stats (3), remind_me_auto_capture (3), remind_me_get_capture (2), remind_me_reindex (2), remind_me_server_status (1)
- 2 resource handler tests: resource_stats and resource_categories
- Full CRUD cycle verified end-to-end: add -> get -> search -> update -> get (new content) -> delete -> get (not found)
- Import + search round-trip: import JSON chat file then FTS5 search confirms memories are findable (Phase 2 success criterion met)
- Auto-capture creates two linked memories with shared capture_id, cross-referencing linked_summary/linked_dialog in metadata
- All 39 tests pass in 0.49s with asyncio_mode=auto (no explicit markers needed)

## Task Commits

Both tasks were captured together in one commit:

1. **Task 1 + Task 2: Full tool handler integration tests** - `93f5041` (feat)

**Plan metadata:** _(docs commit follows SUMMARY.md creation)_

## Files Created/Modified
- `tests/test_tools.py` — 39 integration tests for all 13 MCP tool handlers and 2 resource handlers
- `tests/conftest.py` — Extended db_conn fixture to also patch `_get_db` in `remind_me_mcp.tools` and `remind_me_mcp.importer` modules (Rule 1 bug fix)

## Decisions Made
- The `db_conn` fixture in `conftest.py` was extended to monkeypatch `remind_me_mcp.tools._get_db` and `remind_me_mcp.importer._get_db` because both modules use `from remind_me_mcp.db import _get_db` (a direct import that creates a module-local binding separate from the module attribute). Without patching these local bindings, all tool handler tests wrote to the real DB file rather than the in-memory test connection.
- `test_reindex_with_embedder` asserts only that the result is a non-empty string (not a specific message) because the mock embedder succeeds at generating vectors but the `memories_vec` virtual table is not available in `:memory:` SQLite without sqlite-vec — the function handles the missing table gracefully with try/except.
- `test_server_status_no_ui` monkeypatches `remind_me_mcp.tools.get_server_status` (not `remind_me_mcp.pid.get_server_status`) because `tools.py` does `from remind_me_mcp.pid import get_server_status`, creating a direct binding in the tools module namespace.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Extended db_conn to patch _get_db in tools and importer modules**
- **Found during:** Task 1 (writing test_memory_add_basic)
- **Issue:** All tool handler tests failed because `tools.py` and `importer.py` import `_get_db` via `from remind_me_mcp.db import _get_db`, creating separate bindings not covered by the existing `monkeypatch.setattr(_db_mod, "_get_db", ...)` call in the db_conn fixture. Tool calls were routing to the real file-backed DB instead of the in-memory test DB.
- **Fix:** Added `monkeypatch.setattr(_tools_mod, "_get_db", lambda: db)` and `monkeypatch.setattr(_importer_mod, "_get_db", lambda: db)` to the `db_conn` fixture in `conftest.py`, with an explanatory comment about the direct-import binding pattern.
- **Files modified:** tests/conftest.py
- **Verification:** All 39 tests pass; prior smoke tests still pass (8/8)
- **Committed in:** 93f5041 (included with task commit)

---

**Total deviations:** 1 auto-fixed (Rule 1 bug)
**Impact on plan:** Fix necessary for all tool handler tests to use the correct in-memory database. No scope creep — same pattern as the existing api.py patch already in conftest.

## Issues Encountered
None beyond the deviation above.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- All 13 MCP tool handlers have integration tests (TEST-02 satisfied)
- Import + search round-trip confirmed working (Phase 2 success criterion #5)
- Auto-capture linking verified (capture_id cross-referencing works correctly)
- All tests async with asyncio_mode=auto (TEST-05 satisfied)
- All tests use in-memory SQLite (TEST-06 satisfied)
- Ready for Phase 3 bug fix work — test safety net in place

## Self-Check: PASSED

- FOUND: tests/test_tools.py
- FOUND: tests/conftest.py (with tools and importer patches)
- FOUND commit 93f5041 (feat: add CRUD + import + capture integration tests)
- All 39 tests pass (confirmed by pytest run)
- Smoke tests still pass (8/8)

---
*Phase: 02-test-infrastructure*
*Completed: 2026-02-24*
