---
phase: 03-quality-and-bug-fixes
plan: "04"
subsystem: error-handling
tags: [sqlite3, exception-handling, logging, error-messages, mcp-tools]

# Dependency graph
requires:
  - phase: 03-03
    provides: singleton DB connection and asyncio.to_thread safety (needed before error handler wrapping)
provides:
  - Specific exception types in all DB/IO handlers (no bare except Exception in target files)
  - User-facing error messages from all MCP tool handlers on failure
  - Logging at appropriate levels (debug/warning/error) for all caught exceptions
affects: [03-quality-and-bug-fixes, future-phases]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - specific exception type catching (sqlite3.OperationalError, sqlite3.IntegrityError, json.JSONDecodeError, FileNotFoundError, OSError, ValueError, TypeError, UnicodeDecodeError)
    - log.warning for unexpected-but-handled failures, log.debug for optional/expected failures, log.error for operation failures
    - user-facing error messages starting with "Error:" from MCP tool handlers

key-files:
  created: []
  modified:
    - remind_me_mcp/db.py
    - remind_me_mcp/importer.py
    - remind_me_mcp/tools.py
    - remind_me_mcp/api.py
    - tests/test_tools.py

key-decisions:
  - "sqlite-vec loading split into two try/except blocks: ImportError for missing package, OperationalError for extension load failure"
  - "_embed_and_store and _semantic_search use log.warning (not log.debug) — failed embeddings are worth noting, not silently ignored"
  - "remind_me_auto_capture restructured so both INSERTs are inside a single try/except OperationalError block for atomicity"
  - "test_memory_import_chat_file_not_found creates a real file, then unlinks it before the handler runs — tests the defensive FileNotFoundError path that Pydantic validation cannot reach"
  - "embeddings.py and pid.py bare except Exception left untouched — out of scope for this plan; logged in deferred items"

patterns-established:
  - "Error pattern: specific except types, log before handling, return user-facing message from MCP tools"
  - "MCP tool error messages use 'Error: <description> — <detail>' format for actionability"

requirements-completed: [ERRH-01, ERRH-02, ERRH-03]

# Metrics
duration: 3min
completed: 2026-02-24
---

# Phase 3 Plan 04: Error Handling Hardening Summary

**Specific exception types throughout DB/IO layer with log.warning/error on failures, plus user-facing error messages from MCP tool handlers on sqlite3.OperationalError, IntegrityError, FileNotFoundError, and OSError**

## Performance

- **Duration:** 3 min
- **Started:** 2026-02-24T05:13:52Z
- **Completed:** 2026-02-24T05:16:52Z
- **Tasks:** 2
- **Files modified:** 5

## Accomplishments
- Eliminated all bare `except Exception` blocks from the 4 target modules (db.py, importer.py, tools.py, api.py)
- All caught exceptions now logged at appropriate levels before handling (debug for optional, warning for unexpected-but-handled, error for operation failures)
- MCP tool handlers (memory_add, memory_import_chat, memory_stats, remind_me_auto_capture) now return clear "Error: ..." messages on database and I/O failures
- 5 new error-path tests verify user-facing messages; 172 total tests passing

## Task Commits

Each task was committed atomically:

1. **Task 1: Replace bare except blocks with specific types and add logging** - `8b6ccdf` (fix)
2. **Task 2: Add user-facing error messages to tool handlers and test error paths** - `b409e47` (feat)

**Plan metadata:** (docs commit below)

## Files Created/Modified
- `remind_me_mcp/db.py` - Split sqlite-vec load into ImportError/OperationalError; _embed_and_store/_semantic_search use specific types at log.warning; _ensure_schema OperationalError for vec table; _row_to_dict adds log.debug
- `remind_me_mcp/importer.py` - Added log.debug before continue in JSONL parse loop
- `remind_me_mcp/tools.py` - Specific exception types for import directory, FTS5, memories_vec queries; memory_add/memory_import_chat/memory_stats/remind_me_auto_capture all wrapped with user-facing error returns
- `remind_me_mcp/api.py` - (json.JSONDecodeError, TypeError, ValueError) for JSON parse in api_add/api_update/api_import; specific types for import outer handler; log.debug for stats tags
- `tests/test_tools.py` - 5 new ERRH error-path tests: not-found messages for get/delete/update/capture, file-not-found for import

## Decisions Made
- Split sqlite-vec loading into two nested try/except blocks: `ImportError` for "package not installed" and `sqlite3.OperationalError` for "extension load failed at runtime" — distinct failure modes deserve distinct handling
- `_embed_and_store` and `_semantic_search` upgraded from `log.debug` to `log.warning` — embedding failures are unexpected operational issues, not anticipated behavior
- `remind_me_auto_capture` restructured to put all INSERTs and UPDATE inside a single `try` block — ensures the OperationalError handler covers the full transaction
- `test_memory_import_chat_file_not_found` creates a temporary file (satisfying Pydantic validation) then unlinks it before calling the handler — the only way to exercise the defensive `FileNotFoundError` path in `memory_import_chat`

## Deviations from Plan

None — plan executed exactly as written. Pre-existing `except Exception` blocks in `embeddings.py` and `pid.py` are out of scope and untouched.

## Issues Encountered

- Initial `test_memory_import_chat_file_not_found` used a nonexistent path, which triggered Pydantic's `@field_validator` on `ChatImportInput.file_path` before the handler ran — raised `ValidationError` not `FileNotFoundError`. Fixed by creating a real file, then deleting it before invoking the handler.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- All Phase 3 plans (03-01 through 03-04) are now complete
- Error handling, async safety, schema migrations, and bug fixes are done
- Project is ready for production use or next planned phase

---
*Phase: 03-quality-and-bug-fixes*
*Completed: 2026-02-24*

## Self-Check: PASSED

- FOUND: 03-04-SUMMARY.md
- FOUND: remind_me_mcp/db.py
- FOUND: remind_me_mcp/tools.py
- FOUND: remind_me_mcp/api.py
- FOUND: commit 8b6ccdf (Task 1)
- FOUND: commit b409e47 (Task 2)
