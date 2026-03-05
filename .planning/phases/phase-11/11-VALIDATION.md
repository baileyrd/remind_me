---
phase: 11
slug: decay-vitality-classification
status: complete
nyquist_compliant: true
wave_0_complete: true
created: 2026-03-05
validated: 2026-03-05
---

# Phase 11 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 7.x |
| **Config file** | pyproject.toml |
| **Quick run command** | `.venv/bin/python -m pytest tests/test_vitality.py tests/test_tools.py tests/test_db.py tests/test_retrieval.py -x --ignore=tests/test_api.py` |
| **Full suite command** | `.venv/bin/python -m pytest tests/ -v --ignore=tests/test_api.py` |
| **Estimated runtime** | ~1 second |

---

## Sampling Rate

- **After every task commit:** Run `.venv/bin/python -m pytest tests/ -x --ignore=tests/test_api.py`
- **After every plan wave:** Run `.venv/bin/python -m pytest tests/ -v --ignore=tests/test_api.py`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 2 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 11-01-01 | 01 | 1 | DECAY-01 | unit+migration | `.venv/bin/python -m pytest tests/test_db.py::test_v4_to_v5_schema_version_is_5 tests/test_db.py::test_v4_to_v5_new_columns_exist tests/test_db.py::test_v4_to_v5_defaults -x` | Yes | green |
| 11-01-02 | 01 | 1 | DECAY-02, DECAY-06 | unit | `.venv/bin/python -m pytest tests/test_vitality.py -x` | Yes | green |
| 11-01-03 | 01 | 1 | CLSF-01 | migration | `.venv/bin/python -m pytest tests/test_db.py::test_v4_to_v5_new_columns_exist -x` | Yes | green |
| 11-02-01 | 02 | 1 | CLSF-02, CLSF-03, CLSF-04 | integration | `.venv/bin/python -m pytest tests/test_tools.py::test_reclassify_updates_memory_type_and_decay tests/test_tools.py::test_reclassify_batch_returns_unclassified tests/test_tools.py::test_reclassify_sets_correct_decay_rate -x` | Yes | green |
| 11-03-01 | 03 | 1 | DECAY-03, DECAY-04, DECAY-05 | integration | `.venv/bin/python -m pytest tests/test_tools.py::test_search_excludes_dormant_by_default tests/test_tools.py::test_search_include_dormant_shows_all tests/test_tools.py::test_search_min_vitality_filter -x` | Yes | green |
| 11-03-02 | 03 | 1 | CLSF-05 | integration | `.venv/bin/python -m pytest tests/test_tools.py::test_vitality_report_basic_counts tests/test_tools.py::test_vitality_report_decay_distribution -x` | Yes | green |

*Status: pending / green / red / flaky*

---

## Requirement-to-Test Coverage

| Requirement | Description | Test Files | Key Tests | Status |
|-------------|-------------|------------|-----------|--------|
| DECAY-01 | Schema columns (accessed_at, access_count, etc.) | test_db.py | test_v4_to_v5_new_columns_exist, test_v4_to_v5_defaults, test_v4_to_v5_accessed_at_backfill | COVERED |
| DECAY-02 | ACT-R vitality formula on access | test_vitality.py | test_compute_vitality_formula_exact, test_record_access_updates_db | COVERED |
| DECAY-03 | Dormant below 0.05, excluded from default search | test_vitality.py, test_tools.py | test_is_dormant_below_floor, test_search_excludes_dormant_by_default | COVERED |
| DECAY-04 | include_dormant and min_vitality params | test_tools.py | test_search_include_dormant_shows_all, test_search_min_vitality_filter | COVERED |
| DECAY-05 | Vitality as 4th RRF signal | test_retrieval.py | TestRankRRFVitality (4 tests) | COVERED |
| DECAY-06 | Bridge protection halves decay_rate | test_vitality.py | test_bridge_protection_halves_decay_above_threshold, test_record_access_bridge_protection | COVERED |
| CLSF-01 | memory_type column with 7 types | test_db.py | test_v4_to_v5_new_columns_exist | COVERED |
| CLSF-02 | remind_me_reclassify batch tool | test_tools.py | test_reclassify_updates_memory_type_and_decay, test_reclassify_rejects_invalid_memory_type | COVERED |
| CLSF-03 | Batch retrieval of unclassified memories | test_tools.py | test_reclassify_batch_returns_unclassified, test_reclassify_batch_respects_batch_size | COVERED |
| CLSF-04 | Per-category decay rates | test_tools.py, test_vitality.py | test_reclassify_sets_correct_decay_rate, test_decay_rates_maps_memory_types | COVERED |
| CLSF-05 | Vitality report tool | test_tools.py | test_vitality_report_basic_counts, test_vitality_report_average_vitality, test_vitality_report_decay_distribution, test_vitality_report_vitality_buckets | COVERED |

---

## Wave 0 Requirements

*Existing infrastructure covers all phase requirements.*

---

## Manual-Only Verifications

*All phase behaviors have automated verification.*

---

## Validation Sign-Off

- [x] All tasks have automated verify commands
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 2s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-03-05

## Validation Audit 2026-03-05

| Metric | Count |
|--------|-------|
| Gaps found | 0 |
| Resolved | 0 |
| Escalated | 0 |

All 11 requirements have automated test coverage (16 vitality + 5 migration + 4 RRF-vitality + 12 tool tests).
