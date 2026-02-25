---
phase: 04-code-quality-and-cleanup
plan: 02
subsystem: testing
tags: [exception-handling, code-quality, python, ruff, onnx]

# Dependency graph
requires:
  - phase: 04-01
    provides: Zero ruff warnings baseline and deleted monolith (QUAL-01, QUAL-03)
provides:
  - pid.py _check_ui_server_health narrowed from except Exception to except OSError
  - embeddings.py three broad ONNX handlers preserved with inline rationale comments
  - updater.py background-check broad handler preserved with inline rationale comment
  - QUAL-02 fully satisfied; all exception handlers auditable
affects: [05-ci-and-coverage, 06-security-and-cors, 07-embedding-parity, 08-performance]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - Narrow exception handler to except OSError for urllib.request.urlopen (URLError inherits OSError)
    - Document intentionally broad handlers at ONNX and background-task graceful-degradation boundaries

key-files:
  created: []
  modified:
    - remind_me_mcp/pid.py
    - remind_me_mcp/embeddings.py
    - remind_me_mcp/updater.py

key-decisions:
  - "pid.py: used except OSError (builtin) rather than except urllib.error.URLError — simpler, no import needed, URLError is OSError subclass so coverage is identical"
  - "embeddings.py/updater.py broad handlers preserved: ONNX Runtime raises non-stdlib exception types; background task must never crash the server — both are valid graceful-degradation boundaries"
  - "All four documented broad handlers carry an inline comment with 'Broad catch intentional:' prefix for auditable grep"

patterns-established:
  - "OSError handler pattern: prefer except OSError over urllib.error.URLError when only network/connection failures need catching"
  - "Broad handler documentation: add 'Broad catch intentional:' inline comment explaining WHY (not just that) the handler is broad"

requirements-completed: [QUAL-02]

# Metrics
duration: 1min
completed: 2026-02-24
---

# Phase 4 Plan 02: Narrow Exception Handlers and Document Preserved Broad Handlers Summary

**pid.py narrowed from `except Exception` to `except OSError`; four ONNX/background-task broad handlers documented with auditable rationale; Phase 4 complete with all QUAL-01/02/03 criteria satisfied**

## Performance

- **Duration:** 1 min
- **Started:** 2026-02-24T18:16:57Z
- **Completed:** 2026-02-24T18:18:35Z
- **Tasks:** 2
- **Files modified:** 3

## Accomplishments
- Narrowed `_check_ui_server_health` in pid.py from `except Exception` to `except OSError` (URLError is OSError subclass — no import needed)
- Added `# Broad catch intentional:` comments to three ONNX boundary handlers in embeddings.py (lines 82, 145, 164)
- Added `# Broad catch intentional:` comment to background-check handler in updater.py (line 370)
- Final Phase 4 sweep confirmed: QUAL-01 (0 ruff warnings), QUAL-02 (exception handlers), QUAL-03 (monolith deleted), all 190 tests pass

## Task Commits

Each task was committed atomically:

1. **Task 1: Narrow pid.py exception handler and document preserved handlers** - `2b17526` (fix)
2. **Task 2: Final phase validation sweep** - no code changes (verification only)

## Files Created/Modified
- `remind_me_mcp/pid.py` - `_check_ui_server_health`: `except Exception` -> `except OSError`
- `remind_me_mcp/embeddings.py` - Lines 82, 145, 164: added `Broad catch intentional:` inline comments
- `remind_me_mcp/updater.py` - Line 370: added `Broad catch intentional:` inline comment

## Decisions Made
- Used `except OSError` (builtin) instead of `except urllib.error.URLError` — simpler, no import needed, `URLError` is a direct subclass of `OSError` so coverage is identical.
- Preserved all four broad handlers at ONNX and background-task boundaries. ONNX Runtime raises internal C++ exception types (e.g., `onnxruntime.capi.onnxruntime_pybind11_state.InvalidGraph`) that do not inherit from any stdlib exception; catching them requires `except Exception`. Background check must never crash the server — the broad handler is the correct design.
- Applied a uniform `# Broad catch intentional:` prefix to all preserved handlers so they are auditable with a single `grep`.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None. All five exception handler changes applied cleanly on first attempt. All 190 tests passed after changes. ruff check reported zero warnings throughout.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Phase 4 fully complete: QUAL-01 (zero ruff warnings), QUAL-02 (exception handlers auditable), QUAL-03 (monolith deleted) all satisfied
- All 190 tests pass; clean baseline for Phase 5 (CI and coverage)
- Exception handler audit trail established via `grep "Broad catch intentional"` — easy to verify in code review

---
*Phase: 04-code-quality-and-cleanup*
*Completed: 2026-02-24*
