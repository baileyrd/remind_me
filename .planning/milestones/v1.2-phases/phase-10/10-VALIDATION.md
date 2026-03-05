---
phase: 10
slug: retrieval-pipeline
status: complete
nyquist_compliant: true
wave_0_complete: true
created: 2026-03-05
validated: 2026-03-05
---

# Phase 10 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 7.x |
| **Config file** | pyproject.toml |
| **Quick run command** | `.venv/bin/python -m pytest tests/test_retrieval.py tests/test_tools.py -x --ignore=tests/test_api.py` |
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
| 10-01-01 | 01 | 1 | RETR-02, RETR-03 | unit | `.venv/bin/python -m pytest tests/test_retrieval.py::TestRankRRF -x` | Yes | green |
| 10-01-02 | 01 | 1 | RETR-01 | unit | `.venv/bin/python -m pytest tests/test_retrieval.py::TestApplyTokenBudget -x` | Yes | green |
| 10-01-03 | 01 | 1 | RETR-04 | unit | `.venv/bin/python -m pytest tests/test_retrieval.py::TestSearchEnvelope -x` | Yes | green |
| 10-02-01 | 02 | 1 | RETR-01, RETR-02, RETR-04 | integration | `.venv/bin/python -m pytest tests/test_tools.py::test_search_returns_envelope_json tests/test_tools.py::test_search_token_budget_trims tests/test_tools.py::test_search_rrf_ranking_smoke -x` | Yes | green |
| 10-02-02 | 02 | 1 | RETR-04 | integration | `.venv/bin/python -m pytest tests/test_tools.py::test_search_envelope_markdown -x` | Yes | green |

*Status: pending / green / red / flaky*

---

## Requirement-to-Test Coverage

| Requirement | Description | Test Files | Key Tests | Status |
|-------------|-------------|------------|-----------|--------|
| RETR-01 | Token budget trims excess results | test_retrieval.py, test_tools.py | TestApplyTokenBudget (5 tests), test_search_token_budget_trims, test_search_token_budget_zero_unlimited | COVERED |
| RETR-02 | RRF (k=60) replaces linear blending | test_retrieval.py, test_tools.py | TestRankRRF (9 tests), TestRRFKConfig (2 tests), test_search_rrf_ranking_smoke | COVERED |
| RETR-03 | Recency as 3rd retrieval signal | test_retrieval.py | test_rrf_recency_tiebreak | COVERED |
| RETR-04 | Response envelope with metadata | test_retrieval.py, test_tools.py | TestSearchEnvelope (2 tests), test_search_returns_envelope_json, test_search_envelope_markdown | COVERED |

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

All 4 requirements have automated test coverage (19 unit + 5 integration tests).
