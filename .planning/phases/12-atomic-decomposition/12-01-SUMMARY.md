---
phase: 12-atomic-decomposition
plan: 01
subsystem: database, api
tags: [decomposition, atomic-facts, schema-migration, mcp-tools, pydantic]

# Dependency graph
requires:
  - phase: 11-decay-vitality-classification
    provides: "vitality model, DECAY_RATES, memory_type column, classification tools"
provides:
  - "remind_me_decompose tool for breaking captures into atomic facts"
  - "remind_me_decompose_batch tool for fetching undecomposed captures"
  - "Schema v6 with source_capture_id column and index"
  - "AtomicFact, DecomposeInput, DecomposeBatchInput Pydantic models"
affects: [search, retrieval, future-decomposition-automation]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "source_capture_id linkage for parent-child memory relationships"
    - "Tag inheritance from parent capture to decomposed facts"
    - "NOT EXISTS subquery pattern for finding undecomposed captures"

key-files:
  created: []
  modified:
    - "remind_me_mcp/db.py"
    - "remind_me_mcp/models.py"
    - "remind_me_mcp/tools.py"
    - "tests/test_tools.py"
    - "tests/conftest.py"

key-decisions:
  - "Decomposed facts get category='fact' and source='decomposition' for consistent identification"
  - "source_capture_id column with NULL default enables backward compatibility"
  - "Tag deduplication uses dict.fromkeys for order-preserving uniqueness"
  - "capture_id on decomposed children is NULL (they are not captures themselves)"

patterns-established:
  - "Parent-child memory linkage via source_capture_id column"
  - "Batch fetch with NOT EXISTS subquery to exclude already-processed items"

requirements-completed: [ATOM-01, ATOM-02, ATOM-03, ATOM-05]

# Metrics
duration: 5min
completed: 2026-03-05
---

# Phase 12 Plan 01: Atomic Decomposition Summary

**Two new MCP tools (remind_me_decompose + remind_me_decompose_batch) with schema v6 migration adding source_capture_id for parent-child memory linkage and tag inheritance**

## Performance

- **Duration:** 5 min
- **Started:** 2026-03-05T18:45:03Z
- **Completed:** 2026-03-05T18:50:00Z
- **Tasks:** 2
- **Files modified:** 5

## Accomplishments
- Schema v6 migration adds source_capture_id column with index for efficient parent-child lookups
- remind_me_decompose stores individual atomic facts linked to parent captures with tag inheritance
- remind_me_decompose_batch identifies undecomposed captures using NOT EXISTS subquery
- AtomicFact model validates optional memory_type against VALID_MEMORY_TYPES
- 21 new tests (9 model/migration + 12 integration), 126 total passing, zero regressions

## Task Commits

Each task was committed atomically:

1. **Task 1: Schema migration v5->v6 and Pydantic models** (TDD)
   - `a76c8bb` test: add failing tests for schema v6 migration and decompose models
   - `1bf4bc5` feat: implement schema v6 migration and decompose Pydantic models
2. **Task 2: remind_me_decompose and remind_me_decompose_batch tools** (TDD)
   - `a137366` test: add failing tests for decompose and decompose_batch tools
   - `404b84d` feat: implement remind_me_decompose and remind_me_decompose_batch tools

## Files Created/Modified
- `remind_me_mcp/db.py` - Added _migrate_v5_to_v6 with source_capture_id column, index, and outbox trigger update
- `remind_me_mcp/models.py` - Added AtomicFact, DecomposeInput, DecomposeBatchInput Pydantic models
- `remind_me_mcp/tools.py` - Added remind_me_decompose and remind_me_decompose_batch tool handlers (19 tools total)
- `tests/test_tools.py` - Added 21 new decompose tests (model validation + integration)
- `tests/conftest.py` - Updated memory_factory to support capture_id and source_capture_id columns

## Decisions Made
- Decomposed facts use category='fact' and source='decomposition' for consistent filtering
- source_capture_id is NULL by default (backward compatible with existing memories)
- Tag deduplication uses dict.fromkeys for order-preserving uniqueness
- Decomposed children have capture_id=NULL (they are not captures themselves)
- Fire-and-forget embedding via asyncio.create_task (consistent with existing pattern)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] memory_factory missing capture_id support**
- **Found during:** Task 2 (tool handler tests)
- **Issue:** Test memory_factory did not include capture_id in its INSERT or update columns, so test memories with capture_id were not being stored correctly
- **Fix:** Added capture_id and source_capture_id to the v5_cols update pattern in conftest.py memory_factory
- **Files modified:** tests/conftest.py
- **Verification:** All decompose tests pass with correct capture_id values
- **Committed in:** 404b84d (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (1 blocking)
**Impact on plan:** Necessary fix for test infrastructure. No scope creep.

## Issues Encountered
None beyond the memory_factory deviation above.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Decomposition tools ready for use by Claude to break captures into atomic facts
- source_capture_id linkage enables future queries for "all facts from capture X"
- Batch tool enables retroactive decomposition of existing captures

---
*Phase: 12-atomic-decomposition*
*Completed: 2026-03-05*
