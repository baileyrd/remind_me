# Release Notes

## v1.21.0 — 2026-07-30

New capability: **`remind_me_undo_import`** — roll back a bulk import.

Imports are the one bulk write this server makes, and there was no bulk way to undo one. `remind_me_delete` takes a single id, which is unusable at import scale: a single mempalace run on a live node accounts for 47000 of 49513 memories (95%) and most of a 212 MB database. Removing that by hand meant either 47000 tool calls or raw SQL — and raw SQL is the trap, because it silently orphans everything derived.

### The tool

- **Targets a specific import, not a category.** Each import path records what it created: `mempalace_imports` and `dbs_imports` store a `memory_id` per row, while `chat_imports` keys on `import_id` which the importer stamps onto `memories.doc_id`. The tool follows those links rather than pattern-matching on `category`, which is only approximately right — category and source counts already disagree on this store (47000 vs 46879 + 118 + 3).
- **Dry run by default.** Reports exactly what would go and changes nothing until `dry_run=false`. Bulk deletion that propagates across every node should be opt-in, not the default path.
- **Scopable.** `import_id` narrows to one chat import, one dbs source, or a mempalace drawer prefix (so a single wing can be undone without naming every drawer). A scope that matches nothing removes nothing — a typo is inert, never a full-store wipe.
- **Resumable and bounded.** `limit` (default 500) caps one call so a large undo cannot exceed the MCP call timeout; callers loop until `remaining` is 0. Re-running after completion is a no-op.
- **Clears the import-tracking rows too.** This is the non-obvious part: import paths skip anything already recorded, so leaving those rows behind would make the same content permanently un-importable. For chat imports the tracking row is per-file, so it is dropped only once none of its chunks survive — a partially-drained import keeps its row and cannot be duplicated by a re-import.
- **Honest about disk.** On a sync-enabled node this is a soft delete: rows are tombstoned so the removal propagates to every other node, which means space is *not* reclaimed until compaction (`TOMBSTONE_RETENTION_DAYS`, default 180). The tool says so rather than implying the database shrinks.

### Refactor: one delete path instead of five

Deletion goes through `db._purge_memory`, extracted in this release. The steps are easy to get subtly wrong — chunk vectors, the ANN index entry, entity mention links, stored feedback, and the tombstone-vs-hard-delete split — and were copy-pasted across four call sites (the MCP delete tool, two dashboard REST routes, and sync's tombstone compaction). They had already drifted: **both REST routes skipped the `memory_feedback` cleanup** that the MCP tool performed, so deleting from the dashboard orphaned feedback rows. Extracting the helper fixes that inconsistency and means the new tool could not become a fifth divergent copy.

`soft` is an explicit argument rather than read from `config.SYNC_ENABLED` inside the helper, so each caller keeps its own patchable flag and the behaviour is visible at the call site.

Tests: 13 new cases covering dry-run inertness, tombstone vs hard delete, derived-row cleanup, batching and resumability, scoping, the un-importable-tracking-row trap, and that a typo'd scope matches nothing. Full suite 1514 passed / 17 skipped, coverage 90.56%, on all four legs.

### Corrected: the v1.20.0 note's claim about tag history

The v1.20.0 entry stated that `v1.1.0` was the repository's only tag and that 59 commits had landed since. Both were wrong, and the correction is marked inline there rather than silently rewritten.

There are **six** tags: `v1.0` and `v1.1` (2026-02-24), `v1.2` (2026-03-05), `v1.1.0` (2026-07-20), and `v1.20.0` / `v1.21.0` (2026-07-30). The real gap from `v1.1.0` was 62 commits.

The error came from running `git tag -l` in a clone that had fetched exactly one tag and reporting that as the repository's state; `git ls-remote --tags` shows all six. Worth recording because the wrong conclusion followed from it: "tagging was correct, then stopped" isn't the story. `v1.1` and `v1.2` both point at commits declaring `1.0.0`, so tag-vs-declared-version drift was the norm, not a recent lapse — `v1.20.0` and `v1.21.0` are the first tags since `v1.1.0` where the two actually agree.

Local state is not remote state. `git tag -l` answers "what has this clone fetched", not "what exists".

## v1.20.0 — 2026-07-30

Second stale-backlog correction (same shape as SY-10 in v1.19.6), plus a silent test-coverage bug found while verifying it.

### Fixed: 14 tests were never running in CI

Verifying SY-11 meant checking that its regression test actually passes — and it turned out never to run. `tests/test_chunking.py` had a **module-level** `pytest.importorskip("usearch")` sitting at line 403, ahead of the ANN section. That form executes at import time and skips the *entire module*, and since `usearch` is the `ann` extra — which neither CI leg installs (`semantic` does not imply it) — all **20** tests in the file were skipped in every CI run.

14 of those 20 test chunking and batching and need no `usearch` at all, including `test_sync_style_large_unbatched_pull_is_batched_internally` — the regression test for issue #16, i.e. the guard for the very invariant SY-11 is about. It has been silently inert.

The guard is now a `require_usearch` fixture applied to just the 6 ANN tests that need it. Result: **14 passed / 6 skipped** without the extra (previously 0 passed / 20 skipped), and 20 passed with it. The fixture docstring records why it must not go back to module scope.

Swept the rest of the suite for the same shape: `tests/test_ann_index.py`'s module-level guard is correct (all 13 of its tests genuinely need `usearch`) and `test_openapi_spec.py`'s guards are per-test. No other over-reach.

### Fixed: a latent cross-thread race in two directory-import tests

Making those 14 tests run shifted suite timing enough to expose a pre-existing flake, which failed one CI leg as:

```
FAILED tests/test_tools.py::test_import_directory
SystemError: <sqlite3.Connection object> returned NULL without setting an exception
```

`import_directory` fans out over `asyncio.to_thread` (`IMPORT_CONCURRENCY=8`), but `test_import_directory` used the `db_conn` fixture, which hands the *same* `sqlite3.Connection` object to every thread. SQLite does not serialize cross-thread `execute()`/`commit()` on one connection even with `check_same_thread=False` — the `db_conn_concurrent` fixture's own docstring documents this hazard, and `test_import_directory_concurrent` already existed specifically to avoid it. With only two files the race was narrow enough to pass almost always, which is exactly what made it a flake rather than a failure.

Both affected tests moved to `db_conn_concurrent`: `test_import_directory` (2 files) and `test_import_directory_mixed_chat_and_documents` (4 files, a *wider* fan-out that had not yet failed but carried the same defect). Verified with 12 consecutive stress runs. The single-file and empty-directory cases spawn no concurrent workers and are left on `db_conn`.

The race was pre-existing and independent of this release's other changes; the skip fix only altered timing enough to surface it.

### Corrected: BACKLOG SY-11 was stale

`BACKLOG.md` SY-11 was marked **todo** and proposed batching `_embed_and_store_rows` by `EMBED_BATCH_SIZE` at the sync apply path's call site. That shipped in commit `1493d4e` (#68) — but resolved the *opposite* way to what the row proposed: instead of pre-slicing at each call site, the batching loop moved **inside** `_embed_and_store_rows`, making it the single source of truth. Every caller (reindex, file import, mempalace/dbs import, sync's pulled-record embedding) now gets the bound for free, and a new caller cannot forget it. Both the `embed()` call and the surrounding transaction are bounded, layered beneath `EMBED_FORWARD_BATCH`'s forward-pass ceiling.

Implementing SY-11 as written would now make things worse — pre-slicing in `sync.py` would duplicate a guarantee the callee already makes, which is exactly what `_embed_and_store_rows`' docstring warns against.

Already covered by `test_sync_style_large_unbatched_pull_is_batched_internally` in `tests/test_chunking.py`, whose docstring cites issue #16 directly and reproduces sync's call shape (more rows than `EMBED_BATCH_SIZE` in a single call), asserting no `embed()` call exceeds the bound.

- **SY-11 marked done**, recording that it was closed the opposite way to the approach it sketched.
- With this, `BACKLOG.md` has **no `todo` rows remaining**.

Worth noting the pattern: SY-10 and SY-11 were both marked todo while the work had shipped, and in both cases the row's *premise* stayed technically true while ceasing to be a problem — which is why neither looked obviously stale. The `CONTRIBUTING.md` / PR-template guard added in v1.19.6 (update the BACKLOG row in the same PR, including when you solved it differently) exists for exactly this, and would have caught both.

Suite totals moved because of the skip fix, not because tests were added: 1484 → **1498 passed** on the semantic legs, and coverage 90.27% → **90.54%**, since 14 previously-inert tests now exercise real code paths.

Known gap left open deliberately: the `ann` extra is installed by neither CI leg, so `tests/test_ann_index.py` (13 tests) and the 6 ANN tests in `test_chunking.py` still never run in CI — the ANN code path has no automated coverage. Adding a third matrix leg (the `all` extra exists) would close it; that's a CI-cost decision, not a drive-by change.

### Fixed: the declared package version was seven releases stale

`pyproject.toml` still read `version = "1.19.0"`, last set 2026-07-21, while this file accumulated seven entries beneath it. Bumped to **1.20.0** — a minor rather than patch bump, because three new tools shipped since 1.19.0 (`remind_me_sync_status`, `remind_me_sync_reconcile`, and the hub's `GET /stats`).

This was user-visible, not cosmetic. `remind_me_mcp.__version__` resolves from installed package metadata, which comes from `pyproject.toml`, so `remind_me_check_update` and every status surface reported a version seven releases old — including immediately after a successful update, which is precisely when that number is being read to confirm the update worked.

**This is also the first tag cut since v1.1.0** (`9cc9711`, 2026-07-20), 62 commits back. Earlier tags exist — `v1.0` and `v1.1` (2026-02-24) and `v1.2` (2026-03-05) — so tag names do not sort chronologically here: `v1.2` predates `v1.1.0` by four months.

Tag-vs-declared-version agreement has not historically held either: `v1.1` and `v1.2` both point at commits whose `pyproject.toml` declares `1.0.0`. `v1.1.0` was the exception, matching exactly. So this release is not restarting a lapsed practice so much as establishing one.

A test cannot reliably guard tags — CI clones are often shallow and may not fetch them, making such a check flaky or vacuously passing — so keeping them current belongs to the release step rather than the suite.

> ⚠️ Corrected after publication: this paragraph originally claimed v1.1.0 was the repo's *only* tag and that 59 commits had landed since. Both were wrong. The claim came from `git tag -l` in a clone that had only fetched one tag; `git ls-remote --tags` shows six. Left visible rather than silently rewritten — a release note is a record, and this is the same stale-but-authoritative failure mode the SY-10 and SY-11 corrections in v1.19.6 and v1.20.0 were about.

Guarded by `tests/test_version_consistency.py`, which asserts the declared version equals the newest `RELEASE_NOTES` heading, that entries stay in descending order (so "newest" is well defined), and that `__version__` resolves rather than falling back to the `0.0.0-dev` sentinel. Unlike the BACKLOG-drift class of problem — prose that quietly stops being true, which no test can check — this one is a mechanical equality and cheap to enforce. Verified by reverting the version and confirming the guard fails with an actionable message.

## v1.19.6 — 2026-07-30

Documentation correction, plus a process guard so this class of drift stops recurring. No code changes.

`BACKLOG.md` SY-10 was marked **todo** and stated that a local delete never propagates — that the outbox has INSERT/UPDATE triggers only, so a deleted record survives on the hub and every other node. That has been false since migration **v15→v16** (gap #11), which resolved it a different way than SY-10 proposed: instead of adding a delete trigger, deletion became an UPDATE setting a `deleted_at` tombstone, riding the existing `memories_outbox_au` trigger with no new trigger and no wire-format change.

This was purely a docs bug — the behavior shipped and is covered by 19 tests in `tests/test_tombstones.py`, including `test_deleted_at_rides_the_update_outbox_trigger`. Only the backlog row lied, and it read as authoritative.

It had real operational cost: planning a multi-node cleanup while believing deletes don't propagate leads to deleting the same records separately on every node, rather than soft-deleting once and letting tombstones replicate. Establishing which source was right required reading migration internals.

- **SY-10 marked done**, recording that it was closed by the tombstone column rather than the delete trigger originally sketched.
- **`ARCHITECTURE.md` gains a "Deletion propagates as a tombstone, not a delete" section** — the mechanism is counter-intuitive and was previously discoverable only from a migration docstring. Covers the consequences that bite: every read path must filter `deleted_at IS NULL`; compaction is purely time-based with no per-peer acknowledgement; `memory_delete` hard-deletes when sync is disabled (nothing to propagate to, and compaction only runs from the sync loop); and cross-node counts must *not* filter `deleted_at`, since the hub counts every row and reports tombstones separately.
- **A short "sync is observable through tools" section** pointing at `remind_me_sync_status`, the hub's `GET /stats`, and `remind_me_sync_reconcile`, so the default is asking the system rather than reading logs or querying databases by hand.
- **`CONTRIBUTING.md` workflow step and a checklist item in the feature / bug-fix / chore PR templates**: if a change closes or invalidates a `BACKLOG.md` row, update that row in the same PR — including when it was solved differently than proposed. Cheap to do at the time, expensive to discover later.

## v1.19.5 — 2026-07-30

Completes the sync observability set begun in v1.19.4. SY-12 answered "is my local sync healthy?" and SY-13 made the hub's counts readable; this closes the loop with "does my data actually match the hub?"

### New Features

- **`remind_me_sync_reconcile` (SY-14)** — fetches the hub's `GET /stats`, diffs it against local counts, and returns per-category drift with a **verdict**. Read-only on both sides. Replaces a manual exercise: `psql` on the hub host, local counts gathered separately, two tables diffed by eye.
  - Verdicts are `in-sync`, `pull-lag`, `node-ahead`, and `fault`. The judgment is the deliverable — the benign case and the real fault differ only by a *sign*, which is easy to skim past when reading two count tables. `node-ahead` (local > hub) is checked first and outranks everything, because it's the only direction meaning this node holds records nothing else has.
  - **Judged by evidence, not by category names.** The original sketch classified drift by "bulk/immutable categories," which would require hardcoding which categories are expected to be static — fragile, since any category can be written to. Instead, hub-ahead drift keys off *pull freshness*: a recent successful pull means ordinary lag; a stale one (beyond `max(3 × SYNC_INTERVAL, 300s)`) or none at all means the pull isn't running. Identical numbers, different verdict, decided on real evidence.
  - Local counts deliberately **do not** filter `deleted_at IS NULL`. The hub counts every row and reports tombstones separately, so filtering locally would make a healthy node look permanently behind by exactly its own tombstone count — a false `pull-lag` that never resolves.
  - Categories are compared as a **union** of both sides, so a bulk import that never reached the hub shows up rather than being silently intersected away. Only drifting categories are listed; agreement is reported as a count.
  - Degrades with a named cause instead of a stack trace: `unsupported` when the hub predates SY-13 and 404s `/stats` (the state a rolling upgrade passes through, node-first), `unauthorized` on a `SYNC_SECRET` mismatch, `unreachable` on connection failure, and `disabled` without contacting the hub at all.

Tests: 15 new cases covering every verdict and failure mode, including that `node-ahead` outranks a healthy pull (a node can pull fine while its pushes fail), that tombstones don't read as drift, and that a malformed hub payload yields zeros rather than raising. Full suite 1484 passed / 12 skipped, coverage 90.27%.

Verified against the real 2026-07-29 hub-vs-node numbers: correctly returns `pull-lag` with the same three drifting categories (`dialog` +10, `conversation` +6, `homelab` +2) and 5 categories in sync that a manual reconciliation found.

## v1.19.4 — 2026-07-30

Sync observability. Sync is the most complex and most failure-prone subsystem here, and it was the only one with no status surface — `remind_me_server_status` reported the folder watcher, webhook ingestion, OpenTelemetry, the wiki, and the remote connector, but nothing about sync, and dedicated status tools existed for far simpler subsystems. In practice that meant answering "did that bulk import reach the hub?" or "is the push backlog draining?" required SSH plus hand-written SQL against `sync_outbox` and `psql` inside the hub's Postgres container.

### New Features

- **`remind_me_sync_status` (SY-12)** — node/hub config, the outbox trigger gate, outbox depth, per-remote push/pull watermarks with the most recent error, and tombstone counts split into total vs already past the retention window. Follows the `get_watch_status`/`get_webhook_status` pattern: the logic lives in `sync.py`, the tool is a thin JSON wrapper.
  - The **drain-rate verdict** is the load-bearing part. A pending count on its own is ambiguous — ten thousand queued rows look identical whether they're draining normally or the push is wedged — so the previous observation is persisted in `sync_flags` and one call now reports `draining` / `stalled` / `growing` / `idle` with a per-minute rate and ETA. The baseline only advances after 30s, so two calls seconds apart can't collapse it into meaningless noise.
  - A remote that has **never** completed a cycle has no `sync_log` row, so a naive join would hide the single worst failure mode (sync never worked at all). Those remotes are merged into the report from the error map instead, with `ever_contacted: false`.
  - Disabled sync names the specific missing env vars rather than returning empty counters, matching `watch_status`.
  - `remind_me_server_status` gains a one-line sync summary with a pointer to the new tool, and flags remotes whose last cycle failed.
- **Hub `GET /stats` (SY-13)** — the hub previously exposed only `/health` and five `/sync/*` routes, so its own API could not answer how many records it held, and no client (node, dashboard, or connector) could reconcile against it. Returns totals, tombstones, `by_origin_node`, `by_category`, and entity/link/relation counts. Auth-gated via the existing `_require_auth`, since counts and category names leak information about content; `/health` stays unauthenticated and free of aggregate queries so deploy healthchecks stay cheap and keep working when Postgres is down.
  - `by_origin_node` is the part nothing else can surface: `origin_node` is hub-only and never crosses the sync wire.

Tests: 18 new cases covering every drain verdict, the baseline-interval guard, corrupt-baseline recovery, never-contacted remotes, and tombstone eligibility. The hub route gets static AST checks instead of integration tests — `fastapi`/`psycopg` are deliberately not this package's dependencies, so neither CI leg can import `hub.main`; the checks lock the property most expensive to get wrong, namely that `/stats` is auth-gated while `/health` is not. Full suite 1469 passed / 12 skipped, coverage 90.25%.

Prepares SY-14 (`remind_me_sync_reconcile`), which needs SY-13's endpoint to diff against.

## v1.19.3 — 2026-07-29

Dependency safety fix. `pyproject.toml` declared `mcp[cli]>=1.0.0` with no upper bound, while both the README's install path and `updater.py`'s self-update run `pip install -e .` — which ignores `uv.lock` and resolves fresh. MCP Python SDK 2.0.0 shipped 2026-07-28 and removes `mcp.server.fastmcp` outright (`FastMCP` became `mcp.server.mcpserver.MCPServer`, with no compatibility alias), so the next `remind_me_self_update` on any node would have installed it and left every module in the package non-importable via `server.py`'s top-level import — taking down MCP stdio, the remote connector, and the dashboard together. Capped to `>=1.28,<2`. The 1.x line is still actively maintained (1.29.0 shipped the same day as 2.0.0), so this costs nothing but a deliberate upgrade decision later.

Also adds `docs/mcp-2.0-migration-plan.md`, which documents the full 1.28 → 2.0 API delta — verified by installing both SDK versions into separate venvs and probing every import site rather than reading changelogs. The short version: the OAuth 2.1 stack, all 44 tool registrations, and the dashboard need no changes; the real work is that transport configuration moved off the mutable `mcp.settings` object (which lost `host`, `port`, `streamable_http_path`, and `transport_security`) into call-time kwargs on `streamable_http_app()`.

Verified: full suite green (1401 passed, 52 skipped) against both the locked 1.28.1 and the 1.29.0 that a fresh `pip install -e .` now resolves to.

## v1.19.2 — 2026-07-29

CI repair. `main` had been red on every run since at least 2026-07-21, from two unrelated failures that both turned out to be environment drift rather than anything a commit introduced.

- **mypy failed on both Python 3.12 legs.** `[tool.mypy] python_version = "3.11"` told mypy to target 3.11, but numpy 2.5.1 — the version the lockfile selects for `python_full_version >= '3.12'` — ships stubs using PEP 695 `type X = ...` statements. mypy rejects that syntax outright when targeting 3.11 and bails before checking any of our code (`Found 1 error in 1 file (errors prevented further checking)`). The 3.11 legs resolve numpy 2.4.6 and were unaffected. Unset `python_version` so mypy targets whichever interpreter it runs under; since the CI matrix already covers 3.11 and 3.12, each version is now checked against its own semantics instead of one being mistyped as the other.
- **`test_reconcile_invalidates_the_ann_index` failed on both semantic legs.** The test asserts `ann_index.get_index()` is non-None, but that function returns `None` by design when `usearch` isn't installed. `usearch` is the `ann` extra, which CI's semantic leg doesn't install. The test's own docstring cites `tests/test_ann_index.py` as its precedent, and that module guards itself with `pytest.importorskip("usearch", ...)` — this test adopted the `db_conn_with_vec` half of the precedent but not the guard. Added the matching per-test `importorskip` (module-level would wrongly skip the 18 tests here that only need sqlite-vec).

No production code changes — this is config and test-guard only.

## v1.19.1 — 2026-07-29

Deploy config fix: the hub's Podman Quadlet (`hub/deploy/remind-me-hub.container`) published only on `127.0.0.1:8765`, requiring every client to reach it through an SSH tunnel. Switched `PublishPort` to bind the host's Tailscale IP directly, so clients on the tailnet connect without a tunnel. Verified against the live deployment: pull and push both confirmed working over the new address, hub and local node counts reconciled.

## v1.19.0 — 2026-07-22

Closes the last item from the application capability review: true ACT-R-style memory reinforces associations *between* items retrieved together, not just each item independently, but nothing previously captured "these two memories tend to be useful together" — the entity graph links a memory to entities it mentions, not to other memories via search co-occurrence.

This was explicitly flagged in the issue as the most ambitious/speculative item on the list, with real design risk around "how to weight, decay, and avoid runaway feedback loops." Rather than attempt the full design (the issue itself calls that "a project of its own"), this ships a deliberately scoped-down slice that captures the core value while sidestepping both flagged risks entirely.

### New Features

- **`memory_associations` table** — a bounded, undecayed weight per memory pair that appeared together in a search result set (`vitality.record_co_retrieval`, capped at `CO_RETRIEVAL_MAX_WEIGHT`). No time-decay math in this pass — a simple cap avoids unbounded growth without needing to solve decay, which the issue itself flags as an unresolved design question.
- **`expand_co_retrieval` opt-in search flag** — surfaces up to 5 memories most strongly co-retrieved with the current results, in a separate `related_via_co_retrieval` section (`remind_me_search`), ordered by association strength. Every search passively reinforces associations regardless of this flag; it only controls whether they're surfaced.
- **Never feeds back into ranking** — this is the design choice that eliminates the "runaway feedback loop" risk entirely rather than mitigating it: search results influence what gets recorded, but recorded associations never influence what future rankings look like, only what gets suggested alongside them, the same posture as the existing `expand_entities`/`include_neighbors` expansions.
- Fixed a related bug found while testing: the benchmark harness neutralized `record_access` (singular), which doesn't exist on the `tools` module — the actual function is `record_accesses` (plural, batch) — so the intended neutralization silently no-op'd. Harmless before (a single fast background statement), but the new co-retrieval write turned this into an occasional deadlock between the benchmark teardown closing the connection and the background write still in flight. Fixed the target name and also neutralize `record_co_retrieval` in benchmark runs.

## v1.18.0 — 2026-07-22

Closes a dashboard-visibility gap flagged in the application capability review: the knowledge graph is fully built out server-side (`GET /api/entity`, the `remind_me_entity_traverse` MCP tool's multi-hop relations) but had no dashboard UI at all — a human owner had no way to browse "what does the app know about X and how is it connected" without hand-crafting API calls.

### New Features

- **New "Entities" dashboard view** — a clickable entity list (most-mentioned first, with a client-side name/alias filter) plus a detail panel showing an entity's structured facts, linked memories, and a "Related Entities" drill-down built from a 1-hop traversal, mirroring the existing Wiki view's list+detail layout.
- **`GET /api/entities`** — new paginated REST route listing every entity (most-mentioned first via a `mention_count` computed from `memory_entities`), for the dashboard's entity list. No equivalent MCP tool exists for this — `remind_me_entity` is lookup-by-name/alias only — browsing everything by list is a dashboard-only need.
- **`GET /api/entity/traverse`** — new REST route exposing the same multi-hop `entity_relations` graph walk as the `remind_me_entity_traverse` MCP tool, for the dashboard's drill-down. The traversal logic (`_expand_via_entity_relations`) was moved from `tools/entity.py` into `db.py` so the MCP tool and the new REST route share one implementation instead of duplicating it — the same pattern already used for `_entity_profile`/`GET /api/entity`.
- `docs/openapi.yaml` updated with both new routes.

Per the issue's own "Alternatives considered," entity browsing stays a separate dashboard view rather than folding into the Wiki view — they're different data models (curated wiki pages vs. the raw entity/relation graph) and warrant separate UIs, as the issue anticipated.

## v1.17.0 — 2026-07-22

Closes a dashboard-visibility gap flagged in the application capability review: `remind_me_vitality_report` computes the app's core "memory going stale" model — active/dormant counts, vault health score, vitality-bucket distribution — but none of it was surfaced in the dashboard, only reachable via the MCP tool.

### New Features

- **`GET /api/vitality`** — new REST route returning the same report as `remind_me_vitality_report` (`total_memories`, `active_count`, `dormant_count`, `average_vitality`, `vault_health_score`, `decay_distribution`, `vitality_buckets`). The bucket/health-score computation was extracted out of the MCP tool into a shared `vitality.build_vitality_report(db)` so the dashboard and Claude always see identical numbers, computed in exactly one place.
- **Vitality Distribution chart** — new dashboard panel in the Stats view, reusing the existing `BarChart` component (now supporting an optional `preserveOrder` prop so the vitality buckets render in their natural low→high order instead of sorted by count) with a red→green color gradient and a "Vault health X% · N active · M dormant" summary line.
- `docs/openapi.yaml` updated with the new route.

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
