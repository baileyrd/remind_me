---
phase: 13
slug: structured-memory-and-transparency
status: complete
nyquist_compliant: true
wave_0_complete: true
created: 2026-03-05
validated: 2026-03-05
---

# Phase 13 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 7.x |
| **Config file** | pyproject.toml |
| **Quick run command** | `.venv/bin/python -m pytest tests/test_tools.py tests/test_db.py tests/test_retrieval.py -x --ignore=tests/test_api.py` |
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
| 13-01-01 | 01 | 1 | STRC-01, STRC-02 | migration | `.venv/bin/python -m pytest tests/test_db.py -k v6_to_v7 -x` | Yes | green |
| 13-01-02 | 01 | 1 | STRC-03 | integration | `.venv/bin/python -m pytest tests/test_tools.py -k structured_search -x` | Yes | green |
| 13-01-03 | 01 | 1 | STRC-04 | integration | `.venv/bin/python -m pytest tests/test_tools.py::test_structured_search_excludes_superseded tests/test_db.py::test_v6_to_v7_update_superseded_by -x` | Yes | green |
| 13-02-01 | 02 | 1 | TRNS-01 | unit+integration | `.venv/bin/python -m pytest tests/test_retrieval.py::TestBuildDebugSignals tests/test_tools.py::test_search_verbose_json_includes_debug_signals -x` | Yes | green |
| 13-02-02 | 02 | 1 | TRNS-02 | unit+integration | `.venv/bin/python -m pytest tests/test_retrieval.py::TestComputeTierBreakdown tests/test_tools.py::test_search_json_always_includes_tier_breakdown tests/test_tools.py::test_search_json_always_includes_dormant_excluded -x` | Yes | green |

*Status: pending / green / red / flaky*

---

## Requirement-to-Test Coverage

| Requirement | Description | Test Files | Key Tests | Status |
|-------------|-------------|------------|-----------|--------|
| STRC-01 | subject, predicate, object columns (nullable) | test_db.py | test_v6_to_v7_new_columns_exist, test_v6_to_v7_columns_default_null, test_v6_to_v7_insert_with_subject_predicate_object | COVERED |
| STRC-02 | Indexes on subject, memory_type | test_db.py | test_v6_to_v7_subject_index_exists, test_v6_to_v7_memory_type_index_still_present | COVERED |
| STRC-03 | Structured query routing to indexed lookup | test_tools.py | test_structured_search_by_subject (+ 5 more structured search tests) | COVERED |
| STRC-04 | superseded_by tracks fact replacement | test_db.py, test_tools.py | test_v6_to_v7_update_superseded_by, test_structured_search_excludes_superseded | COVERED |
| TRNS-01 | debug_signals when verbose=True | test_retrieval.py, test_tools.py | TestBuildDebugSignals (5 tests), test_search_verbose_json_includes_debug_signals, test_search_verbose_false_no_debug_signals | COVERED |
| TRNS-02 | tier_breakdown and dormant_excluded in envelope | test_retrieval.py, test_tools.py | TestComputeTierBreakdown (3 tests), test_search_json_always_includes_tier_breakdown, test_search_json_always_includes_dormant_excluded, test_search_dormant_excluded_count_accurate | COVERED |

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

All 6 requirements have automated test coverage (8 migration + 9 structured + 5 debug signals + 3 tier breakdown + 7 integration tests).
