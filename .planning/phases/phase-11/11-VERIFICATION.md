---
phase: 11-decay-vitality-classification
verified: 2026-03-05T20:00:00Z
status: passed
score: 5/5 must-haves verified
---

# Phase 11: Decay, Vitality, and Classification Verification Report

**Phase Goal:** Every memory has a type, a vitality score that decays over time, and dormant memories fade out of default search
**Verified:** 2026-03-05
**Status:** PASSED
**Re-verification:** No -- initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | Schema migration adds vitality/decay columns and memory_type column; existing databases upgrade cleanly | VERIFIED | `_migrate_v4_to_v5` in db.py adds 7 columns (accessed_at, access_count, decay_rate, vitality, base_weight, status, memory_type) with idempotent `contextlib.suppress`. Backfills accessed_at from created_at. 3 new indexes. 5 migration tests in test_db.py (schema version, columns, defaults, backfill, indexes). |
| 2 | Accessing a memory recomputes its vitality using the ACT-R formula; frequently accessed memories stay vital | VERIFIED | `compute_vitality()` in vitality.py implements `base_weight * (access_count+1)^0.5 * exp(-decay_rate * days_since_last_access)`. `record_access()` increments count, applies bridge protection via `get_effective_decay_rate()`, recomputes vitality with days_since=0, updates status. Bridge protection halves decay when access_count >= 10. 16 tests in test_vitality.py cover formula, bridge protection, and DB integration. Fire-and-forget `asyncio.create_task` in tools.py calls `record_access` for all returned search results. |
| 3 | Memories below vitality 0.05 are flagged dormant and excluded from default search (but retrievable with include_dormant) | VERIFIED | `VITALITY_FLOOR = 0.05` constant. `is_dormant()` returns True when vitality < 0.05. `record_access()` sets status='dormant' when below floor. In tools.py `memory_search`, dormant exclusion applied BEFORE RRF: `[m for m in filtered_fts if m.get("status") != "dormant"]` when `include_dormant=False` (default). `MemorySearchInput` has `include_dormant: bool = False` and `min_vitality: float = 0.0` fields. Tests: `test_search_excludes_dormant_by_default`, `test_search_include_dormant_shows_all`, `test_search_min_vitality_filter`. |
| 4 | Claude can call remind_me_reclassify to classify memories in batches, and classification sets the appropriate decay rate | VERIFIED | `remind_me_reclassify` tool registered on mcp, accepts `ReclassifyInput` (list of `MemoryClassification` with field_validator for valid types). Updates `memory_type` and `decay_rate` via `DECAY_RATES.get(type, 0.10)`. `remind_me_reclassify_batch` fetches unclassified memories with `content_snippet` (first 500 chars). 7 classification tests cover type+decay updates, invalid type rejection, count reporting, correct per-type decay rate, batch fetching, batch_size, and empty-when-classified. |
| 5 | remind_me_vitality_report surfaces dormant count, vault health metrics, and decay distribution | VERIFIED | `remind_me_vitality_report` tool registered with `readOnlyHint=True`. Queries total/active/dormant counts, AVG(vitality), GROUP BY memory_type for decay_distribution, 5 vitality buckets (0.00-0.05 through 0.75-1.00), vault_health_score as percentage. Supports JSON and markdown formats. 5 tests cover basic counts, average vitality, decay distribution, vitality buckets, and vault health score. |

**Score:** 5/5 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `remind_me_mcp/db.py` | v4-to-v5 migration with 7 columns, 3 indexes, backfill, outbox triggers | VERIFIED | Lines 505-609: complete migration with idempotent ADD COLUMN, indexes, backfill, trigger recreation |
| `remind_me_mcp/vitality.py` | ACT-R formula, bridge protection, dormancy check, record_access | VERIFIED | 187 lines. Pure functions + DB integration. All constants exported. |
| `remind_me_mcp/models.py` | MemoryClassification, ReclassifyInput, ReclassifyBatchInput, VitalityReportInput, MemorySearchInput updates | VERIFIED | All 4 new models present. include_dormant and min_vitality fields on MemorySearchInput. VALID_MEMORY_TYPES set excludes 'unclassified'. |
| `remind_me_mcp/retrieval.py` | 4-signal RRF with vitality ranking | VERIFIED | rank_rrf sorts by vitality DESC (default 1.0), adds _vitality_rank, sums 4 reciprocal ranks |
| `remind_me_mcp/tools.py` | Dormant filtering, record_access, reclassify tools, vitality report | VERIFIED | All 3 new tools registered. Dormant/min_vitality filtering before RRF. Fire-and-forget access recording. |
| `tests/test_vitality.py` | Pure function + DB integration tests | VERIFIED | 16 tests covering formula, bridge protection, decay rates, dormancy, record_access |
| `tests/test_retrieval.py` | 4-signal RRF vitality tests | VERIFIED | 4 tests in TestRankRRFVitality class |
| `tests/test_tools.py` | Classification + dormant filtering + vitality report tests | VERIFIED | 12 tests covering reclassify (4), reclassify_batch (3), dormant filtering (3), vitality report (5) -- note: 3 dormant filtering tests counted, some share search tests |
| `tests/test_db.py` | Migration v4-to-v5 tests | VERIFIED | 5 tests covering schema version, columns, defaults, backfill, indexes |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| tools.py memory_search | vitality.py record_access | `from remind_me_mcp.vitality import DECAY_RATES, record_access` + `asyncio.create_task(_record_accesses(returned_ids))` | WIRED | Fire-and-forget pattern for non-blocking access recording |
| tools.py memory_search | models.py MemorySearchInput | `include_dormant` and `min_vitality` params used in filtering logic | WIRED | Lines 221-233: dormant exclusion and min_vitality filtering |
| tools.py remind_me_reclassify | vitality.py DECAY_RATES | `DECAY_RATES.get(classification.memory_type, 0.10)` | WIRED | Per-type decay rate applied on classification |
| tools.py remind_me_reclassify | models.py ReclassifyInput | Pydantic validation on input | WIRED | field_validator rejects invalid memory_type values |
| retrieval.py rank_rrf | vitality field | `m.get("vitality", 1.0)` for ranking | WIRED | 4th signal added to RRF score computation |
| db.py _migrate_v4_to_v5 | outbox triggers | json_object includes all 7 new fields | WIRED | Both INSERT and UPDATE triggers updated |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|----------|
| DECAY-01 | 11-01 | memories table has accessed_at, access_count, decay_rate, vitality, base_weight, status columns | SATISFIED | All 7 columns added in _migrate_v4_to_v5 with proper defaults |
| DECAY-02 | 11-01 | Vitality recomputed on access using ACT-R formula | SATISFIED | compute_vitality implements exact formula; record_access calls it |
| DECAY-03 | 11-03 | Memories below 0.05 flagged dormant, excluded from default search | SATISFIED | is_dormant + status='dormant' in record_access; dormant exclusion in memory_search |
| DECAY-04 | 11-03 | Search accepts include_dormant and min_vitality parameters | SATISFIED | Both fields on MemorySearchInput, both used in filtering |
| DECAY-05 | 11-03 | Vitality is 4th RRF signal | SATISFIED | rank_rrf sorts by vitality DESC, adds to RRF score |
| DECAY-06 | 11-01 | Bridge protection: high access_count halves decay_rate | SATISFIED | get_effective_decay_rate with BRIDGE_THRESHOLD=10, BRIDGE_MULTIPLIER=0.5 |
| CLSF-01 | 11-01 | memory_type column on memories table | SATISFIED | Column added in migration, 7 valid types + unclassified default |
| CLSF-02 | 11-02 | remind_me_reclassify tool accepts batch with classifications | SATISFIED | Tool registered, accepts ReclassifyInput with list of MemoryClassification |
| CLSF-03 | 11-02 | remind_me_reclassify returns unclassified memories in batches | SATISFIED | remind_me_reclassify_batch fetches WHERE memory_type='unclassified' with batch_size |
| CLSF-04 | 11-02 | Classification sets appropriate decay_rate per category | SATISFIED | DECAY_RATES lookup applied in reclassify tool |
| CLSF-05 | 11-03 | remind_me_vitality_report surfaces dormant count, health metrics, decay distribution | SATISFIED | Tool returns total, active, dormant, avg_vitality, vault_health_score, decay_distribution, vitality_buckets |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| None | - | - | - | No anti-patterns detected |

No TODO, FIXME, placeholder, or stub patterns found in any phase 11 files.

### Human Verification Required

### 1. Fire-and-forget access recording under load

**Test:** Run multiple concurrent searches and verify access_count increments correctly
**Expected:** No race conditions; all access_count values reflect actual access count
**Why human:** asyncio.create_task fire-and-forget pattern cannot be verified without running the event loop under concurrent load

### 2. Schema migration on real production database

**Test:** Run the server against an existing v4 database with real memories
**Expected:** All memories retain their data; accessed_at backfilled from created_at; new columns have correct defaults
**Why human:** Tests use fresh in-memory databases; real migration on existing data needs manual verification

### Gaps Summary

No gaps found. All 5 observable truths verified with supporting evidence across all three levels (exists, substantive, wired). All 11 requirements (DECAY-01 through DECAY-06, CLSF-01 through CLSF-05) are satisfied. The implementation is complete with 37+ new tests covering pure functions, database integration, tool handlers, retrieval pipeline integration, and edge cases. All commits (7553772, 7002f89, b89c1f2, b84a094, 5496e0d, fdd63e0) verified in git log.

---

_Verified: 2026-03-05_
_Verifier: Claude (gsd-verifier)_
