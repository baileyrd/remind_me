# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-04)

**Core value:** Persistent, searchable memory across all Claude interfaces -- modular, tested, maintainable
**Current focus:** v1.2 Intelligent Retrieval -- Phase 11: Decay, Vitality, and Classification

## Current Position

Phase: 11 of 14 (Decay, Vitality, and Classification)
Plan: 3 of 3
Status: Phase Complete
Last activity: 2026-03-05 -- Plan 11-03 (Wire vitality into search and vitality report) completed

Progress: [#############################...........] 71% (11/14 phases complete, 2/5 v1.2 phases)

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
- Plans completed: 5
- Phase 10: 2 plans, 2 waves
- Phase 11: 3/3 plans complete

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

Full decision log in PROJECT.md Key Decisions table.

### Pending Todos

None.

### Blockers/Concerns

None.

## Session Continuity

Last session: 2026-03-05
Stopped at: Completed 11-03-PLAN.md (wire vitality into search and vitality report)
Resume file: None
