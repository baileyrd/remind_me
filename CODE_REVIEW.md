# remind-me-mcp — Repository Review (2026-06-10)

Scope: full review of `remind_me_mcp/` source, `tests/`, CI, packaging, and repo
hygiene at commit `45c414c` (main). All high-severity findings were verified
directly against the code; line numbers refer to that commit.

## Executive summary

The project is in better shape than most at this stage: 447 tests pass in ~7s,
the test suite mostly tests real behavior against real SQLite/FTS5 (not mocks),
ruff is clean, and benchmark/eval scaffolding for retrieval quality exists.

The problems are concentrated, not diffuse:

1. **CI on `main` is red and would stay red.** The last run failed on lint
   errors (since fixed), but the current HEAD fails its own coverage gate:
   74.88% vs `--cov-fail-under=80`.
2. **The headline feature — multi-machine sync — is the least trustworthy code
   in the repo**: zero test coverage, and at least four independent ways it
   silently loses or corrupts data.
3. **Several advertised memory-lifecycle behaviors don't actually happen**:
   vitality never decays, deleted memories leave vectors behind,
   consolidated-away duplicates still appear in search results.
4. **The local HTTP surface is unauthenticated by default** and can be driven
   by any web page via CSRF, including file ingestion from `$HOME`.

---

## 1. Critical correctness bugs (verified)

### 1.1 Vitality never decays — the decay model is dead code
`compute_vitality` has exactly two call sites (`vitality.py:153`,
`tools.py:1865`) and **both pass `days_since_last_access=0.0`**. There is no
background sweep and no read-time recompute, so stored vitality is always
`base_weight * sqrt(access_count+1)` ≥ 1.0 and only ever increases.
Consequences:
- `is_dormant` (threshold 0.05) can never trigger; `status='dormant'` is
  unreachable; the search's `include_dormant` flag is inert.
- The RRF `vitality_rank` signal degenerates into an access-count rank.
- The vitality report's top bucket (`>= 0.75 AND < 1.01`, `tools.py:1233`)
  loses every accessed memory (one access → vitality ≈ 1.41).

**Fix:** recompute vitality at query time (or periodic sweep) using
`(now - accessed_at).days`. Until then, default `RRF_W_VITALITY` to 0.

### 1.2 `memory_delete` orphans chunk vectors; rowid reuse corrupts search
`tools.py:717` runs only `DELETE FROM memories`. FTS and tags are cleaned by
triggers, but `vec_chunks`/`memories_vec` are not — `_delete_chunks`
(`db.py:854`) is never called on delete. Effects:
- Orphaned vectors consume KNN slots, so semantic search returns fewer than
  `limit` results.
- SQLite reuses freed rowids: a new memory can inherit the deleted memory's
  embedding, and `remind_me_reindex` (`tools.py:1098`) sees the rowid already
  in `vec_chunks` and skips it — wrong search results **indefinitely**.

**Fix:** call `_delete_chunks` in `memory_delete`; make reindex prune
`vec_chunks` rows whose `memory_rowid` no longer exists.

### 1.3 Superseded memories still appear in search
Neither the FTS query (`tools.py:397`) nor `_semantic_search` (`db.py:1001`)
filters `superseded_by IS NULL` (only the structured-lookup path does,
`tools.py:129`). After `remind_me_consolidate` merges duplicates, the merged
members keep their FTS rows and vectors and come back in every hybrid search —
alongside the enlarged canonical. Consolidation therefore makes result quality
*worse*.

**Fix:** add `superseded_by IS NULL` to both retrieval tiers; drop or redirect
members' vectors on merge.

### 1.4 Filters applied after LIMIT — filtered searches miss real matches
`memory_search` fetches only `limit` (default 20) candidates per tier, then
applies category/tag/dormant/`min_vitality` filters in Python
(`tools.py:441-465`). A `category="X"` search can return zero results while
many matches exist past rank 20. The dashboard API has the identical bug in
`api_search` (`api.py:210-230`) — the same class of bug the code itself marks
as fixed in `api_list` ("DATA-02 fix").

**Fix:** push predicates into the SQL of both tiers, or over-fetch by a
multiple when filters are set.

### 1.5 Combined HTTP mode drops the MCP app's lifespan
`__main__.py:50-75`: mounting `mcp.streamable_http_app()` inside a fresh
`Starlette(routes=[Mount(...)])` with no lifespan means the mounted app's
lifespan never runs — and when `MCP_HTTP_SECRET` is set the code explicitly
rebuilds it as `Starlette(routes=list(mcp_http_app.routes))`, discarding the
lifespan again. The StreamableHTTP session manager, DB lifecycle, peer server,
and sync thread all start from that lifespan, so `/mcp` requests fail and sync
never starts in combined mode. Relatedly, standalone `--serve-mcp` passes
`host=`/`port=` kwargs to `FastMCP.run()` (`__main__.py:209`), which the MCP
SDK's `run()` does not accept — verify against the pinned SDK version.

**Fix:** give the combined app a lifespan that enters the sub-app's lifespan
(e.g. `lifespan=lambda app: mcp.session_manager.run()`), and wrap auth as pure
ASGI middleware instead of re-instantiating Starlette. Set
`mcp.settings.host/port` rather than passing kwargs to `run()`.

---

## 2. Sync: silently loses data in several independent ways

All of `sync.py` (166 stmts) and `peer_server.py` (77 stmts) have **0% test
coverage** — this is the riskiest code in the project (network + concurrency +
merge logic) and the README's first-line feature.

- **Outbox `sent_at` is global, not per-destination** (`sync.py:48-97`): the
  first successful push marks rows sent; every other hub/peer never receives
  them except via pull. And the whole batch is marked sent even when the
  remote reports `accepted < len(records)` — rejected records drop out of sync
  forever (verified at `sync.py:85-96`).
- **`_upsert_records` drops most columns** (`sync.py:148-212`): only the 10
  v2-era columns are written; `vitality`, `access_count`, `memory_type`,
  `subject/predicate/object`, `superseded_by`, etc. are present in outbox
  payloads (db.py v7 triggers) but discarded on receive. One malformed record
  also raises out of the loop mid-transaction with no rollback.
- **Pull watermark loses boundary ties** (`sync.py:111-137` +
  `peer_server.py:57-78`): server pages by `updated_at ASC LIMIT 500`, client
  resumes with strict `>` — records sharing the boundary timestamp beyond the
  page are skipped permanently (bulk imports create many identical
  timestamps). Only one page is pulled per cycle, with no drain loop.
- **Echo suppression can swallow genuine local edits** (`sync.py:206-209`):
  after upserting a remote record it marks *all* unsent outbox rows for that
  memory as sent — including a concurrent local edit's row (lost update).
- **Pulled records never get embeddings**: `_upsert_records` never calls the
  embed path, so synced memories are invisible to semantic search until a
  manual reindex.
- **`sync_outbox` grows forever** (`db.py:392-454`): triggers append full JSON
  copies unconditionally — even with sync disabled — and nothing prunes sent
  rows.
- **LWW compares heterogeneous timestamp strings** (`sync.py:187`): trigger
  rows use `datetime('now','utc')` (`YYYY-MM-DD HH:MM:SS`, and the `'utc'`
  modifier on `'now'` is documented-incorrect SQLite usage) while code uses
  `_now_iso()`; a hub emitting `Z`-suffixed timestamps breaks ordering
  silently.
- **`STATIC_PEERS` / `TAILSCALE_SOCKET` config is documented but dead**
  (`config.py:128-131` vs `sync.py:231`): the Tailscale socket path is
  hardcoded to the Linux location and `_discover_peers` never reads
  `STATIC_PEERS` — peer sync is non-functional on macOS/Windows despite the
  README.
- **`peer_server.py` weaknesses**: binds `0.0.0.0` on a single-threaded
  `HTTPServer` (one hung client blocks all sync), unbounded `Content-Length`
  read, `json.loads` with no error handling, uncapped `limit` param, secret
  compared with `==` instead of `hmac.compare_digest`, and no index on
  `memories(updated_at)` (full table scan per pull).

**Recommended approach:** before adding features, write the missing tests
(httpx `MockTransport` for push/pull, direct unit tests of `_upsert_records`
conflict cases, `TestClient`-style auth tests for the peer server), then fix:
per-remote send cursors, full-column upsert with per-record try/rollback,
keyset pagination `(updated_at, id)` with a drain loop, exact-rowid echo
suppression, embed-on-ingest, outbox pruning, and one canonical timestamp
format. Consider folding the peer endpooints into the existing Starlette app —
it removes the weakest server and the duplicated auth logic.

---

## 3. Security

- **Dashboard API unauthenticated by default + CSRF-able** (`api.py:104-118`,
  `config.py:72`): `API_KEY` defaults to `None`, disabling auth entirely. CORS
  only restricts reading responses — any web page can fire simple `POST`s at
  `http://127.0.0.1:5199/api/...` (Starlette parses JSON bodies regardless of
  Content-Type, so no preflight). With `IMPORT_ROOTS` defaulting to
  `[Path.home()]`, a drive-by page can make the server ingest arbitrary home
  files into memory — which is then fed to the model (prompt-injection vector).
  **Fix:** generate/require an API key by default, reject non-JSON
  Content-Type on mutating routes, shrink default import roots.
- **MCP-side imports skip the `IMPORT_ROOTS` check** (`models.py:193-235`):
  the HTTP API enforces SEC-02 path containment but `ChatImportInput` /
  `BulkImportDirInput` only validate existence — inconsistent boundary.
- **`MCP_HTTP_SECRET` auth has no tests**, and the ad-hoc `_BearerAuth` in
  `__main__.py:60-68` duplicates `BearerAuthMiddleware` and compares with `==`
  rather than `hmac.compare_digest`.
- **Self-update is remote code execution by design** (`updater.py:362-395`):
  every server start does a background `git fetch`, and the
  `remind_me_self_update` MCP tool runs `git pull` + `pip install` of whatever
  `origin/main` contains. Acceptable for a personal tool, but add an opt-out
  env var and consider pinned/signed tags.
- **Health check breaks when auth is on** (`pid.py:85-103`): it hits
  `/api/stats` with no bearer token, so with `REMIND_ME_API_KEY` set,
  `--status` reports "not running" and the already-running guard passes →
  port-bind crash and PID-file clobbering. Add an unauthenticated `/health`.

---

## 4. Performance

- **Synchronous network/CPU calls block the event loop**: every API handler
  does blocking SQLite work inline (`api.py:129-318`), and
  `tools.py:525` calls `_get_embedder()` synchronously in the async search
  handler — with the Ollama backend that's a blocking HTTP "ping" per search
  (`embeddings.py:283-290` re-probes availability on every call). ONNX load
  failures are never cached either (`embeddings.py:118-159`), so an offline
  machine re-attempts a HuggingFace download on every search. **Fix:** cache
  embedder availability/failure with a TTL; wrap DB work in
  `asyncio.to_thread`.
- **Import serializes everything under one lock** (`importer.py:304-336`):
  phase 2 holds `_import_lock` for the whole file and embeds one chunk at a
  time, defeating `IMPORT_CONCURRENCY=8` and ignoring the batched
  `_embed_and_store_rows` API. The lock's comment ("workers share the same DB
  connection") is wrong — connections are per-thread (`db.py:46-96`). Dedup by
  file hash also runs *after* full parse+chunk (`importer.py:258-310`).
- **`record_access` is N+1** (`tools.py:490-494`): 20 results → 20
  `asyncio.to_thread` hops and 20 commits per search. Batch into one UPDATE.
- **Fire-and-forget `asyncio.create_task` with no saved reference**
  (`tools.py:354, 494, 1611, 1892`): the event loop holds only weak refs;
  tasks can be GC'd mid-flight, silently dropping embeddings/access updates.
  Keep a task set with done-callback discard.
- **`_embed_and_store_rows` can commit a partial chunk deletion**
  (`db.py:894-918`): on embed failure the except path returns without
  rollback; the uncommitted DELETEs ride along with the next unrelated commit.

---

## 5. Retrieval quality

- **Reranker only shuffles the already-truncated head** (`tools.py:477-480`):
  `ranked[:limit]` happens *before* `maybe_rerank`, so the cross-encoder can
  never promote candidates beyond rank `limit`. Rerank a 3–5× pool, then cut.
- **HyDE runs even when it can't be used** (`tools.py:427`): `expand_query`
  (an Ollama generation, up to 15s) executes before checking embedder
  availability, and identical queries regenerate every time. Gate + LRU cache.
- **Hybrid hits lose semantic metadata** (`retrieval.py:95-101`): RRF dedup is
  first-writer-wins, so a memory in both lists keeps the FTS dict and drops
  `semantic_distance`. Merge the dicts instead.
- **Consolidation breaks on non-384-dim backends** (`consolidation.py:29`):
  `_bytes_to_vector` hardcodes `dim=384`; with Ollama's 768-dim
  `nomic-embed-text` it raises. Infer `dim = len(raw) // 4` like the dry-run
  path already does. Clustering also represents long memories by chunk 0 only
  and selects candidates with `LIMIT` but no `ORDER BY` (`tools.py:1737-1748`).
- Minor: internal `_rrf_score`/`_keyword_rank` fields leak into JSON responses
  (`tools.py:505-519`); chunker can emit empty chunks (`importer.py:85`);
  unclamped overlap in `next_start` (`embeddings.py:89`); token-budget
  estimate doesn't match either output format (`retrieval.py:212`).

---

## 6. Testing & CI

- **Coverage gate currently fails**: 74.88% vs 80% (`--cov-fail-under=80`),
  driven by `sync.py` (0%), `peer_server.py` (0%), `__main__.py` (0%). The
  last green-relevant signal on `main` is misleading — the May 22 run failed
  on lint, and the June 9 merge of PR #5 has no passing main run.
- **CI never runs mypy** despite a full `[tool.mypy]` config — dead config.
- **CI only tests with `[semantic]` extras installed**, yet the base install
  is the README's default path and the code has explicit fallback branches for
  missing extras. Add a no-extras job (vec tests already `importorskip`).
- **CI ignores the committed `uv.lock`** (`uv pip install` is unlocked), has
  no caching, no concurrency cancellation, double-runs PRs (`on: push` +
  `pull_request` with no branch filter), is Ubuntu-only despite documented
  macOS/Windows deployment, and never runs `uv build`.
- **No dev extras in pyproject**: pytest/ruff/mypy exist only as an inline CI
  string; contributors have no `pip install -e ".[dev]"` path.
- **Flakiness risks**: `test_tools.py` waits on fire-and-forget tasks with a
  bare `sleep(0.1)`; `conftest.py:128` seeds fake vectors with Python's salted
  `hash()` (varies per process — any future ordering assertion will flake).
  Use a stable digest.
- Coverage config lives only in the CI command line — move
  `--cov-fail-under` and a `[tool.coverage]` section into pyproject so local
  runs match CI. `.coverage` is also missing from `.gitignore`.

---

## 7. Repo hygiene & maintainability

- **`.planning/` (~1.4 MB, ~126 files — two-thirds of all tracked files)** are
  agent process artifacts (PLAN/SUMMARY/VERIFICATION), not project docs. Move
  a curated architecture doc into `docs/` and untrack the rest.
- **Two divergent 40 KB dashboards**: `remind_me_dashboard.jsx` (root) vs
  `remind_me_mcp/dashboard/App.jsx` differ by ~1200 lines. Delete or clearly
  demote the root copy. The served dashboard also loads React + Babel
  standalone from unpkg unpinned/no-SRI and transpiles JSX in-browser — a
  "local-first" tool that breaks offline; vendor or pre-build.
- **`remind_me_spec.docx`** — binary, executable bit set, undiffable; convert
  to Markdown or remove.
- **`tools.py` (1958 lines) needs splitting** along its existing section
  comments: `tools/search.py`, `crud.py`, `capture.py`, `lifecycle.py`,
  `admin.py`. Within `memory_search`, the structured path duplicates the
  envelope/record-access/no-results logic of the main path nearly verbatim
  (`tools.py:349-391` vs `487-522`).
- **`db.py` migrations restate full outbox triggers five times (~400 of 1100
  lines)** — generate the `json_object(...)` from a column list; the v7/v2
  column mismatch is exactly what produced the sync upsert asymmetry above.
- Misc: `_close_db()` can't close other threads' connections
  (`check_same_thread=True` + suppressed errors → fd leaks; `db.py:99-115`);
  lifespan `yield` not wrapped in `try/finally` (`server.py:58`); import-time
  side effects in `config.py` (`mkdir`, root-logger `basicConfig`, unguarded
  `int(env)`); 48-bit memory IDs are collision-prone across syncing nodes
  (`db.py:1056`); `int(params["limit"])` 500s on bad query strings
  (`api.py:182`).

---

## 8. Suggested priority order

1. **Make CI honest** (small, immediate): fix the coverage shortfall or scope
   the gate, add mypy + no-extras legs, use the lockfile. A red main hides
   every future regression.
2. **Data-integrity trio** (verified, user-visible): delete-orphans-vectors
   (1.2), superseded-in-results (1.3), filter-after-LIMIT (1.4, both MCP and
   API paths). All are small, testable fixes.
3. **Decide what vitality means** (1.1): wire real elapsed-days decay or
   remove the dormancy surface; either way the report buckets need fixing.
4. **Sync hardening behind tests** (section 2): test-first, then per-remote
   cursors, full-column upserts, keyset pagination, embed-on-ingest, pruning.
5. **Security defaults** (section 3): default-on API key, Content-Type checks,
   import-root enforcement in MCP tools, `/health` endpoint.
6. **Performance & structure** (sections 4, 7): availability caching, batched
   access recording, import parallelism, then the `tools.py`/`db.py` refactors.

## What's working well

Behavior-first tests against real SQLite/FTS5 with mocking confined to true
boundaries; rigorous `~/.remind-me` isolation in conftest; pure-function
vitality math with injected clocks; path-traversal tests on the import API;
honest inline comments about ranking trade-offs; env-tunable RRF weights; and
real eval scaffolding (`benchmarks/` with longmemeval + before/after metrics)
that most projects of this size never build.
