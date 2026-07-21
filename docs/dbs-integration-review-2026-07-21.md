# Daily Backup System (dbs) Integration Review — 2026-07-21

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

1. **Export → watched folder (lowest effort).** remind_me's `watcher.py`
   already polls a directory for auto-ingest. Pointing `dbs export --format
   markdown` (or `obsidian`) at that directory after each `dbs backup` run
   needs zero code changes on either side — config only. Freshness is
   per-backup-cycle; fidelity is flattened text (source, tags, and
   timestamps become prose rather than structured metadata).
2. **Per-item webhook push (moderate effort).** A small adapter shapes new
   dbs items into calls against remind_me's `POST /ingest` webhook endpoint
   right after each incremental fetch, instead of waiting for a batch
   export. Near real-time; same flattened-text fidelity unless the payload
   also carries structured fields.
3. **Dedicated `dbs` import connector (highest effort, best fit).**
   remind_me's import pipeline has a connector registry (used today for
   `chat`/`document`/`mempalace` kinds). A `dbs`-specific connector reading
   dbs's SQLite directly could preserve per-source structure — subreddit,
   channel, tags, kind — as entities in remind_me's knowledge graph instead
   of collapsing everything to prose, and could track dbs's own cursors to
   pull only what changed. This is the option that matches both projects'
   stated extension points as designed, rather than routing around them.

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
