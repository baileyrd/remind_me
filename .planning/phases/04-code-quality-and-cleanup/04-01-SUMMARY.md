---
phase: 04-code-quality-and-cleanup
plan: 01
subsystem: testing
tags: [ruff, linting, code-quality, static-analysis, python]

# Dependency graph
requires: []
provides:
  - Zero ruff warnings across all active source and test files
  - Dead monolith file (remind_me_mcp_original.py) removed from repository
  - TYPE_CHECKING block with Starlette and Request imports in api.py
  - contextlib.suppress pattern in db.py migrate_schema
  - Clean lint baseline for all subsequent v1.1 phases
affects: [05-ci-and-coverage, 06-security-and-cors, 07-embedding-parity, 08-performance]

# Tech tracking
tech-stack:
  added: [contextlib.suppress (SIM105 pattern)]
  patterns:
    - TYPE_CHECKING blocks for runtime-lazy imports that appear in annotations
    - contextlib.suppress over try-except-pass for expected single-exception suppression
    - noqa: F401 to preserve intentional side-effect imports from ruff auto-removal

key-files:
  created: []
  modified:
    - remind_me_mcp/api.py
    - remind_me_mcp/db.py
    - remind_me_mcp/tools.py
    - remind_me_mcp/__init__.py
    - remind_me_mcp/__main__.py
    - remind_me_mcp/embeddings.py
    - remind_me_mcp/models.py
    - remind_me_mcp/updater.py
    - remind_me_mcp/importer.py
    - tests/conftest.py
    - tests/test_api.py
    - tests/test_async.py
    - tests/test_db.py
    - tests/test_formatting.py
    - tests/test_importer.py
    - tests/test_models.py
    - tests/test_smoke.py
    - tests/test_tools.py
    - tests/test_updater.py

key-decisions:
  - "Applied ruff --fix (safe) then ruff --fix --unsafe-fixes (unsafe TC/F841) in two separate passes to isolate regressions"
  - "Added Starlette to TYPE_CHECKING block in api.py alongside Request (F821 + TC002 coupled fix)"
  - "Used contextlib.suppress for SIM105 in db.py rather than noqa suppression (idiomatic Python)"
  - "Changed sem_memories loop variable i->_ in tools.py line 180 only; left fts_memories loop at line 174 unchanged (i used for ranking)"

patterns-established:
  - "TYPE_CHECKING guard pattern: runtime-lazy imports used only in annotations go in if TYPE_CHECKING block"
  - "contextlib.suppress pattern: preferred over try-except-pass for single known exception suppression"

requirements-completed: [QUAL-03, QUAL-01]

# Metrics
duration: 2min
completed: 2026-02-24
---

# Phase 4 Plan 01: Delete Monolith and Resolve All Ruff Warnings Summary

**Zero ruff warnings achieved by deleting dead 2495-line monolith and resolving all 85 warnings via safe auto-fix, unsafe auto-fix, and three targeted manual edits**

## Performance

- **Duration:** 2 min
- **Started:** 2026-02-24T18:10:44Z
- **Completed:** 2026-02-24T18:13:01Z
- **Tasks:** 2
- **Files modified:** 20 (including deletion of remind_me_mcp_original.py)

## Accomplishments
- Deleted `remind_me_mcp_original.py` (2495 lines, dead monolith, satisfies QUAL-03)
- Applied 48 safe ruff auto-fixes (I001 import sorting, UP045 Optional->X|None, F541 f-string, F401 unused imports, UP037 quoted annotation, UP017 timezone.utc->datetime.UTC)
- Applied 27 unsafe ruff auto-fixes (TC003 stdlib->TYPE_CHECKING in tests, TC002 third-party->TYPE_CHECKING in api.py, F841 unused variables)
- Applied 3 manual fixes (F821 Starlette in api.py TYPE_CHECKING block, SIM105 contextlib.suppress in db.py, B007 loop variable _ in tools.py)
- All 190 tests pass throughout; ruff check . reports zero warnings (satisfies QUAL-01)

## Task Commits

Each task was committed atomically:

1. **Task 1: Delete monolith and apply safe ruff auto-fixes** - `f2e6bd8` (chore)
2. **Task 2: Apply unsafe ruff auto-fixes and manual fixes** - `86a566f` (chore)

## Files Created/Modified
- `remind_me_mcp_original.py` - DELETED (dead 2495-line monolith, no references in active code)
- `remind_me_mcp/api.py` - Added Starlette to TYPE_CHECKING block; F401/UP037/TC002 auto-fixed
- `remind_me_mcp/db.py` - Added contextlib import; replaced try-except-pass with contextlib.suppress; UP017/F401 auto-fixed
- `remind_me_mcp/tools.py` - Changed sem_memories loop variable i->_ (B007); F541 auto-fixed
- `remind_me_mcp/__init__.py` - I001 import sorting (side-effect import preserved with noqa: F401)
- `remind_me_mcp/__main__.py` - I001 import sorting (side-effect import preserved with noqa: F401)
- `remind_me_mcp/models.py` - UP045 Optional->X|None (all 9 instances)
- `remind_me_mcp/embeddings.py` - I001 import sorting
- `remind_me_mcp/importer.py` - F401 unused import removed
- `remind_me_mcp/updater.py` - F541 f-string prefix removed
- `tests/conftest.py` - I001 import sorting; TC003 stdlib Path->TYPE_CHECKING
- `tests/test_api.py` - TC003 stdlib Path->TYPE_CHECKING; F401 removed
- `tests/test_async.py` - F841 unused variable removed
- `tests/test_db.py` - I001/F401 auto-fixed; TC003 sqlite3->TYPE_CHECKING
- `tests/test_formatting.py` - F401 unused import removed
- `tests/test_importer.py` - TC003 stdlib->TYPE_CHECKING; TC002 pytest->TYPE_CHECKING
- `tests/test_models.py` - I001 import sorting
- `tests/test_smoke.py` - TC003 stdlib->TYPE_CHECKING
- `tests/test_tools.py` - TC003 stdlib->TYPE_CHECKING; TC002 pytest->TYPE_CHECKING; F841 removed
- `tests/test_updater.py` - F841 unused variable removed

## Decisions Made
- Applied ruff --fix (safe) then ruff --fix --unsafe-fixes (unsafe) in two separate passes to isolate regressions; all 190 tests passed after each pass.
- For api.py: the TC002 unsafe fix (moving Request to TYPE_CHECKING) and the F821 manual fix (adding Starlette to the same block) were applied as a coupled pair. The runtime `from starlette.applications import Starlette` inside `_build_api_app()` was preserved because it is needed at runtime.
- Used `contextlib.suppress` for db.py SIM105 (idiomatic) rather than `# noqa: SIM105` (suppression).
- Changed only line 180 (sem_memories loop) for B007; line 174 (fts_memories loop) uses `i` for ranking calculation and was explicitly left unchanged.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None. The safe auto-fixes, unsafe auto-fixes, and all three manual fixes applied cleanly on the first attempt. All 190 tests passed after every step.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Zero ruff warnings established as the clean baseline for Phase 5 (CI and coverage)
- Side-effect imports in `__init__.py` and `__main__.py` preserved with `# noqa: F401` — MCP tool registry unaffected
- ONNX exception handlers in `embeddings.py` (lines 82, 145, 164) and background check in `updater.py` (line 370) were not touched — preserved per STATE.md blockers (QUAL-02 exception narrowing is a separate plan)
- All 190 tests pass; no regressions

---
*Phase: 04-code-quality-and-cleanup*
*Completed: 2026-02-24*
