# Release Notes

## v1.8.0 — 2026-07-21

Closes a precision gap flagged in the application capability review: `rank_rrf` fuses keyword, semantic, recency, vitality, and IDF signals purely by ordinal rank position, discarding the actual score magnitude — a 0.95-cosine semantic match and a 0.55-cosine match tie if they happen to land in adjacent rank positions, even though one is a far stronger match than the other.

### New Features

- **Score-based fusion mode, opt-in** — `rank_rrf` gains a `fusion` parameter (`"rank"` default, `"score"` new) plus a module-level `REMIND_ME_RRF_FUSION` env var. `"score"` mode min-max normalizes the real underlying magnitudes across the candidate pool — FTS5 `bm25()` score, semantic distance, `created_at`, and `vitality` — into `[0, 1]` (higher = better) and sums `weight * normalized_score`, instead of `1/(k + rank)` terms. A memory missing a signal (e.g. a semantic-only hit has no `bm25` score) gets `0.0` for that signal, mirroring rank mode's penalty-rank treatment. `w_idf` reuses the same normalized keyword score in this mode, since both derive from the identical `bm25` magnitude. `"rank"` stays the default, so existing callers and benchmark numbers are unaffected unless explicitly opted in.
- Rank fields (`_keyword_rank` etc.) are still computed and set in `"score"` mode too, so existing debug tooling keeps working; `build_debug_signals` additionally surfaces `keyword_score`/`semantic_score`/`recency_score`/`vitality_score`/`fusion_mode` when score fusion was used (omitted entirely for rank-mode results).
- `benchmarks/runner.py` gains `--rrf-fusion {rank,score}`; `benchmarks/before_after.py` gains `--compare score_fusion` for A/B measurement against the rank-only baseline.

## v1.7.0 — 2026-07-21

Ships the single most-cited unused retrieval-quality lever flagged in the application capability review: cross-encoder reranking (`reranker.py`) was built, tested, and off by default — adoption was effectively zero even though `benchmarks/RESULTS.md` already documented its value clearly.

### Improvements

- **Reranking on by default** — `REMIND_ME_RERANK` now defaults to `"onnx"` instead of unset. Rescoring only ever touches the bounded `REMIND_ME_RERANK_TOP_K` (default 20) head of the RRF-ranked list regardless of how large the underlying result pool is, so the added latency is small and constant. Set `REMIND_ME_RERANK=""` to opt back out for latency-sensitive deployments.
- **Stronger default cross-encoder** — `REMIND_ME_RERANK_MODEL` swaps from the 2019 `cross-encoder/ms-marco-MiniLM-L6-v2` to `BAAI/bge-reranker-base` (2023), still small enough to run on CPU but meaningfully stronger. Fully overridable via `REMIND_ME_RERANK_MODEL` regardless.
- **Reranker failure caching (PF-01)** — `CrossEncoderReranker` now caches load failures exactly like the embedder already does: a missing dependency is permanent for the process, and any other failure (no network, no ONNX export for the configured model) is retried only after a cooldown instead of re-attempting a live HuggingFace download on every single search — necessary now that reranking runs for everyone by default, not just users who explicitly opted in.
- `benchmarks/runner.py`'s `--rerank` flag now explicitly forces the backend on or off, so lever-isolation benchmark runs stay correct regardless of the library's own default.

## v1.6.0 — 2026-07-21

Closes a retrieval-quality gap: modern embedding models (`nomic-embed-text`, `bge-*`, `e5-*`) are trained with an asymmetric query/passage convention — a search query and an indexed document are expected to carry different instruction prefixes (e.g. `search_query:` vs `search_document:`). remind_me embedded both identically, silently leaving quality on the table for anyone using one of these models via the Ollama backend.

### Improvements

- **Query/document embedding prefix asymmetry** — `_Embedder.embed`/`embed_one` (ONNX) and `OllamaEmbedder.embed`/`embed_one` gain a `role: Literal["query", "passage"]` parameter (default `"passage"`). A per-model-family lookup table (`embeddings._ROLE_PREFIXES`, matched by substring against the configured model name) applies the correct instruction prefix — `nomic-embed-text`'s `search_query:`/`search_document:`, `e5-*`'s `query:`/`passage:`, `bge-*`'s query-only instruction — before encoding. Models with no known convention (the ONNX default `all-MiniLM-L6-v2`) are unaffected — no prefix, identical behavior to before.
- Every embed call site is now correctly labeled: document chunks are embedded with `role="passage"` at write time; a search query is embedded with `role="query"`; a fused query+HyDE-passage embedding embeds the literal query as `"query"` and the synthetic HyDE passage as `"passage"` before averaging, rather than treating both halves as the same role.

## v1.5.0 — 2026-07-21

Closes a real gap in the living-memory model: supersession only ever happened via similarity-merge (near-duplicate memories get consolidated), so a genuinely contradictory update — "I moved to Boston" — never replaced an old fact like "I live in Seattle," since the two statements share no text.

### New Features

- **Contradiction-based supersession** — a new `_supersede_contradicting_facts` (`db.py`) deterministically supersedes any other non-superseded, non-deleted memory that shares a new/updated SPO triple's subject+predicate but has a different object. Wired into every place a triple gets attached to a memory: `remind_me_add`, `remind_me_decompose` (per extracted fact), and `remind_me_annotate` (re-checking the memory's full current triple, since annotations can be partial). Uses the same `superseded_by` mechanism as similarity-merge, so every existing superseded-exclusion read path (search, list, entity lookups) picks it up automatically.
- Deliberately narrow to avoid false positives: a differently-worded predicate never contradicts — "I live in Seattle" and "I visited Boston" don't collide, since they don't share a predicate.

## v1.4.0 — 2026-07-21

Fixes a real multi-device correctness bug: sync had no delete semantics at all. Deleting a memory on one device was a hard `DELETE`, which produces no `sync_outbox` row (the sync triggers only fire on INSERT/UPDATE) — so the next pull from another device silently resurrected it.

### New Features

- **Delete/tombstone propagation across sync** — a new `deleted_at` column turns delete into a soft-delete UPDATE, which rides the *existing* update-outbox trigger and last-write-wins conflict resolution for free — no new operation type or wire format. Every normal read path (search, list, get, entity profile, dashboard REST routes) excludes tombstoned memories; sync's pull/push wire paths and full-backup exports deliberately don't, since they need to carry/preserve tombstones.
- **Automatic tombstone compaction** — a background pass hard-deletes tombstones older than `REMIND_ME_TOMBSTONE_RETENTION_DAYS` (default 180, deliberately more generous than the 30-day outbox retention) so the table doesn't grow forever.
- **Hub parity** — the Postgres hub's schema, upsert, and pull-wire columns all carry `deleted_at`, so hub-mediated sync propagates tombstones exactly like direct peer sync.
- On a node with sync disabled entirely, delete stays a plain, immediate hard delete exactly as before — there's nothing to propagate to, so nothing changes for single-device users.

## v1.3.1 — 2026-07-21

Defense-in-depth fix, not a new capability: the sync pull path was the one caller of `_embed_and_store_rows` that never batched its input, relying entirely on the downstream `EMBED_FORWARD_BATCH` forward-pass cap (added in PR #15) to bound memory.

### Improvements

- **`_embed_and_store_rows` now batches internally** by `EMBED_BATCH_SIZE`, regardless of how many rows a caller passes in one call — a single source of truth for "no caller flattens the whole store into one embed()/transaction," instead of every bulk caller having to remember to pre-slice its own input. Fixes sync's pulled-record embedding (`_upsert_records`) without any change to `sync.py` itself.
- Removed the now-redundant external batching loops in the file/mempalace/dbs importers — each now hands its rows to `_embed_and_store_rows` in one call, same as sync already did.

## v1.3.0 — 2026-07-21

Semantic search's `sqlite-vec` KNN was an exact brute-force scan over every chunk vector — correct, but O(n) per query, the one thing that would visibly degrade as a memory store grows into the tens of thousands of chunks.

### New Features

- **Optional ANN index for semantic search** — a new `ann_index.py` module adds an HNSW approximate-nearest-neighbor index (via the `usearch` package, new `ann` extra) that `_semantic_search` consults once a store passes `REMIND_ME_ANN_MIN_CHUNKS` chunk vectors (default 5000, opt-in-by-scale). Below that threshold, or if `usearch` isn't installed, or if the ANN path itself fails for any reason, search transparently falls back to the existing exact brute-force scan — same output shape, same `semantic_distance` meaning either way.
- The index is self-healing: held in memory for the life of the process, mutated incrementally as chunks are added/removed, persisted to disk on clean shutdown, and automatically rebuilt from `memories_vec` if the on-disk index is missing, corrupt, or size-mismatched (e.g. after a hard crash).
- `remind_me_server_status` reports ANN index state (built, vector count, threshold) alongside the existing semantic-search status.
- Benchmarked at 20k chunk vectors: ~11x faster than the brute-force scan, identical top result.

## v1.2.0 — 2026-07-21

The LLM Wiki (FT-08) gains a user-facing surface: until now Claude could read and write it, but the human owner had no way to see it outside the MCP tools.

### New Features

- **Wiki REST API** — five read-only routes (`GET /api/wiki`, `/api/wiki/search`, `/api/wiki/load`, `/api/wiki/status`, `/api/wiki/{slug}`) mirroring the `remind_me_wiki_*` MCP tools' read paths. Writing stays an MCP-tool-only, LLM-curated action by design — no POST/PUT/DELETE.
- **Wiki dashboard view** — a new "Wiki" tab: searchable page catalogue, rendered page body with clickable `[[Wikilinks]]`, and a links/backlinks panel for cross-page navigation; a pending-compile badge flags raw memories not yet folded in.
- `docs/openapi.yaml` updated with the new routes and response schemas.

## v1.1.0 — 2026-07-21

Eight-phase capability expansion closing gaps identified in a comparison against [cognee](docs/cognee-capability-review-2026-07-20.md) and [Cerebras's internal knowledge system](docs/cerebras-knowledge-capability-review-2026-07-20.md). Every change is backward-compatible — opt-in or default-preserving, no breaking changes to tools, storage, or sync wire formats.

### New Features

- **Search feedback loop** — `remind_me_feedback` marks a search result helpful or unhelpful, nudging `base_weight` and future ranking (#19)
- **Opt-in IDF ranking signal** — a `bm25`-derived relevance signal for RRF fusion, off by default (#19)
- **Neighbor-aware chunk retrieval** — `include_neighbors` on `remind_me_search` surfaces adjacent chunks from the same source document (#20)
- **Typed entity-to-entity relations** — a new `entity_relations` table and `remind_me_entity_traverse` tool for multi-hop graph queries (#21)
- **Pluggable import connectors** — `chat`/`document` (and third-party kinds) are parser functions registered by kind string instead of a hardcoded dispatch; `remind_me_list_connectors` reports the registry (#22)
- **Push/webhook ingestion** — a bearer-authenticated `POST /ingest` endpoint accepts content directly over the network, sharing the file importer's connector dispatch and hash dedup (#23)
- **Ingest-time normalization** — `remind_me_normalize_batch` / `remind_me_normalize_apply` distill noisy raw imports into clean `{question, summary, resolution?}` memories, non-destructively linked back to the source (#23)
- **Auto-routing retrieval strategy** — `remind_me_search` gains a `strategy` parameter (`auto`/`balanced`/`keyword_favored`/`semantic_favored`) that heuristically rebalances RRF weights by query shape, with no LLM call on the search hot path (#24)
- **Optional OpenTelemetry tracing** — `maybe_span()` instruments tool calls, sync cycles, and folder-watcher scans; zero-cost and zero-dependency unless explicitly enabled (#25)
- **Storage-interface documentation** — `storage_interfaces.py` documents the entity-graph and vector-search operations as `Protocol`s, verified against the real SQLite implementation via mypy (#26)
- **Alternative hub deploy targets** — Docker Compose, Fly.io, and Railway templates alongside the existing Podman quadlet setup (#26)
- **Published OpenAPI spec** — [`docs/openapi.yaml`](docs/openapi.yaml) covers the full REST API, so a client SDK can be generated in any language (#26)

### Improvements

- `benchmarks/RESULTS.md` gains an honest comparison section explaining why cognee's published BEAM figures aren't directly comparable to remind_me's LongMemEval-S numbers, plus a new weekly non-blocking CI benchmark smoke check (#25)
- Documented explicit scope decisions for multimodal ingestion and multi-tenant/cross-agent isolation — both evaluated and deferred by design, not overlooked (#26)

Tool count: 35 → 41. Full detail per phase is in the [README Changelog](README.md#changelog); complete diffs are in PRs #19–#27.

## v1.0.0

Initial tagged baseline: hybrid FTS5 + semantic search with RRF rank fusion, ACT-R vitality/decay, structured subject/predicate/object triples and entity graph (FT-04), chat/document import (FT-02) with folder watching (FT-03), JSON/JSONL export (FT-01), the LLM Wiki (FT-08), distributed sync (Postgres hub + peer-to-peer over Tailscale), a dashboard UI + REST API, and remote MCP connector support (FT-05/FT-07).
