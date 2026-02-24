---
phase: 03-quality-and-bug-fixes
plan: "02"
subsystem: database
tags: [sqlite, bugfix, importer, embedding, capture_id, memory_tags, tag-filtering, sql-join, pagination]

# Dependency graph
requires:
  - phase: 03-quality-and-bug-fixes/03-01
    provides: "capture_id column + index on memories table, memory_tags junction table and sync triggers"
provides:
  - "BUGF-01 fixed: import_chat_file collects (mem_id, chunk) embed_pairs during INSERT loop — no ID mismatch"
  - "BUGF-02 fixed: remind_me_get_capture uses WHERE capture_id = ? indexed column lookup — no LIKE scan"
  - "DATA-02 fixed: memory_list and api_list tag filtering via SQL EXISTS subquery on memory_tags — LIMIT applies after filter"
  - "remind_me_auto_capture populates capture_id column directly on INSERT for both dialog and summary rows"
  - "5 regression tests proving BUGF-01, BUGF-02, and DATA-02 are resolved"
affects: [03-03, 03-04, import-workflow, capture-lookup, tag-pagination]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "embed_pairs: list[tuple[str, str]] — collect (mem_id, chunk) during INSERT before embedding loop"
    - "SQL EXISTS subquery pattern: EXISTS (SELECT 1 FROM memory_tags mt0 WHERE mt0.memory_id = m.id AND mt0.tag = ?)"
    - "Table alias m.* pattern — required when joining memory_tags to avoid column ambiguity"
    - "capture_id column INSERT — set column directly alongside metadata JSON for reliable column-based lookup"

key-files:
  created: []
  modified:
    - remind_me_mcp/importer.py
    - remind_me_mcp/tools.py
    - remind_me_mcp/api.py
    - tests/test_tools.py
    - tests/test_importer.py
    - tests/test_api.py

key-decisions:
  - "embed_pairs collected during INSERT loop — avoids recomputing _make_id with a different timestamp, ensuring _embed_and_store uses the exact mem_id from the memories row"
  - "SQL EXISTS subquery for tag filtering in both tools.py memory_list and api.py api_list — ensures LIMIT is applied after tag filter, not before"
  - "Table alias m. prefix required in FROM memories m ... WHERE EXISTS — without it SQLite raises ambiguity error when joining memory_tags"
  - "api_search retains Python post-filter for tags — search operates on already-ranked/merged in-memory results; prefetch multiplier (limit * 3) deferred to future plan"

patterns-established:
  - "embed_pairs pattern: always collect (id, text) pairs during INSERT, never recompute IDs in a separate loop"
  - "SQL-first tag filtering: use memory_tags junction table with EXISTS subquery, never Python post-filter on paginated results"

requirements-completed: [BUGF-01, BUGF-02, DATA-02]

# Metrics
duration: 4min
completed: 2026-02-24
---

# Phase 03 Plan 02: Bug Fixes and Tag Filtering Summary

**BUGF-01 (embed ID mismatch), BUGF-02 (LIKE-based capture lookup), and DATA-02 (Python tag post-filter pagination) all fixed with 5 regression tests proving each fix holds**

## Performance

- **Duration:** 4 min
- **Started:** 2026-02-24T04:58:56Z
- **Completed:** 2026-02-24T05:02:30Z
- **Tasks:** 2
- **Files modified:** 6

## Accomplishments

- Fixed BUGF-01 in `importer.py`: collect `embed_pairs` list during INSERT loop so `_embed_and_store` receives the exact `mem_id` used for the database row — no ID mismatch, imported memories are now searchable
- Fixed BUGF-02 in `tools.py`: replaced two fragile `metadata LIKE` queries in `remind_me_get_capture` with a single `WHERE capture_id = ?` indexed column lookup
- Fixed DATA-02 in `tools.py` and `api.py`: replaced Python post-filter for tags with SQL `EXISTS` subquery on `memory_tags` junction table — `LIMIT` now applies after filtering, so `limit=5` returns exactly 5 matching results
- Fixed `remind_me_auto_capture` to set `capture_id` column directly on both `INSERT` statements (dialog + summary) for reliable column-based retrieval
- Added 5 regression tests (1 in test_importer.py, 3 in test_tools.py, 1 in test_api.py) — full suite goes from 156 to 161 tests, all passing

## Task Commits

Each task was committed atomically:

1. **Task 1: Fix BUGF-01, BUGF-02, and DATA-02 tag filtering** - `6748ee5` (fix)
2. **Task 2: Add regression tests for BUGF-01, BUGF-02, and tag-filtered pagination** - `d6bfd67` (test)

**Plan metadata:** (docs commit — see below)

## Files Created/Modified

- `/home/baileyrd/projects/remind_me/remind_me_mcp/importer.py` — Replaced separate embedding loop with `embed_pairs` list collected during INSERT; removed `hashlib.sha256` recomputation (hashlib import kept for `_file_hash`)
- `/home/baileyrd/projects/remind_me/remind_me_mcp/tools.py` — Fixed `remind_me_get_capture` to use `WHERE capture_id = ?`; fixed `memory_list` to use SQL `EXISTS` JOIN on `memory_tags`; fixed `remind_me_auto_capture` INSERT to set `capture_id` column
- `/home/baileyrd/projects/remind_me/remind_me_mcp/api.py` — Fixed `api_list` to use SQL `EXISTS` JOIN on `memory_tags`; removed Python post-filter; removed unused `import sqlite3 as _sqlite3` from `api_list`
- `/home/baileyrd/projects/remind_me/tests/test_importer.py` — Added `test_import_embed_id_matches_insert_id` with `_embed_and_store` spy
- `/home/baileyrd/projects/remind_me/tests/test_tools.py` — Added `test_import_then_search_embeds_correctly`, `test_get_capture_uses_column_lookup`, `test_list_tag_filter_pagination`
- `/home/baileyrd/projects/remind_me/tests/test_api.py` — Added `test_api_list_tag_filter_pagination`

## Decisions Made

- `embed_pairs` collected during INSERT loop: `_make_id(chunk)` uses `_now_iso()` internally, which returns a new timestamp on each call. The original bug recomputed the ID in a separate loop as `hashlib.sha256(f"{chunk}{now}").hexdigest()[:12]`, which produced a different hash. Fix: collect `(mem_id, chunk)` tuples during INSERT, iterate those same pairs for embedding.
- SQL `EXISTS` subquery for tag filtering: `EXISTS (SELECT 1 FROM memory_tags mt0 WHERE mt0.memory_id = m.id AND mt0.tag = ?)` — one EXISTS clause per tag enables AND-intersection semantics (a memory must have all specified tags). Uses table alias to avoid column ambiguity when joining.
- `api_search` retains Python post-filter for tags: search already operates on a merged/ranked in-memory result set; fixing pagination correctness for search is deferred (low-priority since search doesn't paginate the same way list does).

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None — no external service configuration required.

## Next Phase Readiness

- BUGF-01, BUGF-02, and DATA-02 are resolved with regression tests
- 161 tests passing — safe baseline for Phase 3 plans 03-03 and 03-04
- `capture_id` column is now populated by both import and auto-capture paths; column-based lookup is reliable

## Self-Check: PASSED

- FOUND: remind_me_mcp/importer.py
- FOUND: remind_me_mcp/tools.py
- FOUND: remind_me_mcp/api.py
- FOUND: tests/test_tools.py
- FOUND: tests/test_importer.py
- FOUND: tests/test_api.py
- FOUND: .planning/phases/03-quality-and-bug-fixes/03-02-SUMMARY.md
- FOUND commit: 6748ee5 (Task 1 — bug fixes)
- FOUND commit: d6bfd67 (Task 2 — regression tests)

---
*Phase: 03-quality-and-bug-fixes*
*Completed: 2026-02-24*
