# Improvement Backlog

Tracking document for the findings in [CODE_REVIEW.md](CODE_REVIEW.md)
(review of `45c414c`, 2026-06-10). Each item carries an ID used in commit
messages. Status: `todo` / `in-progress` / `done` / `wontfix`.

Workstreams: **CI** (pipeline/tooling), **DI** (data integrity & retrieval
correctness), **SY** (sync hardening), **SE** (security & server lifecycle),
**PF** (performance), **HY** (hygiene & refactoring), **FT** (new features,
not from the review).

## Wave 1 — CI honesty + data integrity

| ID | P | Status | Item | Review ref |
|----|---|--------|------|------------|
| CI-01 | high | done | Make the 80% coverage gate pass honestly: add tests for `__main__.py` CLI dispatch and `pid.py` lifecycle (currently 0%/33%) | §6 |
| CI-02 | high | done | Add mypy step to CI (config exists but never runs) | §6 |
| CI-03 | high | done | Add a no-extras CI leg (base install without `[semantic]`) | §6 |
| CI-04 | med | done | CI: install from `uv.lock`, enable uv caching, concurrency cancel-in-progress, stop double-running PRs | §6 |
| CI-05 | med | done | Add dev dependency group (`pytest`, `pytest-cov`, `pytest-asyncio`, `ruff`, `mypy`) to pyproject; CI installs from it | §6 |
| CI-06 | med | done | Move coverage threshold/config into pyproject; add `.coverage` to `.gitignore` | §6 |
| CI-07 | med | done | Deflake tests: stable digest seed for FakeEmbedder (`conftest.py:128`), replace `sleep(0.1)` waits with deterministic task awaiting | §6 |
| DI-01 | high | done | `memory_delete` must delete chunk vectors (`_delete_chunks`); reindex must prune orphaned `vec_chunks` rows | §1.2 |
| DI-02 | high | done | Filter `superseded_by IS NULL` in both FTS and semantic search tiers | §1.3 |
| DI-03 | high | done | Push category/tag/dormant filters into SQL before LIMIT (both `memory_search` and `api_search`) | §1.4 |
| DI-04 | high | done | Wire real elapsed-days vitality decay at query/report time; fix vitality report buckets (open-ended top bucket) | §1.1 |
| DI-05 | med | done | RRF dedup: merge dicts so hybrid hits keep `semantic_distance` | §5 |
| DI-06 | med | done | Consolidation: infer embedding dim from blob length instead of hardcoded 384 | §5 |
| DI-07 | med | done | Rerank a 3–5× candidate pool before truncating to `limit` | §5 |
| DI-08 | med | done | Gate HyDE expansion on embedder availability; cache by query | §5 |

## Wave 2 — sync hardening + security

| ID | P | Status | Item | Review ref |
|----|---|--------|------|------------|
| SY-01 | high | done | Test coverage for `sync.py` and `peer_server.py` (push/pull via MockTransport, upsert conflict cases, peer auth) — written first, before behavior changes | §2 |
| SY-02 | high | done | Per-remote outbox send tracking; only mark records the remote actually accepted | §2 |
| SY-03 | high | done | Full-column `_upsert_records` with per-record try/rollback and key validation | §2 |
| SY-04 | high | done | Keyset pagination `(updated_at, id)` with drain loop for pulls | §2 |
| SY-05 | med | done | Echo suppression marks only the exact outbox rowids created by the upsert | §2 |
| SY-06 | med | done | Embed pulled records on ingest (semantic search visibility without manual reindex) | §2 |
| SY-07 | med | done | Prune sent outbox rows on a retention window; don't accumulate when sync disabled | §2 |
| SY-08 | med | done | One canonical UTC ISO timestamp format (triggers vs `_now_iso()` vs hub) | §2 |
| SY-09 | med | done | Peer server hardening: ThreadingHTTPServer, body/limit caps, JSON error handling, configurable bind, `hmac.compare_digest`, honor `STATIC_PEERS`/`TAILSCALE_SOCKET`, index on `memories(updated_at)` | §2 |
| SE-01 | high | done | Dashboard API: require/generate API key by default; reject non-JSON Content-Type on mutating routes (CSRF) | §3 |
| SE-02 | high | done | Enforce `IMPORT_ROOTS` in MCP import tool inputs (parity with HTTP API) | §3 |
| SE-03 | high | done | Fix combined-mode lifespan loss; fix `FastMCP.run()` host/port kwargs | §1.5 |
| SE-04 | med | done | Unauthenticated `/health` endpoint; pid health check works with auth enabled | §3 |
| SE-05 | med | done | `hmac.compare_digest` for all secret comparisons; share one bearer middleware | §3 |
| SE-06 | med | done | Opt-out env var for startup `git fetch` / self-update | §3 |
| SE-07 | med | done | DB shutdown: per-thread connection close (or `check_same_thread=False`), lifespan `try/finally`, stop sync/peer threads before close | §7 |

## Wave 3 — performance + hygiene

| ID | P | Status | Item | Review ref |
|----|---|--------|------|------------|
| PF-01 | high | done | Cache embedder availability/failure (TTL); never probe Ollama/HF synchronously in async handlers | §4 |
| PF-02 | med | done | Batch `record_access` into one UPDATE/transaction per search | §4 |
| PF-03 | med | done | Import: dedup by hash before parsing; embed in batches outside `_import_lock` | §4 |
| PF-04 | med | done | Hold references to fire-and-forget `asyncio.create_task` tasks | §4 |
| PF-05 | med | done | `db.rollback()` on failure paths in `_embed_and_store_rows` | §4 |
| PF-06 | low | done | `asyncio.to_thread` for DB work in API handlers | §4 |
| HY-01 | med | done | Remove root `remind_me_dashboard.jsx` duplicate, `remind_me_spec.docx`; untrack `.planning/` | §7 |
| HY-02 | med | done | Split `tools.py` into `tools/` package (search/crud/capture/lifecycle/admin); dedupe structured-path envelope logic | §7 |
| HY-03 | med | done | Generate outbox triggers from a single column list in `db.py` | §7 |
| HY-04 | low | done | Pin/vendor dashboard CDN assets (SRI at minimum) | §7 |
| HY-05 | low | done | Strip internal `_rrf_score`/`_keyword_rank` fields from JSON responses (or move under `debug_signals`) | §5 |
| HY-06 | low | done | Misc robustness: 400 on bad query params, guarded env parsing, no import-time `basicConfig`, longer memory IDs, empty-chunk guard in importer | §5, §7 |

## Features

| ID | P | Status | Item | Review ref |
|----|---|--------|------|------------|
| FT-01 | med | done | Govern — data export: `export_memories` MCP tool (plus HTTP API endpoint) that dumps all memories to JSON/JSONL in an importer-compatible format, enabling backup and round-trip migration between machines | — |
| FT-02 | med | done | Collect — generic document ingestion: extend the importer beyond chat exports to plain Markdown, text, and notes files (per-file/per-section chunking instead of per-message) | — |
| FT-03 | med | done | Collect — source connectors: watch a configured notes/docs folder and auto-ingest new or changed files (reuse import dedup-by-hash), as a path toward email/app connectors | — |
| FT-04 | med | done | Organize — entity & link extraction: during decomposition, extract entities and relations and store them as structured metadata/links between memories (lightweight knowledge-graph layer over SQLite) | — |
| FT-05 | med | done | Use — claude.ai web MCP support: expose the MCP server as a remote connector (Streamable HTTP transport, OAuth/bearer auth, public reachability e.g. Tailscale Funnel or tunnel) so claude.ai custom connectors can attach remind_me from the website | — |
| FT-06 | med | done | Govern — export the entity graph: include `entities` and `memory_entities` in `export_memories` / `GET /api/export` (with import-side restore for round-trip), so backups capture the full knowledge graph, not just memories | — |
| FT-07 | med | todo | Use — OAuth for the remote connector: minimal single-user OAuth 2.1 authorization server (AS metadata, dynamic client registration, PKCE authorization-code flow, token issue/refresh/revoke) on the remote MCP mode, so claude.ai connects with real, revocable per-client auth instead of the secret-path URL | — |
