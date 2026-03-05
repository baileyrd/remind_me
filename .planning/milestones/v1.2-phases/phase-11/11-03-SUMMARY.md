---
phase: 11-decay-vitality-classification
plan: 03
subsystem: vitality-search-integration
tags: [vitality, rrf, dormant-filtering, vault-health, search-pipeline]
dependency_graph:
  requires: [schema-v5, vitality-module]
  provides: [4-signal-rrf, dormant-exclusion, vitality-report-tool]
  affects: [remind_me_mcp/retrieval.py, remind_me_mcp/tools.py, remind_me_mcp/models.py]
tech_stack:
  added: []
  patterns: [fire-and-forget-access-recording, 4-signal-rrf, dormant-filtering]
key_files:
  created: []
  modified:
    - remind_me_mcp/retrieval.py
    - remind_me_mcp/models.py
    - remind_me_mcp/tools.py
    - tests/test_retrieval.py
    - tests/test_tools.py
    - tests/conftest.py
decisions:
  - "Vitality defaults to 1.0 for memories without the field (backwards compatible)"
  - "Dormant filtering applied BEFORE RRF ranking (consistent with category/tag filter pattern)"
  - "record_access uses fire-and-forget asyncio.create_task to avoid blocking search response"
  - "Vitality buckets use 5 fixed ranges matching common vitality thresholds"
metrics:
  duration: ~6min
  completed: 2026-03-05
  tasks: 2/2
  tests_added: 9
  lines_added: ~500
---

# Phase 11 Plan 03: Wire Vitality into Search and Vitality Report Tool Summary

4-signal RRF ranking with vitality, dormant memory exclusion from search, min_vitality filtering, fire-and-forget access recording, and vault health report tool.

## What Was Done

### Task 1: Vitality as 4th RRF Signal and Dormant Filtering

**In retrieval.py**, updated `rank_rrf`:
- Added 4th ranking signal: vitality_rank (sorted by vitality DESC, default 1.0)
- Each result dict now includes `_vitality_rank` metadata
- RRF score sums 4 reciprocal ranks: keyword, semantic, recency, vitality
- Updated docstring to document all 4 signals

**In models.py**, updated `MemorySearchInput`:
- Added `include_dormant: bool = False` field
- Added `min_vitality: float = 0.0` field (ge=0.0, le=1.0)

**In tools.py**, updated `memory_search`:
- Imported `record_access` from `remind_me_mcp.vitality`
- Dormant filtering applied BEFORE RRF: excludes `status='dormant'` by default
- Min vitality filtering: excludes memories below threshold when `min_vitality > 0`
- Fire-and-forget `record_access` via `asyncio.create_task` for all returned memory IDs

**In conftest.py**, updated `memory_factory`:
- Now supports v5 schema columns (status, vitality, memory_type, etc.) via UPDATE after INSERT

4 new tests in test_retrieval.py, 4 new tests in test_tools.py.

### Task 2: Vitality Report Tool

**In models.py**, added `VitalityReportInput`:
- `response_format: ResponseFormat = Field(default=ResponseFormat.JSON)`
- Added to `__all__`

**In tools.py**, added `remind_me_vitality_report` tool:
- Registered as MCP tool with `readOnlyHint=True`
- Queries: total, active, dormant counts; AVG(vitality); GROUP BY memory_type; vitality bucket counts
- 5 vitality buckets: 0.00-0.05, 0.05-0.25, 0.25-0.50, 0.50-0.75, 0.75-1.00
- vault_health_score as percentage (active/total)
- Supports JSON and markdown response formats

5 new tests in test_tools.py covering all report fields.

## Commits

| Task | Commit | Description |
|------|--------|-------------|
| 1 (RED) | b84a094 | test(11-03): add failing tests for 4-signal RRF vitality and dormant filtering |
| 1 (GREEN) | 5496e0d | feat(11-03): add vitality as 4th RRF signal and dormant filtering in search |
| 2 (GREEN) | fdd63e0 | feat(11-03): add vitality report tool with vault health metrics |

## Deviations from Plan

None -- plan executed exactly as written.

## Verification

- All 85 retrieval + tools tests pass (excluding Plan 11-02 reclassify tests pending merge)
- 232 tests pass across full suite (excluding pre-existing sqlite_vec env issue)
- `ruff check remind_me_mcp/retrieval.py remind_me_mcp/models.py` -- clean
- `rank_rrf` docstring confirms 4 signals: keyword, semantic, recency, vitality
- Vitality report returns all required fields: total, active, dormant, avg_vitality, vault_health, decay_distribution, vitality_buckets

## Self-Check: PASSED

All 6 key files found on disk. All 3 task commits (b84a094, 5496e0d, fdd63e0) verified in git log.
