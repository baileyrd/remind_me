# Release Notes

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
