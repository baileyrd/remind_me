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
| Import connector (`register_connector`, `importer.py`) | `chat`, `document`, `pdf` (`pdf_import.py`), `image` (`image_import.py`) (built-in), `mempalace` (`mempalace_import.py`), `dbs` (`dbs_import.py`) | kind-string registry, not a hardcoded dispatch; third-party modules register more without touching `importer.py`. `pdf`/`image` are binary formats — `importer.py` threads the undecoded file bytes through `meta["raw_bytes"]` alongside the lossily-UTF-8-decoded `raw` text every connector receives, since a PDF/image would be corrupted by that decode |
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

### Deletion propagates as a tombstone, not a delete

Worth stating explicitly because the mechanism is counter-intuitive and was
previously only discoverable from a migration docstring.

The sync outbox is populated by AFTER INSERT and AFTER UPDATE triggers on
`memories` — there is no delete trigger. A hard `DELETE` therefore produces no
outbox row at all, so the record would survive on the hub and every other node
and resurface on the next pull.

Rather than add a delete trigger, deletion is modelled as an **update**: when
sync is enabled, `memory_delete` sets a `deleted_at` tombstone (v15→v16, gap
#11), which rides the existing `memories_outbox_au` trigger and propagates
through the normal path with no wire-format change. Consequences worth knowing:

- Every read path filters `deleted_at IS NULL`. A new query over `memories`
  that forgets this will surface deleted records.
- Tombstones are hard-deleted by `sync._compact_tombstones` once older than
  `TOMBSTONE_RETENTION_DAYS`, purely on time — there is no per-peer
  acknowledgement, which this single-owner LWW model accepts deliberately.
- With sync **disabled**, `memory_delete` takes a plain hard-delete fast path;
  there is nothing to propagate to, and an uncompacted tombstone would linger
  forever because compaction only runs from the sync loop.
- Counting rows for cross-node comparison must therefore *not* filter
  `deleted_at` — the hub counts every row and reports tombstones separately
  (see `sync.reconcile_with_hub`).

### Encryption at rest is opt-in, not default (issue #184)

**Why it matters.** This tool's whole value proposition is holding a
person's durable, personal memory — conversations, preferences, facts about
people in their life. That data currently sits in plaintext in three
places: the local `memory.db` file (readable by anything with filesystem
access, including a synced laptop's disk image or a cloud backup of the
home directory), the on-demand/pre-migration backup files under
`BACKUP_DIR` (same exposure, indefinitely retained copies), and the hub's
Postgres instance for anyone running multi-machine sync. None of that is
new risk introduced here — it's the status quo this section gives a way to
change, for the subset of users who want it.

**Why it's opt-in, not default.** Two reasons, both hard constraints:
1. It requires a native C extension (SQLCipher, via the `sqlcipher3-wheels`
   package) that isn't guaranteed to be installable in every environment
   this project runs in (it happened to install and work cleanly here, but
   the requirement — a compiled OpenSSL-linked SQLite fork — is exactly the
   kind of dependency that fails silently on some platforms/sandboxes).
2. Per [Non-goals](#non-goals) and the README, this is a local-first,
   zero-ops, single-user tool. Not every user's threat model needs
   encryption at rest, and forcing a native dependency + passphrase
   management onto every install would work against "zero-ops" for the
   users who don't want it. Consistent with how this codebase already gates
   every other native/heavy dependency (semantic search, PDF/OCR import,
   ANN indexing) behind an extras group and a config check rather than a
   base dependency.

**Mechanism.** [SQLCipher](https://www.zetetic.net/sqlcipher/) via the
`sqlcipher3-wheels` PyPI package (a maintained fork of the
`pysqlcipher3`/`sqlcipher3` lineage that ships a prebuilt `libsqlcipher`, so
it doesn't need system dev headers — this was verified against the
alternative of requiring a source build, which this project's install
story couldn't assume). Setting `REMIND_ME_DB_ENCRYPTION_KEY` routes every
open of the real database file (and its backups) through
`db._open_db_connection`, a single choke point that issues
`PRAGMA key = '<key>'` as the very first statement after connecting —
SQLCipher's required activation sequence; anything executed before that
pragma would read/write against what looks like a corrupt file. The key
cannot be passed as a bound `?` parameter (`PRAGMA key = ?` is a syntax
error in sqlite3's C API — verified directly, not assumed), so it's
embedded in the SQL text with standard single-quote doubling
(`_quote_sql_string`), which is a complete escape for a single-quoted SQL
string literal (no other special character is reachable from inside the
quotes), not a partial mitigation.

With the env var unset (the default), `_open_db_connection` is exactly the
`sqlite3.connect(...)` call it replaced — the `sqlcipher3` import is never
even attempted, so an environment without the `encryption` extra installed
is completely unaffected.

**The `storage_interfaces.py` seam.** SQLCipher does *not* need a new
Protocol adapter. Verified directly (not assumed): `sqlcipher3`'s
`Connection` object exposes the identical surface this codebase actually
uses — `execute`, `row_factory`, `enable_load_extension()` (the
`sqlite-vec` extension loads and runs correctly through it), and
`Connection.backup()` (works connection-to-connection between two
sqlcipher3 connections, copying already-encrypted pages). One real gotcha
surfaced while writing this feature's own tests, not theoretical: `db.
row_factory = sqlite3.Row` — the one production call site that sets it, in
`_get_db()` — cannot simply stay as-is, because `sqlite3.Row`'s constructor
requires an actual `sqlite3.Cursor` and raises `TypeError` on first
dict-style row access (`row["column"]`) against an sqlcipher3 cursor.
`db._row_factory_class()` picks sqlcipher3's own `Row` class (API-identical
otherwise) whenever encryption is active, fixing this at the one call site
that needed it. Every
`storage_interfaces.py` Protocol's `db: sqlite3.Connection` parameter is
satisfied structurally at runtime by an sqlcipher3 connection. The one
caveat: it is **not** an `isinstance` of `sqlite3.Connection`, nor are its
exception classes (`OperationalError`, `DatabaseError`, ...) subclasses of
`sqlite3`'s — `sqlcipher3` is a separately-compiled fork of the same C
extension, so it has its own disjoint DB-API exception hierarchy by
construction, not a bug in this integration. This codebase's `Protocol`s
are verified structurally via mypy assignments, not `isinstance` checks
(see [Boundaries](#boundaries)), so this doesn't break the seam itself —
but it does mean **known limitation**: the ~40 existing
`except sqlite3.OperationalError`/`sqlite3.IntegrityError`/
`sqlite3.DatabaseError`-style handlers scattered across `db.py` and the
`tools/*.py` modules (lock-retry logic, duplicate-content dedup via
`IntegrityError`, corruption detection) will **not** catch the sqlcipher3
equivalent when encryption is enabled, and were not swept and rewritten in
this change — doing so safely across every call site was judged
out-of-scope for a first, opt-in-by-default-off version touching the
storage layer every other feature in this series depends on. Two call
sites directly on this feature's own critical path were fixed regardless
(`_get_db`'s sqlite-vec extension load, and `backup.py`'s restore
validation, via a new `db._sqlite_driver_errors()` helper returning the
right exception tuple for whichever driver is active) since they sit
inside code this change already touches. The rest is a documented,
deliberate v1 gap, not an oversight — a candidate for a dedicated follow-up
issue if this feature sees real adoption.

**Coverage.**
- **Covered:** the `memory.db` file itself (and its `-wal`/`-shm`
  sidecars, which SQLCipher also encrypts), and every file under
  `BACKUP_DIR` — `backup.py`'s `create_backup` now opens its destination
  connection through the same `_open_db_connection` choke point with the
  same key, so `Connection.backup()` (which copies pages exactly as
  stored) produces an equally-encrypted backup file. `restore_backup`'s
  raw `shutil.copy2` file copy needed no change at all: copying an
  encrypted file byte-for-byte is already correct, since the bytes on disk
  are ciphertext regardless of which tool copies them — the risk called
  out in the issue (a restore path that decrypts to a temp file en route)
  does not exist here.
- **Explicitly NOT covered (known limitations, not oversights):**
  - Anything already decrypted in the running process's memory — SQLite
    (and SQLCipher) decrypt pages into the process's page cache to operate
    on them; a memory dump or debugger attached to a live server sees
    plaintext regardless of this feature.
  - OS-level swap and hibernation files, which can contain that same
    decrypted-in-memory content — outside any application's control;
    full-disk encryption is the correct mitigation and is out of scope
    here.
  - The hub's Postgres storage. Multi-machine sync pushes plaintext
    records to the Postgres-backed hub over its own bearer-authenticated
    channel; encrypting that store is a separate, infrastructure-layer
    concern (disk encryption, or a Postgres TDE extension) — see
    [`hub/README.md`](hub/README.md), which documents today's actual
    posture (transport is SSH-tunneled/bearer-authenticated; at-rest
    protection is left to the deployer's disk/infrastructure choices, not
    implemented in this codebase). This feature does not change that.

**Migration/adoption story.** v1 scope limitation, stated plainly rather
than half-implemented: **encryption must be enabled before an install's
first run.** There is no in-place re-encryption of an existing plaintext
`memory.db` in this version — SQLCipher's own documented path for that is
`sqlcipher_export()` (attach a new encrypted database and copy every table
across via SQL, inside a single connection) or an equivalent dump/reimport,
and neither is wired up here. Turning `REMIND_ME_DB_ENCRYPTION_KEY` on
against an *existing* plaintext database will simply fail to open it
(SQLCipher will treat the unencrypted file as encrypted-with-the-wrong-key
and refuse it) — no data loss, but also no automatic upgrade path. The same
asymmetry applies to `restore_backup`: a backup taken while encryption was
enabled can only be restored with that same key configured, and a
plaintext backup cannot be restored while a key is configured (see
`backup._validate_backup_file`'s docstring). A guided
`sqlcipher_export()`-based migration tool is a reasonable follow-up, not
included here.

### Sync is observable through tools, not just logs

`remind_me_sync_status` reports local sync health (outbox depth with a
drain-rate verdict, per-remote watermarks, last error); the hub's auth-gated
`GET /stats` exposes its own counts; `remind_me_sync_reconcile` diffs the two
and classifies the drift. Prefer these over reading logs or querying the
databases by hand.

Two additions worth knowing about before reaching for `/stats`. The hub's
auth-gated `GET /count` returns the same totals without `/stats`' `GROUP BY`
passes, so a dashboard tile or drift alarm can poll it; `/stats` stays the
reconciliation route. And both halves report a version — the hub's
`HUB_VERSION` (hand-bumped, independent of the package version, since the
container holds `main.py` and nothing to derive one from) and each node's
installed package version — through their `/health` routes and both sync
tools, so version skew is visible without inspecting images or shelling into
hosts. The hub's version is unauthenticated so a deploy can be verified
without the sync secret; its counts are not, because totals leak content
information.

## Key decisions
See [docs/adr/](./docs/adr/) for the record of individual decisions and their tradeoffs.

## Non-goals

Explicitly out of scope, per the project's stated design (see README):
pluggable storage backends beyond SQLite, multimodal ingestion, and
multi-tenant/cross-agent isolation. dbs's plugin/connector model and
Playwright/yt-dlp-class dependency surface belong in a separate collection
pipeline (see `docs/dbs-integration-review-2026-07-21.md`), not inside this
server.
