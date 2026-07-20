# Cognee Capability Review — 2026-07-20

Comparison of [topoteretes/cognee](https://github.com/topoteretes/cognee) against
`remind_me`'s current feature set, looking for capabilities remind_me may be
missing.

## What remind_me already covers well

Hybrid FTS5+vector search with RRF fusion, an entity/SPO knowledge graph, LLM
Wiki synthesis, ACT-R vitality decay, consolidation/dedup, multi-machine sync
(hub + P2P), a dashboard, and a hardened OAuth remote connector. This is
already a substantial superset of what most personal-memory MCP servers do.

## Capabilities cognee has that remind_me lacks

1. **Pluggable graph/vector backends** — cognee lets you swap in Neo4j,
   Neptune, Kuzudb (graph) or Qdrant, Weaviate, Milvus, ChromaDB, LanceDB,
   pgvector (vector). remind_me is hard-wired to SQLite + sqlite-vec. Fine for
   a single-user local tool, but there's no path to a real graph database for
   anyone who outgrows SQLite adjacency tables.

2. **True multi-hop graph traversal** — remind_me's entity graph only does
   1-hop expansion (`expand_entities`, capped at 5 related memories). cognee
   does genuine graph-reasoning traversal (multi-hop relationship chains),
   which matters for questions like "who introduced me to the person who
   recommended this tool."

3. **Auto-routing retrieval strategy** — cognee picks vector vs. graph vs.
   hybrid automatically per query. remind_me's RRF fusion always blends the
   same four signals (keyword/semantic/recency/vitality) with static
   configured weights — there's no strategy selection based on query shape.

4. **Multimodal ingestion** — cognee ingests non-text data (images, etc.)
   into the same graph. remind_me is text-only (chat exports, Markdown, plain
   text, JSON/JSONL).

5. **Explicit feedback/improve loop** — cognee's "improve" primitive lets
   agents mark outcomes so future retrieval avoids repeating mistakes.
   remind_me's vitality model reinforces on *access*, not on *outcome* —
   there's no "this retrieved memory was wrong/unhelpful" signal feeding back
   into ranking.

6. **Multi-tenant / cross-agent isolation** — cognee supports per-user/tenant
   isolation with audit trails so multiple agents share one "company brain"
   safely. remind_me is explicitly single-owner (one OAuth owner token, one
   SQLite file); there's no concept of scoped users sharing a store.

7. **Client SDKs beyond MCP** — cognee ships a TypeScript and Rust client for
   embedding directly into apps. remind_me is MCP-only — nothing to import as
   a library from other code.

8. **Cloud/managed & serverless deploy targets** — cognee has a hosted
   Cognee Cloud plus one-click templates for Modal/Railway/Fly.io/Render.
   remind_me's deployment story is self-hosted only (Podman quadlets on
   Fedora, Tailscale tunnels).

9. **Observability (OTEL)** — cognee integrates an OTEL collector. remind_me
   has status/health tools but no tracing/metrics export.

10. **Published benchmark comparison** — cognee reports BEAM benchmark scores
    (SOTA claims at 100K/10M token context). remind_me has its own
    LongMemEval harness (`benchmarks/`) but nothing published against a
    shared standard for external comparison.

## Likely worth pursuing

Given remind_me's design center (single-user, local-first, MCP-native), the
highest-value items are **#3 (auto-routing retrieval)** and **#5
(outcome-based feedback loop)** — both extend the existing RRF/vitality
machinery rather than requiring new infrastructure. #1/#2 (pluggable graph
DBs, multi-hop traversal) matter mainly if the entity graph grows past what
SQLite adjacency handles well. #6/#7/#8 (multi-tenant, SDKs, cloud hosting)
are architecture changes that only make sense if remind_me's scope shifts
from "personal memory for Claude clients" toward "shared memory infra for
multiple agents/apps" — worth deciding on that direction before investing
there.
