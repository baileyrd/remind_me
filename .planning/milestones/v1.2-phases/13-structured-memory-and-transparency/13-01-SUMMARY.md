---
phase: 13-structured-memory-and-transparency
plan: 01
subsystem: database, search
tags: [sqlite, schema-migration, structured-memory, triples, indexed-lookup]

# Dependency graph
requires:
  - phase: 12-atomic-decomposition
    provides: source_capture_id column and decomposition workflow
provides:
  - Schema v7 with subject/predicate/object/superseded_by columns
  - Indexed structured query routing in memory_search
  - _detect_structured_query() and _structured_lookup() functions
affects: [13-02-transparency, future decomposition enhancements]

# Tech tracking
tech-stack:
  added: []
  patterns: [structured-query-routing, subject-predicate-object-triples, supersession-tracking]

key-files:
  created: []
  modified:
    - remind_me_mcp/db.py
    - remind_me_mcp/tools.py
    - tests/test_db.py
    - tests/test_tools.py
    - tests/conftest.py

key-decisions:
  - "Structured query uses regex parsing for subject:/predicate: prefixes with quoted and unquoted values"
  - "Superseded memories excluded via superseded_by IS NULL in SQL WHERE clause"
  - "Structured results bypass RRF pipeline entirely when found; fall back to FTS/semantic with stripped query when not"

patterns-established:
  - "Structured query routing: detect pattern -> indexed lookup -> apply filters -> token budget -> return"
  - "Supersession tracking: superseded_by column links old facts to newer replacements"

requirements-completed: [STRC-01, STRC-02, STRC-03, STRC-04]

# Metrics
duration: 6min
completed: 2026-03-05
---

# Phase 13 Plan 01: Structured Memory Summary

**Schema v7 with subject/predicate/object triples and indexed structured query routing that bypasses semantic search for fast fact lookups**

## Performance

- **Duration:** 6 min
- **Started:** 2026-03-05T19:16:53Z
- **Completed:** 2026-03-05T19:23:03Z
- **Tasks:** 2
- **Files modified:** 5

## Accomplishments
- Schema migration v6->v7 adding subject, predicate, object, superseded_by columns with index on subject
- Structured query detection parsing subject:VALUE and predicate:VALUE patterns from search queries
- Indexed SQL lookup that routes structured queries before FTS/semantic search, with superseded memory exclusion
- 17 new tests (8 migration + 9 structured search) all passing with zero regression

## Task Commits

Each task was committed atomically (TDD: test -> feat):

1. **Task 1: Schema migration v6->v7** - `5530981` (test) -> `134e4ac` (feat)
2. **Task 2: Structured query detection and lookup** - `92c5e5e` (test) -> `acf1ffe` (feat)

_Note: TDD tasks have separate test and implementation commits_

## Files Created/Modified
- `remind_me_mcp/db.py` - Added _migrate_v6_to_v7 with 4 new columns, subject index, updated outbox triggers
- `remind_me_mcp/tools.py` - Added _detect_structured_query, _structured_lookup, _strip_structured_prefixes; modified memory_search routing
- `tests/test_db.py` - 8 new migration tests for v7 schema
- `tests/test_tools.py` - 9 new structured search tests
- `tests/conftest.py` - Updated memory_factory to support v7 columns

## Decisions Made
- Structured query uses regex parsing for subject:/predicate: prefixes supporting both quoted ("multi word") and unquoted single-word values
- Superseded memories excluded via SQL WHERE clause (superseded_by IS NULL) rather than post-filter
- When structured query is detected but yields no results, the structured prefixes are stripped and the remaining query falls through to normal FTS/semantic pipeline
- Structured results bypass RRF ranking entirely (they are direct indexed lookups, not relevance-ranked)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed hardcoded schema version assertions in existing tests**
- **Found during:** Task 1 and Task 2
- **Issue:** test_v4_to_v5_schema_version_is_5 asserted version==5 and test_schema_version_is_6 asserted version==6, both fail after v7 bump
- **Fix:** Changed assertions to use >= instead of == for forward compatibility
- **Files modified:** tests/test_db.py, tests/test_tools.py
- **Verification:** All 135 tests pass
- **Committed in:** 134e4ac (Task 1), acf1ffe (Task 2)

---

**Total deviations:** 1 auto-fixed (1 bug fix across 2 test files)
**Impact on plan:** Essential fix for test compatibility with schema version bump. No scope creep.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Structured columns ready for decomposition pipeline to populate subject/predicate/object triples
- superseded_by tracking ready for fact replacement workflows
- Plan 02 (transparency and debug signals) can proceed independently

---
*Phase: 13-structured-memory-and-transparency*
*Completed: 2026-03-05*
