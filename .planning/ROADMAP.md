# Roadmap: Remind Me MCP

## Milestones

- **v1.0 Full Refactor** -- Phases 1-3 (shipped 2026-02-24)
- **v1.1 Address 1.0 Tech Debt** -- Phases 4-9 (shipped 2026-02-25)
- **v1.2 Intelligent Retrieval** -- Phases 10-14 (shipped 2026-03-05)
- **v1.3 Retrieval Quality** -- Backlog (unscheduled) -- see Backlog below

## Backlog -- Retrieval Quality (candidate v1.3, unscheduled)

Lever labels (A-E) reference the analysis in `benchmarks/RESULTS.md`. Levers A
(model-matched equal-footing) and B (sliding-window chunking) are shipped; C, D,
and E have shipped code but their empirical measurements are outstanding.

- [ ] **C -- Measure the RRF recency+vitality rebalance on real data.** Code is
  shipped (configurable `RRF_W_*` weights + `retrieval`/`semantic` profiles, commit
  `e2c40ae`; proven deterministically in `tests/test_rrf_weights.py`). Outstanding:
  the empirical before/after on `longmemeval_s` -- run
  `benchmarks.before_after --compare rrf` (default 4-signal vs. recency+vitality
  dropped), expect the gain in R@1/MRR, and fill the pending table in the
  "RRF retrieval profile" section of `RESULTS.md`. Note: the equal-footing/chunking
  headline runs already use `--rrf-profile semantic` (recency+vitality zeroed), so
  they bake in C's effect; this task isolates and quantifies it in the hybrid path.
- [ ] **D -- Reranker over top-k.** Code is shipped: ONNX cross-encoder
  (`remind_me_mcp/reranker.py`, default `cross-encoder/ms-marco-MiniLM-L6-v2`)
  rescores the top `REMIND_ME_RERANK_TOP_K` RRF candidates when
  `REMIND_ME_RERANK=onnx`; proven deterministically in `tests/test_reranker.py`.
  Outstanding: the empirical before/after on `longmemeval_s` -- run
  `benchmarks.before_after --compare rerank --rrf-profile semantic` and fill the
  pending table in the "Cross-encoder reranker" section of `RESULTS.md`. The most
  direct path to close the remaining gap to MemPalace's LLM-reranked >=0.99 /
  1.000; expect the lift in R@1/MRR, especially on the weak categories.
- [ ] **E -- Query-side expansion / HyDE.** Code is shipped: Ollama-generated
  hypothetical answer passage averaged into the query embedding
  (`remind_me_mcp/query_expansion.py`, enabled via
  `REMIND_ME_QUERY_EXPANSION=hyde`); proven deterministically in
  `tests/test_query_expansion.py`. Outstanding: the A/B on `longmemeval_s` -- run
  `benchmarks.before_after --compare hyde --rrf-profile semantic` (needs a local
  Ollama daemon) and fill the pending table in the "HyDE query expansion" section
  of `RESULTS.md`. Lower-confidence lever; keep only if the weak categories
  (`single-session-preference`, multi-hop `temporal-reasoning`) move without
  hurting the strong ones.

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

<details>
<summary>v1.2 Intelligent Retrieval (Phases 10-14) -- SHIPPED 2026-03-05</summary>

- [x] Phase 10: Retrieval Pipeline (2/2 plans) -- completed 2026-03-05
- [x] Phase 11: Decay, Vitality, and Classification (3/3 plans) -- completed 2026-03-05
- [x] Phase 12: Atomic Decomposition (2/2 plans) -- completed 2026-03-05
- [x] Phase 13: Structured Memory and Transparency (2/2 plans) -- completed 2026-03-05
- [x] Phase 14: Vault Hygiene (2/2 plans) -- completed 2026-03-05

</details>

## Progress

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
| 11. Decay, Vitality, Classification | v1.2 | 3/3 | Complete | 2026-03-05 |
| 12. Atomic Decomposition | v1.2 | 2/2 | Complete | 2026-03-05 |
| 13. Structured Memory, Transparency | v1.2 | 2/2 | Complete | 2026-03-05 |
| 14. Vault Hygiene | v1.2 | 2/2 | Complete | 2026-03-05 |
