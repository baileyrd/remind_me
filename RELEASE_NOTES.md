# Release Notes

## v1.16.0 — 2026-07-21

Closes a silent-degradation gap flagged in the application capability review: changing `REMIND_ME_EMBEDDING_MODEL`/`REMIND_ME_EMBEDDING_DIM`/`REMIND_ME_EMBEDDING_BACKEND` required a manual `remind_me_reindex`, and there was no stored record of which model actually produced the vectors currently in the store — a forgotten reindex after a model change meant KNN silently ran against vectors from a different model's embedding space, producing garbage nearest-neighbor results with no error at all.

### New Features

- **Embedding-model versioning** — a new `embedding_meta` table (local-only, not synced — vectors themselves are never synced either) records the model/dimension/backend that produced the vectors currently stored in `memories_vec`/`vec_chunks`, written after every successful (re-)embed rather than merely inferred from the running config.
- **Automatic stale-vector clearing** — every startup compares the recorded model/dim/backend against the current config. On a mismatch, `memories_vec`/`vec_chunks` (and the on-disk ANN index, if present) are cleared automatically, and `memories_vec` is recreated at the new dimension if it changed — every memory then falls through to the existing "missing embeddings" path `remind_me_reindex`/`remind_me_server_status` already handle, rather than silently continuing to serve dimension- or model-mismatched results.
- **`remind_me_server_status`** now reports an explicit "Embedding model changed" warning (old vs. new model/dim/backend) distinct from the generic "some memories aren't embedded yet" message, so the cause is clear at a glance.
- Deliberately scoped to detect-and-clear-and-warn rather than an automatic background re-embed at startup — an unconditional background reindex thread on every server start with a pending mismatch would run inside tests and quick CLI invocations too, for a potentially expensive operation the existing `remind_me_reindex` tool already does deliberately, on request.

## v1.15.0 — 2026-07-21

Closes a data-safety gap flagged in the application capability review: there was no backup command anywhere in the app, and schema migrations ran with no snapshot or safety net — a failed or buggy migration against the single SQLite file holding someone's entire memory store had no way back short of a manual file copy the user had to remember to make themselves.

### New Features

- **`remind_me_backup` MCP tool** — creates an on-demand backup using SQLite's WAL-safe `Connection.backup()` API (not a raw file copy, which could read a torn or partially-checkpointed page while the WAL is mid-write). Backups are written under `MEMORY_DIR/backups/`; `remind_me_server_status` now reports the current backup count and the most recent backup's timestamp.
- **Pre-migration snapshot guard** — `_migrate_schema()` now snapshots the database before running any pending migration, so a migration that fails outright, or completes but is semantically wrong, can be rolled back by restoring the snapshot. Skipped for a brand-new, empty database (nothing to protect yet); snapshot failure (e.g. disk full) is logged and never blocks the migration itself.
- **Automatic retention** — only the most recent `REMIND_ME_BACKUP_RETENTION_COUNT` backups (default 10, covering both manual and pre-migration snapshots) are kept; older ones are pruned after each new backup.

## v1.14.0 — 2026-07-21

Closes a dashboard-usability gap flagged in the application capability review: `api_search` (and, before this, the general listing routes) returned a flat, capped list with no `offset`/`total`/`has_more` fields, so a dashboard or external client had no way to page through results beyond the cap. Separately, there was no bulk delete/tag/reclassify REST endpoint despite the equivalent batch MCP tools already existing.

### New Features

- **Search pagination** — `GET /api/memories/search` gains an `offset` query parameter and now returns the standard pagination envelope (`total`, `count`, `offset`, `limit`, `has_more`) that `GET /api/memories` already had, including on the `entity:`-not-found early-return path.
- **Bulk REST endpoints** — `POST /api/memories/bulk/delete`, `POST /api/memories/bulk/tag`, `POST /api/memories/bulk/reclassify`. Each takes an explicit id list (capped at 200 per request) rather than a filter — a deliberate scope choice: a dashboard selects a batch from a list/search result, then acts on exactly that selection, rather than a filter silently matching more than intended with no preview step.
  - `bulk/delete` applies the exact same per-memory logic as `DELETE /api/memories/{id}` (chunk vector + ANN cleanup, `memory_entities` cleanup, soft-delete when sync is configured) to each id independently.
  - `bulk/tag` supports `add` (default, union), `remove`, and `set` (replace wholesale) modes.
  - `bulk/reclassify` mirrors the `remind_me_reclassify` MCP tool exactly: sets each memory's `memory_type` and its matching `decay_rate`.
  - Every endpoint reports per-id success/failure (`not_found` alongside `deleted`/`updated`) instead of failing the whole batch on one bad id.
- `docs/openapi.yaml` updated with the new routes and pagination fields.

## v1.13.0 — 2026-07-21

Closes a multi-device data-loss gap flagged in the application capability review: `_upsert_one` (`sync.py`) overwrote *every* column on last-write-wins conflict resolution, so if two devices edited different fields of the same memory (one adds a tag, another edits the content) between sync cycles, whichever write arrived second silently clobbered the other's change entirely — not just the conflicting field. Entities already had union-merge semantics for aliases; memories didn't have the equivalent.

### New Features

- **Field-level conflict merge for memory sync** — `_upsert_one` now field-level merges `tags` and `metadata` regardless of which side wins last-write-wins on `updated_at`, falling back to whole-row LWW only for genuinely conflicting scalar fields like `content`:
  - `tags`: union-merge, dedup, order-preserving (local first) — identical semantics to the existing entity alias merge (`_upsert_entity_one`).
  - `metadata`: shallow, per-key merge. Both sides' keys are kept; on an actual key collision, the LWW winner's value takes precedence. Deliberately shallow (not recursive) — memory metadata is typically flat per-import bookkeeping, not nested structured data.
  - A record that loses LWW on `content`/other scalar fields still gets its tags/metadata folded into the local row via a merge-only `UPDATE` that deliberately does **not** bump `updated_at` (mirrors the entity alias-fill precedent — the contributing peer's own outbox row already propagates its side of the merge, so bumping would only cause churn) and does not trigger a needless re-embed (content is unchanged).
  - Applies uniformly to both hub-pull and peer-pull, since both share the same client-side `sync.py` upsert path.
  - The hub's own Postgres storage (`hub/main.py`) still does whole-row LWW for now — an explicit, documented scope decision, not an oversight: extending the merge there needs a live Postgres to test against at all (`hub/e2e_test.py` is explicitly outside the pytest suite), and the remaining gap is narrower than the general case — specifically "two pushes racing at the hub before either side pulls."

## v1.12.0 — 2026-07-21

Closes a ranking gap flagged in the application capability review: every new memory started at a flat `base_weight=1.0` regardless of kind, so a throwaway aside ("it's raining today") competed evenly in ranking with a real decision ("we're migrating to Postgres") until feedback or access patterns accrued enough signal to differentiate them — and the highest-value memories (decisions) are exactly the ones a user is least likely to re-query immediately, so they'd lose the ranking race to frequently-hit trivia before feedback ever kicked in.

### New Features

- **Importance prior at write time** — a new `vitality.seed_base_weight(*, memory_type=None, source=None)` seeds `base_weight` from a small lookup table (`BASE_WEIGHT_TYPE_PRIORS`, `BASE_WEIGHT_SOURCE_PRIORS`) instead of the flat 1.0 default:
  - `remind_me_decompose` already classifies each fact's `memory_type` at write time, so it seeds directly from that (`decision` 1.3x, `blocker` 1.2x, `fact`/`insight` 1.15x, `preference` 1.1x, `learning` 1.05x, `action_item`/`unclassified` at the flat default).
  - `remind_me_add` doesn't have a `memory_type` yet (set later by `remind_me_reclassify`), so it seeds from `source` instead — `manual` keeps the flat default; `chat_import`/`document_import`/`webhook` start slightly lower (0.85–0.9x), since raw imports are unreviewed and often noisy.
  - A fresh memory's `vitality` is set to match its seeded `base_weight` exactly (the ACT-R formula reduces to `vitality == base_weight` when `access_count=0` and `days_since_last_access=0`) — previously it defaulted to a hardcoded 1.0 independent of `base_weight`, which would have been silently inconsistent once seeding was added.
  - Purely additive: an unrecognized or absent source, or `memory_type="unclassified"`, still falls through to the original flat 1.0 default, so this changes nothing for content that predates the feature.
  - Deliberately scoped to the two write paths above for now — the chat/document importer's bulk INSERT, `mempalace`/`dbs` imports, `remind_me_normalize_apply`, and the dashboard REST API's `POST /api/memories` still use the flat default; an explicit, documented scope decision (see README), not an oversight.

## v1.11.0 — 2026-07-21

Closes a vault-hygiene gap flagged in the application capability review: `merge_cluster` (`consolidation.py`) unioned raw content lines from clustered memories rather than summarizing them, so merged memories grew unbounded and stayed verbose instead of becoming genuinely consolidated. Its clustering step was also a Python-level O(n²) double loop, worth capping regardless of the summarization fix.

### New Features

- **Summarization instead of concatenation** — `remind_me_consolidate`'s auto-merge (`dry_run=False`) now requires an LLM-authored `summaries` entry (`{canonical_id: summary}`) per cluster, produced client-side after reviewing a `dry_run=True` report — routing consolidation through the same client-side-LLM pattern already used by `remind_me_decompose`/`remind_me_normalize_apply`, rather than a server-side heuristic. A found cluster with no matching entry in `summaries` is skipped and listed in the response's `skipped_no_summary`, not silently merged with a raw concatenation. `merge_cluster` gained an optional `summary` keyword parameter: when given, it replaces `merged_content` entirely; when omitted, it falls back to the original deduplicated-line-union, preserving exact behavior for callers with no LLM in the loop (tests, benchmarks).
- **Bounded, vectorized clustering** — `find_clusters`'s O(n²) similarity-threshold comparison is now a single vectorized numpy operation (`np.triu_indices` + boolean masking) instead of a Python-level double loop; only pairs that actually clear the threshold cost a Python `union()` call. A new `REMIND_ME_CONSOLIDATE_MAX_CANDIDATES` (default 1500) hard-caps the candidate pool per call — `remind_me_consolidate`'s own `limit` (max 5000) doesn't alone bound the O(n²) memory/comparison cost — so a large vault degrades gracefully (a logged, non-silent truncation) instead of an unbounded comparison.

## v1.10.0 — 2026-07-21

Closes the biggest gap in the feedback loop flagged in the application capability review: `record_feedback` (`vitality.py`) always adjusted `base_weight` globally, silently discarding `FeedbackInput`'s `query` field — a memory marked unhelpful for "what's my favorite editor" got demoted for every future query, including an unrelated "what IDE did I mention last year."

### New Features

- **Query-contextual feedback** — `remind_me_feedback` now has two modes, selected by whether `query` is given:
  - **No `query`** (back-compat, unchanged): the original global `base_weight` mutation.
  - **With `query`**: query-contextual instead. The event is logged (memory, query, normalized query-token set, signal, magnitude) to a new `memory_feedback` table (schema v17) rather than touching `base_weight`/vitality. At ranking time, a new `vitality.apply_feedback_adjustment` (wired into `memory_search` right before reranking, mirroring `maybe_rerank`'s position in the pipeline) compares the current query against every stored feedback query for each candidate memory using Jaccard token-overlap similarity — no embedder dependency, works identically with or without semantic search configured — and nudges `_rrf_score` by up to ±40% (`FEEDBACK_ADJUSTMENT_CAP`) for matches above `FEEDBACK_SIMILARITY_THRESHOLD` (0.3). A memory with no matching feedback is completely unaffected.
  - `memory_delete` now also cleans up a memory's `memory_feedback` rows, mirroring the existing `memory_entities` cleanup.
  - Purely local bookkeeping: no sync outbox trigger, same explicit scope decision as `dbs_imports`/`mempalace_imports` — feedback given on one device doesn't (yet) propagate to others.

## v1.9.0 — 2026-07-21

Closes a query-routing gap flagged in the application capability review: `choose_rrf_weights` (the `strategy="auto"` heuristic router) routed purely on word count, `?`, and quoted phrases, with no awareness of temporal expressions — even though `temporal-reasoning` is one of the two weakest query categories documented in `benchmarks/RESULTS.md`.

### New Features

- **Temporal-expression query routing** — a new `_looks_temporal_shaped` detector recognizes temporal expressions ("before I moved", "last summer", "when I lived in Seattle", a bare 4-digit year) and boosts `w_recency` by `_TEMPORAL_RECENCY_MULTIPLIER` (1.5x) on top of whichever keyword/semantic profile the query's shape already resolved to. Composes rather than replaces: a temporal query gets the recency boost whether it's also short/keyword-shaped or long/semantic-shaped, and a profile that's already zeroed `w_recency` (e.g. `--rrf-profile semantic`) stays zeroed (`0 * 1.5 == 0`). Deliberately excludes "may" from the recognized month names, since as a modal auxiliary verb it's a disproportionate false-positive source. Always active under `strategy="auto"` — no separate env var or toggle, matching the existing keyword/semantic shape heuristics.
- `benchmarks/before_after.py` gains `--compare temporal` for isolated A/B measurement of the temporal-detection effect against `RESULTS.md`'s `temporal-reasoning` category, independent of the `strategy="auto"` routing it composes with.

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
