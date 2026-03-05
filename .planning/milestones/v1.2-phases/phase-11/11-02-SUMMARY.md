---
phase: 11-decay-vitality-classification
plan: 02
subsystem: classification
tags: [pydantic, mcp-tools, memory-classification, decay-rates]
dependency_graph:
  requires:
    - phase: 11-01
      provides: [schema-v5, vitality-module, decay-rates]
  provides:
    - remind_me_reclassify tool
    - remind_me_reclassify_batch tool
    - MemoryClassification, ReclassifyInput, ReclassifyBatchInput models
  affects: [remind_me_mcp/tools.py, remind_me_mcp/models.py]
tech_stack:
  added: []
  patterns: [batch-classification, per-category-decay-rate]
key_files:
  created: []
  modified:
    - remind_me_mcp/models.py
    - remind_me_mcp/tools.py
    - tests/test_tools.py
key_decisions:
  - "Classification excludes 'unclassified' from valid types -- it is the default state, not a classification"
  - "DECAY_RATES lookup with fallback to 0.10 for unknown types"
  - "Batch tool returns content_snippet (first 500 chars) for Claude to classify"
patterns_established:
  - "Classification tools pattern: batch fetch unclassified -> Claude classifies -> call reclassify with results"
requirements_completed: [CLSF-01, CLSF-02, CLSF-03, CLSF-04]
metrics:
  duration: ~5min
  completed: 2026-03-05
  tasks: 1/1
  tests_added: 7
  lines_added: ~180
---

# Phase 11 Plan 02: Classification Tools Summary

**Two MCP tools for Claude-driven memory classification with Pydantic validation and per-category decay rates from DECAY_RATES table**

## Performance

- **Duration:** ~5 min
- **Started:** 2026-03-05T15:39:47Z
- **Completed:** 2026-03-05T15:45:00Z
- **Tasks:** 1
- **Files modified:** 3

## Accomplishments

- Added MemoryClassification model with field_validator rejecting invalid memory_type values
- Added remind_me_reclassify tool: batch classification with memory_type + decay_rate updates
- Added remind_me_reclassify_batch tool: fetches unclassified memories for Claude to process
- 7 new integration tests, 65 total tests pass with zero regressions

## Task Commits

Each task was committed atomically:

1. **Task 1: Pydantic models and reclassify tools** - `b89c1f2` (feat)

## Files Created/Modified

- `remind_me_mcp/models.py` - Added MemoryClassification, ReclassifyInput, ReclassifyBatchInput models with VALID_MEMORY_TYPES set
- `remind_me_mcp/tools.py` - Added remind_me_reclassify and remind_me_reclassify_batch tool handlers, imported DECAY_RATES
- `tests/test_tools.py` - Added 7 reclassify integration tests, fixed pytest import for ValidationError usage

## Decisions Made

- Classification excludes 'unclassified' from valid types -- it is the default state, not a user classification
- DECAY_RATES.get() with 0.10 fallback ensures graceful handling if DECAY_RATES changes
- Content snippet capped at 500 chars via SQL substr() for efficient batch review

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed missing pytest runtime import**
- **Found during:** Task 1 (GREEN phase)
- **Issue:** `pytest` was only imported under `TYPE_CHECKING`, causing NameError in `test_reclassify_rejects_invalid_memory_type`
- **Fix:** Added `import pytest` as a runtime import
- **Files modified:** tests/test_tools.py
- **Verification:** All 7 reclassify tests pass
- **Committed in:** b89c1f2 (part of task commit)

---

**Total deviations:** 1 auto-fixed (1 bug)
**Impact on plan:** Minor fix necessary for test execution. No scope creep.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- Classification tools are registered on the mcp instance and ready for use
- Plan 11-03 (vitality in search, dormant filtering, vitality report) can proceed
- Pre-added 11-03 tests already exist in test_tools.py

---
*Phase: 11-decay-vitality-classification*
*Completed: 2026-03-05*
