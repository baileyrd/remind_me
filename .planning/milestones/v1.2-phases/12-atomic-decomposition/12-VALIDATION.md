---
phase: 12
slug: atomic-decomposition
status: complete
nyquist_compliant: true
wave_0_complete: true
created: 2026-03-05
validated: 2026-03-05
---

# Phase 12 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 7.x |
| **Config file** | pyproject.toml |
| **Quick run command** | `.venv/bin/python -m pytest tests/test_tools.py tests/test_db.py -x --ignore=tests/test_api.py` |
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
| 12-01-01 | 01 | 1 | ATOM-02 | migration | `.venv/bin/python -m pytest tests/test_db.py::test_capture_id_column_exists -x` | Yes | green |
| 12-01-02 | 01 | 1 | ATOM-01, ATOM-05 | integration | `.venv/bin/python -m pytest tests/test_tools.py -k decompose -x` | Yes | green |
| 12-01-03 | 01 | 1 | ATOM-03 | integration | `.venv/bin/python -m pytest tests/test_tools.py -k decompose_batch -x` | Yes | green |
| 12-02-01 | 02 | 1 | ATOM-04 | integration | `.venv/bin/python -m pytest tests/test_tools.py::test_auto_capture_decomposition_pending -x` | Yes | green |

*Status: pending / green / red / flaky*

---

## Requirement-to-Test Coverage

| Requirement | Description | Test Files | Key Tests | Status |
|-------------|-------------|------------|-----------|--------|
| ATOM-01 | remind_me_decompose stores facts linked to parent | test_tools.py | 12 decompose tests (creation, linking, storage) | COVERED |
| ATOM-02 | source_capture_id links facts to parent | test_db.py, test_tools.py | test_capture_id_column_exists, decompose tests verify linkage | COVERED |
| ATOM-03 | remind_me_decompose_batch returns undecomposed | test_tools.py | decompose_batch tests (batch retrieval, filtering) | COVERED |
| ATOM-04 | auto_capture includes decomposition_pending hint | test_tools.py | test_auto_capture_decomposition_pending | COVERED |
| ATOM-05 | Facts inherit parent tags + type-specific tags | test_tools.py | test_decompose_inherits_parent_tags, test_decompose_merges_extra_tags | COVERED |

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

All 5 requirements have automated test coverage (12 decompose + 1 auto_capture + migration tests).
