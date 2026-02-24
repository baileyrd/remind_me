---
phase: 03-quality-and-bug-fixes
plan: "01"
subsystem: database
tags: [sqlite, migration, user_version, pragma, junction-table, fts5, triggers]

# Dependency graph
requires:
  - phase: 02-test-infrastructure
    provides: "db_conn fixture, FakeEmbedder, memory_factory — test isolation layer for db.py"
provides:
  - "PRAGMA user_version schema migration system in db.py (_migrate_schema)"
  - "capture_id TEXT column + idx_memories_capture_id index on memories table"
  - "memory_tags junction table with indexes and three sync triggers (INSERT/UPDATE/DELETE)"
  - "9 migration/schema tests covering user_version, column existence, trigger behavior, backfill, idempotency"
affects: [03-02, 03-03, 03-04, capture_id-lookup, tag-filtering]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "PRAGMA user_version — integer schema version tracked inside SQLite file"
    - "Incremental migration with try/except ADD COLUMN for idempotency"
    - "json_valid() guard in triggers — prevents malformed JSON tags from breaking INSERT"
    - "junction table backfill from JSON column during migration"

key-files:
  created: []
  modified:
    - remind_me_mcp/db.py
    - tests/test_db.py

key-decisions:
  - "json_valid(NEW.tags) guard added to memories_tags_ai and memories_tags_au triggers — SQLite evaluates WHERE before json_each, so malformed tags strings pass silently (idempotent Rule 1 auto-fix)"
  - "ADD COLUMN wrapped in try/except OperationalError — SQLite raises if column already exists, try/except makes migration idempotent on re-run"
  - "memory_tags junction table is additive — JSON tags column kept for backward compatibility and _row_to_dict deserialization"
  - "executescript() used for DDL batches in _migrate_v1_to_v2 — implicit transaction for table/index/trigger creation"

patterns-established:
  - "Migration function pattern: _migrate_vX_to_vY(db) helper + version guard in _migrate_schema()"
  - "Schema evolution via incremental ALTER TABLE, never DROP TABLE — safe for existing databases"

requirements-completed: [DATA-01, DATA-02]

# Metrics
duration: 2min
completed: 2026-02-24
---

# Phase 03 Plan 01: Schema Migration System Summary

**PRAGMA user_version migration system in db.py — capture_id column, memory_tags junction table, three sync triggers, and 9 migration tests covering triggers, backfill, and idempotency**

## Performance

- **Duration:** 2 min
- **Started:** 2026-02-24T04:53:15Z
- **Completed:** 2026-02-24T04:55:30Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments

- Added `_migrate_schema()` with two migration steps: v0->v1 (capture_id column + index + backfill from metadata JSON) and v1->v2 (memory_tags junction table + indexes + three sync triggers + backfill from JSON tags)
- Added `json_valid()` guard to INSERT and UPDATE triggers to tolerate rows with malformed tags strings (auto-fix for pre-existing test compatibility)
- Added 9 comprehensive migration tests covering schema state, trigger behavior (insert/update/delete), backfill logic, and idempotent re-runs; full suite now 156 tests passing

## Task Commits

Each task was committed atomically:

1. **Task 1: Add schema migration system and new schema objects** - `98da536` (feat)
2. **Task 2: Add migration and schema tests** - `39dd2e3` (feat)

**Plan metadata:** (docs commit — see below)

## Files Created/Modified

- `/home/baileyrd/projects/remind_me/remind_me_mcp/db.py` — Added `_migrate_schema()`, `_migrate_v0_to_v1()`, `_migrate_v1_to_v2()`, `_SCHEMA_VERSION`, updated module docstring, added `_migrate_schema` to `__all__`
- `/home/baileyrd/projects/remind_me/tests/test_db.py` — Added 9 migration/schema tests, imported `_migrate_schema`

## Decisions Made

- `json_valid(NEW.tags)` added to sync triggers: SQLite evaluates WHERE conditions before iterating json_each results, so a `WHERE json_valid(NEW.tags)` guard prevents `malformed JSON` errors when tags contain non-JSON strings. Discovered during Task 1 verification when existing test `test_row_to_dict_handles_invalid_json` failed after trigger creation.
- `ADD COLUMN` wrapped in `try/except sqlite3.OperationalError`: SQLite raises if the column already exists; the except block silently continues, making v0->v1 safe to re-run on already-migrated databases.
- `executescript()` used for DDL batch in `_migrate_v1_to_v2`: SQLite requires DDL statements (CREATE TABLE, CREATE INDEX, CREATE TRIGGER) to run outside explicit Python-level transactions. `executescript()` commits any open transaction first and then runs all statements atomically.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Added json_valid() guard to sync triggers**
- **Found during:** Task 1 (schema verification — running existing test suite after implementation)
- **Issue:** `memories_tags_ai` and `memories_tags_au` triggers called `json_each(NEW.tags)` unconditionally. When `tags` is not valid JSON (e.g., the string `"not json"`), SQLite raises `OperationalError: malformed JSON` on any INSERT. The pre-existing test `test_row_to_dict_handles_invalid_json` inserts a row with `tags='not json'` and expects no error.
- **Fix:** Added `AND json_valid(NEW.tags)` to the WHERE clause of both triggers. SQLite evaluates WHERE predicates before iterating the json_each virtual table, so the guard prevents the error entirely.
- **Files modified:** `remind_me_mcp/db.py`
- **Verification:** All 15 existing db tests pass, including `test_row_to_dict_handles_invalid_json`
- **Committed in:** `98da536` (Task 1 commit)

---

**Total deviations:** 1 auto-fixed (Rule 1 — bug in trigger logic)
**Impact on plan:** Fix essential for correctness and backward compatibility. No scope creep.

## Issues Encountered

None beyond the auto-fixed trigger bug above.

## User Setup Required

None — no external service configuration required.

## Next Phase Readiness

- Schema migration system is in place; subsequent plans in Phase 3 can rely on `capture_id` and `memory_tags` being present
- BUGF-02 (capture_id indexed lookup) and DATA-02 (SQL-level tag filtering) are now unblocked by the presence of `capture_id` column/index and `memory_tags` junction table
- All 156 tests pass — safe baseline for Phase 3 bug-fix plans

## Self-Check: PASSED

- FOUND: remind_me_mcp/db.py
- FOUND: tests/test_db.py
- FOUND: .planning/phases/03-quality-and-bug-fixes/03-01-SUMMARY.md
- FOUND commit: 98da536 (Task 1 — schema migration system)
- FOUND commit: 39dd2e3 (Task 2 — migration tests)

---
*Phase: 03-quality-and-bug-fixes*
*Completed: 2026-02-24*
