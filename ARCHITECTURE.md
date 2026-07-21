# Architecture

## Overview

An MCP server giving Claude persistent, searchable long-term memory: hybrid
FTS5 + vector search with RRF rank fusion, ACT-R-style vitality/decay, a
structured entity knowledge graph, pluggable import connectors, distributed
sync across machines, and a dashboard UI + REST API. It is not a general
document store, not a pluggable-storage-backend framework, and not
multi-tenant — see [Non-goals](#non-goals).

## Boundaries

Domain logic (`remind_me_mcp/db.py`, `importer.py`, search/ranking) never
imports a specific storage or embedding backend directly — each seam is
documented as a `typing.Protocol` in `storage_interfaces.py`, verified
against the real SQLite implementation by mypy rather than by a runtime
`isinstance` check (there is deliberately only one production adapter today;
the Protocols exist to keep the *shape* of a replacement honest, not to
predict a second one).

| Port | Adapter(s) | Notes |
| ---- | ---------- | ----- |
| `EntityUpserter` / `MemoryEntityLinker` / `EntityRelationUpserter` / `EntityResolver` / `EntityProfileReader` | SQLite (`db.py`) | entity graph reads/writes (FT-04); no second implementation exists, Protocol exists for interface discipline |
| `VectorSearcher` / `ChunkEmbedder` / `ChunkBatchEmbedder` | SQLite + sqlite-vec, ONNX embedder | `vec_search_available()` gates on the `memories_vec` table actually existing, separately from whether the embedder loaded — the two can split if the native extension fails |
| `OrphanChunkPruner` | SQLite (`db.py`) | chunk lifecycle cleanup after a memory is deleted/superseded |
| Import connector (`register_connector`, `importer.py`) | `chat`, `document` (built-in), `mempalace` (`mempalace_import.py`), `dbs` (`dbs_import.py`) | kind-string registry, not a hardcoded dispatch; third-party modules register more without touching `importer.py` |
| Sync backend | Postgres hub, peer-to-peer over Tailscale | both drive the same wire format (JSON records tagged with a `record_type` discriminator) |

## Structure

Modular monolith — one Python package (`remind_me_mcp/`) exposing MCP tools,
a REST API, and a dashboard UI over one SQLite store, plus optional sidecar
processes (folder watcher, webhook ingest server, sync daemon) that all call
back into the same storage/import modules rather than duplicating logic.
Nothing here has hit a forcing function (independent scaling, a
team/language boundary, hard fault isolation) that would justify splitting a
component into its own service.

## Data flow

A typical write path: `remind_me_add`/`remind_me_import_*` → connector parse
(kind-specific) → `_ingest_parsed` (hash dedup, chunking, batched embedding)
→ SQLite (`memories` + entity graph tables) → optional sync fan-out to the
Postgres hub / peers. A typical read path: `remind_me_search` → `strategy`
picks RRF weights (auto/balanced/keyword_favored/semantic_favored) → FTS5 +
vector KNN candidates fused → vitality/decay-adjusted ranking → optional
neighbor-chunk expansion.

## Key decisions
See [docs/adr/](./docs/adr/) for the record of individual decisions and their tradeoffs.

## Non-goals

Explicitly out of scope, per the project's stated design (see README):
pluggable storage backends beyond SQLite, multimodal ingestion, and
multi-tenant/cross-agent isolation. dbs's plugin/connector model and
Playwright/yt-dlp-class dependency surface belong in a separate collection
pipeline (see `docs/dbs-integration-review-2026-07-21.md`), not inside this
server.
