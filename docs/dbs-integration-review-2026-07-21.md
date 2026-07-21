# Daily Backup System (dbs) Integration Review — 2026-07-21

> **Update (same day):** option 1 below has shipped on the dbs side as `dbs
> export-notes` — no code changes were needed in remind_me itself. See
> [Status](#status) at the end.

Review of [baileyrd/daily-backup-system](https://github.com/baileyrd/daily-backup-system)
("dbs") against `remind_me`, prompted by a request to determine whether the
two projects should be integrated. dbs's intent is broader than its name
suggests: it isn't just a backup tool, it's a **collection pipeline** —
incremental, deduplicated archives of a user's Reddit saves, YouTube
history, GitHub stars, Raindrop bookmarks, Readwise highlights, podcast
episodes, and more, landing in one local SQLite store with full revision
history. That framing, not "notify me when a backup fails," is what makes it
worth a real integration rather than a passing mention.

## Determination

**Do not merge the codebases.** dbs is a CLI archival engine with a
plugin/connector model, browser-automation dependencies (Playwright,
yt-dlp), and an explicit local-disk-first storage design; remind_me is an
MCP server with a documented, deliberate scope that excludes pluggable
storage backends, multimodality, and multi-tenancy. Folding dbs's dependency
surface and domain logic into remind_me (or vice versa) would violate that
stated scope for no benefit — dbs doesn't need memory/search/entity-graph
machinery to archive a Reddit account, and remind_me doesn't need
Playwright/yt-dlp to remember things.

**Do build a one-directional content pipeline: dbs → remind_me.** dbs
produces exactly the kind of normalized documents remind_me's ingestion path
already expects, and remind_me already has three ingestion seams that fit
without new architecture on either side.

## Why dbs is a good fit as a source, not as a merge candidate

- Six exporters (`json`, `ndjson`, `csv`, `markdown`, `obsidian`, `archive`)
  already flatten fourteen heterogeneous source schemas into either
  structured records or clean prose — the normalization work remind_me would
  otherwise have to do per source.
- Idempotent, cursor-based incremental fetches mean re-running an export
  after each backup cycle naturally produces "what's new since last time,"
  not a full re-dump.
- A `notify_url` webhook (`notify_on = failure|warning|always`) already
  exists as an outbound integration point, independent of the export path.
- Deliberately excludes any built-in AI/embedding/search layer (confirmed in
  its own architecture docs) — it has no ambition to compete with or
  duplicate remind_me's job, only to feed it.

## Where it plugs into remind_me, by effort/fidelity

1. **Export → watched folder (lowest effort). SHIPPED, dbs side only.**
   remind_me's `watcher.py` already polls a directory for auto-ingest and
   needed no changes. dbs's zip-only exporters did — `dbs export --format
   obsidian` produces an archive, and the watcher only reads loose files —
   so dbs gained a new `dbs export-notes --out-dir DIR` command that unzips
   the same tested obsidian path into one Markdown file per item,
   incrementally by default. Point `REMIND_ME_WATCH_DIRS` at that
   directory and new items auto-ingest on the next poll. Freshness is
   per-backup-cycle; fidelity is flattened text — dbs's structured fields
   (source, tags, timestamps) arrive as YAML frontmatter inside each note's
   body, ingested as plain content rather than structured metadata, until
   option 3 exists.
2. **Per-item webhook push (moderate effort).** A small adapter shapes new
   dbs items into calls against remind_me's `POST /ingest` webhook endpoint
   right after each incremental fetch, instead of waiting for a batch
   export. Near real-time; same flattened-text fidelity unless the payload
   also carries structured fields.
3. **Dedicated `dbs` import connector (highest effort, best fit). SHIPPED,
   remind_me side only.** `remind_me_mcp/dbs_import.py` reads dbs's
   `items`/`sources` tables directly (read-only, no dependency on the `dbs`
   package) and registers under the `"dbs"` kind, mirroring
   `mempalace_import.py`'s pattern of a bespoke bulk-pull loop plus a
   discovery-only connector registration. Each live item becomes a memory
   with its dbs source and tags linked as first-class knowledge-graph
   entities (`memory_entities`), not collapsed into prose. New MCP tool:
   `remind_me_import_dbs`.

## Recommendation

Treat this as a staged build, not a single decision: start with option 1 to
validate that dbs's exported content is actually useful as memory before
investing in option 2 or 3. Option 3 is the one worth reaching for if dbs
becomes a primary, ongoing memory source rather than an occasional import —
it's the only option that gives Claude entity-level knowledge of *where*
something came from (r/whatever, a specific YouTube channel, a Readwise
highlight) instead of a paragraph of text.

No code has been changed in either repository as part of this review; this
document exists to record the analysis so implementation can start directly
from option 1 or 3 above without re-deriving the tradeoffs.

## Status

- **Option 1 — shipped**, entirely in dbs (`dbs export-notes`; see
  [baileyrd/daily-backup-system docs/remind-me-integration-review-2026-07-21.md](https://github.com/baileyrd/daily-backup-system/blob/main/docs/remind-me-integration-review-2026-07-21.md)
  for implementation notes and known gaps). remind_me needs only a config
  change: set `REMIND_ME_WATCH_DIRS` to the directory passed to
  `dbs export-notes --out-dir`. Verified end-to-end — a real
  `dbs export-notes` run followed by remind_me's actual
  `FolderWatcher.scan_once()` against that directory produced a memory row
  with the item's title/tags/body content intact.
- **Option 3 — shipped**, entirely in remind_me (`remind_me_mcp/dbs_import.py`
  + the `remind_me_import_dbs` tool). Reads dbs's SQLite directly; each item
  becomes a memory with its dbs source (kind `dbs_source`) and tags (kind
  `tag`) linked as entities via `memory_entities`, and a new `dbs_imports`
  table (keyed by `(dbs_source, external_id)` + content_hash) tracks what's
  already imported. An edited item gets a fresh memory with the old one
  marked `superseded_by`, so — unlike option 1 — this has no
  `item_created_at`-only staleness gap at all: every pull compares the
  actual current content_hash, not a date cutoff. No dbs-side changes were
  needed (dbs's items/sources schema is read directly, the same way
  MemPalace's ChromaDB store is read directly rather than through its own
  MCP tools). Verified end-to-end against a real `dbs.sqlite3` produced by
  the actual `dbs` CLI (`dbs init` + `dbs restore`), including a rerun
  no-op and an edited-item resupersession.
- **Option 2 — not started.** The remaining gap between the three options:
  near-real-time push instead of on-demand/scheduled pulls. Worth
  reaching for only if option 3's pull cadence (call `remind_me_import_dbs`
  after each `dbs backup`, e.g. from the same cron job) turns out to be too
  slow for a given use case.
