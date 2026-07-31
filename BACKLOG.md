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
| SY-10 | med | done | Tombstone deletes: a local delete used not to propagate — the outbox has INSERT/UPDATE triggers only, so a hard `DELETE` produced no outbox row and the record survived on the hub and every other node, resurfacing on the next pull. **Resolved by migration v15→v16 (gap #11) rather than the delete trigger sketched here**: a `deleted_at` tombstone column turns deletion into an UPDATE, which rides the existing `memories_outbox_au` trigger, so no new trigger or wire-format change was needed. Reads filter `deleted_at IS NULL`; `sync._compact_tombstones` hard-deletes past `TOMBSTONE_RETENTION_DAYS`. `memory_delete` keeps a plain hard-delete fast path when sync is disabled, since there is nothing to propagate to | — |
| SY-11 | med | done | Batch `_embed_and_store_rows` by `EMBED_BATCH_SIZE` in the sync apply path: the initial bulk hub pull flattened the whole pulled batch into one embed call and one transaction. **Resolved by commit `1493d4e` (#68) the opposite way to what this row proposed**: rather than pre-slicing at each call site, the batching loop moved *inside* `_embed_and_store_rows`, making it the single source of truth so every caller (reindex, file import, mempalace/dbs import, sync's pulled-record embedding) gets the bound for free and a new caller cannot forget it. Both the `embed()` call and the transaction are now bounded, layered under `EMBED_FORWARD_BATCH`'s forward-pass ceiling. Regression test: `test_sync_style_large_unbatched_pull_is_batched_internally` in `tests/test_chunking.py`, which simulates sync's exact call shape | [#16](https://github.com/baileyrd/remind_me/issues/16) |
| SY-12 | high | done | `remind_me_sync_status` tool: sync was the only subsystem with no status surface (watcher and webhook both have one), so diagnosing it meant shell access and hand-written SQL against `sync_outbox`. Reports node/hub config, the outbox trigger gate, per-remote push/pull watermarks and last error, tombstone counts, and — the load-bearing part — a **drain-rate verdict** (`draining`/`stalled`/`growing`/`idle`) from a baseline persisted in `sync_flags`, since a bare pending count can't distinguish a healthy backlog from a wedged push | [#88](https://github.com/baileyrd/remind_me/issues/88) |
| SY-13 | med | done | Hub `GET /stats` (auth-gated): the hub exposed only `/health` plus five `/sync/*` routes, so "how many records do you hold?" was unanswerable over its own API and reconciliation required `psql` inside the Postgres container. Returns totals, tombstones, `by_origin_node` (hub-only column, observable nowhere else), `by_category`, and entity/link/relation counts. `/health` stays unauthenticated and aggregate-free so deploy healthchecks stay cheap | [#89](https://github.com/baileyrd/remind_me/issues/89) |
| SY-14 | med | done | `remind_me_sync_reconcile`: diffs a node against the hub's `/stats` (SY-13) and returns per-category drift with a verdict — `in-sync` / `pull-lag` / `node-ahead` (pushes aren't landing) / `fault`. The encoded judgment is the deliverable; raw counts already existed but the benign-vs-fault distinction is a sign change that's easy to miss by eye. **Deviates from the issue's sketch**: rather than classifying by "bulk/immutable categories" (which would need a hardcoded list of categories users are free to write to), hub-ahead drift is judged by *pull freshness* — recent successful pull means lag, stale or never means the pull isn't running. Same numbers, different evidence | [#90](https://github.com/baileyrd/remind_me/issues/90) |
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

## Wave 4 — sync observability correctness

Found 2026-07-31 while diagnosing an apparent hub outage on node
`baileyai-connector` that turned out to be no outage at all. `remind_me_sync_status`
reported `drain: stalled`, 57,249 pending, 0 sent, and `last_push: 1970-01-01`
on every remote — while the hub was reachable and pulls were demonstrably
landing. Every alarm signal was an instrumentation defect. The cost is real
even though no data was at risk: the status surfaces added by SY-12/SY-14 exist
precisely to answer "is sync healthy?", and they currently answer "no" on a
healthy node, which trains the reader to ignore them.

| ID | P | Status | Item | Review ref |
|----|---|--------|------|------------|
| SY-15 | high | todo | `sync_log.last_push` is never written by any code path — the column is declared with an epoch default (`db.py:547`), read back by `remind_me_sync_status` (`sync.py:1176`), and written nowhere in the repo. Both `sync_log` upserts (`sync.py:279`, `:370`) set `last_pull`/`last_pull_id` only; `_push_outbox` records success in `sync_sends` and never touches `sync_log`. So `last_push` is structurally incapable of being anything but `1970-01-01`, and it reads as "this node has never once pushed" — the single most alarming thing the status tool can say. `ever_contacted` (`sync.py:1180`) is derived from it and inherits the defect. Fix under SY-18 | [#102](https://github.com/baileyrd/remind_me/issues/102) |
| SY-16 | high | todo | Outbox `pending`/`sent` counters measure echo suppression, not push progress, and the `drain` verdict is built on them. A successful push inserts into `sync_sends` (`sync.py:200-204`) and never sets `sync_outbox.sent_at`; the only writers of `sent_at` are the four echo-suppression paths (`sync.py:562`, `:738`, `:772`, `:815`), as the comment at `:919` states outright — "echo-suppressed rows (`sent_at != ''`) are never pushed". But `pending` counts `sent_at = ''` (`:1152`) and `sent` counts `sent_at != ''` (`:1155`). So `pending` is really "rows originating locally" and `sent` is really "rows pulled from a remote", and neither moves when a push succeeds — locally-originated rows only leave via retention pruning (`:931`). The drain sampler (`:1426`) reads the same column, so `verdict: stalled` is the permanent steady state of a healthy node that writes its own memories. Recompute both against `sync_sends` per remote (`pending` = rows not yet acked by *every* configured remote), and re-sample drain on that | [#103](https://github.com/baileyrd/remind_me/issues/103) |
| SY-17 | med | todo | `last_pull` is a content watermark presented as a contact time. `_pull_remote` sets it to the greatest `(updated_at, id)` of the records received (`sync.py:264-284`), not to the wall-clock moment of the pull — so a remote that is contacted every 60s but has published nothing new for a week shows a week-old `last_pull`. Two consequences: (a) `remind_me_sync_status` invites the reader to conclude the pull is dead when it is merely quiet; (b) `_verdict` in `remind_me_sync_reconcile` compares that watermark against wall-clock with a grace window (`sync.py:1300-1320`) and returns `fault` — "the pull is not running" — for a perfectly healthy quiet remote. Note the interaction with SY-16: an idle-but-connected node can report `stalled` *and* `fault` simultaneously while nothing is wrong. Fix under SY-18 | [#104](https://github.com/baileyrd/remind_me/issues/104) |
| SY-18 | med | todo | **Schema: separate the sync cursor from the liveness clock.** The root cause behind SY-15 and SY-17 is that `sync_log` has one pair of columns doing two unrelated jobs — resume-from-here (a position in the remote's record stream) and is-this-remote-alive (a wall-clock heartbeat) — and only the first is maintained. Split them: keep `last_pull`/`last_pull_id` as the cursor but rename to `pull_cursor_ts`/`pull_cursor_id` so the name stops implying a timestamp-of-event; add `last_pull_at`, `last_push_at`, and `last_attempt_at` as true UTC wall-clock stamps written on every successful pull, successful push, and attempt respectively. `last_attempt_at` is the one that closes the diagnostic gap: with `last_error` alone, "never tried" and "tried and it worked" are indistinguishable from "tried and failed silently". Migration adds columns with epoch defaults (no backfill possible — the history was never recorded). Once this lands, every verdict in `_verdict` and `drain` should read the wall-clock columns, and `ever_contacted` becomes meaningful | [#105](https://github.com/baileyrd/remind_me/issues/105) |

## Features

| ID | P | Status | Item | Review ref |
|----|---|--------|------|------------|
| FT-01 | med | done | Govern — data export: `export_memories` MCP tool (plus HTTP API endpoint) that dumps all memories to JSON/JSONL in an importer-compatible format, enabling backup and round-trip migration between machines | — |
| FT-02 | med | done | Collect — generic document ingestion: extend the importer beyond chat exports to plain Markdown, text, and notes files (per-file/per-section chunking instead of per-message) | — |
| FT-03 | med | done | Collect — source connectors: watch a configured notes/docs folder and auto-ingest new or changed files (reuse import dedup-by-hash), as a path toward email/app connectors | — |
| FT-04 | med | done | Organize — entity & link extraction: during decomposition, extract entities and relations and store them as structured metadata/links between memories (lightweight knowledge-graph layer over SQLite) | — |
| FT-05 | med | done | Use — claude.ai web MCP support: expose the MCP server as a remote connector (Streamable HTTP transport, OAuth/bearer auth, public reachability e.g. Tailscale Funnel or tunnel) so claude.ai custom connectors can attach remind_me from the website | — |
| FT-06 | med | done | Govern — export the entity graph: include `entities` and `memory_entities` in `export_memories` / `GET /api/export` (with import-side restore for round-trip), so backups capture the full knowledge graph, not just memories | — |
| FT-07 | med | done | Use — OAuth for the remote connector: minimal single-user OAuth 2.1 authorization server (AS metadata, dynamic client registration, PKCE authorization-code flow, token issue/refresh/revoke) on the remote MCP mode, so claude.ai connects with real, revocable per-client auth instead of the secret-path URL | — |
| FT-08 | med | done | Synthesise — LLM Wiki layer (Karpathy pattern): a synthesis layer over the raw memory store. Plain markdown pages on disk are the source of truth (`REMIND_ME_WIKI_DIR`), with `[[wikilinks]]`/backlinks, an auto-generated `index.md`, an append-only `log.md`, and a seeded `SCHEMA.md` maintainer contract. DB (`wiki_pages`/`wiki_links`/`wiki_fts`) is a reconcile-from-files search cache (no sync triggers). Tools: `remind_me_wiki_write/read/list/search/load/delete` plus `remind_me_wiki_compile` (the two-phase synthesis workflow that distils pending raw memories into pages and advances a watermark); `wiki://schema` + `wiki://index` resources | — |
