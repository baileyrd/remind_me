---
gsd_state_version: 1.0
milestone: v1.2
milestone_name: Intelligent Retrieval
status: completed
stopped_at: Completed 14-02-PLAN.md (consolidation tool wiring)
last_updated: "2026-03-05T20:17:31.601Z"
last_activity: 2026-03-05 -- Plan 14-02 (consolidation tool wiring) completed
progress:
  total_phases: 5
  completed_phases: 3
  total_plans: 6
  completed_plans: 6
  percent: 100
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-04)

**Core value:** Persistent, searchable memory across all Claude interfaces -- modular, tested, maintainable
**Current focus:** v1.2 Intelligent Retrieval -- Phase 14: Vault Hygiene

## Current Position

Phase: 14 of 14 (Vault Hygiene)
Plan: 2 of 2
Status: Phase 14 Complete
Last activity: 2026-03-05 -- Plan 14-02 (consolidation tool wiring) completed

Progress: [██████████] 100%

## Performance Metrics

**Velocity (v1.0):**
- Total plans completed: 12
- Average duration: 3.7min
- Total execution time: ~0.6 hours

**Velocity (v1.1):**
- Total plans completed: 11
- Phases: 6
- Timeline: 1 day

**Velocity (v1.2 so far):**
- Plans completed: 6
- Phase 10: 2 plans, 2 waves
- Phase 11: 3/3 plans complete
- Phase 12: 2/2 plans complete
- Phase 13: 2/2 plans complete (structured memory and transparency)
- Phase 14: 2/2 plans complete (vault hygiene consolidation)

## Accumulated Context

### Decisions

- Atomic decomposition and classification are Claude-driven (not server-side LLM) -- server stores results, Claude does extraction
- Structured memory uses columns on existing table (not separate structured_memories table) -- simpler, same benefits
- RRF k=60 default, configurable via env var (REMIND_ME_RRF_K)
- Retroactive decomposition via batch tool loop (Claude classifies, calls back with results)
- Phase 11 combines decay + classification because classification sets per-category decay rates
- retrieval.py is a pure-function module (rank_rrf, apply_token_budget) -- tools.py does wiring
- Filters applied BEFORE RRF ranking (not after)
- token_budget=0 means unlimited
- ACT-R formula uses (access_count+1)^0.5 for diminishing returns on repeated access
- Bridge protection at 10 accesses halves decay rate via BRIDGE_MULTIPLIER=0.5
- 8 memory types with decay rates from 0.02 (decision) to 0.20 (action_item)
- Classification excludes 'unclassified' from valid types -- it is the default state, not a classification
- Batch classification pattern: fetch unclassified -> Claude classifies -> reclassify with results
- Vitality defaults to 1.0 for memories without the field (backwards compatible with pre-v5 data)
- Dormant filtering applied BEFORE RRF ranking (consistent with category/tag filter pattern)
- record_access uses fire-and-forget asyncio.create_task to avoid blocking search response

- Decomposed facts get category='fact' and source='decomposition' for consistent filtering
- source_capture_id is NULL default (backward compatible); decomposed children have capture_id=NULL
- Tag deduplication uses dict.fromkeys for order-preserving uniqueness
- NOT EXISTS subquery pattern for finding undecomposed captures

Full decision log in PROJECT.md Key Decisions table.
- [Phase 12]: Response-embedded workflow hints guide Claude tool chaining (decomposition_pending in auto_capture)
- [Phase 13]: Debug signals use underscore-prefixed internal rank keys from RRF output
- [Phase 13]: Tier breakdown and dormant_excluded always included in envelope (not gated by verbose)
- [Phase 13]: Dormant exclusion count uses deduplicated IDs across FTS+semantic to avoid double-counting
- [Phase 13]: Structured query uses regex for subject:/predicate: prefix parsing with quoted and unquoted values
- [Phase 13]: Superseded memories excluded via SQL WHERE clause (superseded_by IS NULL)
- [Phase 13]: Structured results bypass RRF pipeline entirely; fall back to FTS/semantic with stripped query when no results
- [Phase 14]: Union-Find for transitive clustering (A~B and B~C implies single cluster)
- [Phase 14]: Content merge uses dict.fromkeys for order-preserving line deduplication
- [Phase 14]: pick_canonical tiebreaks on accessed_at (most recent wins when vitality equal)
- [Phase 14]: Consolidation tool uses deferred numpy import (function scope) to avoid top-level dependency in tools.py
- [Phase 14]: Merge operations wrapped in single DB transaction for atomicity

### Pending Todos

None.

### Blockers/Concerns

None.

## Session Continuity

Last session: 2026-03-05T20:08:07Z
Stopped at: Completed 14-02-PLAN.md (consolidation tool wiring)
Resume file: None
