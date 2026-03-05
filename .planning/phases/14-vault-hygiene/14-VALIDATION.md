---
phase: 14
slug: vault-hygiene
status: complete
nyquist_compliant: true
wave_0_complete: true
created: 2026-03-05
validated: 2026-03-05
---

# Phase 14 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 7.x |
| **Config file** | pyproject.toml |
| **Quick run command** | `.venv/bin/python -m pytest tests/test_consolidation.py -x --ignore=tests/test_api.py` |
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
| 14-01-01 | 01 | 1 | HYGN-01 | unit | `.venv/bin/python -m pytest tests/test_consolidation.py::TestFindClusters -x` | Yes | green |
| 14-01-02 | 01 | 1 | HYGN-03, HYGN-05 | unit | `.venv/bin/python -m pytest tests/test_consolidation.py::TestPickCanonical tests/test_consolidation.py::TestMergeCluster -x` | Yes | green |
| 14-01-03 | 01 | 1 | HYGN-02 | unit | `.venv/bin/python -m pytest tests/test_consolidation.py::TestConsolidateInput -x` | Yes | green |
| 14-02-01 | 02 | 1 | HYGN-01, HYGN-02 | integration | `.venv/bin/python -m pytest tests/test_consolidation.py::TestConsolidateToolIntegration::test_consolidate_tool_dry_run -x` | Yes | green |
| 14-02-02 | 02 | 1 | HYGN-03, HYGN-04, HYGN-05 | integration | `.venv/bin/python -m pytest tests/test_consolidation.py::TestConsolidateToolIntegration::test_consolidate_tool_auto_merge -x` | Yes | green |

*Status: pending / green / red / flaky*

---

## Requirement-to-Test Coverage

| Requirement | Description | Test Files | Key Tests | Status |
|-------------|-------------|------------|-----------|--------|
| HYGN-01 | Cluster semantically similar memories | test_consolidation.py | test_cluster_above_threshold, test_no_cluster_below_threshold, test_transitive_clustering, test_consolidate_tool_dry_run | COVERED |
| HYGN-02 | dry_run reports without modifying | test_consolidation.py | test_dry_run_default_true, test_consolidate_tool_dry_run | COVERED |
| HYGN-03 | Auto-merge into highest-vitality canonical | test_consolidation.py | test_pick_canonical_highest_vitality, test_merge_cluster_content, test_consolidate_tool_auto_merge | COVERED |
| HYGN-04 | superseded_by set on merged members | test_consolidation.py | test_merge_cluster_superseded_ids, test_consolidate_tool_auto_merge, test_consolidate_tool_skips_superseded | COVERED |
| HYGN-05 | Canonical inherits summed access_count | test_consolidation.py | test_merge_cluster_access_count, test_consolidate_tool_auto_merge | COVERED |

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

All 5 requirements have automated test coverage (13 unit + 5 integration tests).
