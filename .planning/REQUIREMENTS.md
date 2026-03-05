# Requirements: Remind Me MCP

**Defined:** 2026-03-04
**Core Value:** Persistent, searchable memory across all Claude interfaces -- modular, tested, maintainable

## v1.2 Requirements

Requirements for Intelligent Retrieval milestone. Each maps to roadmap phases.

### Retrieval Pipeline

- [x] **RETR-01**: Search respects a configurable token_budget parameter (default 800) and trims results that exceed the budget
- [x] **RETR-02**: Search uses Reciprocal Rank Fusion (RRF, k=60 configurable) to combine signals instead of linear score blending
- [x] **RETR-03**: Recency is added as a third retrieval signal ranked by age ascending
- [x] **RETR-04**: Response envelope includes metadata (total_candidates, returned, trimmed, tokens_used, budget)

### Decay & Vitality

- [x] **DECAY-01**: memories table has accessed_at, access_count, decay_rate, vitality, base_weight, and status columns
- [x] **DECAY-02**: Vitality is recomputed on every access using ACT-R formula: base_weight * (access_count+1)^0.5 * e^(-decay_rate * days_since_last_access)
- [ ] **DECAY-03**: Memories below vitality floor (< 0.05) are flagged status='dormant' and excluded from default search
- [ ] **DECAY-04**: Search accepts include_dormant and min_vitality parameters
- [ ] **DECAY-05**: Vitality is a fourth RRF signal in search ranking
- [x] **DECAY-06**: Bridge protection: memories with high access_count get decay_rate multiplied by 0.5

### Classification

- [x] **CLSF-01**: memories table has memory_type column (decision, preference, fact, insight, learning, blocker, action_item)
- [x] **CLSF-02**: remind_me_reclassify tool accepts batch of memory IDs with classifications from Claude and applies them
- [x] **CLSF-03**: remind_me_reclassify returns unclassified memories in configurable batch sizes for Claude to classify
- [x] **CLSF-04**: Classification sets appropriate decay_rate per category from the decay rate table
- [ ] **CLSF-05**: remind_me_vitality_report tool surfaces dormant count, vault health metrics, and decay distribution

### Atomic Decomposition

- [ ] **ATOM-01**: remind_me_decompose tool accepts capture_id and array of extracted atomic facts, stores each as a linked memory
- [ ] **ATOM-02**: Each decomposed fact is linked to parent via source_capture_id column
- [ ] **ATOM-03**: remind_me_decompose_batch returns N undecomposed memories for Claude to process
- [ ] **ATOM-04**: remind_me_auto_capture response includes decomposition_pending hint when summary is stored
- [ ] **ATOM-05**: Decomposed facts inherit tags from parent capture plus type-specific tags

### Structured Memory

- [ ] **STRC-01**: memories table has subject, predicate, object columns (nullable)
- [ ] **STRC-02**: Indexes on subject, memory_type for fast structured lookups
- [ ] **STRC-03**: Search routes structured queries (subject/predicate patterns) to indexed lookup before falling back to semantic search
- [ ] **STRC-04**: superseded_by column tracks when a structured fact is replaced by a newer version

### Vault Hygiene

- [ ] **HYGN-01**: remind_me_consolidate tool clusters semantically similar memories above a configurable similarity threshold
- [ ] **HYGN-02**: Consolidation supports dry_run mode that reports clusters without modifying data
- [ ] **HYGN-03**: Auto-merge mode merges cluster content into highest-vitality canonical record
- [ ] **HYGN-04**: Superseded memories get superseded_by set to canonical ID (not deleted)
- [ ] **HYGN-05**: Canonical record inherits summed access_count from all merged members

### Transparency

- [ ] **TRNS-01**: Search results include debug_signals block when verbose=True (semantic_rank, keyword_rank, recency_rank, vitality_rank, days_old)
- [ ] **TRNS-02**: Response envelope includes tier_breakdown and dormant_excluded count

## Future Requirements

### Deferred from Active

- **REST-01**: REST API semantic search endpoint (`/api/memories/semantic-search`)
- **QUAL-01**: mypy strict mode enforcement

## Out of Scope

| Feature | Reason |
|---------|--------|
| Separate structured_memories table | subject/predicate/object columns on existing table sufficient; avoids dual-table complexity |
| Server-side LLM calls | Decomposition and classification are Claude's job; server stores results |
| HTTPS/TLS | Localhost traffic; self-signed certs add complexity with no benefit |
| Rate limiting | Single-user personal tool; no multi-tenant scenario |
| Full OAuth2/JWT auth | Static bearer token sufficient for personal localhost tool |
| Automatic consolidation | Requires human review; dry_run + manual approval by design |

## Traceability

Which phases cover which requirements. Updated during roadmap creation.

| Requirement | Phase | Status |
|-------------|-------|--------|
| RETR-01 | Phase 10 | Complete |
| RETR-02 | Phase 10 | Complete |
| RETR-03 | Phase 10 | Complete |
| RETR-04 | Phase 10 | Complete |
| DECAY-01 | Phase 11, Plan 01 | Complete |
| DECAY-02 | Phase 11, Plan 01 | Complete |
| DECAY-03 | Phase 11 | Pending |
| DECAY-04 | Phase 11 | Pending |
| DECAY-05 | Phase 11 | Pending |
| DECAY-06 | Phase 11, Plan 01 | Complete |
| CLSF-01 | Phase 11 | Complete |
| CLSF-02 | Phase 11 | Complete |
| CLSF-03 | Phase 11 | Complete |
| CLSF-04 | Phase 11 | Complete |
| CLSF-05 | Phase 11 | Pending |
| ATOM-01 | Phase 12 | Pending |
| ATOM-02 | Phase 12 | Pending |
| ATOM-03 | Phase 12 | Pending |
| ATOM-04 | Phase 12 | Pending |
| ATOM-05 | Phase 12 | Pending |
| STRC-01 | Phase 13 | Pending |
| STRC-02 | Phase 13 | Pending |
| STRC-03 | Phase 13 | Pending |
| STRC-04 | Phase 13 | Pending |
| HYGN-01 | Phase 14 | Pending |
| HYGN-02 | Phase 14 | Pending |
| HYGN-03 | Phase 14 | Pending |
| HYGN-04 | Phase 14 | Pending |
| HYGN-05 | Phase 14 | Pending |
| TRNS-01 | Phase 13 | Pending |
| TRNS-02 | Phase 13 | Pending |

**Coverage:**
- v1.2 requirements: 31 total
- Mapped to phases: 31
- Unmapped: 0

---
*Requirements defined: 2026-03-04*
*Last updated: 2026-03-04 after roadmap creation*
