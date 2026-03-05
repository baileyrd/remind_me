# Roadmap: Remind Me MCP

## Milestones

- **v1.0 Full Refactor** -- Phases 1-3 (shipped 2026-02-24)
- **v1.1 Address 1.0 Tech Debt** -- Phases 4-9 (shipped 2026-02-25)
- **v1.2 Intelligent Retrieval** -- Phases 10-14 (in progress)

## Phases

<details>
<summary>v1.0 Full Refactor (Phases 1-3) -- SHIPPED 2026-02-24</summary>

- [x] Phase 1: Package Structure (3/3 plans) -- completed 2026-02-24
- [x] Phase 2: Test Infrastructure (4/4 plans) -- completed 2026-02-24
- [x] Phase 3: Quality and Bug Fixes (5/5 plans) -- completed 2026-02-24

</details>

<details>
<summary>v1.1 Address 1.0 Tech Debt (Phases 4-9) -- SHIPPED 2026-02-25</summary>

- [x] Phase 4: Code Quality and Cleanup (2/2 plans) -- completed 2026-02-24
- [x] Phase 5: CI/CD Pipeline (2/2 plans) -- completed 2026-02-24
- [x] Phase 6: Security Hardening (2/2 plans) -- completed 2026-02-24
- [x] Phase 7: API Embedding Parity (1/1 plan) -- completed 2026-02-24
- [x] Phase 8: Performance Improvements (2/2 plans) -- completed 2026-02-25
- [x] Phase 9: Gap Closure -- Async Bug Fix and Coverage Gate (2/2 plans) -- completed 2026-02-25

</details>

### v1.2 Intelligent Retrieval (In Progress)

**Milestone Goal:** Transform retrieval from naive hybrid search to a precision pipeline with token budgets, rank fusion, memory decay, atomic fact storage, and vault hygiene.

- [x] **Phase 10: Retrieval Pipeline** - RRF fusion, recency signal, token budget, and response envelope -- completed 2026-03-05
- [x] **Phase 11: Decay, Vitality, and Classification** - Schema migration, ACT-R vitality model, memory types, and per-category decay rates -- completed 2026-03-05
- [x] **Phase 12: Atomic Decomposition** - Claude-driven fact extraction from captures with batch processing tools (completed 2026-03-05)
- [x] **Phase 13: Structured Memory and Transparency** - Subject/predicate/object columns, structured query routing, and debug signals (completed 2026-03-05)
- [x] **Phase 14: Vault Hygiene** - Semantic clustering, consolidation, and deduplication of the memory vault (completed 2026-03-05)

## Phase Details

### Phase 10: Retrieval Pipeline
**Goal**: Search returns precise, budget-aware results ranked by fused signals instead of naive linear blending
**Depends on**: Nothing (first v1.2 phase, no schema changes needed)
**Requirements**: RETR-01, RETR-02, RETR-03, RETR-04
**Success Criteria** (what must be TRUE):
  1. Search results stay within the token budget (800 default) and the response reports how many candidates were trimmed
  2. Search ranking uses RRF to fuse keyword, semantic, and recency signals instead of linear score blending
  3. More recently accessed/created memories rank higher when relevance scores are close
  4. Every search response includes a metadata envelope with total_candidates, returned, trimmed, tokens_used, and budget
**Plans:** 2 plans
Plans:
- [ ] 10-01-PLAN.md -- Create retrieval module with RRF ranking, recency signal, token budget, and envelope
- [ ] 10-02-PLAN.md -- Wire retrieval pipeline into memory_search tool and add integration tests

### Phase 11: Decay, Vitality, and Classification
**Goal**: Every memory has a type, a vitality score that decays over time, and dormant memories fade out of default search
**Depends on**: Phase 10 (vitality becomes a fourth RRF signal in the existing pipeline)
**Requirements**: DECAY-01, DECAY-02, DECAY-03, DECAY-04, DECAY-05, DECAY-06, CLSF-01, CLSF-02, CLSF-03, CLSF-04, CLSF-05
**Success Criteria** (what must be TRUE):
  1. Schema migration adds vitality/decay columns and memory_type column; existing databases upgrade cleanly
  2. Accessing a memory recomputes its vitality using the ACT-R formula; frequently accessed memories stay vital
  3. Memories below vitality 0.05 are flagged dormant and excluded from default search (but retrievable with include_dormant)
  4. Claude can call remind_me_reclassify to classify memories in batches, and classification sets the appropriate decay rate
  5. remind_me_vitality_report surfaces dormant count, vault health metrics, and decay distribution
**Plans:** 3 plans
Plans:
- [ ] 11-01-PLAN.md -- Schema migration v4->v5 (decay/vitality/classification columns) and ACT-R vitality module
- [ ] 11-02-PLAN.md -- Classification tools (remind_me_reclassify and remind_me_reclassify_batch)
- [ ] 11-03-PLAN.md -- Wire vitality into search (4th RRF signal, dormant exclusion) and vitality report tool

### Phase 12: Atomic Decomposition
**Goal**: Claude can decompose captured conversations into atomic facts that are individually searchable and linked to their source
**Depends on**: Phase 11 (decomposed facts need memory_type for proper classification and decay rates)
**Requirements**: ATOM-01, ATOM-02, ATOM-03, ATOM-04, ATOM-05
**Success Criteria** (what must be TRUE):
  1. Claude can call remind_me_decompose with a capture_id and an array of extracted facts, and each fact is stored as a separate memory linked to the parent via source_capture_id
  2. remind_me_decompose_batch returns undecomposed memories for Claude to process in configurable batch sizes
  3. After remind_me_auto_capture stores a summary, the response includes a decomposition_pending hint
  4. Decomposed facts inherit tags from the parent capture plus any type-specific tags
**Plans:** 2/2 plans complete
Plans:
- [ ] 12-01-PLAN.md -- Schema migration v5->v6 (source_capture_id), decompose models, and decompose/decompose_batch tools
- [ ] 12-02-PLAN.md -- Wire decomposition_pending hint into auto_capture response

### Phase 13: Structured Memory and Transparency
**Goal**: Memories can carry structured subject/predicate/object triples for fast lookup, and search results expose ranking debug signals
**Depends on**: Phase 11 (structured queries benefit from memory_type routing; transparency needs vitality_rank)
**Requirements**: STRC-01, STRC-02, STRC-03, STRC-04, TRNS-01, TRNS-02
**Success Criteria** (what must be TRUE):
  1. Memories can store nullable subject, predicate, object columns with indexes on subject and memory_type
  2. Search detects structured query patterns (subject/predicate) and routes to indexed lookup before falling back to semantic search
  3. A superseded_by column tracks when a structured fact is replaced by a newer version
  4. Search results include a debug_signals block (semantic_rank, keyword_rank, recency_rank, vitality_rank, days_old) when verbose=True, plus tier_breakdown and dormant_excluded count in the envelope
**Plans:** 2/2 plans complete
Plans:
- [ ] 13-01-PLAN.md -- Schema migration v6->v7 (subject/predicate/object/superseded_by columns) and structured query routing
- [ ] 13-02-PLAN.md -- Debug signals, tier breakdown, and dormant_excluded transparency in search results

### Phase 14: Vault Hygiene
**Goal**: The memory vault can be cleaned up by clustering and consolidating semantically similar memories
**Depends on**: Phase 11 (consolidation uses vitality to pick the canonical record), Phase 13 (superseded_by column used by consolidation)
**Requirements**: HYGN-01, HYGN-02, HYGN-03, HYGN-04, HYGN-05
**Success Criteria** (what must be TRUE):
  1. remind_me_consolidate clusters semantically similar memories above a configurable similarity threshold
  2. dry_run mode reports clusters without modifying any data
  3. Auto-merge mode merges cluster content into the highest-vitality canonical record, sets superseded_by on merged members, and sums access_count into the canonical record
**Plans:** 2/2 plans complete
Plans:
- [ ] 14-01-PLAN.md -- TDD consolidation module (find_clusters, pick_canonical, merge_cluster) and ConsolidateInput model
- [ ] 14-02-PLAN.md -- Wire remind_me_consolidate MCP tool handler with integration tests

## Progress

**Execution Order:**
Phases execute in numeric order: 10 -> 11 -> 12 -> 13 -> 14

| Phase | Milestone | Plans | Status | Completed |
|-------|-----------|-------|--------|-----------|
| 1. Package Structure | v1.0 | 3/3 | Complete | 2026-02-24 |
| 2. Test Infrastructure | v1.0 | 4/4 | Complete | 2026-02-24 |
| 3. Quality and Bug Fixes | v1.0 | 5/5 | Complete | 2026-02-24 |
| 4. Code Quality and Cleanup | v1.1 | 2/2 | Complete | 2026-02-24 |
| 5. CI/CD Pipeline | v1.1 | 2/2 | Complete | 2026-02-24 |
| 6. Security Hardening | v1.1 | 2/2 | Complete | 2026-02-24 |
| 7. API Embedding Parity | v1.1 | 1/1 | Complete | 2026-02-24 |
| 8. Performance Improvements | v1.1 | 2/2 | Complete | 2026-02-25 |
| 9. Gap Closure | v1.1 | 2/2 | Complete | 2026-02-25 |
| 10. Retrieval Pipeline | v1.2 | 2/2 | Complete | 2026-03-05 |
| 11. Decay, Vitality, and Classification | v1.2 | 3/3 | Complete | 2026-03-05 |
| 12. Atomic Decomposition | v1.2 | 2/2 | Complete | 2026-03-05 |
| 13. Structured Memory and Transparency | 2/2 | Complete    | 2026-03-05 | - |
| 14. Vault Hygiene | 2/2 | Complete   | 2026-03-05 | - |
