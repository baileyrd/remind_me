---
phase: 10-retrieval-pipeline
plan: 02
subsystem: retrieval
tags: [rrf, ranking, token-budget, search, integration]
dependency-graph:
  requires: [rank_rrf, apply_token_budget, SearchEnvelope]
  provides: [rrf-powered-memory-search, envelope-metadata-response]
  affects: [remind_me_mcp/tools.py, tests/test_tools.py]
tech-stack:
  added: []
  patterns: [RRF fusion in tool handler, pre-ranking filters, SearchEnvelope response]
key-files:
  created: []
  modified:
    - remind_me_mcp/tools.py
    - tests/test_tools.py
decisions:
  - Filters applied BEFORE RRF ranking to avoid ranking irrelevant results
  - Hybrid detection uses set intersection of FTS and semantic ID sets after RRF merge
  - Import ordering fixed by ruff auto-sort (retrieval import moved alphabetically)
metrics:
  duration: 3min
  completed: 2026-03-05
---

# Phase 10 Plan 02: Wire RRF Retrieval Pipeline Summary

Replaced linear score blending in memory_search with RRF ranking + token budget trimming; JSON/Markdown responses now include SearchEnvelope metadata.

## What Was Built

### tools.py (modified)
- **Import**: Added `from remind_me_mcp.retrieval import apply_token_budget, rank_rrf`
- **`_apply_filters()` helper**: New module-level function that filters memories by category and/or tags before passing to RRF ranking
- **`memory_search()` rewrite**: Replaced linear score blending (FTS position weighting + semantic distance + hybrid boost) with:
  1. Tag `_search_method` on raw FTS/semantic results
  2. Apply category/tag filters BEFORE RRF ranking
  3. `rank_rrf(filtered_fts, filtered_sem)` for fusion ranking
  4. Mark hybrid results (appeared in both FTS and semantic)
  5. Apply limit, then `apply_token_budget()` for token-aware trimming
  6. JSON response returns full envelope (total_candidates, returned, trimmed, tokens_used, budget, memories)
  7. Markdown response includes envelope summary line with token/budget info

### tests/test_tools.py (modified)
- **Updated** `test_memory_search_json_format` to check new envelope keys instead of old `count` key
- **Added 5 new integration tests**:
  - `test_search_returns_envelope_json` -- validates all 5 envelope keys exist with correct types
  - `test_search_token_budget_trims` -- verifies small budget produces trimmed > 0
  - `test_search_token_budget_zero_unlimited` -- verifies budget=0 returns all results
  - `test_search_envelope_markdown` -- verifies markdown output contains token/budget info
  - `test_search_rrf_ranking_smoke` -- verifies RRF path produces valid envelope with _rrf_score metadata

## Commits

| Task | Commit | Description |
|------|--------|-------------|
| 1+2 | 41ac015 | feat: wire RRF retrieval pipeline into memory_search with envelope metadata |

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Updated existing test for new response format**
- **Found during:** Task 1 verification
- **Issue:** `test_memory_search_json_format` asserted `"count" in data` but the new envelope response uses `"returned"` instead of `"count"`
- **Fix:** Updated test to check for all 5 envelope keys (total_candidates, returned, trimmed, tokens_used, budget)
- **Files modified:** tests/test_tools.py

**2. [Rule 3 - Blocking] Fixed import ordering lint error**
- **Found during:** Task 1 verification
- **Issue:** ruff I001 import sort violation from new retrieval import placement
- **Fix:** Ran `ruff check --fix` to auto-sort imports
- **Files modified:** remind_me_mcp/tools.py

## Verification Results

- `python -m pytest tests/test_tools.py -x -v -k "search"` -- 23 passed (6 existing + 5 new search tests + 12 related)
- `python -m pytest tests/test_retrieval.py -x -v` -- 18/18 passed (no regressions)
- `python -m pytest tests/ -v` -- 252 passed, 5 pre-existing errors (missing sqlite_vec)
- `ruff check remind_me_mcp/tools.py tests/test_tools.py` -- All checks passed

## Self-Check: PASSED
