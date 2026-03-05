---
phase: 14
slug: vault-hygiene
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-05
---

# Phase 14 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 7.x |
| **Config file** | pyproject.toml |
| **Quick run command** | `.venv/bin/python -m pytest tests/ -x --ignore=tests/test_api.py` |
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
| 14-01-01 | 01 | 1 | HYGN-01, HYGN-02 | unit | `.venv/bin/python -m pytest tests/test_consolidation.py -x` | ❌ W0 | ⬜ pending |
| 14-01-02 | 01 | 1 | HYGN-03, HYGN-04, HYGN-05 | unit+integration | `.venv/bin/python -m pytest tests/test_consolidation.py tests/test_tools.py -x` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_consolidation.py` — stubs for HYGN-01 through HYGN-05
- [ ] `tests/conftest.py` — extend memory_factory with embedding support for consolidation tests

*Existing infrastructure covers most needs; only consolidation-specific test file is new.*

---

## Manual-Only Verifications

*All phase behaviors have automated verification.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 2s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
