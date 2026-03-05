---
phase: 11-decay-vitality-classification
plan: 01
subsystem: vitality
tags: [schema-migration, act-r, vitality, decay, classification]
dependency_graph:
  requires: []
  provides: [schema-v5, vitality-module, decay-rates, bridge-protection]
  affects: [remind_me_mcp/db.py, remind_me_mcp/vitality.py]
tech_stack:
  added: []
  patterns: [act-r-vitality, exponential-decay, bridge-protection]
key_files:
  created:
    - remind_me_mcp/vitality.py
    - tests/test_vitality.py
  modified:
    - remind_me_mcp/db.py
    - tests/test_db.py
    - tests/conftest.py
decisions:
  - "ACT-R formula uses (access_count+1)^0.5 for diminishing returns on repeated access"
  - "Bridge protection at 10 accesses halves decay rate via BRIDGE_MULTIPLIER=0.5"
  - "8 memory types with decay rates from 0.02 (decision) to 0.20 (action_item)"
  - "record_access sets days_since=0 on access, recomputing vitality immediately"
metrics:
  duration: ~24min
  completed: 2026-03-05
  tasks: 2/2
  tests_added: 21
  lines_added: ~600
---

# Phase 11 Plan 01: Schema Migration v4->v5 and ACT-R Vitality Module Summary

Schema migration adds 7 decay/vitality/classification columns with ACT-R vitality computation using bridge protection for frequently accessed memories.

## What Was Done

### Task 1: Schema Migration v4 to v5

Added `_migrate_v4_to_v5` to `remind_me_mcp/db.py`:
- 7 new columns: `accessed_at`, `access_count`, `decay_rate`, `vitality`, `base_weight`, `status`, `memory_type`
- 3 new indexes: `idx_memories_status`, `idx_memories_memory_type`, `idx_memories_vitality`
- Backfills `accessed_at` from `created_at` for existing records
- Updated outbox triggers to include all new fields in JSON payload
- `_SCHEMA_VERSION` incremented from 4 to 5

5 new tests in `tests/test_db.py` covering version check, column existence, defaults, backfill, and indexes.

### Task 2: Vitality Module

Created `remind_me_mcp/vitality.py` (187 lines) with:
- `compute_vitality()`: Pure ACT-R formula -- `base_weight * (access_count + 1)^0.5 * exp(-decay_rate * days_since_last_access)`
- `get_effective_decay_rate()`: Bridge protection -- halves decay rate when `access_count >= 10`
- `is_dormant()`: Returns True when vitality < 0.05 (VITALITY_FLOOR)
- `record_access()`: Database integration -- increments count, recomputes vitality, updates status
- `DECAY_RATES`: 8 memory types mapped to decay rates (decision=0.02 to action_item=0.20)
- Constants: `VITALITY_FLOOR=0.05`, `BRIDGE_THRESHOLD=10`, `BRIDGE_MULTIPLIER=0.5`

16 new tests in `tests/test_vitality.py` covering pure functions and database integration.

Updated `tests/conftest.py` to patch `_get_db` in `vitality.py` for test isolation.

## Commits

| Task | Commit | Description |
|------|--------|-------------|
| 1 | 7553772 | feat(11-01): schema migration v4->v5 with decay, vitality, and classification columns |
| 2 | 7002f89 | feat(11-01): add vitality module with ACT-R formula and access recording |

## Deviations from Plan

None -- plan executed exactly as written.

## Verification

- All 45 tests pass (`tests/test_db.py`: 29, `tests/test_vitality.py`: 16)
- `ruff check remind_me_mcp/vitality.py` -- clean
- Imports verified: `from remind_me_mcp.vitality import compute_vitality, record_access, DECAY_RATES, VITALITY_FLOOR`
- Schema version confirmed: `_SCHEMA_VERSION = 5`

## Self-Check: PASSED

All 6 key files found on disk. Both task commits (7553772, 7002f89) verified in git log.
