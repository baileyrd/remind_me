---
phase: 13-structured-memory-and-transparency
plan: 02
subsystem: retrieval
tags: [debug-signals, transparency, rrf, tier-breakdown, verbose]

# Dependency graph
requires:
  - phase: 10-search-quality
    provides: RRF ranking with _keyword_rank, _semantic_rank, _recency_rank, _vitality_rank signals
provides:
  - build_debug_signals function for extracting ranking signals from RRF-ranked memories
  - compute_tier_breakdown function for counting search method distribution
  - verbose=True search mode with per-result debug signals
  - Envelope enrichment with tier_breakdown and dormant_excluded
affects: [13-structured-memory-and-transparency]

# Tech tracking
tech-stack:
  added: []
  patterns: [envelope-enrichment, verbose-debug-mode]

key-files:
  created: []
  modified:
    - remind_me_mcp/retrieval.py
    - remind_me_mcp/tools.py
    - remind_me_mcp/models.py
    - tests/test_retrieval.py
    - tests/test_tools.py

key-decisions:
  - "Debug signals use underscore-prefixed internal rank keys (_keyword_rank etc) from RRF output"
  - "days_old computed from created_at with graceful None fallback for missing/unparseable dates"
  - "Dormant exclusion count uses deduplicated IDs across FTS and semantic lists to avoid double-counting"
  - "Tier breakdown and dormant_excluded are always included in envelope (not gated by verbose)"

patterns-established:
  - "Verbose debug mode pattern: verbose=True attaches debug_signals dict per memory in results"
  - "Envelope enrichment pattern: tier_breakdown and dormant_excluded always in JSON response"

requirements-completed: [TRNS-01, TRNS-02]

# Metrics
duration: 5min
completed: 2026-03-05
---

# Phase 13 Plan 02: Debug Signals and Envelope Transparency Summary

**Debug ranking signals (verbose mode) and tier breakdown/dormant_excluded envelope enrichment for search transparency**

## Performance

- **Duration:** 5 min
- **Started:** 2026-03-05T19:16:43Z
- **Completed:** 2026-03-05T19:21:24Z
- **Tasks:** 2
- **Files modified:** 5

## Accomplishments
- build_debug_signals extracts semantic_rank, keyword_rank, recency_rank, vitality_rank, and days_old from RRF-ranked memories
- compute_tier_breakdown counts keyword/semantic/hybrid distribution across search results
- verbose=True search includes debug_signals block per memory in both JSON and Markdown
- JSON envelope always includes tier_breakdown and dormant_excluded
- Markdown response always includes tier summary line and per-result ranking info when verbose

## Task Commits

Each task was committed atomically:

1. **Task 1: Debug signal builder and tier breakdown in retrieval.py**
   - `3bf4be4` (test: failing tests for build_debug_signals and compute_tier_breakdown)
   - `bfcb333` (feat: implement build_debug_signals and compute_tier_breakdown)
2. **Task 2: Wire debug signals and envelope enrichment into memory_search**
   - `1476f60` (test: failing tests for verbose debug signals and envelope enrichment)
   - `cd21112` (feat: wire debug signals and envelope enrichment into memory_search)

_Note: TDD tasks have two commits each (RED: test, GREEN: implementation)_

## Files Created/Modified
- `remind_me_mcp/retrieval.py` - Added build_debug_signals and compute_tier_breakdown functions
- `remind_me_mcp/tools.py` - Wired debug signals, tier_breakdown, dormant_excluded into memory_search
- `remind_me_mcp/models.py` - Added verbose field to MemorySearchInput
- `tests/test_retrieval.py` - 8 tests for debug signal building and tier breakdown
- `tests/test_tools.py` - 7 integration tests for verbose search, tier breakdown, dormant_excluded

## Decisions Made
- Debug signals use underscore-prefixed internal rank keys from RRF output for extraction
- days_old computed from created_at with graceful None fallback for missing/unparseable dates
- Dormant exclusion count uses deduplicated IDs across FTS and semantic lists to avoid double-counting
- Tier breakdown and dormant_excluded always included in envelope (not gated by verbose)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Added verbose field to MemorySearchInput**
- **Found during:** Task 2 (Wire debug signals)
- **Issue:** Plan 01 Task 2 was supposed to add verbose field but it was not present
- **Fix:** Added `verbose: bool = Field(default=False, ...)` to MemorySearchInput
- **Files modified:** remind_me_mcp/models.py
- **Verification:** Tests pass with verbose=True/False
- **Committed in:** 1476f60 (Task 2 RED commit)

---

**Total deviations:** 1 auto-fixed (1 blocking)
**Impact on plan:** Necessary for verbose mode to function. No scope creep.

## Issues Encountered
- Pre-existing test failure: test_schema_version_is_6 expects version 6 but schema is at 7 (from Plan 01 structured memory work). Out of scope.
- Pre-existing test failures: structured_search tests from Plan 01 not yet passing (structured query lookup not fully implemented). Out of scope.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- Search transparency complete: verbose mode provides full ranking explainability
- Envelope enrichment provides tier distribution and dormant exclusion counts
- Ready for remaining Phase 13 plans

## Self-Check: PASSED

All files verified present. All 4 commit hashes confirmed in git log.

---
*Phase: 13-structured-memory-and-transparency*
*Completed: 2026-03-05*
