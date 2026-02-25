---
phase: 09-gap-closure-async-fix-and-coverage
plan: 02
subsystem: testing
tags: [pytest, coverage, pytest-cov, api, importer, ci-cd]

# Dependency graph
requires:
  - phase: 09-gap-closure-async-fix-and-coverage
    provides: Plan 01 — await fix + directory import integration test (216 tests passing at ~77.8%)
  - phase: 05-ci-cd-pipeline
    provides: CI pipeline with --cov-fail-under=74 gate
provides:
  - Line coverage >= 80% (80.19%) measured by pytest-cov (CICD-02 requirement satisfied)
  - 18 new branch-coverage tests for api.py and importer.py highest-yield uncovered lines
  - CI coverage gate raised from 74% to 80% in .github/workflows/ci.yml
affects: [future testing phases, ci-pipeline]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Targeted branch-coverage tests: write tests that specifically exercise except-handler branches,
      walrus-operator branches, and edge-case returns — not just happy paths"
    - "When plan specifies line numbers for coverage targets, always cross-check actual line numbers
      in the source file — they can shift between phases"

key-files:
  created: []
  modified:
    - tests/test_api.py
    - tests/test_importer.py
    - .github/workflows/ci.yml

key-decisions:
  - "Plan-specified line numbers (e.g., 219-220 for category filter) were stale — actual api.py lines
    differ after earlier phases modified the file. All tests were written by reading actual source
    lines, not trusting the plan's line numbers."
  - "api.py except-handler branches (invalid JSON bodies, FTS5 OperationalError, OSError in import)
    covered via targeted tests sending malformed input or monkeypatching to raise exceptions"
  - "importer.py line 287-289 (malformed JSONL handler) required a test with both a valid and an
    invalid JSONL line in the same file — the valid line exercises the success path, the invalid
    triggers the except branch"
  - "Coverage of 79.53% rounded to 80% for display but --cov-fail-under=80 uses raw float —
    required 4 additional importer tests to reach 80.19% actual coverage"

patterns-established:
  - "Monkeypatch import_chat_file to raise OSError for testing the api_import exception handler
    while keeping the path-guard and existence check as real (not mocked)"
  - "For FTS5 OperationalError coverage: use an unmatched-quote query string to trigger sqlite3 parse error"

requirements-completed: [CICD-02]

# Metrics
duration: 15min
completed: 2026-02-24
---

# Phase 09 Plan 02: Coverage Gate Raised to 80% — CICD-02 Satisfied

**18 targeted branch-coverage tests push api.py to 100% and total coverage to 80.19%; CI gate raised from 74% to 80% in ci.yml.**

## Performance

- **Duration:** ~15 min
- **Started:** 2026-02-24T00:00:00Z
- **Completed:** 2026-02-24T00:15:00Z
- **Tasks:** 2 (plus 2 supplementary test batches within Task 1)
- **Files modified:** 3

## Accomplishments

- Added 18 new tests across test_api.py and test_importer.py targeting specific uncovered branches
- api.py reaches 100% line coverage (was 90%)
- Total project coverage reaches 80.19% (was 77.8% after Plan 01)
- CI `--cov-fail-under` raised from 74 to 80 — CICD-02 requirement fully satisfied
- All 234 tests pass (was 216 before Phase 09)

## Task Commits

Each task was committed atomically:

1. **Task 1a: Initial branch-coverage tests** - `4080a6b` (feat)
2. **Task 1b: Supplementary importer tests to cross 80%** - `232b860` (feat)
3. **Task 2: Raise CI coverage gate to 80%** - `f281815` (feat)

**Plan metadata:** (docs commit follows)

## Files Created/Modified

- `/home/baileyrd/projects/remind_me/tests/test_api.py` - Added 12 new branch-coverage tests for api.py routes (stats malformed tags, source filter, search category/tag/fts-error filters, update invalid-JSON/metadata-only, import invalid-JSON/OSError)
- `/home/baileyrd/projects/remind_me/tests/test_importer.py` - Added 9 new tests for importer.py branches (JSONL format, JSONL with malformed line, multi-conversation JSON, unsupported format, string content blocks, non-string content, list content in role list, conversations empty list, unrecognized data fallthrough)
- `/home/baileyrd/projects/remind_me/.github/workflows/ci.yml` - Updated comment from 74%/headroom to 80%/CICD-02; changed --cov-fail-under from 74 to 80

## Decisions Made

- Plan's stated line numbers were stale (api.py had been modified by Phases 05-08); wrote tests by reading actual source lines rather than trusting plan line references.
- The `--cov-fail-under=80` gate uses raw float comparison — 79.53% reported as 79% display but fails the gate. Required additional tests to reach 80.19%.
- Used `monkeypatch.setattr(_api_mod, "import_chat_file", fake_import)` to trigger the api_import OSError exception handler without needing a real broken file.
- Used an unmatched-quote FTS5 query `"unclosed+quote` to trigger `sqlite3.OperationalError` in the search handler.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Corrected stale line number references in plan's test descriptions**
- **Found during:** Task 1 (writing tests for api.py lines 219-220, 275-276, 324-325)
- **Issue:** Plan described lines 219-220 as "category filter branch" but actual api.py shows lines 219-220 are the `except _sqlite3.OperationalError` handler in api_search. Category filter is at lines 224-225. Similarly, 275-276 are the invalid-JSON except in api_update (not the tags branch at 289-291), and 324-325 are the invalid-JSON except in api_import.
- **Fix:** Wrote tests targeting the actual uncovered lines by reading the source file directly. Added tests for the correct branches (FTS error, invalid JSON PUT body, invalid JSON POST /api/import, OSError in import).
- **Files modified:** tests/test_api.py
- **Verification:** Coverage report confirmed those specific lines reached after test execution.
- **Committed in:** 4080a6b (Task 1a commit)

**2. [Rule 1 - Bug] Added supplementary tests after initial 79.53% fell short of 80% gate**
- **Found during:** Task 1 verification (`--cov-fail-under=80` failed at 79.53%)
- **Issue:** First batch of 12 api.py tests pushed total to 79.53% — insufficient for exact 80% gate. Plan estimated 40+ lines covered but actual implementation had fewer uncovered lines remaining than estimated.
- **Fix:** Added 6 more importer.py tests targeting lines 121-122 (string content blocks), 127 (non-string content fallthrough), 143-146 (list content in role list), 155 (unrecognized data return), 180 (empty conversations return), 287-289 (malformed JSONL except). Reached 80.19%.
- **Files modified:** tests/test_importer.py
- **Verification:** `uv run pytest --cov=remind_me_mcp --cov-fail-under=80 -q` exits 0 with "80.19%"
- **Committed in:** 232b860 (Task 1b commit)

---

**Total deviations:** 2 auto-fixed (Rule 1 — stale line numbers in plan; insufficient initial coverage)
**Impact on plan:** Both fixes were necessary to satisfy the plan's success criteria. No scope creep.

## Issues Encountered

- Plan-specified target line numbers in api.py were stale due to file modifications in earlier phases. This required reading the actual source file to determine which lines were truly uncovered.
- `--cov-fail-under=80` uses strict float comparison — 79.53% is not sufficient even though it displays as "79%". The gate requires >= 80.00% raw coverage.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- CICD-02 fully satisfied: CI gate now enforces 80% coverage on every push/PR
- api.py at 100% coverage; importer.py at 97%; total project at 80.19%
- 234 tests passing — project is in strong test health for any future development
- Phase 09 complete — all gap-closure work done (PERF-02 + CICD-02)

---
*Phase: 09-gap-closure-async-fix-and-coverage*
*Completed: 2026-02-24*

## Self-Check: PASSED

- FOUND: tests/test_api.py
- FOUND: tests/test_importer.py
- FOUND: .github/workflows/ci.yml
- FOUND: .planning/phases/09-gap-closure-async-fix-and-coverage/09-02-SUMMARY.md
- FOUND commit: 4080a6b (Task 1a — initial branch-coverage tests)
- FOUND commit: 232b860 (Task 1b — supplementary importer tests)
- FOUND commit: f281815 (Task 2 — CI gate raised to 80%)
- VERIFIED: .github/workflows/ci.yml contains `--cov-fail-under=80`
- VERIFIED: .github/workflows/ci.yml contains `Coverage gate: 80%` comment
- VERIFIED: 234 tests pass, total coverage 80.19%
