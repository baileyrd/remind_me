# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-04)

**Core value:** Persistent, searchable memory across all Claude interfaces -- modular, tested, maintainable
**Current focus:** v1.2 Intelligent Retrieval -- Phase 11: Decay, Vitality, and Classification

## Current Position

Phase: 11 of 14 (Decay, Vitality, and Classification)
Plan: 1 of 3
Status: Executing
Last activity: 2026-03-05 -- Plan 11-01 (Schema migration v4->v5 and vitality module) completed

Progress: [########################................] 60% (10/14 phases complete, 1/5 v1.2 phases)

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
- Plans completed: 3
- Phase 10: 2 plans, 2 waves
- Phase 11: 1/3 plans complete

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

Full decision log in PROJECT.md Key Decisions table.

### Pending Todos

None.

### Blockers/Concerns

None.

## Session Continuity

Last session: 2026-03-05
Stopped at: Completed 11-01-PLAN.md (schema migration v4->v5 and vitality module)
Resume file: None
