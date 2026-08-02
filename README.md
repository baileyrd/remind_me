# Remind Me MCP Server

[![CI](https://github.com/baileyrd/remind_me/actions/workflows/ci.yml/badge.svg)](https://github.com/baileyrd/remind_me/actions/workflows/ci.yml)

Persistent, searchable memory that works across **Claude.ai**, **Claude Code**, and **Claude Desktop** — with intelligent retrieval, multi-machine sync, and a built-in dashboard UI.

## Features

**Capture & import**
- **Chat export import** — ingest JSON, JSONL, or Markdown exports from Claude, ChatGPT, or custom formats
- **Document ingestion** — import Markdown notes and plain-text files, chunked per-section (heading context preserved) or per-paragraph; `kind=auto` detects chat vs document per file
- **PDF and image (OCR) ingestion** — import `.pdf` files (chunked per-page, page number kept as metadata) and `.png`/`.jpg`/`.jpeg` images (OCR'd into a single memory); `kind=auto` routes both automatically. Requires the optional `pdf`/`image` extras — see [PDF and Image Import](#pdf-and-image-import)
- **Audio transcription ingestion** — import `.mp3`/`.m4a`/`.wav`/`.ogg` files, transcribed via a local Whisper model and chunked per transcript segment (start/end timestamp kept as metadata); `kind=auto` routes all four extensions automatically. Requires the optional `audio` extra — see [Audio Import](#audio-import)
- **Readwise highlights import** — import a Readwise "Export" JSON file as one memory per highlight (book/article title, author, category, and the highlight's own note all kept as metadata/content); requires explicit `kind=readwise` — see [Importing from Readwise](#importing-from-readwise)
- **Obsidian vault import** — frontmatter `tags:`, inline `#tags`, and `[[wikilinks]]` (resolved into knowledge-graph entities) are understood, not flattened into prose; a `.obsidian/` directory at or above a watched/imported path auto-detects the vault with zero configuration — see [Importing from Obsidian](#importing-from-obsidian)
- **Bulk directory import** — point at a folder of exports/notes/PDFs/images/audio and import them all
- **Watched folders** — set `REMIND_ME_WATCH_DIRS` and new or changed files auto-ingest in the background; changed files supersede their previous import
- **Push/webhook ingestion** — set `REMIND_ME_WEBHOOK_SECRET` and `POST /ingest` accepts content directly over the network, no filesystem staging required
- **Ingest-time normalization** — `remind_me_normalize_batch`/`remind_me_normalize_apply` distill noisy raw imports into clean `{question, summary, resolution?}` memories, non-destructively linked back to the source
- **Auto-capture** — store a full conversation dialog plus a distilled summary as two linked memories
- **Deduplication** — re-importing the same content is a safe no-op (tracked by content hash)

**Organize: entity knowledge graph**
- **Atomic decomposition** — Claude-driven extraction of atomic facts from conversations, linked to parent memories
- **Structured triples** — subject/predicate/object columns written by add/decompose/annotate for precise query routing; a triple that contradicts an existing one (same subject+predicate, different object) automatically supersedes it
- **Entity graph** — entities with kinds and aliases, deterministic ids, mention links from memories; backfill via `remind_me_extract_batch` + `remind_me_annotate`, look up with `remind_me_entity`
- **Tagging & categorization** — organize memories with categories and tags
- **Memory classification** — 7 memory types with single and batch reclassification tools

**Synthesise: LLM Wiki**
- **LLM Wiki layer** (Karpathy pattern) — a *synthesis* layer over the raw memory store: Claude distils memories into a small set of interlinked markdown pages you can load directly into context instead of retrieving fragments
- **Files are the source of truth** — pages live as plain `.md` files (`REMIND_ME_WIKI_DIR`), with `[[wikilinks]]` + backlinks, an auto-generated `index.md`, an append-only `log.md`, and a seeded `SCHEMA.md` maintainer contract; the database is just a reconcile-from-files search index
- **Compile workflow** — `remind_me_wiki_compile` surfaces pending raw memories plus the current wiki state and schema, then advances a watermark once the batch is integrated

**Evolve & maintain**
- **ACT-R vitality model** — cognitive-science-inspired memory decay with per-category rates, access-based reinforcement, bridge protection for high-value memories, and an importance prior seeded from `memory_type`/`source` at write time so a decision outranks a throwaway aside before any feedback exists
- **Vault consolidation** — semantic clustering with Union-Find, canonical selection, and dry-run merge previews; merging requires an LLM-authored summary per cluster (real consolidation, not unbounded concatenation)

**Search & retrieval**
- **Full-text search** via SQLite FTS5 — fast, offline, no external services
- **Hybrid semantic search** — FTS5 keyword matching + vector similarity via `sqlite-vec` and a local ONNX embedding model
- **RRF rank fusion** — Reciprocal Rank Fusion merges keyword, semantic, recency, vitality, and an opt-in IDF signal for best-match retrieval
- **Auto-routing retrieval strategy** — `strategy=auto` (default) heuristically rebalances keyword vs. semantic weight by query shape (short/quoted/wildcard queries favor keyword, long/question-shaped queries favor semantic); pin `balanced`/`keyword_favored`/`semantic_favored` explicitly to A/B test
- **Structured queries** — `subject:`, `predicate:`, and `entity:"..."` filters route straight to indexed lookups; opt-in 1-hop graph expansion surfaces related memories
- **Neighbor-aware chunk retrieval** — opt-in expansion surfaces a result's adjacent chunks from the same source document, so context split apart by chunking isn't lost
- **Token budget** — search results are trimmed to fit within an 800-token default cap (configurable), preventing context overflow
- **Search transparency** — debug signals, tier breakdown, and dormant exclusion counts in search results
- **Search feedback** — `remind_me_feedback` records a helpful/unhelpful signal on a memory. Without a `query`, adjusts `base_weight` (and therefore vitality and future ranking) globally; with a `query`, the signal is query-contextual instead — it only nudges future searches with a similar query, so demoting a memory for one question doesn't punish it for an unrelated one

**Sync, backup & access**
- **Distributed sync** — offline-first with outbox pattern, Postgres hub, and peer-to-peer sync over Tailscale; the entity graph syncs too
- **Memory export** — full logical backup to JSON/JSONL (entity graph included) via MCP tool or `GET /api/export`, round-trippable through the importer
- **Dashboard UI** — browse, search, add, edit, and delete memories from a web interface
- **Claude.ai custom connector** — expose the server over an HTTPS tunnel with single-user OAuth 2.1 (or a secret-path URL fallback) and attach it to claude.ai
- **WAL mode** — SQLite Write-Ahead Logging ensures safe concurrent reads
- **Optional OpenTelemetry tracing** — off by default; export tool-call/sync/watcher spans to any OTLP collector you already run

## Quick Start

### 1. Install

Pick **one** install method below. Each puts the `remind-me-mcp` entrypoint at a known, stable path — reference that exact path in your MCP client config (see step 2/3) so the launcher can find it.

#### Option A — `uv tool install` (recommended, isolated, no venv to manage)

```bash
git clone https://github.com/baileyrd/remind_me.git ~/remind-me-mcp
cd ~/remind-me-mcp
uv tool install -e .
```

Entrypoint lands at `~/.local/bin/remind-me-mcp` (i.e. `/home/<user>/.local/bin/remind-me-mcp`).

#### Option B — project-local `.venv`

```bash
git clone https://github.com/baileyrd/remind_me.git ~/remind-me-mcp
cd ~/remind-me-mcp

# Create the venv first — without this, `uv pip install -e .` may install
# into the system Python and leave .venv/bin/remind-me-mcp missing.
uv venv                       # or: python3.11 -m venv .venv
uv pip install -e .            # or: .venv/bin/pip install -e .
```

Entrypoint lands at `~/remind-me-mcp/.venv/bin/remind-me-mcp`.

> **Heads up — the MCP client config must reference an absolute path that actually exists.** A common failure mode is to put `/path/to/repo/.venv/bin/remind-me-mcp` in `claude_desktop_config.json` while the install actually went to `~/.local/bin/remind-me-mcp` (or vice versa). The server then silently fails to launch and no tools are discovered. Run `ls -l <the-path-from-your-config>` to confirm it exists before debugging anything else.

### 2. Configure for Claude Code

Add to your Claude Code MCP config (`~/.claude/claude_code_config.json` or project-level `.mcp.json`):

```json
{
  "mcpServers": {
    "remind-me": {
      "command": "remind-me-mcp",
      "env": {
        "REMIND_ME_MCP_DIR": "~/.remind-me"
      }
    }
  }
}
```

Or run via `uv` without installing:

```json
{
  "mcpServers": {
    "remind-me": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/remind-me-mcp", "python", "-m", "remind_me_mcp"],
      "env": {
        "REMIND_ME_MCP_DIR": "~/.remind-me"
      }
    }
  }
}
```

### 3. Configure for Claude Desktop

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS, `%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "remind-me": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/remind-me-mcp", "python", "-m", "remind_me_mcp"],
      "env": {
        "REMIND_ME_MCP_DIR": "~/.remind-me"
      }
    }
  }
}
```

#### Claude Desktop on Windows with WSL

When running MCP servers in Claude Desktop via `wsl.exe`, environment variables in the `env` block **do not pass through** to the WSL process. You must inline them directly in the command string:

```json
{
  "mcpServers": {
    "remind-me": {
      "command": "wsl.exe",
      "args": [
        "bash", "-c",
        "REMIND_ME_MCP_DIR=~/.remind-me REMIND_ME_NODE_ID=my-pc REMIND_ME_HUB_URL=http://hub:8765 REMIND_ME_SYNC_SECRET=your-secret remind-me-mcp"
      ]
    }
  }
}
```

> The `env` block in the config is ignored by `wsl.exe` — all environment variables must be part of the `bash -c` command string.

### 4. Configure for Claude.ai (via Claude in Chrome)

If using the Claude in Chrome extension with MCP support, add the same server configuration to your extension's MCP settings.

To attach the claude.ai **website** itself, run the server as a remote connector instead — see [Claude.ai Custom Connector (Remote MCP)](#claudeai-custom-connector-remote-mcp).

### 5. Recommended: set a per-server timeout

Most tool calls here finish in well under a second — reads, writes, and status checks are all local SQLite. If a client's default MCP idle timeout is long (some default to 300s), a genuinely stuck call sits silent for the full window before you see anything, turning a fast diagnosis into a slow one. If your client supports a per-server timeout, set it low — 30-60s is plenty of headroom for even a slow embedding call, and a call still running past that is itself a signal something is wrong (an unexpectedly large query, a hung sync attempt, etc.), not normal variance. In Claude Code, this is the `timeout` field (milliseconds) alongside a server's entry in your MCP config, or the `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT` environment variable globally.

Independently, `REMIND_ME_SLOW_CALL_SECONDS` (default `30`) arms an in-process watchdog that dumps every thread's stack to stderr if a call runs past that threshold — see `remind_me_server_status` for whether it's currently armed. That is the server-side complement to a short client timeout: the client timeout tells you a call is stuck; the watchdog's stack dump tells you where.

## Dashboard UI

The server includes a built-in web dashboard for browsing, searching, and managing your memories visually.

### Starting the Dashboard

```bash
# Option A: environment variable
REMIND_ME_MCP_SERVE_UI=true remind-me-mcp

# Option B: command-line flag
remind-me-mcp --serve-ui

# Option C: custom port and host
remind-me-mcp --serve-ui --ui-port 8080 --ui-host 0.0.0.0
```

Then open **http://localhost:5199** in your browser.

> The `--serve-ui` mode runs the HTTP dashboard server. Without it, the server runs in stdio mode for Claude Code / Claude Desktop. They are separate modes — run one instance for MCP and optionally another for the UI.

### Authentication

The `/api/*` routes require a bearer token by default. On first run a key is
auto-generated and stored at `~/.remind-me/api_key` (mode 0600); the dashboard
page prompts for it once and remembers it in the browser. For direct API use:

```bash
curl -H "Authorization: Bearer $(cat ~/.remind-me/api_key)" http://localhost:5199/api/stats
```

Set `REMIND_ME_API_KEY` to use your own token, or `REMIND_ME_API_KEY=disabled`
to run an open localhost API (not recommended). Mutating requests must send
`Content-Type: application/json` (cross-origin form posts are rejected with 415).
`GET /health` is an unauthenticated liveness probe, and reports the installed
version so you can tell which build a node is running without a token.

#### Scoped API keys

The default key above always has full read-write access — it's the same
credential in every request. To share a read-only dashboard view, or embed a
key in a lower-trust client without handing out full write access, create an
additional **named, scoped** key with the `remind_me_api_key` MCP tool:

```
remind_me_api_key(action="create", name="dashboard-viewer", scope="read")
```

This returns the plaintext key **exactly once** — save it immediately; only
its SHA-256 hash is stored afterward, so it cannot be retrieved again, only
revoked and replaced. A `read`-scoped key authenticates normally against
every `GET` route but is rejected with `403` on any mutating route
(`POST`/`PUT`/`PATCH`/`DELETE`); a `read-write`-scoped key has the same full
access as the default key. Use it exactly like the default key:

```bash
curl -H "Authorization: Bearer <scoped-key>" http://localhost:5199/api/stats
```

`remind_me_api_key(action="list")` shows every key's name/scope/created_at
(never the key material), including a synthetic `default` entry for the
backward-compat key above. `remind_me_api_key(action="revoke", name=...)`
immediately ends a named key's access — the default key isn't revocable this
way, since it's config-managed (the env var or its own auto-generated file),
not app-managed; set `REMIND_ME_API_KEY=disabled` or delete the persisted
`api_key` file to rotate it instead.

This is not multi-tenancy (see [ARCHITECTURE.md](ARCHITECTURE.md)): every key
— default or scoped — reads and writes the exact same single vault; only the
*scope* differs per key, there is no per-key data partitioning.

### What It Does

- **Browse & search** — full-text search with `⌘K` shortcut, category sidebar with counts, clickable tag filters
- **View stats** — bar charts for categories, sources, vitality distribution, and top tags; a **Vault Trend** line chart plotting total-memory-count drift over the daily analytics snapshots the background scheduler captures automatically (empty state on a fresh install with no history yet); database size and server info
- **Add memories** — modal form with content editor, color-coded category picker, and tag input
- **Edit & delete** — inline controls on every memory card with confirmation dialogs
- **Expand/collapse** — long memories truncate at 200 characters with a click to expand
- **Browse the Wiki** — read-only view of the LLM Wiki (FT-08): searchable page catalogue in the sidebar, rendered page body with clickable `[[Wikilinks]]`, and a backlinks/links panel for cross-page navigation; a pending-compile badge flags raw memories not yet folded in
- **Live data** — the dashboard reads and writes your real SQLite database; changes appear immediately

### Mobile / PWA Support

The dashboard is usable at phone widths and installable as a standalone app
(issue #199 mobile audit — a spike, not a full native-mobile build):

- A `<meta name="viewport">` tag has been present in the HTML shell since
  the dashboard's original build, so pinch-zoom/text-size already scaled
  correctly on phones before this audit.
- The header, sidebar/main split, and the Stats view's two-up chart grid
  now reflow at narrow widths (~≤680px): the sidebar stacks above the main
  content instead of squeezing it, and the "By Category"/"By Source" bar
  charts drop to one column instead of getting crushed unreadably narrow.
  Verified with a real headless-Chromium render at 390×844 (iPhone-width):
  zero horizontal overflow across Browse/Stats/Wiki/Entities, versus real,
  reproducible overflow (`document.documentElement.scrollWidth` 610px vs.
  a 390px viewport) before the fix.
- Icon-only buttons (copy/edit/delete on memory cards, modal close) and the
  view-tab/Import/Add buttons now have a ~40-44px minimum tap target,
  mobile-accessibility guidance, without changing their visible icon size.
- A minimal `manifest.json` (`GET /manifest.json`, linked via `<link
  rel="manifest">`) lets a phone browser "Add to Home Screen" the dashboard
  as a standalone-display PWA. **Known gaps, left alone deliberately**: no
  service worker or offline support, and no app icon yet (the repo has no
  icon/logo asset — the manifest is still spec-valid without one; the OS
  falls back to a generic glyph). Smaller interactive controls — inline
  tag pills, form category chips — were left at their current size rather
  than widened, since doing so for every one would start to reshape the
  visual density of the UI rather than being a targeted fix.

### REST API

The dashboard is powered by a REST API you can also use directly:

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Liveness probe + node role and installed version (no auth) |
| `GET` | `/manifest.json` | PWA manifest for "Add to Home Screen" (no auth) |
| `GET` | `/api/stats` | Memory statistics, categories, tags, DB info |
| `GET` | `/api/vitality` | Vault vitality report: active/dormant counts, health score, vitality-bucket distribution |
| `GET` | `/api/analytics/trend` | Daily analytics-snapshot history for the dashboard's Vault Trend panel: `{snapshots: [{captured_at, total_memories, vitality_buckets, category_counts}, ...]}`, oldest first (empty array on a fresh install) |
| `GET` | `/api/memories?category=&tags=&limit=&offset=` | List memories with filters, paginated (`total`/`count`/`offset`/`limit`/`has_more`) |
| `GET` | `/api/memories/search?q=&category=&tags=&limit=&offset=` | Full-text search, paginated the same way |
| `GET` | `/api/memories/{id}` | Get a single memory |
| `POST` | `/api/memories` | Add a memory (JSON body: `{content, category, tags, source, metadata}`) |
| `PUT`/`PATCH` | `/api/memories/{id}` | Update a memory |
| `DELETE` | `/api/memories/{id}` | Delete a memory |
| `POST` | `/api/memories/bulk/delete` | Delete multiple memories by id (JSON body: `{ids: [...]}`, max 200) |
| `POST` | `/api/memories/bulk/tag` | Add/remove/set tags on multiple memories (JSON body: `{ids: [...], tags: [...], mode: "add"\|"remove"\|"set"}`) |
| `POST` | `/api/memories/bulk/reclassify` | Apply `memory_type` classifications to multiple memories (JSON body: `{classifications: [{memory_id, memory_type}, ...]}`) |
| `POST` | `/api/import` | Import a chat/document file or directory (JSON body: `{file_path, kind, extract_mode, category, tags, max_length}`; paths must be inside `REMIND_ME_IMPORT_ROOTS`) |
| `GET` | `/api/export?format=&category=&tags=&file_path=&include_graph=` | Export memories (+ entity graph by default) as JSON/JSONL — streamed as the response body, or written server-side when `file_path` (inside `REMIND_ME_EXPORT_ROOTS`) is given |
| `GET` | `/api/entity?name=&limit=` | Look up a knowledge-graph entity by name or alias (404 if unknown) |
| `GET` | `/api/entities?limit=&offset=` | List entities, most-mentioned first, paginated |
| `GET` | `/api/entity/traverse?name=&hops=&relation=&cap=` | Multi-hop traversal of the typed entity-relation graph (404 if the starting entity is unknown) |
| `GET` | `/api/wiki` | List every LLM Wiki page (slug, title, summary, updated_at) |
| `GET` | `/api/wiki/search?q=&limit=` | Full-text search the wiki (title + body), distinct from `/api/memories/search` |
| `GET` | `/api/wiki/load?token_budget=&include_index=` | Concatenate the whole wiki into one blob (the core LLM-Wiki move) |
| `GET` | `/api/wiki/status` | Page count + pending-compile count, for a dashboard badge |
| `GET` | `/api/wiki/{slug}` | Read a single page by title or slug, with its links and backlinks (404 if unknown) |

All `/api/*` routes require the bearer token described above (`GET /health` does not). The wiki surface is read-only — writing stays an MCP-tool-only, LLM-curated action (see `SCHEMA.md`).

A full [OpenAPI 3.0 spec](docs/openapi.yaml) covers every route above (request/response schemas, error shapes, auth) — feed it to `openapi-generator`, `openapi-typescript`, or similar to generate a typed client in any language, rather than remind_me maintaining hand-written SDKs itself.

### Instance Detection

The server tracks running instances via a PID file (`~/.remind-me/server.pid`):

- **Starting the dashboard** writes a PID file. If a dashboard is already running, the second instance exits with a warning instead of conflicting.
- **MCP stdio mode** checks for a running dashboard on startup and logs its URL.
- **`--status` flag** lets you check from the command line without starting anything:

```bash
remind-me-mcp --status
# ✓ Dashboard running at http://127.0.0.1:5199 (PID 12345)
#   Database: /home/user/.remind-me/memory.db (exists)
```

- **`remind_me_server_status` tool** — Claude can check from inside a conversation whether the dashboard is up.
- **PID file cleanup** happens automatically on shutdown (SIGTERM, SIGINT, or normal exit). Stale PID files from crashed processes are detected and removed.

### UI Layout

```
┌──────────────────────────────────────────────────┐
│  🧠 Memory          [Browse|Stats]  [+ Add]      │
├────────┬─────────────────────────────────────────┤
│        │  🔍 Search memories… (⌘K)               │
│ Categ. │                                         │
│  All   │  ┌─────────────────────────────────┐    │
│  pref  │  │ PREFERENCE  64c309c735fc    ✎ 🗑 │    │
│  fact  │  │ Nano prefers Python with type…  │    │
│  ...   │  │ 🏷 python  coding-style         │    │
│        │  └─────────────────────────────────┘    │
│ Tags   │                                         │
│  python│  ┌─────────────────────────────────┐    │
│  work  │  │ FACT  e1a4fd005625          ✎ 🗑 │    │
│  ...   │  │ The DTO manages a 398-app…      │    │
│        │  │ 🏷 work  dto  portfolio         │    │
│        │  └─────────────────────────────────┘    │
└────────┴─────────────────────────────────────────┘
```

The stats view replaces the main content area with summary cards, horizontal bar charts, and server configuration info.

## MCP Tools

### Search & retrieval

| Tool | Description |
|------|-------------|
| `remind_me_search` | Hybrid search with RRF rank fusion, auto-routed or pinned ranking `strategy`, token budget, dormant exclusion, structured `subject:`/`predicate:`/`entity:` queries, opt-in `expand_entities` graph expansion, opt-in `include_neighbors` sibling-chunk expansion, opt-in `expand_co_retrieval` co-retrieval expansion, and opt-in `include_sensitive` to include memories marked sensitive — see [Sensitive Memories](#sensitive-memories) |
| `remind_me_entity` | Look up a knowledge-graph entity by name or alias: canonical record, facts, and linked memories |
| `remind_me_entity_traverse` | Multi-hop traversal of the typed entity-relation graph (1-3 hops, both directions, optional relation filter) — for questions that require chaining relations, not just co-mention |
| `remind_me_feedback` | Mark a memory helpful/unhelpful for a search result — a signed signal, distinct from the always-positive reinforcement of a plain access. Without `query`: global `base_weight`/vitality adjustment. With `query`: query-contextual instead — only applies to future searches with a similar query |

### CRUD

| Tool | Description |
|------|-------------|
| `remind_me_add` | Store a new memory with content, category, tags, metadata, optional SPO triple, entity mentions, and optional `sensitive` flag — see [Sensitive Memories](#sensitive-memories) |
| `remind_me_list` | List memories with filters (category, tags, source), pagination, and opt-in `include_sensitive` |
| `remind_me_get` | Retrieve a single memory by ID — always returns it, even if marked sensitive (a direct id lookup isn't "surfacing by default") |
| `remind_me_update` | Update a memory's content, category, tags, metadata, or `sensitive` flag |
| `remind_me_delete` | Permanently delete a memory |
| `remind_me_history` | List a memory's prior content revisions, newest first — see [Edit History](#edit-history) |
| `remind_me_revert` | Restore a memory to a prior revision — see [Edit History](#edit-history) |

### Reminders

| Tool | Description |
|------|-------------|
| `remind_me_set_reminder` | Set a future `remind_at` timestamp on an existing memory, or clear one already set (omit/null `remind_at`) — must be a valid ISO-8601 timestamp in the future |
| `remind_me_list_reminders` | List memories with a set reminder: `upcoming` (still in the future), `overdue` (due but not yet delivered — e.g. the server was offline), or `all` |
| `remind_me_digest` | Summarize recent additions, vault vitality, reminders, and sync health in one read — see [Digest](#digest) |

### Saved Searches

| Tool | Description |
|------|-------------|
| `remind_me_save_search` | Save a named, replayable `remind_me_search` query (`query`, `category`, `tags`, `include_sensitive`, `watch`). Saving again with an existing name updates it in place — see [Saved Searches](#saved-searches) |
| `remind_me_list_saved_searches` | List every saved search with its query, filters, and watch status |
| `remind_me_run_saved_search` | Re-run a saved search's stored query/filters — identical output to calling `remind_me_search` with those params |
| `remind_me_delete_saved_search` | Delete a saved search by name |

### Capture & decomposition

| Tool | Description |
|------|-------------|
| `remind_me_auto_capture` | Capture a full conversation dialog + distilled summary as two linked memories |
| `remind_me_get_capture` | Retrieve a linked dialog/summary pair by their shared capture_id |
| `remind_me_decompose` | Break a conversation capture into atomic facts with parent-child linking, SPO triples, and entity mentions |
| `remind_me_decompose_batch` | Fetch captures that have not been decomposed yet |

### Ingest-time normalization

| Tool | Description |
|------|-------------|
| `remind_me_normalize_batch` | Fetch raw document/chat import chunks that have not been normalized yet |
| `remind_me_normalize_apply` | Write a distilled `{question, summary, resolution?, refs?, entities?}` as a new memory, non-destructively linked back to the raw import |

### Entity graph & annotation

| Tool | Description |
|------|-------------|
| `remind_me_extract_batch` | Fetch memories that have no SPO triple and no entity mentions yet (backfill queue) |
| `remind_me_annotate` | Apply subject/predicate/object triples and entity mentions to existing memories in batch |

### Lifecycle

| Tool | Description |
|------|-------------|
| `remind_me_vitality_report` | Generate vault health metrics with decay and vitality scores |
| `remind_me_reclassify` | Apply a memory type classification to a single memory |
| `remind_me_reclassify_batch` | Fetch unclassified memories for batch classification |
| `remind_me_recalibrate_candidates` | Fetch old, high-importance memories that have never received a feedback signal, for review — pairs with `remind_me_reclassify`/`remind_me_reclassify_batch` (the apply half) and `remind_me_feedback` (a pure importance nudge); no separate apply tool |
| `remind_me_contradiction_candidates` | Fetch bounded pairs of memories sharing an entity that might conflict but weren't caught by structured-triple supersession, for review — pairs with `remind_me_update`/`remind_me_delete`/`remind_me_add` (the apply half); no separate apply tool |
| `remind_me_consolidate` | Find semantically similar memories, preview clusters (dry_run=true), and merge duplicates using an LLM-authored `summaries` entry per cluster (dry_run=false) — a cluster with no matching summary is skipped, not merged with a raw concatenation |

### LLM Wiki

| Tool | Description |
|------|-------------|
| `remind_me_wiki_write` | Create or replace a wiki page (full markdown body; H1 title added if absent; refreshes `index.md`/`log.md`) |
| `remind_me_wiki_read` | Read one page with its outgoing links and backlinks |
| `remind_me_wiki_list` | List all pages with their one-line summaries (the index) |
| `remind_me_wiki_search` | Full-text search the synthesised pages (distinct from `remind_me_search`, which searches raw memories) |
| `remind_me_wiki_load` | Load the whole wiki into context as one markdown document (token-budgeted) |
| `remind_me_wiki_delete` | Delete a page by title or slug |
| `remind_me_wiki_compile` | Two-phase synthesis: surface pending raw memories + the schema, then advance the watermark once integrated |

### Import, export & admin

| Tool | Description |
|------|-------------|
| `remind_me_import_chat` | Import a single chat export, document, PDF, image, or audio file (`kind`: auto/chat/document/pdf/image/audio/readwise/obsidian) |
| `remind_me_import_directory` | Bulk import all exports/documents/PDFs/images/audio from a directory |
| `remind_me_import_mempalace` | Bulk-import memories from a MemPalace ChromaDB store, one page at a time (requires the optional `mempalace` extra) |
| `remind_me_import_dbs` | Bulk-import memories from a [dbs](https://github.com/baileyrd/daily-backup-system) SQLite store, one page at a time — source and tags land as knowledge-graph entities, not flattened prose |
| `remind_me_list_connectors` | List every registered import connector (built-in and third-party) and which are valid `remind_me_import_chat` kinds |
| `remind_me_export_memories` | Export memories (+ entity graph by default) to JSON/JSONL, inline or to a file inside the export roots |
| `remind_me_stats` | View statistics: counts, categories, recent activity |
| `remind_me_reindex` | Build vector embeddings for any memories missing them |
| `remind_me_backup` | Create an on-demand, WAL-safe backup of the database under `MEMORY_DIR/backups/` |
| `remind_me_server_status` | Check dashboard, embedding, folder-watcher, and remote-connector state and verify DB connectivity |
| `remind_me_watch_status` | Folder watcher status: watched dirs, scan counters, recent errors |
| `remind_me_webhook_status` | Push/webhook ingestion status: bind/port, request counters, recent errors |
| `remind_me_revoke_clients` | List OAuth connector clients, or revoke one (with all of its tokens) |
| `remind_me_api_key` | Create, list, or revoke named, scope-limited (`read`/`read-write`) dashboard API keys — see [Authentication](#authentication) |
| `remind_me_check_update` | Check if a newer version is available on origin/main |
| `remind_me_self_update` | Pull latest changes from origin and reinstall the package |

51 tools + 8 prompts + 4 resources (`memory://stats`, `memory://categories`, `wiki://schema`, `wiki://index`).

### Prompts: the maintenance loops as one-shot workflows

Every LLM-driven maintenance workflow is a *sequence* — a batch tool surfaces work, Claude does the reasoning, an apply tool writes it back, and some loops then advance a watermark. The tools have always been there, but the sequencing lived only in this README, so running a loop meant remembering both the tool names and their order. Each loop is now also an **MCP prompt**, which clients surface as a user-invocable workflow (in Claude Code, `/mcp__remind-me__<name>`):

| Prompt | Drives |
|---|---|
| `decompose_facts` | `remind_me_decompose_batch` → `remind_me_decompose` |
| `normalize_imports` | `remind_me_normalize_batch` → `remind_me_normalize_apply` |
| `backfill_graph` | `remind_me_extract_batch` → `remind_me_annotate` |
| `classify_memories` | `remind_me_reclassify_batch` → `remind_me_reclassify` |
| `compile_wiki` | `remind_me_wiki_compile` → `remind_me_wiki_write` ×N → `mark_integrated=true` |
| `consolidate_duplicates` | `remind_me_consolidate` dry run → merge with LLM-authored summaries |
| `recalibrate_importance` | `remind_me_recalibrate_candidates` → `remind_me_reclassify`/`remind_me_feedback` |
| `review_contradictions` | `remind_me_contradiction_candidates` → `remind_me_update`/`remind_me_delete`/`remind_me_add` |

Every argument is optional (batch size, similarity threshold), so invoking a prompt bare runs the loop with the tool's own defaults. The two loops whose second phase is hard to undo — `compile_wiki`'s watermark advance and `consolidate_duplicates`' merge — put the preview phase first and say why, so the destructive step is never the first thing done.

### Tool profiles (context cost)

The full surface is 48 tools costing **~21k tokens of context in every session**, on every client, whether or not an admin tool is ever touched. For a server whose job is putting memories *into* context that's an awkward ratio — `remind_me_wiki_load` defaults to a 12k budget, so the tool definitions cost ~1.8× an entire wiki load before a single memory is retrieved.

```bash
REMIND_ME_TOOL_PROFILE=standard remind-me-mcp
```

| Profile | Tools | Context | Drops |
|---|---|---|---|
| `full` *(default)* | 48 | ~21k | nothing — today's behavior |
| `standard` | 30 | ~14.8k | imports, sync, backup, updater, ops |
| `core` | 17 | ~7.8k | the above, plus the maintenance loops and their prompts |

- **Default `full` means upgrading never narrows an existing deployment.** Opt in deliberately.
- **`standard` keeps the maintenance loops**, so a maintenance pass still works. `core` hides them *and* their prompts — a prompt that sequences tools the client can't see is worse than absent.
- **Hidden means gone**, not merely undocumented: pruned tools are unlistable *and* uncallable, so there's no listable/callable split to trip over.
- **`remind_me_server_status` survives every profile** and reports the active one plus its measured cost — a profile you can't diagnose from inside a session is a trap, and nobody goes hunting for an env var to fix a cost they can't see.
- A tool in neither tier is treated as admin, so a newly added tool can't silently smuggle itself into a narrowed surface.

**This is not a fix for tool-selection accuracy**, and it was originally proposed as one. The tools that genuinely compete — `remind_me_search`, `remind_me_list`, `remind_me_get`, `remind_me_entity`, all of which read as "find things" — are *every one of them in `core`*, so no profile can separate them. That confusion is addressed by [disambiguating their descriptions](#server-instructions) instead. Profiles buy context, and only context.

### Maintenance nudges

The backlog counts (`pending_wiki_compile` and friends) used to live only inside `remind_me_server_status` and `remind_me_watch_status` — tools a conversational session has no reason to call — so a growing queue of un-decomposed captures was invisible in practice. Search and add responses now carry a short nudge naming the deepest backlogs and the prompt that drains each:

```
---
**Maintenance pending** — run when convenient:
- 143 memories with no entity/triple annotation → `backfill_graph` prompt
- 38 memories not folded into the wiki → `compile_wiki` prompt
```

- **Throttled** — at most one *check* per `REMIND_ME_MAINTENANCE_NUDGE_INTERVAL` (default 1h). The timer is claimed *before* the counts are queried, so the hot path pays one clock comparison and nothing else in between; a quiet vault costs the same as a busy one.
- **Thresholded** — a queue must reach `REMIND_ME_MAINTENANCE_NUDGE_THRESHOLD` (default 25) to be mentioned. A handful of pending items is the normal steady state of a system in use, and nudging at 1 would just train the reader to ignore nudges.
- **Markdown paths only** — the nudge is prose, and appending it to a JSON envelope would make the response unparseable. Same wiring as the pre-existing update notice.
- **One definition per queue** — `remind_me_mcp/maintenance.py` owns the `WHERE` clauses, and the batch tools import them from there. A second copy would let the count Claude is nudged about drift from the batch the tool actually returns.
- Silence them with `REMIND_ME_MAINTENANCE_NUDGES=false`.

### Closing the feedback loop

`remind_me_feedback` tunes ranking, but nothing in a normal session ever asked for it, so the signal it depends on effectively never arrived. Two changes:

- The `query` parameter's description used to read *"for future audit/reporting"* — which is not what it does. Passing `query` switches the whole mechanism to query-contextual (the signal only affects future searches similar to this one); omitting it applies a **global** weight change that penalises the memory for every future query. A caller who believed the old description would reasonably omit it, which is a plain reason the query-contextual path stayed untrained. Now described accurately.
- Search responses carry an occasional hint pointing at the query-contextual form, throttled on its own timer (`REMIND_ME_FEEDBACK_HINT_INTERVAL`, default 2h).

A search response shows **at most one** advisory: a maintenance backlog is concrete work to do, so it outranks the standing feedback affordance. Stacking both would start training the reader to skip the tail of every search — the exact failure mode both signals exist to avoid.

### Capture health

`remind_me_auto_capture` only runs if you've added the [capture instruction](#auto-capture-persisting-full-conversations) to your client, so "never configured" and "configured but nothing captured yet" both used to present as pure silence — nothing anywhere distinguished them. `remind_me_server_status` now reports capture count and the last capture time, and says so explicitly when there are none:

```
**Conversation capture:** none recorded — `remind_me_auto_capture` is opt-in; add the
capture instruction to your client's custom instructions (see the README) if you
expected captures here
```

This is deliberately **reported, not nudged about**: capture is opt-in by design, so a vault with none is a legitimate configuration rather than a backlog to nag about.

### Server instructions

The server sends **instructions** in its MCP `initialize` response — guidance the client surfaces to Claude automatically, in every session and every client. It covers when to search before answering, when to store a fact (and to include `subject`/`predicate`/`object` + `entities` so it joins the graph), when to send feedback, and that batch/admin tools are operator workflows rather than conversational ones.

This used to be prose you pasted into each client's custom instructions by hand — which was per-client, silently absent wherever you forgot, and free to drift from the tools it described. It now ships with the server and is versioned alongside them (`SERVER_INSTRUCTIONS` in `remind_me_mcp/server.py`).

### Auto-Capture: Persisting Full Conversations

The `remind_me_auto_capture` tool stores **two linked memories** from each conversation:

1. **Dialog** (category: `dialog`) — the full verbatim conversation, every turn preserved
2. **Summary** (category: `conversation`) — a concise distillation of key topics, decisions, facts, and preferences

Both memories share a `capture_id` in their metadata, so you can retrieve them together with `remind_me_get_capture`.

The [server instructions](#server-instructions) already tell Claude to store durable facts as they come up, with no per-client setup. Capturing the *whole* conversation at the end of every session is a stronger, more opinionated behavior, so it stays opt-in — add this to your Claude Desktop or Claude.ai custom instructions if you want it:

```
At the end of every conversation, use the remind_me_auto_capture tool to save:
- The full conversation dialog (all turns verbatim)
- A concise summary covering: topics discussed, decisions made, facts learned,
  preferences expressed, and action items
Use descriptive titles and relevant tags. Do this automatically without asking.
```

**How it works when searching:**
- Searching for "FastAPI" finds both the summary and the full dialog
- Summaries are compact and appear first in relevance-ranked results
- Full dialogs contain every detail for when you need exact context
- Use `remind_me_get_capture` with a capture_id to see both side by side

## Semantic Search (Vector Embeddings)

The server supports **hybrid search**: FTS5 keyword matching combined with semantic vector similarity via `sqlite-vec` and a local ONNX embedding model. This means searching for "Python concurrency" also finds memories about "asyncio coroutines" even if those exact words aren't used.

### Enabling Semantic Search

Install the optional dependencies:

```bash
pip install sqlite-vec onnxruntime tokenizers huggingface-hub numpy
# Or with uv:
uv pip install "remind-me-mcp[semantic]"
```

The embedding model (`all-MiniLM-L6-v2`, ~80MB) downloads automatically on first use and is cached in `~/.remind-me/models/`.

### How It Works

- **On add/update/import**: each memory is embedded and stored in a `sqlite-vec` vector table alongside the existing FTS5 index
- **On search**: both FTS5 (keyword) and vector (semantic) results are merged, deduplicated, and ranked together
- **Graceful fallback**: if the embedding dependencies aren't installed, everything still works — you just get FTS5 keyword search only
- **Results are labeled** with their search method: ⚡ hybrid (matched both), 🔮 semantic only, 🔤 keyword only

### Language Coverage

Both the default embedding model and the default reranker (see [Changing the Embedding Model](#changing-the-embedding-model) and the [`REMIND_ME_RERANK_MODEL` reference below](#environment-variables)) are optimized for specific languages, not general-purpose multilingual retrieval — worth knowing before relying on semantic search over non-English content:

- **Embedding model** — the default [`sentence-transformers/all-MiniLM-L6-v2`](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2) is trained and evaluated on English sentence pairs. It has no documented multilingual training data or evaluation, so semantic similarity for non-English content (or cross-lingual queries) is unreliable — expect it to behave close to random for languages it wasn't trained on. FTS5 keyword search is unaffected (it matches literal terms regardless of language), so hybrid search degrades to keyword-only quality for non-English content rather than failing outright.
- **Reranker** — the default [`BAAI/bge-reranker-base`](https://huggingface.co/BAAI/bge-reranker-base) is a **bilingual Chinese/English** cross-encoder (it pairs with BGE's `bge-large-en-v1.5`/`bge-large-zh-v1.5` embedding models), not a general multilingual model. It reorders candidates well for English or Chinese queries; for any other language it's providing little more signal than chance. That said, reranking only ever *reorders* the existing RRF-ranked candidate list — it never filters — so a bad rerank on unsupported-language content degrades at worst to the plain RRF order, never to dropped results.
- If your vault is primarily non-English (or mixed-language), see the multilingual embedding model recommendation below; there is currently no non-English/multilingual alternative wired up for the reranker (`REMIND_ME_RERANK=""` disables reranking entirely if it's doing more harm than good for your content).

### Scaling Semantic Search (ANN Index)

By default, semantic search does an exact brute-force scan over every stored chunk vector via `sqlite-vec` — fast enough for a typical personal store, but it gets slower as the number of chunks grows (linearly with the store size). Once a store passes a size threshold, an optional HNSW approximate-nearest-neighbor index (via [`usearch`](https://github.com/unum-cloud/usearch)) takes over automatically:

```bash
pip install usearch
# Or with uv:
uv pip install "remind-me-mcp[ann]"
```

- **Automatic, size-gated**: below `REMIND_ME_ANN_MIN_CHUNKS` (default 5000) chunk vectors, the exact brute-force scan is used — an approximate index would only add overhead for no benefit at typical single-user scale. Above it, the ANN index serves the search instead, transparently — same result shape, same `semantic_distance` meaning.
- **Self-healing**: the index lives in memory for the life of the process and is persisted to `~/.remind-me/ann_index.usearch` on clean shutdown. A missing, corrupt, or out-of-date index file (e.g. after a hard crash) triggers an automatic rebuild from the stored vectors on next use.
- **Graceful fallback**: if `usearch` isn't installed, or the ANN path fails for any reason, search transparently falls back to the exact brute-force scan — never a broken or empty result.
- Check `remind_me_server_status` for the current ANN index state (built, vector count, threshold).

| Variable | Default | Description |
|---|---|---|
| `REMIND_ME_ANN_MIN_CHUNKS` | `5000` | Chunk-vector count above which semantic search uses the ANN index instead of the exact brute-force scan |

### Reindexing Existing Memories

If you enable semantic search after already having memories stored, run reindex to backfill embeddings:

```
Use remind_me_reindex
```

Or ask Claude: "Reindex my memories for semantic search."

This only generates embeddings for memories that don't have them yet — existing embeddings are preserved.

### Changing the Embedding Model

Switching `REMIND_ME_EMBEDDING_MODEL`, `REMIND_ME_EMBEDDING_DIM`, or `REMIND_ME_EMBEDDING_BACKEND` no longer requires remembering to manually reindex. The server records which model/dimension/backend produced the vectors currently stored, and detects a mismatch automatically at startup: stale vectors (and the on-disk ANN index, if built) are cleared so search never silently serves results from the wrong embedding space, and every memory falls through to the normal "missing embeddings" path — run `remind_me_reindex` to rebuild them under the new model.

**Recommended override for multilingual vaults**: [`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2) — trained on parallel data for 50+ languages, widely adopted for multilingual semantic search, and a genuine drop-in: it outputs the same 384-dimensional vectors as the default model, so only `REMIND_ME_EMBEDDING_MODEL` needs to change, not `REMIND_ME_EMBEDDING_DIM`:

```bash
REMIND_ME_EMBEDDING_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
```

Then run `remind_me_reindex` (the model-mismatch detection above clears stale vectors automatically). **Tradeoff, not a strict upgrade**: a multilingual model spreads its capacity across 50+ languages instead of specializing in one, so on an English-only vault it typically scores somewhat below the English-specialized `all-MiniLM-L6-v2` default — only switch if your content actually spans languages.

For heavier multilingual needs (100+ languages, much longer documents, or combined dense/sparse/multi-vector retrieval), [`BAAI/bge-m3`](https://huggingface.co/BAAI/bge-m3) is a stronger option in principle, but it is **not** a same-effort drop-in with this server's ONNX loader today: the official `BAAI/bge-m3` repo doesn't publish an `onnx/model.onnx` at the path `embeddings.py`'s loader expects (only community-converted mirrors do, e.g. `aapot/bge-m3-onnx`, unverified here for correctness/currency), and it outputs 1024-dimensional vectors, so it would also need `REMIND_ME_EMBEDDING_DIM=1024`. Treat it as a "possible future option requiring more verification," not a documented recommendation like the multilingual MiniLM model above.

### Checking Status

Use `remind_me_server_status` to see how many memories have embeddings and whether the model is loaded.

## PDF and Image Import

Two more import kinds, `pdf` and `image`, sit alongside `chat`/`document` — same `remind_me_import_chat`/`remind_me_import_directory` tools, same hash-dedup and `kind=auto` routing, just two more optional extras so a base install doesn't pull in PDF/OCR dependencies.

### Enabling PDF Import

```bash
pip install pypdf
# Or with uv:
uv pip install "remind-me-mcp[pdf]"
```

A `.pdf` file is chunked **per page** via [`pypdf`](https://pypdf.readthedocs.io/) (pure-Python — no system binary like poppler-utils required); each chunk's metadata carries a `page` number, the same role the `document` connector's Markdown heading breadcrumb plays. A page too long for one memory is split further, with every resulting sub-chunk still tagged with that page's number.

### Enabling Image (OCR) Import

```bash
pip install rapidocr-onnxruntime
# Or with uv:
uv pip install "remind-me-mcp[image]"
```

A `.png`/`.jpg`/`.jpeg` file is OCR'd via [RapidOCR](https://github.com/RapidAI/RapidOCR) into a single memory (whole image as one chunk). **Why RapidOCR over `pytesseract`:** this server already depends on `onnxruntime` for the embedder/reranker, so an ONNX-based OCR engine reuses infrastructure already present rather than adding a new runtime family — and RapidOCR's detection/recognition models ship *inside* the pip package itself, so OCR works fully offline with no HuggingFace download (unlike the embedder/reranker). `pytesseract` was considered and rejected: it additionally requires the system `tesseract` binary, which pip can't install and which isn't present in this project's CI/dev images.

**Language coverage**: the connector constructs `RapidOCR()` with no arguments, so it loads the models bundled inside the `rapidocr-onnxruntime` package — the `ch_PP-OCRv4` detection and recognition models plus a `ch_ppocr_mobile_v2.0` orientation classifier. That recognition model's character set (baked into the model itself) covers **Chinese and English/Latin script + digits only**; other scripts — Japanese, Korean, Arabic, Cyrillic, Devanagari, and others — are not recognized (detection may still find text regions, but recognized characters will be garbage). If you need OCR for one of those, three optional env vars pass straight through to RapidOCR's own model-path constructor arguments, so you can point the connector at an alternate-language model downloaded separately from [RapidOCR's model zoo](https://github.com/RapidAI/RapidOCR) — unset by default, so behavior is unchanged unless you opt in:

| Variable | Default | Description |
|---|---|---|
| `REMIND_ME_OCR_DET_MODEL_PATH` | *(unset)* | Path to an alternate ONNX text-detection model (`RapidOCR(det_model_path=...)`) |
| `REMIND_ME_OCR_CLS_MODEL_PATH` | *(unset)* | Path to an alternate ONNX text-orientation-classification model (`RapidOCR(cls_model_path=...)`) |
| `REMIND_ME_OCR_REC_MODEL_PATH` | *(unset)* | Path to an alternate ONNX text-recognition model (`RapidOCR(rec_model_path=...)`) — this is the one whose character set actually determines what script(s) get recognized; pair it with a matching `_DET_MODEL_PATH` if the target script needs different detection geometry |

### Without These Extras

Importing a `.pdf`/image file without its extra installed returns a clear, actionable error (e.g. *"PDF import requires the 'pdf' extra: pip install remind-me-mcp[pdf]"*) — not a bare `ModuleNotFoundError` traceback. Every other import kind, and the rest of the server, is completely unaffected either way.

## Audio Import

A third import kind, `audio`, sits alongside `chat`/`document`/`pdf`/`image` — same `remind_me_import_chat`/`remind_me_import_directory` tools, same hash-dedup and `kind=auto` routing, one more optional extra so a base install doesn't pull in a Whisper runtime.

### Enabling Audio Import

```bash
pip install faster-whisper
# Or with uv:
uv pip install "remind-me-mcp[audio]"
```

An `.mp3`/`.m4a`/`.wav`/`.ogg` file is transcribed via [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (a CTranslate2 re-implementation of OpenAI's Whisper) and chunked **per transcript segment** — Whisper's own sentence/phrase-level output unit — with each chunk's metadata carrying a `start`/`end` timestamp (seconds), the same role a PDF's `page` number plays. A segment too long for one memory is split further, with every resulting sub-chunk still tagged with that segment's full timestamp range.

**Library choice.** This was researched and verified against real audio in a sandbox before committing to it, in order of preference:

1. An ONNX-based option ([`onnx-asr`](https://github.com/istupakov/onnx-asr), which does support a Whisper model over `onnxruntime` — the same runtime this server already depends on for the embedder/reranker/OCR) was tried first, but rejected: it only accepts raw PCM WAV, with no built-in decoder for compressed containers at all — three of this feature's four required extensions (`.mp3`/`.m4a`/`.ogg`) would need a *second* new dependency just for decoding.
2. [`pywhispercpp`](https://github.com/absadiki/pywhispercpp) (Python bindings for whisper.cpp — lighter-weight than a full CTranslate2/PyTorch runtime) was tried next. It worked fine on a plain 16kHz WAV, but failed reproducibly on `.mp3`: it shells out to a **system** `ffmpeg` binary for anything other than 16kHz mono WAV, and a sandbox with no `ffmpeg` installed hit `Exception: FFMPEG is not installed or not in PATH.` — reintroducing the exact class of dependency this project's own precedent already rejected once (`pdf_import.py` chose pure-Python `pypdf` over poppler-utils; `image_import.py` chose RapidOCR over pytesseract's system `tesseract` binary).
3. **`faster-whisper` (chosen).** Verified end-to-end with a real synthesized speech clip, including a re-encoded `.mp3` of the same clip — the exact case that eliminated pywhispercpp. It decodes every common audio container out of the box via its own bundled [PyAV](https://github.com/PyAV-Org/PyAV) (`av`) dependency, which ships a statically-linked ffmpeg build inside its own wheel — no system `ffmpeg` binary required. It also already shares three of its four dependencies (`onnxruntime`, `huggingface-hub`, `tokenizers`) with this project's own `semantic` extra.
4. `openai-whisper` (the heavier, plain-PyTorch reference implementation) was the documented last-resort fallback; not needed since faster-whisper worked cleanly.

**Model size.** Defaults to `base` (~145MB int8-quantized, ~74M parameters) — deliberately smaller than Whisper's largest (`large-v3`, ~3GB), trading some transcription accuracy for a small download and fast CPU-only inference. Consistent with this being a local-first tool's DEFAULT, not its ceiling:

| Variable | Default | Description |
|---|---|---|
| `REMIND_ME_AUDIO_MODEL` | `base` | Whisper model size/name (`tiny`/`base`/`small`/`medium`/`large-v2`/`large-v3`/`large-v3-turbo`/`distil-*`/etc., or a full HuggingFace repo id for a custom CTranslate2-converted model) — set to `small` or larger for noticeably better accuracy if you have the CPU/RAM/disk budget, no code change needed |

The model downloads from HuggingFace Hub on first use and caches under `MODEL_DIR` (the same directory the embedder/reranker cache their own models in), and runs on CPU only, matching this server's other in-process models.

### Without This Extra

Importing an audio file without `faster-whisper` installed returns a clear, actionable error (*"Audio import requires the 'audio' extra: pip install remind-me-mcp[audio]"*) — not a bare `ModuleNotFoundError` traceback. Every other import kind, and the rest of the server, is completely unaffected either way.

## CLI

For quick one-shot access without going through an MCP client — scripting, cron jobs, or just checking something from a terminal — `remind-me-mcp` also accepts three direct subcommands, alongside its existing flags:

```bash
remind-me-mcp add "buy oat milk on the way home" [--category CAT] [--tags a,b,c]
remind-me-mcp search "wifi password" [--limit N] [--json]
remind-me-mcp list [--limit N] [--category CAT] [--json]
```

- `add` stores a memory and prints its id — the same `remind_me_add` logic an MCP client would trigger, not a separate implementation.
- `search` runs the same hybrid FTS5 + semantic retrieval as `remind_me_search`, printing Markdown by default or JSON with `--json`.
- `list` browses by filter (no ranking) exactly like `remind_me_list`, same output options.

These operate on the exact same `REMIND_ME_MCP_DIR` (and therefore the exact same `memory.db`) as the server — set the env var the same way for both, or rely on the shared `~/.remind-me` default. A CLI command can run safely at any time, whether or not a server is currently running: it opens an ordinary WAL-mode SQLite connection (the same one the server itself opens) and closes it when done, and it deliberately never touches the server's own single-instance lock file (`MCP_PID_FILE`, issue #126) — that lock only prevents two *server* processes (each running background sync/watcher/scheduler threads) from racing each other, not a short-lived CLI read or write. A first invocation against a fresh `REMIND_ME_MCP_DIR` auto-initializes the database exactly like a fresh server start does — there is no separate "init" step.

## Backups

For a single-user app where one SQLite file holds someone's entire memory store, a failed or buggy migration (or just wanting a checkpoint before a risky bulk edit) needs a real safety net, not a reminder to "remember to copy the file."

- **On-demand** — run `remind_me_backup` any time. It uses SQLite's WAL-safe `Connection.backup()` API, so it's safe even while the server is actively handling other requests, unlike a raw file copy (which could capture a torn or partially-checkpointed page mid-write). Backups land under `MEMORY_DIR/backups/`.
- **Pre-migration snapshot** — every server startup that has a pending schema migration takes an automatic snapshot first, so a migration that fails outright, or completes but is semantically wrong, can be rolled back by restoring it. Skipped for a brand-new, empty database (nothing to protect yet). A snapshot failure (e.g. disk full) is logged and never blocks the migration itself.
- **Retention** — only the most recent `REMIND_ME_BACKUP_RETENTION_COUNT` backups (default 10, manual and pre-migration combined) are kept; older ones are pruned automatically after each new backup.
- Check `remind_me_server_status` for the current backup count and most recent backup timestamp.
- **Restoring** — with the server stopped:
  ```bash
  python -m remind_me_mcp --list-backups
  python -m remind_me_mcp --restore pre-migration-v12-20260101T000000Z.db --yes
  ```
  Validates the backup (`PRAGMA integrity_check` plus a sanity check that it's actually a remind-me database) before touching anything, and snapshots the *current* database first so a bad restore is itself recoverable. Refuses to run while an MCP server is holding the lock on this database. `--restore` accepts either a bare filename (resolved against the backups directory) or a full path to any valid backup file.

### Cloud Backup Upload

Optional, opt-in, off by default. Setting `REMIND_ME_BACKUP_S3_BUCKET` (requires `pip install remind-me-mcp[cloud-backup]`) makes every backup `remind_me_backup` or the pre-migration snapshot writes also get uploaded to S3 or an S3-compatible bucket, as a strict post-success hook — it runs only after the local backup file is already fully written under its final name, and a failed or unconfigured cloud upload never affects the local backup, which remains the primary guarantee.

- `REMIND_ME_BACKUP_S3_BUCKET` — target bucket name. Empty (default) disables cloud upload entirely.
- `REMIND_ME_BACKUP_S3_PREFIX` — optional key prefix within the bucket (e.g. `my-host/backups`). Empty (default) uploads at the bucket root. The object key is `<prefix>/<filename>`, reusing the exact filename the local backup already has, so local and cloud backups correspond 1:1.
- `REMIND_ME_BACKUP_S3_ENDPOINT_URL` — optional S3-compatible endpoint override, e.g. `https://s3.us-west-002.backblazeb2.com` (Backblaze B2) or `http://localhost:9000` (a self-hosted MinIO). Unset (default) means real AWS S3. `boto3`'s S3 client works against essentially any S3-compatible provider through this one setting — no per-provider integration needed.
- `REMIND_ME_BACKUP_S3_REGION` — optional AWS region, passed through to the S3 client.
- **Credentials are never a new env var here.** `boto3` already has its own standard credential resolution chain — `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` env vars, the shared `~/.aws/credentials` file, an EC2/ECS/Lambda instance role, and so on — and this feature relies on that chain as-is rather than inventing a parallel, bespoke `REMIND_ME_BACKUP_S3_*` secret to configure and keep safe.

**The plaintext-upload gate.** Whether cloud upload is safe by default depends on [Encryption at Rest](#encryption-at-rest), below:
- If `REMIND_ME_DB_ENCRYPTION_KEY` **is** set, the local backup file is already SQLCipher ciphertext — uploading it as-is to cloud storage is safe by default, no extra flag needed.
- If it is **not** set, the local backup file is plaintext personal data, and uploading plaintext personal data to a third-party bucket is a real, distinct risk on top of everything else this tool already does locally. Cloud upload is refused with a clear error explaining why, unless `REMIND_ME_BACKUP_S3_ALLOW_PLAINTEXT_UPLOAD=1` is explicitly set — this needs deliberate consent, not silent default behavior.

A missing `boto3` (bucket configured but the `cloud-backup` extra not installed) or any upload failure (network, credentials, wrong bucket, ...) is logged clearly and never fails the local backup that already succeeded.

## Encryption at Rest

Optional, opt-in, off by default. Setting `REMIND_ME_DB_ENCRYPTION_KEY` (requires `pip install remind-me-mcp[encryption]`) encrypts `memory.db` and its backups at rest via [SQLCipher](https://www.zetetic.net/sqlcipher/). Not for every user or install — see [ARCHITECTURE.md's "Encryption at rest" design note](ARCHITECTURE.md#encryption-at-rest-is-opt-in-not-default-issue-184) for the full rationale, what is and isn't covered, and the v1 adoption story (encryption must be enabled before an install's first run; there's no in-place re-encryption of an existing plaintext database yet).

## Importing Chats & Documents

The import tools (`remind_me_import_chat`, `remind_me_import_directory`, `POST /api/import`) share one pipeline: hash-based deduplication (re-importing the same file content is a no-op), batched embedding, and a `kind` parameter that controls parsing.

### Import Kinds

| Kind | Behavior |
|------|----------|
| `auto` *(default)* | `.json`/`.jsonl` always parse as chat. `.pdf` always parses as pdf; `.png`/`.jpg`/`.jpeg` always parses as image; `.mp3`/`.m4a`/`.wav`/`.ogg` always parses as audio. `.md`/`.markdown`/`.txt` are content-sniffed: files with chat role markers (`**User:**`, `## Assistant`, …) import as chat, everything else as a document. **Never** resolves to `readwise` by content sniffing — see below. **Does** resolve to `obsidian` for a `.md`/`.markdown` file inside a detected Obsidian vault (directory context, not content sniffing) — see [Importing from Obsidian](#importing-from-obsidian) |
| `chat` | Force the chat-export parser (chunked per-message) |
| `document` | Force document chunking (`.md`/`.markdown`/`.txt` only) |
| `pdf` | Force per-page PDF chunking (`.pdf` only; requires the optional `pdf` extra) — see [PDF and Image Import](#pdf-and-image-import) |
| `image` | Force OCR of an image into a single memory (`.png`/`.jpg`/`.jpeg` only; requires the optional `image` extra) — see [PDF and Image Import](#pdf-and-image-import) |
| `audio` | Force per-segment transcription of an audio file (`.mp3`/`.m4a`/`.wav`/`.ogg` only; requires the optional `audio` extra) — see [Audio Import](#audio-import) |
| `readwise` | Force a Readwise "Export" JSON file into one memory per highlight (`.json` only, must be requested explicitly — never chosen by `auto`) — see [Importing from Readwise](#importing-from-readwise) |
| `obsidian` | Force frontmatter/wikilink/inline-`#tag`-aware Markdown import (`.md`/`.markdown` only) — see [Importing from Obsidian](#importing-from-obsidian) |

Document imports chunk Markdown per-section (the heading context is kept with each chunk and stored in metadata) and plain text per-paragraph. They get `source: document_import` and default to category `document`.

Imports are restricted to paths inside `REMIND_ME_IMPORT_ROOTS` (default: your home directory) — enforced by both the MCP tools and the HTTP API.

### Pluggable Connectors

Every import kind — including the built-in `chat`, `document`, `pdf`, `image`, `readwise`, and `obsidian` — is a plain parser function registered by kind string in `remind_me_mcp/importer.py`, not a hardcoded dispatch. `remind_me_import_chat`/`remind_me_import_directory`/`POST /api/import` resolve the effective kind (by extension, or by content-sniffing for `auto`, or by whatever the caller forced) and then look it up in one registry. A third-party module can register more kinds **without touching `importer.py` at all** — this is the whole point of the registry, and it's meant to be actually used by someone outside this codebase, not just an internal implementation detail. What follows is the contract, not just a pointer to source.

**Registering a connector.** Call `register_connector(kind, parser)` at import time (module-level, right after defining `parser` — see every built-in below):

```python
from remind_me_mcp.importer import register_connector

def my_connector(raw: str, meta: dict) -> tuple[list[tuple[str, dict]], int]:
    ...

register_connector("my_kind", my_connector)
```

- `kind` is any string. It's only reachable through `remind_me_import_chat`/`remind_me_import_directory`/`POST /api/import` if it's also added to `importer.IMPORT_KINDS` (a first-party change) *and*, if it needs `kind="auto"` to route to it, wired into the effective-kind resolution in `_ingest_parsed` — most third-party connectors instead register purely for **discovery** (`remind_me_list_connectors`) and drive their own bespoke ingestion function/tool, the way `mempalace_import.py` and `dbs_import.py` do (see below): a MemPalace drawer or a dbs item arrives individually from a paginated read, not as one raw file, so neither ever flows through the file-based `import_chat_file` pipeline at all.
- The parser's signature is fixed: `(raw: str, meta: dict[str, Any]) -> tuple[list[tuple[str, str_or_dict]], int]` — concretely, `(content, chunk_metadata)` pairs plus a raw-entry count. `raw` is the file's content decoded as UTF-8 (`errors="replace"`); a binary format (like `pdf`/`image`) instead reads `meta["raw_bytes"]` — the *undecoded* original bytes — and ignores `raw` entirely, since UTF-8-decoding binary data would corrupt it. `meta` also carries `suffix`, `extract_mode`, and `max_length`.
- Each `content` string becomes one memory's content; each paired `chunk_metadata` dict is merged into that memory's stored `metadata` JSON — this is the mechanism for attaching source-specific context (a PDF's page number, a Readwise highlight's book title/author) without threading a special case through the shared pipeline. Use `remind_me_mcp.importer._chunk_text(text, max_length)` to split anything that might exceed `max_length` — every built-in connector does, so oversized content is handled the exact same way everywhere.
- The `int` return value is the count of logical source units found *before* chunking (e.g. messages, or highlights) — it becomes the `raw_entries` field of the import result. For a connector where chunking *is* the extraction unit (like `document`), it just equals the number of chunks returned.

**What you get for free.** Once a connector returns that shape, `_ingest_parsed` (the one function every kind funnels through) handles everything else identically: SHA-256 content-hash dedup against `chat_imports` (re-importing the same file is a no-op), assigning deterministic memory ids, batched embedding, and the `doc_id`/`chunk_index` bookkeeping that makes neighbor-aware chunk retrieval and `remind_me_undo_import` work. None of that is something a connector author needs to think about, let alone reimplement.

**Reference implementations**, roughly in order of how much of the shared pipeline they use:

- `remind_me_mcp/readwise_import.py` — the fullest example of "just implement the parser": turns a Readwise export into one `(highlight_text[+note], {book/author/... metadata})` pair per highlight, and nothing else — dedup, chunking, and embedding are entirely `_ingest_parsed`'s job. Also the best example of *deliberately not* joining `kind="auto"`'s content-sniffing (documented in its own module docstring) when a format shares a suffix with an existing kind and can't be told apart reliably.
- `remind_me_mcp/obsidian_import.py` — the best example of a connector *wrapping* another connector's chunker (`_parse_document`) instead of reimplementing chunking, and of the two reserved chunk-metadata keys, `extra_tags`/`mention_entities`, that let a connector hook into tag/entity handling generically rather than duplicating it. Also the reference for reaching `kind="auto"` through *directory* context (`config.resolve_import_kind`) instead of content-sniffing when content-sniffing alone would be unreliable.
- `remind_me_mcp/dbs_import.py` — a connector registered purely for discovery (`remind_me_list_connectors`), with its own dedicated tool (`remind_me_import_dbs`) and bespoke per-item dedup/supersession loop, because dbs items arrive individually from a live SQLite read rather than as one file. Read this one if your source is a live store you'd page through, not a static export file.
- `remind_me_mcp/mempalace_import.py` — the same discovery-only pattern as `dbs_import.py`, for a ChromaDB-backed store.

Call `remind_me_list_connectors` to see every registered connector and which subset are valid `remind_me_import_chat`/`remind_me_import_directory` kinds (`IMPORT_KINDS` — narrower than the full registry, for exactly the discovery-only reason above).

### Importing from dbs

[dbs](https://github.com/baileyrd/daily-backup-system) archives a user's data from many external sources (Reddit, YouTube, Raindrop, GitHub stars, podcasts, ...) into one SQLite database with a uniform `items`/`sources` schema. `remind_me_import_dbs` reads that database directly (read-only, no dependency on the `dbs` package itself) and imports each live item as a memory — with dbs's source and tags preserved as first-class knowledge-graph entities (linked via `memory_entities`), not flattened into note prose:

```
remind_me_import_dbs(db_path="/path/to/dbs.sqlite3")
```

- Already-imported items are skipped when unchanged, tracked by `(dbs source, external_id)` plus a content hash (the `dbs_imports` table) — reruns, including paging through a large database with `limit`/`offset`, are safe.
- An item whose content changed since its last import (detected by content_hash, not a creation-date cutoff) gets a *fresh* memory, with the previous one marked `superseded_by` the new id — the same pattern the folder watcher uses for a changed file. This is what lets this path pick up edited items with no equivalent to the file-export pipeline's `item_created_at`-only staleness gap (see dbs's own `docs/BACKLOG.md` #4).
- `source`/`item_type` filter to one dbs source or item kind; `tags` adds extra tags to every imported memory; `dry_run` reports what would happen without writing.

This is the highest-fidelity of the three ways to feed dbs's collected content into remind_me (see dbs's `docs/remind-me-integration-review-2026-07-21.md` for the other two — an unzipped-notes export watched by `REMIND_ME_WATCH_DIRS`, or a per-item webhook push) — the only one that gives Claude entity-level provenance (which source, which tags) instead of prose to parse back out.

### Importing from Readwise

[Readwise](https://readwise.io) exports your highlights as JSON — either via Settings → Export, or by calling its documented Export API (`GET https://readwise.io/api/v2/export/`, see [readwise.io/api_deets](https://readwise.io/api_deets)) and saving the response. Import the resulting file with `kind` set explicitly:

```
Use remind_me_import_chat with:
  file_path: ~/Downloads/readwise-export.json
  kind: readwise
```

- **`kind=readwise` is never inferred by `auto`.** A Readwise export and an arbitrary chat export are both plain `.json` files, and unlike the Markdown chat-role-marker sniff `auto` already does, there's no reliable, false-positive-free way to content-sniff a Readwise export apart from other JSON shapes — guessing wrong would risk silently misrouting an existing chat export. You always have to ask for `readwise` by name.
- **One memory per highlight**, not one memory per book/article. A highlight is Readwise's own atomic unit — grouping a book's highlights into one memory would force every highlight to compete with every other highlight from the same book for search ranking and embedding budget, which is exactly the retrieval precision a memory store exists to protect. Book context isn't lost, just demoted to metadata: every highlight's memory carries the book/article's `title`, `author`, `category` (`books`/`articles`/`tweets`/`podcasts`/...), and `source_url`, plus the highlight's own `location`, `highlighted_at`, `tags`, and Readwise highlight URL, all under a `readwise_`-prefixed key in metadata.
- **A highlight's own note is appended to its content**, not discarded — `"{highlight text}\n\nNote: {your note}"` — since the note is often the actual reason you highlighted the passage, and only memory content participates in full-text search.
- Same hash-based dedup, chunking, and embedding as every other import kind (it flows through the same shared pipeline) — re-importing the same export file is a no-op.

### Importing from Obsidian

An [Obsidian](https://obsidian.md) vault is just a directory of Markdown files, so it works with the same watched-folder/bulk-directory-import machinery every other document import uses — the `obsidian` kind adds understanding of Obsidian's own conventions on top:

```
Use remind_me_import_directory with:
  directory: ~/Documents/MyVault
```

That's it — **no `kind` argument needed**. `remind_me_import_directory` (and the folder watcher, if you point `REMIND_ME_WATCH_DIRS` at the vault instead) auto-detects the vault by the `.obsidian/` directory Obsidian itself already creates at the vault's root, and imports every `.md`/`.markdown` file with the `obsidian` kind automatically. You can also force it explicitly on a single file (`kind: obsidian`) for a note outside any detected vault.

- **YAML frontmatter.** A leading ```---\n...\n---``` block's `tags:` field (list or a single comma-separated string) becomes memory tags; every other field lands under `metadata.obsidian_frontmatter`. Frontmatter this codebase's parser can't represent (nested/flow mappings, anchors, block scalars — real Obsidian frontmatter is almost always flatter than this) degrades to "skip frontmatter, ingest the body" rather than crashing; the delimited block is still stripped from the stored content either way.
- **`[[Wikilinks]]`.** `[[Note]]`, `[[Note|Display Text]]`, and `[[Note#Heading]]` are all recognized. Each resolves to a knowledge-graph entity for the linked note's *title* — created or matched via the same entity-upsert machinery `remind_me_entity`/FT-04 already use — and the memory is linked to it as a mention, so `remind_me_entity`/`remind_me_search`'s `entity:"..."` syntax can find it. A link to a note title that hasn't been imported yet (or never will be) still resolves — order doesn't matter. **v1 limitation**: the `#Heading`/`^block-id` anchor is stripped, not tracked — `[[Note#Overview]]` resolves to the same entity a plain `[[Note]]` would, not to a specific section.
- **Inline `#tags`.** Distinct from a Markdown heading (`# Heading` has a space after the `#`; a tag doesn't) and from a wikilink's own heading anchor. Extracted from the body and merged into the memory's tags, deduplicated (case-insensitively) against frontmatter tags. A `#tag` inside a fenced code block or an inline code span is never mistaken for a real tag.
- **Why frontmatter parsing is hand-rolled, not `pyyaml`.** `pyyaml` isn't a direct, always-installed dependency of this project's base install (it only shows up transitively through optional extras like `semantic`/`image`), and this codebase consistently prefers hand-rolling small, bounded formats over adding a dependency for them (`rate_limit.py`, `telemetry.py`, `metrics.py`'s Prometheus exposition format). Real Obsidian frontmatter is overwhelmingly flat `key: value`/`key: [list]`/block-list shapes, which the built-in parser covers directly.
- **Why a separate `obsidian` kind, not a `document` enhancement.** Keeping it a distinct kind (mirroring `pdf`/`image`/`readwise`) means an ordinary, non-Obsidian Markdown `document` import is completely unaffected. Chunking itself isn't reimplemented — the connector strips frontmatter and hands the body to the same per-section chunker `document` uses.
- **`.obsidian/` is never scanned for content.** It's Obsidian's own internal config folder (plugin settings, workspace state) — the folder watcher's existing hidden-directory skip already excludes it, same as any other dot-directory.

### Claude Export Format

Export your Claude conversations from claude.ai (Settings → Export Data), then:

```
Use remind_me_import_directory with:
  directory: ~/Downloads/claude-export/
  extract_mode: assistant_messages
  tags: ["claude", "historical"]
```

### Supported Extract Modes

| Mode | What it extracts |
|------|-----------------|
| `assistant_messages` | Only Claude/assistant responses (default — best for building a knowledge base) |
| `user_messages` | Only your messages |
| `all_messages` | Both sides, prefixed with role |
| `conversations` | Entire conversations as single memories |
| `summaries` | Only entries with 'summary' in the role |

### Supported Formats

- **JSON**: Claude exports (`chat_messages` with `content` arrays), OpenAI exports (`messages` with `role`/`content`), or any `[{role, content}]` array
- **JSONL**: One message or conversation per line
- **Markdown**: Chat exports (headings or bold markers for roles: `## Human`, `**Assistant:**`, …) or plain notes (imported as documents)
- **Plain text** (`.txt`): imported as documents, chunked per-paragraph
- **Readwise export** (`.json`, `kind=readwise` required — see [Importing from Readwise](#importing-from-readwise)): one memory per highlight
- **Obsidian vault notes** (`.md`/`.markdown`, `kind=obsidian` — auto-detected for a `.obsidian/`-marked directory, see [Importing from Obsidian](#importing-from-obsidian)): frontmatter tags, `[[wikilinks]]` resolved to entities, and inline `#tags`, chunked per-section like `document`

## Exporting & Backup

`remind_me_export_memories` (MCP) and `GET /api/export` (HTTP) dump the memory store to **JSON** (single array) or **JSONL** (one record per line):

- **Complete logical backup** — every column of the memories table is included (id, content, category, tags, source, metadata, timestamps, vitality, superseded_by, …).
- **Entity graph included by default** — entities, memory-entity links, and entity-to-entity relations follow the memories as records tagged with a `record_type` discriminator (`entity` / `memory_entity` / `entity_relation`; memory records carry none — the same wire shape sync uses). Pass `include_graph=false` for a memories-only export.
- **Embeddings are excluded** — they are derived data; run `remind_me_reindex` after importing on the target machine.
- **Filters** — optional `category` and `tags` narrow the export (and scope the graph records to the exported memories).
- **Destination** — small exports (≤200 memories) are returned inline by the MCP tool; pass `file_path` to write to disk. File destinations must be inside `REMIND_ME_EXPORT_ROOTS` (default: your home directory). The HTTP route streams the payload as the response body when no `file_path` is given (`curl .../api/export > backup.json`).

### Round-trip caveats (honest fine print)

Each memory record also carries a `role` key, so the export file is directly consumable by `remind_me_import_chat` / `remind_me_import_directory` (the generic `{role, content}` format). But re-import is **lossy for everything except content**:

- The importer **re-chunks** long content and assigns **fresh ids**, category, tags, and source — the original values stay in the export file for manual restoration.
- Graph records restore on import: entities **upsert** (deterministic ids, alias union-merge), links insert when the referenced memory still exists under its **original id**. Since a chat re-import assigns new memory ids, links only fully restore into a database that still holds the referenced memories — **dangling links are skipped and counted** in the import result. Relations restore the same way, keyed on their entity endpoints rather than a memory id — a relation only restores when both its subject and object entities exist, and **dangling relations are skipped and counted** too.

## Watched Folders (Auto-Ingest)

Set `REMIND_ME_WATCH_DIRS` (`os.pathsep`-separated — `:` on macOS/Linux, `;` on Windows — each directory must lie inside `REMIND_ME_IMPORT_ROOTS`) and the server polls those folders in the background, auto-ingesting new or changed `.md`, `.markdown`, `.txt`, `.json`, and `.jsonl` files through the same import pipeline (`kind=auto`, hash dedup applies):

```bash
REMIND_ME_WATCH_DIRS=~/notes:~/Downloads/exports remind-me-mcp
```

```powershell
$env:REMIND_ME_WATCH_DIRS = "C:\notes;C:\Downloads\exports"
```

- **Polling, not inotify** — directories are scanned every `REMIND_ME_WATCH_INTERVAL` seconds (default 60); no extra dependencies.
- **Debounce** — a file whose mtime is younger than `REMIND_ME_WATCH_GRACE` seconds (default 5) is deferred until a later scan observes the same (mtime, size) signature, so partially-written files are never ingested mid-write.
- **Changed files supersede** — a changed file has a new hash, so it imports fresh; the watcher then marks every memory from the file's previous import as superseded (`superseded_by` = the new import id). Stale chunks drop out of search results (which filter `superseded_by IS NULL`) but remain in the database for audit.
- **Status** — the `remind_me_watch_status` tool reports watched dirs, scan counters, ingest/skip/supersede counts, and recent errors; `remind_me_server_status` includes a watcher summary too.
- **Wiki is downstream, not automatic** — the watcher feeds the **memory store**, not the wiki. Synthesis into wiki pages is a separate LLM-driven step (`remind_me_wiki_compile`). So both status tools also report `pending_wiki_compile` — the count of non-superseded memories created since the last compile watermark — as a nudge that newly ingested files are waiting to be folded into the wiki.
- **Obsidian vaults auto-detect, no extra config** — if a watched directory (or any of its ancestors, bounded by `REMIND_ME_IMPORT_ROOTS`) contains a `.obsidian/` directory, the watcher treats it as an Obsidian vault: every `.md`/`.markdown` file is imported with the frontmatter/wikilink/inline-`#tag`-aware `obsidian` kind instead of plain `document` — see [Importing from Obsidian](#importing-from-obsidian). `.obsidian/` itself (Obsidian's internal plugin-settings/workspace-state folder, never real notes) is never scanned — it's excluded by the same dot-directory skip that already keeps any hidden folder out of the watcher's scans.
- **Example upstream feed** — [dbs](https://github.com/baileyrd/daily-backup-system) (a personal-data archiver for Reddit/YouTube/Raindrop/etc.) can write one Markdown note per backed-up item straight into a watched directory via `dbs export-notes --out-dir DIR`, incrementally, after each `dbs backup`. See [docs/dbs-integration-review-2026-07-21.md](docs/dbs-integration-review-2026-07-21.md) for the full setup and rationale.

## Push/Webhook Ingestion

For content that shows up as an event rather than a file on disk (a chat-export tool, a CI job, another automation), set `REMIND_ME_WEBHOOK_SECRET` and the server accepts pushes directly instead of waiting for the folder watcher to find a file:

```bash
REMIND_ME_WEBHOOK_SECRET=$(openssl rand -hex 32) remind-me-mcp
```

```bash
curl -X POST http://127.0.0.1:8769/ingest \
  -H "Authorization: Bearer $REMIND_ME_WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"filename": "export.json", "content": "{\"chat_messages\": [...]}"}'
```

- **Disabled by default** — without `REMIND_ME_WEBHOOK_SECRET` the server refuses to start; every request needs the bearer token, so an unsecured push endpoint never exists.
- **Localhost by default** — `REMIND_ME_WEBHOOK_BIND` defaults to `127.0.0.1` (unlike the Tailscale-oriented peer sync server's `0.0.0.0` default), since a push endpoint writes arbitrary content directly into memory; widen it deliberately if you need remote access.
- **Same pipeline as file import** — `content` is UTF-8 text; `filename`'s extension selects the parser (JSON/JSONL chat exports, Markdown/plain-text documents), and hash dedup applies exactly like `remind_me_import_chat`. `category`, `tags`, `extract_mode`, `max_length`, and `kind` are all optional, with the same defaults as `remind_me_import_chat`.
- **Status** — `remind_me_webhook_status` reports enabled/running state, bind/port, and request counters (ingested/skipped/errored); `remind_me_server_status` includes a one-line summary too.
- **Configuration** — `REMIND_ME_WEBHOOK_PORT` (default 8769), `REMIND_ME_WEBHOOK_BIND`, `REMIND_ME_WEBHOOK_SECRET`.
- **Rate limited** — `POST /ingest` enforces `REMIND_ME_RATE_LIMIT_REQUESTS` (default 60) per `REMIND_ME_RATE_LIMIT_WINDOW_SECONDS` (default 60), returning `429` with a `Retry-After` header once exceeded. The check runs *before* the bearer check, keyed by IP, so an anonymous flood is bounded too — but a request presenting the exact `REMIND_ME_WEBHOOK_SECRET` gets its own dedicated bucket, so a legitimate high-volume pusher is never throttled by unrelated traffic hitting the same tunnel. Set `REMIND_ME_RATE_LIMIT_ENABLED=""` to disable entirely (shared with the [remote connector](#claudeai-custom-connector-remote-mcp)'s limit — see [Environment Variables](#environment-variables)).

## Ingest-Time Normalization

Raw imports (chat/document, from a file import, the watcher, or a webhook push) are often verbatim and noisy. `remind_me_normalize_batch` surfaces un-normalized `document_import`/`chat_import` chunks for the calling agent to distill into `{question, summary, resolution?, refs?}` — the LLM work happens client-side, exactly like `remind_me_decompose` already does for atomic-fact extraction, so the server itself has no LLM dependency. `remind_me_normalize_apply` then writes each distillation as a new memory (category `normalized`), non-destructively linked back to the raw row via a `normalized_from` metadata pointer — the raw memory is kept, not replaced, and `remind_me_normalize_batch` skips it on the next call. The normalized memory inherits the raw row's `doc_id`/`chunk_index` (so `include_neighbors` still finds it) and accepts its own optional `entities` list (FT-04) — the raw import is never entity-linked automatically, so without it a normalized memory would be invisible to `remind_me_entity`/`remind_me_entity_traverse`.

## Observability (OpenTelemetry)

Off by default and zero-cost when unset. Enable tracing to see where time goes across three boundaries — every MCP tool call, each sync cycle, and each folder-watcher scan pass — exported to whatever OTLP collector you already run (Jaeger, Tempo, Honeycomb, ...). remind_me never bundles or manages a collector itself, which would conflict with the zero-ops, local-first design.

```bash
uv pip install "remind-me-mcp[otel]"
REMIND_ME_OTEL_ENABLED=1 REMIND_ME_OTEL_ENDPOINT=http://localhost:4318/v1/traces remind-me-mcp
```

- **Graceful degradation** — if `REMIND_ME_OTEL_ENABLED=1` is set but the `otel` extra isn't installed (or setup fails for any other reason), tracing silently no-ops after a one-time warning in the log — it can never break the server it's observing.
- **Status** — `remind_me_server_status` reports whether tracing is enabled and actually active.

## Metrics

Off by default, same posture as OTel tracing above — this is instrumentation surface, not a core feature. Set `REMIND_ME_METRICS_ENABLED=1` and a Prometheus scrape target appears at `GET /metrics` on the dashboard server (`--serve-ui`):

```bash
REMIND_ME_METRICS_ENABLED=1 remind-me-mcp --serve-ui
curl http://127.0.0.1:5199/metrics
```

- **What's exposed** — `remind_me_build_info{version="..."}` (a constant-1 gauge whose labels carry the build, the Prometheus idiom for metadata — join on it to annotate or group a panel by version, so "latency changed" and "we upgraded" are the same graph rather than two unrelated observations); `remind_me_tool_calls_total{tool="..."}` and `remind_me_tool_call_duration_seconds_sum`/`_count{tool="..."}` (call count and total latency per MCP tool, from the single `_TracedFastMCP.call_tool` dispatch choke point already used for OTel/the watchdog); `remind_me_search_tier_results_total{tier="keyword"|"semantic"|"hybrid"}` (cumulative `remind_me_search` result counts by ranking tier); `remind_me_rate_limit_rejections_total` (requests rejected by the #183 rate limiter, from `RateLimiter.hit()`'s own rejection path); plus two gauges computed fresh on every scrape rather than tracked as counters — `remind_me_memories_total` and, when sync is configured, `remind_me_sync_outbox_pending`.
- **No new dependency** — the Prometheus text exposition format is hand-rolled in `remind_me_mcp/metrics.py` (a few dozen lines of `# HELP`/`# TYPE`/`name{labels} value` formatting) rather than adding the `prometheus_client` package, consistent with this codebase's general bias toward minimal dependencies. Thread-safe counter increments use the same single-`threading.Lock`-around-a-plain-dict approach `rate_limit.py` already established for an identical concurrency requirement.
- **Auth stance: unauthenticated, gated on the enable flag instead.** `GET /metrics` sits outside `BearerAuthMiddleware`'s `/api/` prefix — the same posture as `/health` (SE-04) — because Prometheus scrape configs typically send no custom headers, and the endpoint is already opt-in at the config level. **This means anyone who can reach the dashboard port can see tool-call and search-volume patterns while it's enabled** — firewall the port or put a reverse proxy with its own auth in front of it if that's a concern on your network, the same mitigation already documented for the peer sync server's default bind and the reminders ICS feed above.
- **Disabled behavior** — `GET /metrics` returns a plain `404` while `REMIND_ME_METRICS_ENABLED` is unset, not an empty-but-200 body.

## Entity Knowledge Graph

Memories can carry a structured **subject/predicate/object triple** plus links to **entities** (people, projects, tools, places, orgs — each with a kind and aliases). The graph builds up through normal use:

- **`remind_me_add`** accepts optional `subject`/`predicate`/`object` fields and an `entities` list (`{name, kind, aliases}`).
- **`remind_me_decompose`** writes SPO triples and entity mentions on every extracted fact.
- **`remind_me_extract_batch` + `remind_me_annotate`** backfill older memories: the batch tool returns memories with no triple and no entity mentions; Claude reviews them and annotates triples + entities in bulk.

### How identity works

- **Deterministic ids** — an entity's id is derived from its normalized name (lowercased, whitespace-collapsed), so 'Bailey  Robertson ' and 'bailey robertson' are the same entity, and two machines independently creating the same-named entity converge to the same row.
- **Alias union-merge** — re-upserting an entity merges new aliases into the existing list (deduplicated, existing order preserved) and fills in a missing kind; the canonical name is never auto-merged with a different name.

### Search surfaces

- **`entity:"Bailey Robertson"`** in a search query resolves the name/alias and returns memories linked to that entity or whose SPO subject/object matches it; composes with `subject:`/`predicate:` filters.
- **`expand_entities=true`** on `remind_me_search` appends up to 5 related memories that share an entity with the results (1-hop graph expansion, in a separate `related_via_entities` section that doesn't affect ranking).
- **`remind_me_entity`** (or `GET /api/entity?name=`) returns the canonical record, its facts (memories whose SPO subject/object is the entity), and the memories that mention it.

### Typed entity-to-entity relations

`remind_me_entity`/`expand_entities` describe a memory↔entity bipartite graph (entity X is *mentioned in* memory Y). Layered on top of that is a genuine entity↔entity graph: whenever a fact's SPO subject *and* object both resolve to known entities (from that same call's `entities` list, or an earlier annotation), `remind_me_decompose`/`remind_me_annotate` also write a typed **`subject --relation--> object`** edge to `entity_relations` — e.g. `Bailey --works_with--> Alex`. SPO values that don't name a known entity keep working exactly as before: a memory-level triple with no graph edge.

- **`remind_me_entity_traverse`** (or `GET /api/entity/traverse?name=&hops=`) walks this edge graph breadth-first from a starting entity, up to `hops` (1-3) steps in both directions, with an optional exact-match `relation` filter and a result cap. This answers questions that require chaining relations rather than co-mention — e.g. "who introduced me to the person who recommended this tool" (`Alice --introduced--> Bob`, `Bob --recommended--> tool`).
- Relation edges have a **deterministic id** (hash of the subject/relation/object triple, same convergence property as entity ids) and are **immutable** — insert-or-ignore, like memory↔entity links, so re-recording the same triple is a no-op.

### Browsing the graph

The dashboard's **Entities** view is a human-facing browser for the graph: a clickable, most-mentioned-first entity list (backed by `GET /api/entities`) and a detail panel showing an entity's facts, linked memories, and a "Related Entities" drill-down built from a 1-hop traversal — for exploring what the app knows without hand-crafting API calls.

### Contradiction-based supersession

Supersession previously only happened via similarity-merge (`remind_me_consolidate`) — near-duplicate memories get merged. That misses a genuine contradiction: "I moved to Boston" doesn't textually resemble "I live in Seattle," even though they're in direct conflict. Whenever an SPO triple is written (`remind_me_add`, `remind_me_decompose`, `remind_me_annotate`), any other non-superseded, non-deleted memory sharing the same **subject + predicate** but a **different object** is automatically superseded — the same `superseded_by` mechanism, so every existing superseded-exclusion read path (search, list, entity lookups) picks it up for free.

This is deliberately narrow: a differently-worded predicate never contradicts, e.g. "I *live in* Seattle" and "I *visited* Boston" don't collide, since they don't share a predicate — the caller (an LLM choosing predicate names) controls specificity, not this check. It's a cheap, deterministic first pass over the existing SPO columns.

**The free-text gap (issue #201).** The mechanism above only fires on exact structured-triple matches — it says nothing about two pieces of free-text prose that conflict without ever being decomposed into a shared subject/predicate ("I moved to Boston last month" vs. "My apartment in Seattle has great light" as two unstructured memories). `remind_me_contradiction_candidates` (`limit`, default 20) closes that gap the same read-only, Claude-judged way [Importance Recalibration](#importance-recalibration-issue-200) closes its own gap: it surfaces bounded PAIRS of memories using a deterministic heuristic, and the calling Claude session judges whether a given pair actually conflicts — no LLM call happens inside the server, and nothing is auto-superseded by this tool.

The comparison space is bounded two ways, so this never becomes an all-pairs O(n²) scan of the vault:

1. **Shared entity.** Both memories in a pair must mention at least one common entity in the entity graph (FT-04, the same graph `remind_me_entity` traverses) — two memories that share no entity are extremely unlikely to be a direct contradiction.
2. **Not already covered.** Pairs that would already auto-supersede via the exact subject+predicate mechanism above are excluded — a genuinely covered pair can't actually coexist as two live (non-superseded) memories by the time both would be visible, since the write path already resolved it.

Each candidate pair carries both memories' content snippets, category, `memory_type`, any SPO triple, and the shared entity name(s), so the calling session has enough context to judge. This surfaces pairs that MIGHT conflict, not pairs that ARE confirmed contradictions — prose comparison is inherently less certain than exact triple matching. There is deliberately no third "apply"/"resolve" tool: once a genuine contradiction is confirmed, use the EXISTING `remind_me_update` (correct the stale memory in place), `remind_me_delete` (remove it), or `remind_me_add` with an explicit SPO triple (which also lets the exact-triple mechanism catch any future conflict on the same claim automatically). A matching `review_contradictions` prompt drives the loop end to end.

Architectural note, same correction as #200's: the issue's literal text proposed a scheduler-hosted LLM pass; this server has no in-server LLM dependency and never calls an LLM API itself, so this is built as the two-phase, on-demand pattern instead. A `contradiction_candidates` queue joins [Maintenance Nudges](#maintenance-nudges) using the same deterministic pairing query, so a growing backlog surfaces the same way every other maintenance queue does.

### Sync & export

The graph syncs between machines alongside memories: entity, link, and relation records travel with `record_type` discriminators through the outbox/hub and the peer endpoints (`/sync/pull_entities`, `/sync/pull_links`, `/sync/pull_entity_relations`). Deterministic ids make concurrent creation converge; aliases union-merge on receipt; links and relations are immutable insert-or-ignore rows, and a record that arrives before its endpoints simply stays invisible until they do. Exports include the graph by default (see [Exporting & Backup](#exporting--backup)).

## LLM Wiki

The entity graph and semantic search are *retrieval* tools: they fetch raw memories on demand. The **LLM Wiki** is the opposite move — a *synthesis* layer inspired by [Andrej Karpathy's "LLM Wiki" pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f). Instead of re-deriving knowledge from fragments every time, Claude distils memories into a small set of interlinked markdown pages that can be loaded straight into context. RAG retrieves and forgets; a wiki accumulates and compounds.

Three layers:

1. **Raw sources** — the existing `memories` store (captures, imports, decomposed facts). Immutable from the wiki's point of view.
2. **The wiki** — plain markdown files under `REMIND_ME_WIKI_DIR` (default `~/.remind-me/wiki`), one concept/entity/project per page, cross-linked with `[[Wikilinks]]`, plus an auto-generated `index.md` catalogue and an append-only `log.md`.
3. **The schema** — `SCHEMA.md`, the maintainer contract Claude follows (seeded with a sensible default on first use, surfaced as the `wiki://schema` resource).

### Files are the source of truth

Pages are real files on disk — edit them by hand, version them with `git`, sync the folder however you like. The database (`wiki_pages` / `wiki_links` / `wiki_fts`) is only a search/index cache: every read path **reconciles** it from the files first (a cheap mtime comparison), so external edits, deletions, and `git pull`s are picked up automatically. Because the files are canonical, these tables deliberately carry **no sync outbox triggers** — wiki sync is the file layer's job, not the database's.

### The compile workflow

`remind_me_wiki_compile` drives synthesis in two phases:

1. **Brief** (default) — returns the maintainer schema, the current page index, and up to `limit` raw memories created since the last compile (the *pending sources*). Calling it repeatedly is safe; it never advances anything on its own.
2. **Mark integrated** (`mark_integrated=true`) — call this *after* writing the pages (with `remind_me_wiki_write`) to advance the compile watermark past the surfaced batch, so the same sources aren't re-served next time.

A typical session: `remind_me_wiki_compile` → read the brief → write/revise several pages, flag contradictions, add cross-links → `remind_me_wiki_compile(mark_integrated=true)`. To consume the wiki, `remind_me_wiki_load` pulls the whole thing into context (token-budgeted, newest pages first), or `remind_me_wiki_read` / `remind_me_wiki_search` navigate it page by page.

### Page kinds: knowledge pages vs. procedure pages

Most pages are free-form **knowledge pages** — one concept, entity, or project each. The default `SCHEMA.md` also documents a **procedure page** convention for sources that describe a repeatable task (a setup, a fix, a recurring workflow) rather than a fact: a `# Task name` page with `## Steps`, `## Edge cases / branches`, and `## Related` sections, so the next run can follow it directly instead of re-deriving it from raw memories. The schema nudges Claude to decide **patch vs. create** — revise an existing procedure page's steps/edge-cases rather than writing a near-duplicate — the same judgment call `remind_me_wiki_compile` already makes for knowledge pages, just with a task-shaped template. This is a prompt/schema convention only (see `wiki.default_schema()`); there's no separate page-kind column or enforcement — an existing `SCHEMA.md` already seeded on disk keeps whatever it has until you edit it by hand.

```bash
# Point the wiki somewhere git-friendly (optional; defaults to ~/.remind-me/wiki)
export REMIND_ME_WIKI_DIR=~/notes/wiki
# Cap the default whole-wiki load (estimated tokens; 0 = unlimited)
export REMIND_ME_WIKI_LOAD_TOKEN_BUDGET=12000
```

## Reminders

A memory can carry an optional future `remind_at` timestamp. A background scheduler polls for due reminders every `REMIND_ME_REMINDER_POLL_INTERVAL` seconds (default 60) and delivers each exactly once:

- **`remind_me_set_reminder`** — sets or clears `remind_at` on an existing memory. A past or unparseable timestamp is rejected outright rather than silently accepted as a no-op reminder. Setting/clearing a reminder is a real content change, so it bumps `updated_at` like any other field edit (routed through the same internal update path as `remind_me_update`).
- **`remind_me_list_reminders`** — lists memories with a set reminder: `upcoming` (still in the future), `overdue` (due but not yet delivered — typically because the server was offline when it came due), or `all`.
- **Fires exactly once, even across restarts** — a `reminder_deliveries` table records which `(memory_id, remind_at)` pairs have already fired. A reminder that becomes due while the server is offline still fires exactly once on the next poll after restart; it neither re-fires on every subsequent poll nor gets silently dropped.
- **Delivery is a log line today** — outbound notification channels (email, push, etc.) are tracked separately; the scheduler's due-reminder logic is already structured with a swappable delivery hook so a real channel can plug in without changing how due reminders are found.
- **Configuration** — `REMIND_ME_REMINDER_POLL_INTERVAL` (default 60 seconds). The scheduler itself always runs; there is no separate enable switch.

## Calendar Export

`GET /api/reminders/{token}.ics` (issue #190) is a subscribable ICS feed of every `upcoming`/`overdue`-and-undelivered reminder — paste it into Google/Apple/Outlook calendar's "subscribe by URL" feature to see reminders alongside the rest of your calendar, refreshed on whatever poll interval the calendar provider itself uses.

- **`remind_me_reminders_ics_url`** (MCP tool) — returns the full feed URL (scheme/host/port from the running dashboard server) so you don't need filesystem or env access to read the token yourself. Returns a placeholder explaining how to enable the HTTP surface if the current MCP connection is stdio-only (there is no server to serve the feed from in that mode).
- **Secret-path auth, not a bearer header** — a calendar subscription is polled unauthenticated by the provider's own servers on a schedule you don't control, with no way to attach an `Authorization` header, so this route can't use the same bearer-token scheme the rest of `/api/*` uses. Instead the token lives in the URL path itself (`REMIND_ME_ICS_TOKEN`, auto-generated and persisted at `~/.remind-me/ics_token` on first use, exactly like the dashboard API key — SE-01) and is checked with `hmac.compare_digest`. **⚠ Treat this URL like a password** — whoever holds it can read every reminder's content — same caveat the [Claude.ai Custom Connector](#claudeai-custom-connector-remote-mcp) section already states for its own secret-path fallback. Rotate by deleting the token file; every calendar app subscribed to the old URL then gets a 404 and needs re-pointing at the new one.
- **Stable event identity** — each VEVENT's `UID` is derived deterministically from the memory id and `remind_at` (not a random UUID per fetch), so an unchanged reminder updates in place across polls instead of piling up duplicate events; changing `remind_at` mints a new UID for what is genuinely a new occurrence.
- **No new dependency** — ICS generation is hand-rolled (`remind_me_mcp/ics_export.py`), including RFC 5545 text escaping and 75-octet line folding, rather than pulling in a third-party iCalendar library for a format this small (same minimal-dependency bias as the `[semantic]`/`[ann]`/etc. optional extras).

## Notifications

Optional outbound notification channels (issue #180) that a fired reminder or a faulted sync status can push out, instead of only being visible to whoever happens to be reading logs or calling a status tool. Each channel is gated on its own config being present — no separate enable flag, and no channel configured means `remind_me_mcp.notifications.notify()` is a safe no-op everywhere it's called.

- **Webhook** (`REMIND_ME_NOTIFY_WEBHOOK_URL`) — POSTs one generic JSON payload, `{"subject": ..., "body": ..., "source": "remind-me"}`, to the configured URL. One config covers ntfy/Slack/Discord/Mattermost/Pushover-via-webhook uniformly; there's no per-service formatting (Slack blocks, ntfy priority headers, Discord embeds, ...) built in — point the URL at a small relay/transform first if you want a service's native formatting. A short timeout (`REMIND_ME_NOTIFY_WEBHOOK_TIMEOUT`, default 5s) keeps a hung endpoint from blocking the caller.
- **Email** (`REMIND_ME_NOTIFY_SMTP_HOST` + `REMIND_ME_NOTIFY_SMTP_TO`) — sends via stdlib `smtplib`/`email.message.EmailMessage` (no new dependency). `REMIND_ME_NOTIFY_SMTP_PORT` 465 always uses implicit TLS (`SMTP_SSL`); any other port uses plain `SMTP` with STARTTLS applied when `REMIND_ME_NOTIFY_SMTP_USE_TLS` is true (the default). `REMIND_ME_NOTIFY_SMTP_USER`/`_PASSWORD` are optional — omit both to skip SMTP AUTH against a relay that allows unauthenticated submission.
- **Both, one, or neither** can be configured at once — `notify()` fans out to every configured channel, catching and logging any individual notifier's failure so a broken channel never blocks another or the caller.

Two wiring points, both optional in the sense that they're no-ops with nothing configured:

- **Reminders** — the scheduler's default delivery hook (see [Reminders](#reminders)) logs the due reminder *and* calls `notify()` with the memory's content as the body, on every fired reminder.
- **Sync faults** — `remind_me_sync_reconcile` calls `notify()` when its verdict is `fault` (not `pull-lag`/`node-ahead`/`in-sync` — see [Multi-Machine Sync](#multi-machine-sync)). Throttled to once per `REMIND_ME_NOTIFY_SYNC_FAULT_INTERVAL` seconds (default 1800) per persisting fault, since reconcile can be called repeatedly by an external monitor — alerting on every call would repeat the alert-fatigue mistake `remind_me_sync_status`/`remind_me_sync_reconcile` themselves were once guilty of (see the Wave 4 incident in `BACKLOG.md`).

Deliberately *not* wired into `remind_me_server_status`'s maintenance-backlog nudges or the feedback hint — those are in-band by design (surfaced only inside a live tool response), not outbound alerts.

### Automation event stream vs. notifications — which one do I want?

`REMIND_ME_NOTIFY_WEBHOOK_URL` (above) and `REMIND_ME_EVENT_WEBHOOK_URL` (issue #198) both POST JSON to a webhook, but they answer different questions and are configured, and fire, independently:

| | `REMIND_ME_NOTIFY_WEBHOOK_URL` | `REMIND_ME_EVENT_WEBHOOK_URL` |
|---|---|---|
| **For** | A human, on their phone/Slack/ntfy | An automation consumer — a relay, a second indexer, an audit log |
| **Fires on** | A fired reminder; a *faulted* sync verdict | Every `remind_me_add` / `remind_me_update` / `remind_me_delete` call (and their REST equivalents) |
| **Throttling** | Sync faults throttled to one per `REMIND_ME_NOTIFY_SYNC_FAULT_INTERVAL` (default 1800s) | None — every qualifying mutation fires, since a raw event stream needs completeness, not alert-fatigue protection |
| **Payload** | `{"subject": ..., "body": ..., "source": "remind-me"}` — body is human-readable text, e.g. a reminder's own content | `{"event": "created"\|"updated"\|"deleted", "memory_id": ..., "category": ..., "timestamp": ...}` — metadata only, **never memory content** |

If you want "ping me when something's due," use `REMIND_ME_NOTIFY_WEBHOOK_URL`. If you want "tell my other system every time a memory changes," use `REMIND_ME_EVENT_WEBHOOK_URL`.

`remind_me_mcp.events.emit_event()` fires the POST as a held-reference fire-and-forget background task (same PF-04 discipline as the tools package's own background embedding tasks — see `BACKLOG.md`), bounded by `REMIND_ME_EVENT_WEBHOOK_TIMEOUT` (default 5s, mirroring `REMIND_ME_NOTIFY_WEBHOOK_TIMEOUT`), and never raises into the calling tool/API path on failure. `remind_me_set_reminder` and `remind_me_revert` deliberately do **not** fire an `updated` event even though both funnel through the same internal `_apply_memory_field_update` helper `remind_me_update` uses — a reminder set/clear touches no content field at all, and a revert is its own distinct, separately-documented operation (see [Edit History](#edit-history)); only genuine `remind_me_add`/`remind_me_update`/`remind_me_delete` (and `api_add`/`api_update`/`api_delete`) calls emit an event.

## Edit History

`remind_me_update` overwrites a memory's content/category/tags/metadata in place — issue #187 gives it the same "don't lose data on a destructive-looking operation" treatment [deletion already gets](#deletion-propagates-too) from `deleted_at` tombstones, applied to edits instead of deletes.

- **`remind_me_history`** — lists a memory's prior revisions, newest first, each with a timestamp and a short content preview. `limit` (default 10) caps how many come back; `response_format` supports `markdown` or `json`.
- **`remind_me_revert`** — restores a memory's content/category/tags/metadata to a prior revision by `revision_id` (from `remind_me_history`'s output). A `revision_id` that doesn't exist, or belongs to a different memory, fails with a clear error instead of silently doing nothing or reverting the wrong thing.
- **A revision is captured automatically** whenever `remind_me_update` (or a revert) genuinely changes tracked content — a no-op update (setting a field to the value it already has) creates no revision, mirroring how sync only propagates genuine content changes.
- **A revert is itself an edit, not a raw overwrite** — it's implemented by calling the exact same internal update path `remind_me_update` uses, so it bumps `updated_at`, rides the normal sync outbox trigger like any other change, and — because it's just another edit through that path — automatically snapshots the state just before the revert. That means reverting is itself undoable, with no special-case code for it.
- **Scope: content fields only.** What's tracked is exactly what `remind_me_update` can change (`content`, `category`, `tags`, `metadata`) — not `remind_at` or the vitality/classification columns. `remind_me_set_reminder` happens to funnel through the same shared internal update helper, but since it only ever touches `remind_at`, it never produces a revision.
- **Local only, never synced** — like `reminder_deliveries` and the wiki index tables, `memory_revisions` carries no sync outbox trigger. Edit history is per-device audit trail, not a replicated entity; a revert on one device does not (yet) merge with another device's edit history for the same memory.
- **Retention** — `REMIND_ME_REVISION_RETENTION_DAYS` (default 90) bounds how far back `remind_me_revert` can reach. Old revisions are pruned by the always-on reminder-scheduler loop (not the sync loop — revisions accumulate regardless of whether sync is configured at all).

## Sensitive Memories

`sensitive: bool = False` on `remind_me_add`/`remind_me_update` (and their REST equivalents) marks a memory as one that shouldn't surface in ambient/passive reads by default (issue #195).

- **This is NOT access control.** remind_me is local-first and single-user (see [Design Scope](#design-scope)) — anyone with access to the SQLite database file already sees every row in it regardless of this flag. `sensitive` only reduces *accidental* exposure — a memory about something you'd rather not have pop up in an ordinary search or a scheduled digest, not a memory you need cryptographically hidden from other people. Real secrecy from other people requires filesystem/OS-level access control on the database file (or [encryption at rest](#encryption-at-rest)), not this flag.
- **Excluded by default from:** `remind_me_search`, `remind_me_list`, `GET /api/memories`, `GET /api/memories/search` — each gained a matching `include_sensitive: bool = False` opt-in for the (rarer) case where you actually want to see sensitive results, e.g. because the question is specifically about that content.
- **Always excluded, no opt-in, from:** `remind_me_digest` and `remind_me_wiki_compile`'s pending-sources query. Both are ambient/passive surfaces by nature — a digest can be scheduled and pushed to a notification channel without you asking a specific question, and a wiki page is meant to be read by anyone who opens the wiki — so neither offers an escape hatch. Want to write about a sensitive topic in the wiki anyway? Call `remind_me_wiki_write` directly; nothing stops that, only *automatic* inclusion during compile.
- **Never filtered:** `remind_me_get` (fetch by an id you already hold), `remind_me_history`, `remind_me_revert`. A direct lookup by a known id isn't "surfacing by default" — the caller already knows exactly what they're asking for.
- **A tracked, revertable field** — like content/category/tags/metadata (see [Edit History](#edit-history) above), toggling `sensitive` via `remind_me_update` snapshots the prior value and shows up in `remind_me_history`; `remind_me_revert` restores it along with everything else a revision captures.
- **Not yet synced across devices** — the column rides the normal write path and enters the sync outbox payload, but (mirroring `remind_at`'s own existing scope limit) the receiving side of sync does not yet apply it, so marking a memory sensitive on one device does not currently propagate to another. A reasonable follow-up, not implemented in this pass.

## Digest

`remind_me_digest` (issue #188) is a compressed, one-read snapshot of the vault: recent additions, vault vitality, reminders, and sync health. It is pure synthesis — every section calls the exact same underlying function its own standalone tool already uses (`vitality.build_vitality_report`, the same function behind `remind_me_vitality_report` in [Lifecycle](#lifecycle); the reminders window logic behind [`remind_me_list_reminders`](#reminders); `sync.get_sync_status`, the same function behind `remind_me_sync_status` in [Multi-Machine Sync](#multi-machine-sync)), so the digest can never disagree with those tools' own numbers.

- **Works standalone, no configuration required** — call `remind_me_digest` any time; an empty vault gets a coherent "nothing to report" digest rather than an error or a blank response.
- **`since_days`** (default 7) controls how far back counts as a "recent addition."
- **`response_format`** — `markdown` (default) for a readable report, or `json` for the same underlying data programmatically.
- When sync is enabled, the tool also fetches a fresh `remind_me_sync_reconcile`-equivalent hub verdict (`in-sync`/`pull-lag`/`node-ahead`/`fault`) and appends it to the Sync Health section — best-effort; a hub that can't be reached doesn't fail the digest, that line is simply omitted.

**Optional scheduled delivery** — `REMIND_ME_DIGEST_INTERVAL` (`"daily"`, `"weekly"`, or unset/`""` to disable, the default) periodically builds the digest and pushes it through [Notifications](#notifications)' `notify()`, exactly like a fired reminder or a sync fault. Unlike the reminder scheduler (always on), this is genuinely opt-in — a digest is a standing summary, not core reminder functionality.

- **Piggybacks on the existing reminder-poll thread** rather than a second background thread: the check is a single disabled-by-default attribute read when unset, so a zero-config server pays nothing extra per poll tick. See `remind_me_mcp/scheduler.py`'s module docstring for the full reasoning.
- **Throttled by a persisted watermark**, not an in-memory timer — the last-sent timestamp lives in the same `sync_flags` key/value table `remind_me_mcp.sync` already uses for its own cross-restart bookkeeping (under the key `digest_last_sent_at`), so a server restart mid-interval does not immediately re-fire a digest that was already sent.

## Saved Searches

A saved search (issue #194) is a named, replayable `remind_me_search` call — save a query once, then either re-run it on demand or turn on background `watch` polling that notifies (see [Notifications](#notifications)) when a genuinely new memory starts matching.

- **`remind_me_save_search`** — creates or updates (by name) a saved search: `name`, `query`, optional `category`/`tags` filters, `include_sensitive` (default `false`, same as `remind_me_search` — issue #195), and `watch` (default `false`). Saving again under an existing name overwrites it in place — there's no separate "toggle watch" tool; re-save with a different `watch` value to change it, the same "same name is the same thing" convention `remind_me_wiki_write` already uses for pages.
- **`remind_me_list_saved_searches`** — lists every saved search with its query, filters, and watch status, as JSON.
- **`remind_me_run_saved_search`** — re-runs the stored query/filters through the exact same search code `remind_me_search` itself calls, so the output is identical to calling `remind_me_search` directly with those params — it *is* that call, not a re-implementation of it.
- **`remind_me_delete_saved_search`** — deletes a saved search by name, along with its watch-polling "already seen" state (below) so no orphaned rows are left behind.

### Watch polling

Setting `watch=true` has the background scheduler (the same loop [Reminders](#reminders)/[Digest](#digest) piggyback on) re-run the search every `REMIND_ME_SAVED_SEARCH_POLL_INTERVAL` seconds (default 300 — deliberately coarser than the 60s reminder poll, since a search's matching set changes far less often than a reminder's due time) and diff its results against what it matched last time.

- **⚠ The first poll after turning `watch` on seeds silently — it never notifies.** Every memory the search currently matches at that moment is recorded as "already seen" without calling `notify()`. This is deliberate: if it notified on every existing match the instant you turned watch on, that would read as a flood of "new" results for memories that were never actually new — they were just never looked at through this saved search before. Only a match that shows up on a **later** poll, absent from what was seeded, is treated as genuinely new and triggers one notification (memory id + a content preview, identifying which saved search fired).
- **No re-notification** — once a match has been notified (or seeded), it's recorded in a per-saved-search "seen" table and never fires again for that same memory, even across further polls.
- **`watch=false` searches are never polled** — the scheduler's per-tick check is a plain `WHERE watch = 1` query, so an unwatched saved search costs nothing beyond its own row.
- **Storage**: a dedicated `saved_search_seen_memories` table (`saved_search_id`, `memory_id`, `first_seen_at`), not a single watermark like the digest check uses — watch-polling needs a per-(search, memory) fact ("have I already notified about *this* match"), which one timestamp per search can't represent. Local-only, no sync outbox trigger, matching the precedent `reminder_deliveries`/`memory_revisions`/`analytics_snapshots` already set for per-device bookkeeping tables — see `remind_me_mcp/db.py`'s v26→v27 migration.

## Multi-Machine Sync

### Distributed Sync (recommended)

The built-in sync engine provides automatic, offline-first synchronization across machines using a hub-and-spoke architecture:

- **Local SQLite** on each machine preserves FTS5 and sqlite-vec functionality
- **Outbox pattern** captures all local writes for reliable delivery
- **Postgres hub** acts as the central sync point (runs as a container)
- **Peer-to-peer** direct sync between machines via Tailscale (optional)
- **Last-write-wins** conflict resolution on `updated_at`
- **Background sync** runs in a daemon thread at a configurable interval

#### Setting Up the Hub

The sync hub is a FastAPI server backed by Postgres, and it lives in this repo under [`hub/`](hub/) with one-command deployment for Fedora + rootless Podman:

```bash
# on the server
git clone https://github.com/baileyrd/remind_me.git ~/remind_me
~/remind_me/hub/setup.sh install                          # secrets, Quadlets, image, services
~/remind_me/hub/setup.sh restore /path/to/backup.sql      # optional: restore a previous database

# on each client (Fedora/WSL)
~/projects/remind_me/hub/client-setup.sh --node-id my-pc --tunnel you@server --apply-code
```

See [`hub/README.md`](hub/README.md) for the protocol details, manual setup reference, restore procedure, and operations guide.

#### Versions and Record Counts

Both sides report their version, because sync problems across a fleet are
often version-skew problems and the two halves release independently:

| Where | Field | Notes |
|-------|-------|-------|
| `GET /health` on the hub | `version` | `HUB_VERSION`, hand-bumped in `hub/main.py`; no auth needed, so a deploy can be verified without the sync secret |
| `GET /health` on a node's dashboard | `version` | the installed `remind-me-mcp` package version, same string `remind-me-mcp --version` prints |
| `GET /health` on a node's peer server | `version` | same, but behind the sync secret — the peer port binds all interfaces by default |
| `remind_me_sync_status` | `version` | this node's build, next to its `node_id` |
| `remind_me_sync_reconcile` | `version`, `hub_version` | both sides in one report; `hub_version` is `null` against a hub older than this feature |

`remind_me_sync_reconcile` accepts `quick=true`, which checks the hub's
`/count` first and skips `/stats` when every total already agrees — measured
at ~41ms against ~128ms on a 200k-row hub. It is **off by default on
purpose**: equal totals don't prove equal contents (a recategorization that
synced on one side only leaves the total unchanged while two categories drift
in opposite directions), and catching exactly that drift is what reconcile is
for. A quick run that took the fast path says so, via `checked: "totals"`.

The hub also serves **`GET /count`** (bearer auth), the cheap counterpart to
`/stats`: scalar `COUNT(*)`s with no `GROUP BY`, optionally narrowed with
`?table=memories|entities|memory_entities|entity_relations`. Poll it from a
dashboard tile or a drift alarm; reach for `/stats` only when you need the
per-node/per-category breakdown that reconciliation uses. `memories.live`
excludes tombstones and is the figure that should match a node's own count.

#### Configuring a Node

Add the sync environment variables to your MCP config:

```json
{
  "mcpServers": {
    "remind-me": {
      "command": "remind-me-mcp",
      "env": {
        "REMIND_ME_MCP_DIR": "~/.remind-me",
        "REMIND_ME_NODE_ID": "my-laptop",
        "REMIND_ME_HUB_URL": "http://100.x.x.x:8765",
        "REMIND_ME_SYNC_SECRET": "your-shared-secret",
        "REMIND_ME_SYNC_INTERVAL": "60",
        "REMIND_ME_PEER_PORT": "8766",
        "REMIND_ME_STATIC_PEERS": "[]"
      }
    }
  }
}
```

Sync is enabled automatically when `NODE_ID`, `HUB_URL`, and `SYNC_SECRET` are all set. Each machine needs its own unique `NODE_ID`.

> Moving a node to new hardware? See [docs/MIGRATION.md](docs/MIGRATION.md) — copying `memory.db` carries the old node's sync cursor and will skip older hub records unless you reset it.

#### How It Works

1. Every local write (add, update, delete) is recorded in a `sync_outbox` table
2. The background sync thread pushes outbox entries to the hub and pulls new records
3. Incoming records are upserted with last-write-wins on `updated_at` for most columns — **except `tags` and `metadata`, which field-level merge instead** (below)
4. Records pulled from the hub are marked as already-sent in the outbox to prevent echo
5. Optionally, peers discover each other via Tailscale and sync directly
6. The **entity graph syncs too**: entity and link records carry a `record_type` discriminator on the wire; entities upsert with alias union-merge, links are insert-or-ignore (see [Entity Knowledge Graph](#entity-knowledge-graph))

**Field-level conflict merge.** Whole-row LWW means two devices editing *different* fields of the same memory between sync cycles (one adds a tag, another edits content) would have whichever write arrived second silently clobber the other's change entirely — not just the field that actually conflicted. `tags` and `metadata` are field-level merged instead, regardless of which side wins LWW: `tags` union-merge (dedup, order-preserving — the same semantics entities already use for aliases); `metadata` shallow-merges key by key, with the LWW winner's value taking precedence only on an actual key collision, while keys unique to either side are kept. A record that loses LWW on `content`/other scalar fields still gets its tags/metadata folded in via a merge-only update that doesn't bump `updated_at` (so it doesn't cause sync churn). This client-side merge (`remind_me_mcp/sync.py`) applies uniformly whether pulling from a hub or a peer; the hub's own Postgres storage still does whole-row LWW for now — a documented scope decision (see `hub/main.py`), not an oversight, since two pushes racing at the hub before either side pulls is a narrower case than the general two-devices-diverge scenario this closes.

#### Deletion Propagates Too

Deleting a memory (`remind_me_delete` or `DELETE /api/memories/{id}`) is a **soft delete** whenever sync is configured: the row is tombstoned (a `deleted_at` timestamp set) instead of removed outright. A tombstone is just another update, so it rides the same outbox/LWW machinery as any other edit — no separate delete protocol. The tombstoned memory is excluded from every normal read (search, list, get, the dashboard), but the row itself sticks around long enough for the deletion to actually reach your other devices; a background compaction pass hard-deletes it once it's old enough (`REMIND_ME_TOMBSTONE_RETENTION_DAYS`, default 180 days) that every device has almost certainly already seen it. On a single device with no sync configured, delete is a plain, immediate hard delete exactly as before — there's nothing to propagate to.

#### Peer Server Endpoints

Each node runs a small HTTP server (default port 8766, bind via `REMIND_ME_PEER_BIND`) for direct peer sync. Every request requires `Authorization: Bearer <REMIND_ME_SYNC_SECRET>`:

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Node liveness + node_id + installed version |
| `GET` | `/sync/pull?since=&since_id=&exclude_node=&limit=` | Pull memory records (keyset cursor on `(updated_at, id)` when `since_id` is sent) |
| `POST` | `/sync/push` | Push records (responds with `processed_ids`) |
| `GET` | `/sync/pull_entities?since=&since_id=&limit=` | Pull entity records (404 on pre-entity-graph peers is treated as "no entity support") |
| `GET` | `/sync/pull_links?since=&since_id=&limit=` | Pull memory-entity link records |

The sync hub (`hub/`) implements the same wire protocol against Postgres, plus a few hub-only extensions not present here (an opt-in `since_seq` cursor immune to late-push-from-an-offline-node ordering bugs, a `full=1` re-seed mode, request timeouts, a push size cap, and a tombstone-purge endpoint) — see [`hub/README.md`](hub/README.md#protocol) for the details.

### File-Based Sync (alternative)

If you prefer not to run a hub, the memory database lives in a single directory (default: `~/.remind-me/`) and can be synced with file-based tools:

**Syncthing** (real-time, no cloud):
1. Install Syncthing on both machines
2. Share `~/.remind-me/` between them
3. SQLite WAL mode handles concurrent access safely

**Git**:
```bash
cd ~/.remind-me && git init && git add -A && git commit -m "sync"
git remote add origin <your-repo> && git push
# On other machine: git clone <your-repo> ~/.remind-me
```

**Dropbox / Google Drive / OneDrive**:
```bash
mv ~/.remind-me ~/Dropbox/remind-me
ln -s ~/Dropbox/remind-me ~/.remind-me
```

### Custom Location

Set `REMIND_ME_MCP_DIR` to any path:

```bash
export REMIND_ME_MCP_DIR="/mnt/synced-drive/remind-me"
```

## HTTP Transport (Local or Remote)

The `--serve-mcp` flag runs the MCP server over Streamable HTTP transport instead of stdio: one long-lived process that any number of clients point at over `/mcp`, rather than each client spawning (and each session re-spawning) its own subprocess.

### Local Use: One Shared Server for Multiple Agents

The default config in [step 2](#2-configure-for-claude-code) (`"command": "remind-me-mcp"`) spawns a fresh stdio subprocess *per agent, per session*. Fine for one agent at a time, but if you routinely run several Claude Code sessions, IDE windows, or other local MCP clients against the same memory store concurrently, each subprocess independently loads its own copy of the embedding model, opens its own SQLite connection, and runs its own copy of every background loop (watcher, sync, self-update check) — redundant work, and more opportunities to contend on the same WAL-mode database file.

Running one `--serve-mcp` process instead and pointing every local client at it over HTTP avoids all of that: one embedding model in memory, one set of background loops, one place logs land.

1. Start the server once, bound to localhost (the default — no `--mcp-host` needed for same-machine use):
   ```bash
   remind-me-mcp --serve-mcp
   ```
   It refuses to start a second time while one is already running (`MCP_PID_FILE` liveness check). A wrapper script that checks whether the port is already listening before launching avoids paying for a doomed process spawn on top of that check; keep any such script — and the secrets in its environment — out of version control. Run it under whatever this OS's normal "keep a background process alive and restart it on crash" mechanism is — a systemd user service on Linux (same shape as the [example under Remote Access](#remote-access)), a Scheduled Task on Windows, `launchd` on macOS.

2. Point every local agent's MCP config at the HTTP endpoint instead of a spawned command:
   ```json
   {
     "mcpServers": {
       "remind-me": {
         "type": "http",
         "url": "http://127.0.0.1:8767/mcp"
       }
     }
   }
   ```
   No `Authorization` header needed here — standalone `--serve-mcp` bound to its default `127.0.0.1` stays unauthenticated by design (see `REMIND_ME_MCP_HTTP_SECRET` in the [environment variables](#environment-variables) table), since anything that can reach localhost on this machine could reach the memory store directly anyway.

3. Every agent now shares the same live memory: something one agent stores or edits is immediately visible to the others' next `remind_me_search`/`remind_me_list` call — there's no per-session cache to go stale.

### Remote Access

Point a client on a *different* machine at the same transport over Tailscale or an SSH tunnel — same `--serve-mcp` server, no separate mode.

Claude Code config (remote machine via Tailscale):
```json
{
  "mcpServers": {
    "remind-me": {
      "type": "http",
      "url": "http://100.x.x.x:8767/mcp",
      "headers": {
        "Authorization": "Bearer your-secret-here"
      }
    }
  }
}
```

SSH tunnel for restricted networks (e.g. work laptop):
```bash
ssh -L 8767:localhost:8767 home-pc-wsl
# then point client at http://localhost:8767/mcp
```

Systemd user service (`~/.config/systemd/user/remind-me-mcp-http.service`):
```ini
[Unit]
Description=Remind Me MCP HTTP Transport
After=network-online.target

[Service]
Type=simple
ExecStart=/home/nano/.venv/bin/remind-me-mcp --serve-mcp --mcp-host 0.0.0.0
Environment=REMIND_ME_MCP_HTTP_SECRET=your-secret
Environment=REMIND_ME_MCP_HTTP_PORT=8767
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

## Claude.ai Custom Connector (Remote MCP)

The `--serve-remote` flag (or `REMIND_ME_REMOTE_MCP=1`) exposes the MCP server
as a remote connector that the claude.ai **website** can attach to, over the
Streamable HTTP transport. Two auth modes share the same server:

- **OAuth (recommended)** — set `REMIND_ME_REMOTE_ISSUER` to your public
  HTTPS origin and the connector serves a minimal single-user OAuth 2.1
  authorization server (AS metadata, dynamic client registration, PKCE
  authorization-code flow, refresh, revocation). claude.ai connects with a
  real, revocable per-client token instead of a secret URL.
- **Secret-path fallback** — without an issuer, the FT-05 mode applies: the
  URL `https://<host>/mcp/<connector-token>` is itself the credential. The
  token is generated on first run and stored at `~/.remind-me/connector_token`
  (mode 0600, same scheme as the dashboard API key); the full URL path is
  logged once at generation, redacted afterwards. Header-capable clients
  (Claude Code, scripts) may instead use `https://<host>/mcp` with
  `Authorization: Bearer <connector-token>`. The secret path and bearer
  token keep working even when OAuth is on. Everything else gets a 404/401.

**Rate limited** — the MCP endpoint (both `/mcp/<token>` and `/mcp`, in either auth mode) enforces `REMIND_ME_RATE_LIMIT_REQUESTS` (default 60) per `REMIND_ME_RATE_LIMIT_WINDOW_SECONDS` (default 60), returning `429` with a `Retry-After` header once exceeded — the same limiter and defaults as the [push/webhook endpoint](#pushwebhook-ingestion). It runs as the outermost check, ahead of the secret-path/bearer gate and (in OAuth mode) the SDK's own auth stack, so a flood of entirely unauthenticated requests against a leaked or guessed tunnel URL is bounded too. A request presenting the exact connector token — via either the secret path or the legacy bearer header — gets its own dedicated bucket, so the owner's real traffic is never throttled by unrelated probing that happens to share the tunnel's forwarding address (which is what every remote caller's apparent IP collapses to, behind most tunnel setups). OAuth's dynamically-issued per-client access tokens aren't individually re-verified for this check (that would duplicate the provider's own async token lookup); they share the IP-keyed bucket like anonymous traffic — a deliberate tradeoff documented in `remind_me_mcp/rate_limit.py`. Set `REMIND_ME_RATE_LIMIT_ENABLED=""` to disable entirely.

### 1. Expose the port over HTTPS

claude.ai requires a publicly reachable HTTPS endpoint. With Tailscale:

```bash
tailscale funnel 8768
# → https://your-machine.your-tailnet.ts.net/
```

Any HTTPS tunnel works the same way — the tunnel terminates TLS; the
connector server itself keeps listening on localhost. `GET /health` is an
unauthenticated liveness probe for the tunnel.

#### Exposure options

- **Tailscale Funnel** (shown above) — stable hostname, automatic TLS, no
  account beyond your tailnet. The easiest path for OAuth.
- **cloudflared quick tunnel / ngrok** —
  `cloudflared tunnel --url http://localhost:8768` or `ngrok http 8768`.
  Note: the free tiers hand out a **new hostname on every start**. That's
  fine for the secret-path fallback (just paste the new URL into claude.ai),
  but OAuth needs a stable `REMIND_ME_REMOTE_ISSUER` — use a **named
  Cloudflare tunnel** or an ngrok **static domain** if you want OAuth over
  these.
- **VPS + reverse proxy** — terminate TLS on a box you control (e.g. Caddy)
  and feed it from your home machine with a persistent reverse SSH tunnel:

  ```bash
  # on the home machine — keeps a reverse tunnel up through restarts
  autossh -M 0 -N -R 127.0.0.1:8768:localhost:8768 user@vps
  ```

  ```caddyfile
  # /etc/caddy/Caddyfile on the VPS
  memory.example.com {
      reverse_proxy 127.0.0.1:8768
  }
  ```

  Stable hostname, so OAuth works (`REMIND_ME_REMOTE_ISSUER=https://memory.example.com`).
- **SSH-based tunnel services** — `ssh -R 80:localhost:8768 nokey@localhost.run`
  (or pinggy and similar) need nothing installed, but the hostnames are
  ephemeral: fine for trying out the secret-path mode, not for OAuth.
- **What does NOT work: plain `ssh -L` local forwarding.** claude.ai
  connectors are fetched by **Anthropic's servers**, not by your browser — a
  port forwarded to your own laptop is invisible to them. `ssh -L` *is* the
  right tool for reaching the connector from your own other machines (point
  a header-capable client like Claude Code at the forwarded port with
  `Authorization: Bearer <connector-token>`), just not for claude.ai.

### 2. Start the connector server

```bash
REMIND_ME_REMOTE_ISSUER=https://your-machine.your-tailnet.ts.net \
  remind-me-mcp --serve-remote                    # binds 127.0.0.1:8768
# or without OAuth: remind-me-mcp --serve-remote
```

The issuer must be the public **origin only** (https, no path) — it is what
the OAuth metadata advertises, and it is deliberately never derived from the
request's Host header.

### 3. Add it as a claude.ai custom connector (OAuth)

1. On claude.ai go to **Settings → Connectors → Add custom connector**
2. Enter the plain MCP URL — no token in it:
   `https://your-machine.your-tailnet.ts.net/mcp`
3. claude.ai discovers the authorization server via the well-known metadata,
   registers itself as a client, and opens the **authorize page**. Paste your
   owner token (`cat ~/.remind-me/connector_token`) and click **Approve**.
4. Done — claude.ai holds a short-lived access token (1 h, auto-refreshed for
   up to 30 days) scoped to its own client registration.

Without OAuth (legacy fallback), paste the full secret URL instead —
`https://<host>/mcp/$(cat ~/.remind-me/connector_token)` — and connect
"without authentication".

### Revoking access

- `remind_me_revoke_clients` (MCP tool) lists every registered OAuth client
  with live token counts; call it with a `client_id` to revoke that client
  and all of its tokens immediately. The client must re-register and pass the
  consent page again to reconnect.
- `rm ~/.remind-me/oauth.json` revokes **every** OAuth client at once.
- Clients can also revoke their own tokens at the standard RFC 7009
  `/revoke` endpoint.
- The legacy connector token rotates by deleting
  `~/.remind-me/connector_token` and restarting — note this also invalidates
  the owner credential used on the consent page.

### Security caveats

- **The owner token is the trust boundary.** Anyone who has it can approve
  new OAuth clients (and use the legacy URL). Treat
  `~/.remind-me/connector_token` like a password; `REMIND_ME_REMOTE_TOKEN`
  overrides the file if you want to manage the secret yourself.
- **The legacy URL is a password too.** It keeps working alongside OAuth as a
  fallback. Don't paste it into shared docs or screenshots.
- **Registration is open by design** (RFC 7591 dynamic client registration,
  as the MCP spec expects) — but a registration alone grants nothing: every
  authorization stops at the owner-token consent page, wrong credentials
  auto-deny, and all comparisons are constant-time.
- **Always front it with HTTPS.** Over a plain-HTTP tunnel the tokens travel
  in cleartext. Tailscale Funnel and the usual tunnels handle this for you.
  This app never terminates TLS itself — that's deliberate, so any tunnel
  can front it — which means the same cleartext risk applies to the raw
  bind address too: widening `REMIND_ME_REMOTE_HOST` beyond `127.0.0.1`
  without an actual tunnel (or your own TLS termination) in front logs a
  startup warning, since every credential the connector accepts would then
  cross the wire in cleartext to anything that can reach that address.
- OAuth state lives at `~/.remind-me/oauth.json` (0600): client records plus
  SHA-256 hashes of issued tokens — raw tokens are never written to disk.
- The OAuth issuer comes **only** from `REMIND_ME_REMOTE_ISSUER` — it is never
  derived from the request's Host header, which is attacker-influenced behind
  a tunnel.
- The remote mode is standalone: run the dashboard (`--serve-ui`) or local
  MCP HTTP (`--serve-mcp`) in separate processes if you need them too.

## Search Syntax

The search tool uses SQLite FTS5 for keyword queries. Examples:

| Query | Matches |
|-------|---------|
| `python async` | Memories containing both "python" AND "async" |
| `python OR rust` | Memories containing either word |
| `python NOT django` | Python memories excluding Django |
| `"exact phrase"` | Memories with the exact phrase |
| `deploy*` | Prefix matching: deploy, deployment, deployed… |

### Structured Queries

Queries containing `subject:`, `predicate:`, or `entity:` prefixes route to an indexed structured lookup instead of full-text search (values can be quoted for multi-word matches):

| Query | Matches |
|-------|---------|
| `subject:Bailey` | Memories whose SPO subject is "Bailey" |
| `subject:"Bailey Robertson" predicate:works_at` | Subject AND predicate combined |
| `entity:"remind_me"` | Memories linked to that entity in the graph, or whose SPO subject/object is its canonical name (resolves aliases, case-insensitive) |

An unresolvable `entity:` filter returns an empty result with a message (no silent fallback); if a structured lookup finds nothing, the remaining query words fall back to hybrid search. Pass `expand_entities=true` to append up to 5 related memories that share an entity with the results (1-hop graph expansion).

### Neighbor-Aware Chunk Retrieval

Documents and chat exports are chunked on import (per Markdown section or per message); every chunk from the same file is tagged with a shared `doc_id` and a sequential `chunk_index`. Pass `include_neighbors=true` on `remind_me_search` to append up to 5 additional non-superseded sibling chunks — chunk_index ± 1 within the same `doc_id` — for any result that came from an import, in a separate `related_via_neighbors` section that doesn't affect ranking. This surfaces the surrounding context (a preceding heading, a caveat in the next paragraph) that per-chunk retrieval alone can split apart. Manually added memories (`remind_me_add`, `remind_me_auto_capture`, ...) have no `doc_id`/`chunk_index` and are skipped.

### Co-Retrieval Reinforcement

Every `remind_me_search` call passively reinforces a bounded association weight between memories returned together in the same result set (`memory_associations` table, capped at `vitality.CO_RETRIEVAL_MAX_WEIGHT` = 50, only the first 10 ids of a result set pairing per search regardless of `limit`), regardless of any flag. Pass `expand_co_retrieval=true` to surface the strongest associations: up to 5 additional non-superseded memories that have previously appeared alongside the current results, in a separate `related_via_co_retrieval` section, ordered by association strength.

This deliberately never affects ranking — it's a discovery aid, not a scoring input, the same posture as `expand_entities`/`include_neighbors`. That one-way flow (search results → recorded associations → surfaced as *suggestions*, never as a ranking input) is what avoids the runaway-feedback-loop risk that comes with reinforcement learning on ranking itself, without needing any decay math to counteract it. There's deliberately no time-decay in this pass either — just a simple weight cap — a scoped-down slice of the full "learned association weights" idea, not the complete design.

### Auto-Routing Retrieval Strategy

`remind_me_search`'s `strategy` parameter picks the RRF weight profile used to fuse keyword and semantic ranking:

| Value | Behavior |
|-------|----------|
| `auto` (default) | Deterministic heuristic on query shape: quoted phrases, `prefix*` wildcards, or queries of 2 words or fewer favor keyword relevance; long (6+ word) or question-shaped (ending in `?`) natural-language queries favor semantic similarity; everything else is balanced |
| `balanced` | Pins the tuned RRF defaults (equivalent to not overriding anything) |
| `keyword_favored` | Always favors keyword relevance, regardless of query shape |
| `semantic_favored` | Always favors semantic similarity, regardless of query shape |

This is a deterministic heuristic, not an LLM planner call — no extra latency or opacity on the search hot path, consistent with keeping server-side synthesis out of scope. `keyword_favored`/`semantic_favored` are relative *multipliers* on top of whatever RRF weights are already configured (`REMIND_ME_RRF_W_*` env vars), not fixed replacements — a signal you've deliberately zeroed stays zeroed. `strategy` only affects the hybrid ranking path; structured `subject:`/`predicate:`/`entity:` lookups bypass RRF entirely. Pass `verbose=true` to see the resolved `strategy` and `weights_used` in each result's `debug_signals`.

On top of the profile above, a query containing a temporal expression ("before I moved", "last summer", "when I lived in Seattle", a bare year like "2019") additionally boosts `w_recency` by 1.5x — recency-weighted ranking for questions that are asking to place a fact in time, regardless of whether the query is also short/keyword-shaped or long/semantic-shaped. Always active under `strategy="auto"`; no separate toggle.

### Query-Contextual Feedback

`remind_me_feedback` (`memory_id`, `signal`, optional `query`) has two modes:

- **No `query`** — the original global signal: "helpful"/"unhelpful" adjusts the memory's `base_weight` up or down immediately, affecting every future search.
- **With `query`** — query-contextual instead: a memory can be a poor match for "what's my favorite editor" but a perfect match for "what IDE did I mention last year," and global demotion would punish the second case for the first's feedback. The event is logged (memory, query, signal, magnitude) rather than touching `base_weight`; at ranking time, a future search's query is compared against every stored feedback query for that memory using Jaccard token-overlap similarity (coarse clustering, no embedder dependency), and matches above a threshold nudge that memory's RRF score up or down by up to 40%, before reranking. A memory with no matching feedback is completely unaffected — this only ever adds signal, never removes a candidate.
- Feedback logs are per-node local bookkeeping (like the dbs/MemPalace import dedup tables) and aren't synced across devices — an explicit scope decision, not an oversight.

### Importance Prior at Write Time

Every new memory used to start at a flat `base_weight=1.0` regardless of kind, so a throwaway aside ("it's raining today") competed evenly in ranking with a real decision ("we're migrating to Postgres") until feedback or access patterns accrued enough signal to differentiate them — and the highest-value memories (decisions) are exactly the ones a user is least likely to re-query immediately, so they'd lose the ranking race to frequently-hit trivia before feedback ever kicked in.

`base_weight` (and the matching initial `vitality`, since a fresh memory's vitality equals its base_weight exactly) is now seeded at write time:

- `remind_me_decompose` already classifies each fact's `memory_type` at write time, so it seeds directly from that (`decision`/`blocker` highest, down to `action_item` at the flat 1.0 default).
- `remind_me_add` doesn't have a `memory_type` yet (that's set later by `remind_me_reclassify`), so it seeds from `source` instead — a deliberate `manual` entry keeps the historical flat default; raw bulk-import sources (`chat_import`, `document_import`, `webhook`) start slightly lower, since they're unreviewed and often noisy.
- An unrecognized or absent source (and `memory_type="unclassified"`) falls through to the original flat 1.0 default — this is purely additive, not a behavior change for existing content.
- Other write paths (the chat/document importer's bulk INSERT, `mempalace`/`dbs` imports, `remind_me_normalize_apply`, the dashboard REST API) still use the flat default for now — an explicit, documented scope decision, not an oversight; extend the same `seed_base_weight()` helper (`vitality.py`) there later if it proves valuable.

### Importance Recalibration (issue #200)

The write-time prior above and `remind_me_feedback`'s adjustments are the only two ways `base_weight` moves — nothing periodically re-checks whether a memory's *original* importance classification has gone stale, e.g. a memory classified as a "decision" that was later reversed by a different memory, or a "fact" that's since been superseded in spirit but not via the formal triple-supersession mechanism (see [Contradiction-Based Supersession](#contradiction-based-supersession)).

`remind_me_recalibrate_candidates` (`limit`, default 20) surfaces a bounded batch of candidates using a deterministic heuristic — no LLM call happens inside the server. A memory qualifies when it looks important (`base_weight >= 1.15`, matching the fact/insight write-time prior, **or** a durability-implying `memory_type` like `decision`/`fact`) yet has gone stale (no access/creation activity in the last 90 days) and has never received a `remind_me_feedback` signal — used as a proxy for "never actually reviewed," since nothing else in the schema records that. Each candidate carries its content snippet, category, `memory_type`, `base_weight`, and access history so the calling Claude session can judge whether it's still classified correctly.

This is a **two-phase, Claude-driven workflow**, the same shape as `remind_me_normalize_batch`/`remind_me_normalize_apply` and `remind_me_consolidate`'s dry-run mode: the tool surfaces structured candidates, the calling agent does the actual reasoning, and — deliberately — there is no third "apply" tool. The apply half is the tools that already exist: `remind_me_reclassify`/`remind_me_reclassify_batch` for a genuine `memory_type` change, or `remind_me_feedback` (an "unhelpful"/"helpful" signal with no `query`) for a pure importance nudge with no type change. Building a redundant write path here would just duplicate reclassify's.

Architectural note: the original issue proposed "a periodic (scheduler-loop-hosted) LLM-driven pass," following the pattern of the reminder/digest/analytics-snapshot scheduler loops (#186/#187/#188). This server has no in-server LLM dependency and never calls an LLM API itself — a background thread can only run deterministic code — so the scheduler-hosted framing doesn't fit here the way it does for those purely-mechanical loops. What *is* mechanical and scheduler-appropriate is the count: [Maintenance Nudges](#maintenance-nudges) gained a `recalibration_candidates` queue using this same heuristic, so a growing backlog surfaces on ordinary tool responses (once past the usual threshold) the same way every other maintenance queue already does — only the counting is deterministic background work; the judgment stays client-side, on demand.

This importance-staleness gap has a free-text-prose analog: `remind_me_contradiction_candidates` surfaces entity-linked memory pairs that might conflict without ever sharing a formal SPO triple — see [Contradiction-Based Supersession](#contradiction-based-supersession) for the full writeup. Same two-phase, no-in-server-LLM shape as this section, just surfacing PAIRS instead of single memories.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `REMIND_ME_MCP_DIR` | `~/.remind-me` | Directory for the SQLite database |
| `REMIND_ME_WIKI_DIR` | `~/.remind-me/wiki` | Root directory for the LLM Wiki markdown files (the source of truth; the DB only indexes them) |
| `REMIND_ME_WIKI_LOAD_TOKEN_BUDGET` | `12000` | Default estimated-token ceiling for `remind_me_wiki_load`. `0` = unlimited |
| `REMIND_ME_MCP_SERVE_UI` | `false` | Start the HTTP dashboard server instead of stdio MCP |
| `REMIND_ME_MCP_UI_PORT` | `5199` | Port for the dashboard server |
| `REMIND_ME_MCP_SERVE_HTTP` | `false` | Run MCP server over Streamable HTTP transport |
| `REMIND_ME_MCP_HTTP_PORT` | `8767` | Port for the MCP HTTP transport |
| `REMIND_ME_MCP_HTTP_HOST` | `127.0.0.1` | Host to bind the MCP HTTP transport |
| `REMIND_ME_MCP_HTTP_SECRET` | *(auto-generated)* | Bearer token gating `/mcp` in combined mode (`--serve-mcp --serve-ui`). When unset, generated on first use and stored at `~/.remind-me/mcp_http_secret` (0600) — delete the file to rotate. Standalone `--serve-mcp` (without `--serve-ui`) is unaffected and stays unauthenticated by design, relying on its localhost-only default bind |
| `REMIND_ME_REMOTE_MCP` | `false` | Run the remote MCP connector (Streamable HTTP behind a secret URL path) for claude.ai custom connectors |
| `REMIND_ME_REMOTE_PORT` | `8768` | Port for the remote MCP connector |
| `REMIND_ME_REMOTE_HOST` | `127.0.0.1` | Host to bind the remote MCP connector (keep localhost; let the tunnel do the exposing) |
| `REMIND_ME_REMOTE_TOKEN` | *(auto-generated)* | Connector token (doubles as the secret URL path and the OAuth owner credential). When unset, generated on first run and stored at `~/.remind-me/connector_token` (0600). Delete the file to rotate |
| `REMIND_ME_REMOTE_ISSUER` | *(unset)* | Public HTTPS origin of the remote connector (e.g. the tunnel hostname). Setting it activates the single-user OAuth 2.1 authorization server; unset falls back to the secret-path mode with a warning |
| `REMIND_ME_EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | HuggingFace model for semantic embeddings (ONNX backend). The default is English-trained/English-optimized — see [Language Coverage](#language-coverage) for a recommended multilingual override |
| `REMIND_ME_EMBEDDING_BACKEND` | `onnx` | Embedding backend: `onnx` (in-process) or `ollama` (local daemon) |
| `REMIND_ME_EMBEDDING_DIM` | `384` | Embedding dimension — must match the model (nomic-embed-text=768, bge-m3=1024). Changing it requires recreating the vector table + `remind_me_reindex` |
| `REMIND_ME_BACKUP_RETENTION_COUNT` | `10` | Number of backup files (manual + pre-migration) kept under `MEMORY_DIR/backups/`; oldest pruned after each new backup |
| `REMIND_ME_DB_ENCRYPTION_KEY` | *(unset)* | SQLCipher passphrase for encryption at rest — see [Encryption at Rest](#encryption-at-rest). Requires the `encryption` extra (`pip install remind-me-mcp[encryption]`); unset (default) leaves `memory.db`/backups exactly as before this option existed. Never logged |
| `REMIND_ME_OLLAMA_URL` | `http://localhost:11434` | Ollama daemon URL (when backend is `ollama`) |
| `REMIND_ME_OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Ollama embedding model name. Query/passage instruction prefixes (e.g. `search_query:`/`search_document:`) are applied automatically for known model families (`nomic-embed-text`, `bge-*`, `e5-*`) — see `embeddings._ROLE_PREFIXES` |
| `REMIND_ME_EMBED_CHUNK_CHARS` | `1600` | Character window size for sliding-window embedding of long content |
| `REMIND_ME_EMBED_CHUNK_OVERLAP` | `200` | Overlap between embedding windows |
| `REMIND_ME_EMBED_MAX_CHUNKS` | `16` | Max embedding chunks per memory |
| `REMIND_ME_EMBED_BATCH_SIZE` | `32` | Memories embedded per batch — enforced inside `_embed_and_store_rows` itself, so every caller (reindex, import, sync's pulled-record embedding) is bounded the same way regardless of how many rows it hands over in one call |
| `REMIND_ME_EMBED_FORWARD_BATCH` | `32` | Chunks per ONNX forward pass inside the embedder — the hard ceiling on embedding memory per call |
| `REMIND_ME_ANN_MIN_CHUNKS` | `5000` | Chunk-vector count above which semantic search uses the optional HNSW ANN index (`usearch`) instead of sqlite-vec's exact brute-force scan. Requires the `ann` extra (`pip install remind-me-mcp[ann]`); degrades gracefully to the exact scan if missing |
| `REMIND_ME_MEMPALACE_PATH` | `~/.mempalace/palace` | Path to a MemPalace ChromaDB persistent store, read (read-only) by `remind_me_import_mempalace` |
| `REMIND_ME_CONSOLIDATE_MAX_CANDIDATES` | `1500` | Hard cap on how many memories `remind_me_consolidate`'s clustering step pairwise-compares in one call — `remind_me_consolidate`'s own `limit` (max 5000) doesn't alone bound the O(n²) comparison cost |
| `REMIND_ME_API_KEY` | *(auto-generated)* | Bearer token for `/api/*` routes. When unset, a key is generated on first run and stored at `~/.remind-me/api_key` (0600) — check the server log or that file for the value. Set to `disabled` to explicitly turn dashboard auth off |
| `REMIND_ME_IMPORT_ROOTS` | `$HOME` | `os.pathsep`-separated (`:` on macOS/Linux, `;` on Windows) allowed filesystem roots for import operations (enforced by both the HTTP API and the MCP import tools) |
| `REMIND_ME_EXPORT_ROOTS` | `$HOME` | `os.pathsep`-separated (`:` on macOS/Linux, `;` on Windows) allowed filesystem roots for export destinations (enforced by both the HTTP API and the MCP export tool) |
| `REMIND_ME_WATCH_DIRS` | *(unset)* | `os.pathsep`-separated (`:` on macOS/Linux, `;` on Windows) directories for the folder watcher to auto-ingest. Empty = watcher disabled. Each directory must lie inside `REMIND_ME_IMPORT_ROOTS` |
| `REMIND_ME_WATCH_INTERVAL` | `60` | Seconds between folder watcher scan passes |
| `REMIND_ME_WATCH_GRACE` | `5` | Debounce grace period in seconds — files modified more recently than this are deferred until a scan sees a stable (mtime, size) |
| `REMIND_ME_REMINDER_POLL_INTERVAL` | `60` | Seconds between the reminder scheduler's poll passes for due `remind_at` timestamps. The scheduler itself always runs — no separate enable switch |
| `REMIND_ME_REVISION_RETENTION_DAYS` | `90` | An edit-history snapshot (see [Edit History](#edit-history)) older than this is hard-deleted by the reminder-scheduler loop — purely time-based, no per-peer acknowledgment tracking (`memory_revisions` is never synced). Bounds how far back `remind_me_revert` can reach |
| `REMIND_ME_ANALYTICS_RETENTION_DAYS` | `730` | A daily analytics-trend snapshot (`analytics_snapshots`, `GET /api/analytics/trend`) older than this is hard-deleted by the reminder-scheduler loop — purely time-based, never synced. Deliberately an order of magnitude past `REMIND_ME_REVISION_RETENTION_DAYS`'s 90-day default: each row is one tiny daily rollup meant for long-range trend viewing, not audit |
| `REMIND_ME_NOTIFY_WEBHOOK_URL` | *(unset)* | Webhook URL that receives a generic `{"subject", "body", "source": "remind-me"}` JSON POST per notification. Empty disables the webhook notifier — gated on config presence, no separate enable flag |
| `REMIND_ME_NOTIFY_WEBHOOK_TIMEOUT` | `5` | Seconds to wait for the webhook POST before giving up, so a hung endpoint can't block the reminder scheduler or sync thread |
| `REMIND_ME_NOTIFY_SMTP_HOST` | *(unset)* | SMTP server host. Empty (with no recipients) disables the email notifier |
| `REMIND_ME_NOTIFY_SMTP_PORT` | `587` | SMTP port. `465` always uses implicit TLS (`SMTP_SSL`) regardless of `_USE_TLS`; any other port uses plain `SMTP` with STARTTLS applied when `_USE_TLS` is true |
| `REMIND_ME_NOTIFY_SMTP_USER` | *(unset)* | SMTP AUTH username. Empty skips SMTP AUTH entirely |
| `REMIND_ME_NOTIFY_SMTP_PASSWORD` | *(unset)* | SMTP AUTH password |
| `REMIND_ME_NOTIFY_SMTP_FROM` | *(unset)* | From address. Falls back to `REMIND_ME_NOTIFY_SMTP_USER` when unset |
| `REMIND_ME_NOTIFY_SMTP_TO` | *(unset)* | Comma-separated recipient address(es). Required (with `_SMTP_HOST`) for the email notifier to be considered configured |
| `REMIND_ME_NOTIFY_SMTP_USE_TLS` | `true` | STARTTLS a plaintext SMTP connection before authenticating. No effect on port 465 (always implicit TLS) |
| `REMIND_ME_NOTIFY_SYNC_FAULT_INTERVAL` | `1800` | Minimum seconds between sync-fault notifications, so a persisting `fault` verdict from `remind_me_sync_reconcile` doesn't re-alert on every call |
| `REMIND_ME_DIGEST_INTERVAL` | *(unset)* | `daily`, `weekly`, or unset/empty to disable scheduled digest delivery via `notify()`. The on-demand `remind_me_digest` tool call always works regardless of this setting |
| `REMIND_ME_SAVED_SEARCH_POLL_INTERVAL` | `300` | Seconds between the scheduler's poll passes checking `watch=true` saved searches for new matches — see [Saved Searches](#saved-searches). Not itself an enable switch; whether a pass does anything is gated per-search by `watch` |
| `REMIND_ME_TOOL_PROFILE` | `full` | Advertised tool surface: `full` (48 tools, ~21k context), `standard` (30, ~14.8k — drops imports/sync/ops), or `core` (17, ~7.8k — conversational only, also hides the maintenance prompts). An unrecognised value logs a warning and falls back to `full` |
| `REMIND_ME_MAINTENANCE_NUDGES` | `true` | Whether search/add responses may carry a maintenance-backlog nudge. Set `false` to silence them entirely |
| `REMIND_ME_MAINTENANCE_NUDGE_INTERVAL` | `3600` | Minimum seconds between nudge *checks*. Bounds cost as well as noise — the backlog COUNTs only run when this has elapsed |
| `REMIND_ME_MAINTENANCE_NUDGE_THRESHOLD` | `25` | Queue depth a backlog must reach before it's worth mentioning |
| `REMIND_ME_FEEDBACK_HINT_INTERVAL` | `7200` | Minimum seconds between feedback hints on search responses. Longer than the maintenance interval by default — a standing affordance repeated too often is wallpaper. Also silenced by `REMIND_ME_MAINTENANCE_NUDGES=false` |
| `REMIND_ME_WEBHOOK_SECRET` | *(unset)* | Bearer token for the push/webhook ingestion server. Empty = disabled — the server refuses to start without it |
| `REMIND_ME_WEBHOOK_PORT` | `8769` | Port for the push/webhook ingestion server |
| `REMIND_ME_WEBHOOK_BIND` | `127.0.0.1` | Bind address for the push/webhook ingestion server. Widen deliberately (e.g. a Tailscale IP) since it writes arbitrary pushed content directly into memory |
| `REMIND_ME_RATE_LIMIT_ENABLED` | `true` | Whether `POST /ingest` and the remote MCP connector's endpoint enforce a request-rate limit. `""` disables it entirely, mirroring how `REMIND_ME_RERANK=""` disables reranking |
| `REMIND_ME_RATE_LIMIT_REQUESTS` | `60` | Max requests per `REMIND_ME_RATE_LIMIT_WINDOW_SECONDS` per rate-limit key (a verified bearer/connector token, or the caller's IP as a fallback) |
| `REMIND_ME_RATE_LIMIT_WINDOW_SECONDS` | `60` | Window length in seconds for `REMIND_ME_RATE_LIMIT_REQUESTS` |
| `REMIND_ME_OTEL_ENABLED` | `false` | Enable OpenTelemetry tracing of tool calls, sync cycles, and watcher scans. Requires the `otel` extra (`pip install remind-me-mcp[otel]`); degrades gracefully to a no-op if missing |
| `REMIND_ME_OTEL_ENDPOINT` | *(unset)* | OTLP/HTTP collector endpoint (e.g. `http://localhost:4318/v1/traces`). Unset uses the OTLP exporter's own default |
| `REMIND_ME_OTEL_SERVICE_NAME` | `remind-me-mcp` | `service.name` resource attribute reported to the collector |
| `REMIND_ME_AUTO_UPDATE_CHECK` | `true` | Set to `false` to skip the background `git fetch` update check at server startup (the manual check/update tools keep working) |
| `REMIND_ME_UPDATE_EXPECTED_ORIGIN` | *(unset)* | Optional trust pin for `remind_me_self_update`: when set, refuses to `git pull`/`pip install` unless the local `origin` remote's URL matches exactly — a repointed remote (compromise, a stray `git remote set-url`) is refused instead of silently trusted |
| `REMIND_ME_RRF_K` | `60` | Smoothing constant for Reciprocal Rank Fusion scoring |
| `REMIND_ME_RRF_W_KEYWORD` | `1.0` | RRF weight for the keyword (FTS5) signal |
| `REMIND_ME_RRF_W_SEMANTIC` | `1.0` | RRF weight for the semantic (vector) signal |
| `REMIND_ME_RRF_W_RECENCY` | `1.0` | RRF weight for the recency signal (set `0` for a pure-retrieval profile) |
| `REMIND_ME_RRF_W_VITALITY` | `1.0` | RRF weight for the vitality signal (set `0` for a pure-retrieval profile) |
| `REMIND_ME_RRF_W_IDF` | `0.0` | RRF weight for the IDF signal (derived from FTS5's `bm25()` score). Off by default — set a positive value to opt in |
| `REMIND_ME_RRF_FUSION` | `rank` | Fusion mode: `rank` (classic ordinal Reciprocal Rank Fusion) or `score` (normalized-magnitude fusion over `bm25`/semantic-distance/recency/vitality — preserves match-strength information that rank-only RRF discards). Off by default; opt in with `score` |
| `REMIND_ME_RERANK` | `onnx` | Reranks the top search candidates with a cross-encoder (on by default — bounded to `REMIND_ME_RERANK_TOP_K` candidates, so latency is small and constant). Set to `""` to disable for latency-sensitive deployments |
| `REMIND_ME_RERANK_MODEL` | `BAAI/bge-reranker-base` | HuggingFace cross-encoder repo (must ship `onnx/model.onnx`). The default is a bilingual Chinese/English model — see [Language Coverage](#language-coverage) |
| `REMIND_ME_RERANK_TOP_K` | `20` | How many top RRF candidates the reranker rescores |
| `REMIND_ME_OCR_DET_MODEL_PATH` | *(unset)* | Path to an alternate ONNX text-detection model for image OCR (`RapidOCR(det_model_path=...)`) — see [Enabling Image (OCR) Import](#enabling-image-ocr-import) |
| `REMIND_ME_OCR_CLS_MODEL_PATH` | *(unset)* | Path to an alternate ONNX text-orientation-classification model for image OCR (`RapidOCR(cls_model_path=...)`) |
| `REMIND_ME_OCR_REC_MODEL_PATH` | *(unset)* | Path to an alternate ONNX text-recognition model for image OCR (`RapidOCR(rec_model_path=...)`) — determines which script(s) OCR can actually read; the bundled default only covers Chinese + English/Latin+digits |
| `REMIND_ME_AUDIO_MODEL` | `base` | Whisper model size/name for audio transcription (`faster-whisper`) — see [Audio Import](#audio-import) |
| `REMIND_ME_QUERY_EXPANSION` | *(unset)* | Set to `hyde` to expand queries with a hypothetical answer passage before vector search |
| `REMIND_ME_HYDE_MODEL` | `llama3.2` | Ollama model that writes the HyDE passage |
| `REMIND_ME_HYDE_TIMEOUT` | `15` | Seconds to wait for HyDE generation before falling back to the plain query |
| `REMIND_ME_CLIENT` | `unknown` | Client identifier reported in server status |
| `REMIND_ME_NODE_ID` | *(unset)* | Unique identifier for this machine (enables sync when set with HUB_URL and SYNC_SECRET) |
| `REMIND_ME_HUB_URL` | *(unset)* | URL of the sync hub (e.g., `http://100.x.x.x:8765`) |
| `REMIND_ME_SYNC_SECRET` | *(unset)* | Shared bearer token for hub and peer authentication |
| `REMIND_ME_SYNC_INTERVAL` | `60` | Seconds between sync cycles |
| `REMIND_ME_PEER_PORT` | `8766` | Local port for the peer-to-peer sync server |
| `REMIND_ME_PEER_BIND` | `0.0.0.0` | Bind address for the peer sync server (set to a Tailscale IP or `127.0.0.1` to narrow exposure; every request still requires the sync secret) |
| `REMIND_ME_OUTBOX_RETENTION_DAYS` | `30` | Sync outbox rows older than this are pruned each sync cycle |
| `REMIND_ME_TOMBSTONE_RETENTION_DAYS` | `180` | A deleted memory (tombstoned via `deleted_at`) is hard-deleted this many days after the delete — purely time-based, no per-peer acknowledgment tracking. Deliberately more generous than `OUTBOX_RETENTION_DAYS`: compacting a tombstone too early risks a still-offline device later resurrecting it |
| `REMIND_ME_STATIC_PEERS` | `[]` | JSON array of static peer configs (for environments without Tailscale) |
| `REMIND_ME_TAILSCALE_SOCKET` | *(unset)* | Path to Tailscale socket for peer discovery (auto-detected if empty) |

## Project Structure

```
remind-me-mcp/
├── remind_me_mcp/              # Main package
│   ├── __init__.py             # Package exports, version
│   ├── __main__.py             # CLI entry point, mode dispatch
│   ├── server.py               # FastMCP instance, app lifespan
│   ├── tools/                  # 41 MCP tools + 4 resources
│   │   ├── search.py           # Hybrid search + structured/entity queries
│   │   ├── crud.py             # add / list / get / update / delete
│   │   ├── capture.py          # auto-capture, decompose, extract/annotate
│   │   ├── lifecycle.py        # vitality, reclassify, consolidate
│   │   ├── entity.py           # entity lookup + multi-hop relation traversal
│   │   ├── normalize.py        # ingest-time normalization batch/apply
│   │   ├── wiki.py             # LLM Wiki: page read/write/list/search/load/delete + compile
│   │   └── admin.py            # import/export, stats, status, updates, OAuth revocation
│   ├── models.py               # Pydantic input models
│   ├── config.py               # Environment configuration, constants
│   ├── wiki.py                 # LLM Wiki engine: file IO, wikilinks, index/log, reconcile
│   ├── db.py                   # SQLite schema, migrations (v0–v14), entity helpers
│   ├── api.py                  # Starlette HTTP API + dashboard HTML
│   ├── remote.py               # Remote MCP connector (Streamable HTTP; OAuth or secret-path)
│   ├── oauth.py                # Single-user OAuth 2.1 authorization server
│   ├── importer.py             # Chat export + document parser & import engine
│   ├── mempalace_import.py     # Optional MemPalace (ChromaDB) bulk importer
│   ├── dbs_import.py           # dbs (SQLite) bulk importer, entities not prose
│   ├── exporter.py             # Memory + entity-graph export engine
│   ├── watcher.py              # Watched-folder auto-ingest (poll, debounce, supersede)
│   ├── webhook_server.py       # Push/webhook ingestion HTTP endpoint
│   ├── telemetry.py            # Optional OpenTelemetry span instrumentation
│   ├── storage_interfaces.py   # Storage-layer Protocol documentation (prep-only, no new backend)
│   ├── embeddings.py           # ONNX/Ollama embedding engine
│   ├── formatting.py           # Memory markdown/JSON formatters
│   ├── retrieval.py            # RRF rank fusion, recency signals, token budget
│   ├── reranker.py             # Optional ONNX cross-encoder reranking
│   ├── query_expansion.py      # Optional HyDE query expansion (Ollama)
│   ├── vitality.py             # ACT-R decay model, access recording, bridge protection
│   ├── consolidation.py        # Semantic clustering (Union-Find), canonical selection, merge
│   ├── pid.py                  # PID file management, instance detection
│   ├── sidecars.py             # Tunnel/dashboard sidecar processes tied to server lifetime
│   ├── updater.py              # Version checking, self-update logic
│   ├── sync.py                 # Background sync engine (hub + peer push/pull, entity graph)
│   ├── peer_server.py          # Lightweight HTTP server for peer-to-peer sync
│   └── dashboard/
│       └── App.jsx             # React dashboard component
├── benchmarks/                 # Retrieval benchmark harness (LongMemEval)
├── tests/                      # Test suite — 1100+ tests (pytest + pytest-asyncio)
├── pyproject.toml              # Package configuration and dependencies
└── README.md                   # This file

~/.remind-me/                   # Data directory (synced across machines)
├── memory.db                   # SQLite database with FTS5 + sqlite-vec (schema v14)
├── wiki/                       # LLM Wiki markdown files (source of truth: pages, index.md, log.md, SCHEMA.md)
├── models/                     # Cached ONNX embedding model (~80MB, auto-downloaded)
├── api_key                     # Auto-generated dashboard API key (0600)
├── connector_token             # Auto-generated remote-connector token (0600)
├── oauth.json                  # OAuth client registrations + token hashes (0600)
├── import_log.json             # Import history
└── server.pid                  # PID file when dashboard is running
```

## CLI Reference

```bash
remind-me-mcp                        # MCP stdio mode (default)
remind-me-mcp --serve-ui             # Start dashboard UI server
remind-me-mcp --serve-ui --ui-port 8080 --ui-host 0.0.0.0
remind-me-mcp --serve-mcp                        # MCP HTTP transport on port 8767
remind-me-mcp --serve-mcp --mcp-host 0.0.0.0     # Bind to all interfaces
remind-me-mcp --serve-mcp --serve-ui              # Combined: dashboard + MCP HTTP
remind-me-mcp --serve-remote                      # Remote connector for claude.ai (port 8768)
remind-me-mcp --serve-remote --remote-port 9000   # Custom connector port
remind-me-mcp --status               # Check if dashboard is running
remind-me-mcp --version              # Print installed version
remind-me-mcp --check-update         # Check for available updates
remind-me-mcp --update               # Pull latest and reinstall
```

You can also run via `python -m remind_me_mcp` with the same flags.

| Flag | Default | Description |
|------|---------|-------------|
| *(none)* | — | MCP stdio mode for Claude Code / Claude Desktop |
| `--serve-ui` | off | Start the HTTP dashboard server |
| `--ui-port PORT` | `5199` | Dashboard port |
| `--ui-host HOST` | `127.0.0.1` | Dashboard bind address |
| `--serve-mcp` | off | MCP server over Streamable HTTP transport |
| `--mcp-port PORT` | `8767` | MCP HTTP port |
| `--mcp-host HOST` | `127.0.0.1` | MCP HTTP bind address |
| `--serve-remote` | off | Remote MCP connector for claude.ai (standalone mode — `--serve-ui`/`--serve-mcp` are ignored when set) |
| `--remote-port PORT` | `8768` | Remote connector port |
| `--remote-host HOST` | `127.0.0.1` | Remote connector bind address (keep localhost; let the tunnel do the exposing) |
| `--status` | — | Check if the dashboard is running, then exit |
| `--version` | — | Print the installed version, then exit |
| `--check-update` | — | Check for available updates, then exit |
| `--update` | — | Pull latest changes from origin and reinstall, then exit |

Each serve flag has an environment-variable equivalent (`REMIND_ME_MCP_SERVE_UI`, `REMIND_ME_MCP_SERVE_HTTP`, `REMIND_ME_REMOTE_MCP`) — see the table above.

## Architecture

The server uses:
- **SQLite FTS5** for keyword full-text search (inverted index, boolean queries)
- **sqlite-vec** for semantic vector search (cosine similarity on embeddings)
- **all-MiniLM-L6-v2** via ONNX Runtime for local embedding generation (~80MB model, no API keys)
- **Optional HNSW ANN index** (`usearch`) — takes over from sqlite-vec's exact brute-force scan once the store passes `REMIND_ME_ANN_MIN_CHUNKS` chunk vectors (default 5000), self-healing and always falling back to the exact scan if unavailable
- **RRF rank fusion** (k=60) — merges keyword, semantic, recency, vitality, and an opt-in IDF signal without score normalization
- **Auto-routing retrieval strategy** — a deterministic query-shape heuristic (no LLM call) picks a keyword-favored, semantic-favored, or balanced RRF weight profile; presets are relative multipliers on the live weights, so a deliberately-zeroed signal (e.g. a benchmark's `--rrf-profile`) is never resurrected
- **Token budget** — search results are trimmed to an 800-token default cap to prevent LLM context overflow
- **ACT-R vitality model** — cognitive-science decay with per-category rates, access reinforcement, signed helpful/unhelpful feedback, and bridge protection
- **Structured triples** — subject/predicate/object columns with indexed query routing
- **Contradiction-based supersession** — a new/updated SPO triple sharing an existing fact's subject+predicate but a different object supersedes it, deterministically, at write time
- **Entity knowledge graph** — `entities` and `memory_entities` tables with deterministic name-derived ids, alias union-merge, and 1-hop search expansion
- **Union-Find clustering** — transitive semantic similarity grouping for vault consolidation
- **Section-aware document chunking** — Markdown imports split per heading section, plain text per paragraph
- **Pluggable connectors** — `chat`/`document` (and third-party kinds like `mempalace`) are parser functions registered by kind string, not a hardcoded dispatch — `remind_me_list_connectors` reports the registry
- **Neighbor-aware chunk retrieval** — every import-produced chunk carries a `doc_id`/`chunk_index`; opt-in search expansion surfaces adjacent chunks from the same source document
- **Polling folder watcher** — mtime/size scans with a debounce grace window and changed-file supersession (no inotify dependency)
- **Push/webhook ingestion** — a bearer-authenticated `POST /ingest` endpoint accepts content directly (no filesystem staging), sharing the same connector pipeline and hash dedup as file import
- **Client-side ingest normalization** — `remind_me_normalize_batch`/`remind_me_normalize_apply` distill noisy raw imports into `{question, summary, resolution?}` memories; the LLM work happens in the calling agent, not the server, same as `remind_me_decompose`
- **Outbox-based sync** — local writes (memories, entities, links) are captured in `sync_outbox`, pushed to hub/peers in background
- **Tombstone-based delete propagation** — deleting a memory sets `deleted_at` instead of removing the row, so the deletion rides the existing update/LWW sync path instead of silently failing to propagate; a background compaction pass hard-deletes old-enough tombstones
- **Postgres hub** — central sync point with last-write-wins conflict resolution
- **Peer-to-peer sync** — direct machine-to-machine sync via Tailscale peer discovery
- **WAL journal mode** for safe concurrent access
- **Content-based hashing** for deduplication
- **stdio + Streamable HTTP transports** — stdio for local Claude interfaces; HTTP for remote access via Tailscale or SSH tunnel; a hardened remote-connector mode (OAuth 2.1 or secret-path) for claude.ai
- **Starlette + Uvicorn** for the optional HTTP dashboard and REST API
- **Self-contained HTML** — the dashboard is served as a single inline page with no build step
- **Graceful degradation** — semantic search, vitality scoring, and distributed sync are all optional; core functionality works with just FTS5 and local storage
- **Optional OTEL instrumentation** — a single `maybe_span()` no-op context manager wraps tool calls, sync cycles, and watcher scans; zero-cost and zero-dependency unless `REMIND_ME_OTEL_ENABLED=1` and the `otel` extra are both set, exporting to any OTLP/HTTP collector (no bundled collector — that would conflict with the zero-ops, local-first design)
- **Storage-interface Protocols** (`storage_interfaces.py`) — the entity-graph and vector-search operations `db.py` implements, documented as `typing.Protocol`s and mypy-verified against the real functions; prep/documentation only, not a second backend (see "Design Scope" below)

## Design Scope

remind_me is local-first, single-user, and MCP-native by design — some capabilities other memory/knowledge systems offer are deliberately out of scope rather than missing, because building them would work against that center. Documented here so the reasoning doesn't have to be reconstructed from a GitHub issue thread:

- **Pluggable vector/graph storage backends (Neo4j, Qdrant, etc.)** — not planned. remind_me stores everything in one SQLite file (+ `sqlite-vec` for vectors) so it stays zero-ops: no second service to run, back up, or lose sync with. `storage_interfaces.py` documents the storage operations as `Protocol`s (mypy-verified against the real SQLite implementation) purely so the seam is legible if this ever changes — it ships no second backend and implies no near-term plan to build one.
- **Multimodal *retrieval* (visual/audio search over binary content itself)** — deferred; images and audio are extracted to plain text at import time instead (OCR for images since #181, Whisper transcription for audio since #192) and flow through the exact same text-native pipeline as every other memory: chunking, FTS5, embedding, the wiki. What's deliberately still out of scope is a *second* embedding pipeline over the binary content itself (e.g. CLIP-style image embeddings, audio fingerprinting) — extract-to-text has served every concrete use case so far, so a genuinely multimodal retrieval index hasn't been justified yet. Revisit only if a concrete use case emerges that extraction-to-text can't serve.
- **Multi-tenant / cross-agent isolation** — deferred. remind_me is explicitly single-owner by design: one OAuth owner token, one SQLite file per node. Multi-tenancy is an architecture change orthogonal to "personal memory," not a gap in the current design — worth revisiting only if the project's scope deliberately shifts toward shared/team memory infrastructure.
- **Client SDKs beyond MCP** — no hand-written TS/Rust/etc. SDKs (maintenance surface disproportionate to a single-user local tool whose real client is Claude via MCP). Instead, the existing `GET /api/*` REST surface is published as an [OpenAPI 3.0 spec](docs/openapi.yaml) so any language can generate a thin client for free.
- **Cloud/managed & serverless hosting** — no managed hosting product. The per-user SQLite node is designed to stay local; the one component that's natural to host centrally (the sync hub) already had a Podman quadlet deploy path, and now also has [Docker Compose, Fly.io, and Railway templates](hub/deploy/) — deliberately still self-hosted, not a one-click managed service.
- **Native adapters for other coding-agent hosts (Codex, Cursor, OpenClaw, Hermes, ...)** — deferred, and host auto-detection (a `detect`-style utility) along with it, since detection only has something to detect *among* once more than one host adapter exists. remind_me's live integration surface is MCP itself — Claude.ai, Claude Code, and Claude Desktop attach as an MCP server — and any other file/log-based source already has a general path in via the chat-export importer, watched folders, or the webhook endpoint. Building adapters that tail a specific other agent's proprietary, undocumented session-log format is an architecture change orthogonal to "personal memory for Claude clients," not a gap in the current design — it only pays off if the project's scope deliberately shifts toward shared memory infrastructure for arbitrary coding agents. Revisit only if that scope shift happens ([#109](https://github.com/baileyrd/remind_me/issues/109), [#110](https://github.com/baileyrd/remind_me/issues/110)).

## Changelog

See [`RELEASE_NOTES.md`](RELEASE_NOTES.md) for a per-version feature breakdown with PR references; this section summarizes the same history phase-by-phase.

### 1.27.0 — 2026-08-01

Reverses v1.26.0's "not built" call on the tool-profile gate, which rested on a token count that was wrong by 2.6x (descriptions only, ignoring input schemas). The real surface is ~21k tokens per session. Adds `REMIND_ME_TOOL_PROFILE` (`full`/`standard`/`core`, default `full`), reports the cost in `remind_me_server_status` so the knob is discoverable, and slims `remind_me_search`'s schema by ~19% — the one saving no profile can deliver, since every session needs that tool. Still not an accuracy fix, and tested to stay honest about that.

### 1.26.0 — 2026-08-01

Tool-selection clarity and the feedback loop. Disambiguates the four tools that all read as "find things" (`search`/`list`/`get`/`entity`) so each names the neighbour to prefer, and **drops the planned `core`/`full` profile gate** — those four are all in the core set a profile would keep, so hiding admin tools cannot fix the confusion (see [Design Scope](#design-scope) for the measured trade-off). Also fixes `remind_me_feedback`'s `query` parameter, which was described as being "for audit/reporting" when it actually switches the signal from global to query-contextual — a mechanical reason that path stayed untrained.

### 1.25.0 — 2026-08-01

Makes two existing-but-unreachable signals visible. **Maintenance nudges** move the backlog counts off `remind_me_server_status` (which a conversational session never calls) onto search/add responses — throttled, thresholded, markdown-only, with `maintenance.py` owning one `WHERE` clause per queue so the nudge and the batch tool can't disagree. **Capture health** makes "auto-capture was never configured" a visible state in `remind_me_server_status` instead of something inferred from silence.

### 1.24.0 — 2026-08-01

Closes the gap that made remind_me's behaviour depend on prose the user pasted into each client by hand: the server now ships **instructions** in its MCP `initialize` response (when to retrieve, when to store, when to send feedback, and that batch tools are operator workflows), and exposes the six multi-step maintenance loops as **MCP prompts** so their sequencing lives in the server rather than the README. Additive only — no schema, tool-signature, or wire-format change.

### 1.23.0 — 2026-07-31

Closes the "worth pursuing" item from the [memU capability review](docs/memu-capability-review-2026-07-31.md): the wiki's default `SCHEMA.md` now documents a **procedure page** convention (steps/edge-cases/branches, plus explicit patch-vs-create guidance) for task-shaped sources, alongside the existing free-form knowledge pages. Prompt/schema-only — `remind_me_wiki_compile` needed no code change since it already embeds the live schema verbatim into its brief.

### 1.19.0 — 2026-07-22

Closes the last item from the application capability review: true ACT-R-style memory reinforces associations *between* items retrieved together, not just each item independently — nothing previously captured "these two memories tend to be useful together." Explicitly flagged as the most speculative item on the list, with real design risk around weighting/decay/runaway feedback loops; shipped a deliberately scoped-down slice instead of the full design.

- **Co-retrieval reinforcement** — a new `memory_associations` table tracks a bounded, undecayed weight per memory pair returned together in a search (`vitality.record_co_retrieval`). New opt-in `expand_co_retrieval` flag on `remind_me_search` surfaces the strongest associations in a `related_via_co_retrieval` section. Every search reinforces associations regardless of the flag; only surfacing is opt-in.
- **Never affects ranking** — the design choice that eliminates the flagged "runaway feedback loop" risk entirely: recorded associations only ever get *suggested*, never fed back into RRF scoring, same posture as `expand_entities`/`include_neighbors`.

### 1.18.0 — 2026-07-22

Closes a dashboard-visibility gap flagged in the application capability review: the knowledge graph is fully built out server-side but had no dashboard UI at all — no way to browse "what does the app know about X and how is it connected" without hand-crafting API calls.

- **New "Entities" dashboard view** — clickable entity list (most-mentioned first) plus a detail panel with facts, linked memories, and a "Related Entities" drill-down, mirroring the Wiki view's list+detail layout.
- **`GET /api/entities`** (list, paginated) and **`GET /api/entity/traverse`** (multi-hop relation walk) — new REST routes powering the view. The traversal logic moved from `tools/entity.py` into `db.py` so the MCP tool and REST route share one implementation.

### 1.17.0 — 2026-07-22

Closes a dashboard-visibility gap flagged in the application capability review: `remind_me_vitality_report`'s active/dormant counts and vitality-bucket distribution — the app's core "memory going stale" model — were only reachable via the MCP tool, invisible in the dashboard.

- **`GET /api/vitality`** — new REST route, sharing its computation with `remind_me_vitality_report` via a new `vitality.build_vitality_report(db)` so both surfaces report identical numbers.
- **Vitality Distribution chart** in the dashboard's Stats view, reusing the existing `BarChart` component (now with an optional `preserveOrder` prop for bucket-ordered rather than count-sorted display) plus a vault-health summary line.

### 1.16.0 — 2026-07-21

Closes a silent-degradation gap flagged in the application capability review: changing the embedding model/dimension/backend required remembering a manual `remind_me_reindex`, and there was no stored record of which model produced the vectors currently in the store — a forgotten reindex meant KNN silently ran against a different model's embedding space with no error at all.

- **Embedding-model versioning** — a new local-only `embedding_meta` table records the model/dimension/backend that actually produced the currently stored vectors, updated after every successful (re-)embed.
- **Automatic stale-vector clearing** — every startup compares recorded vs. configured model/dim/backend; on a mismatch, `memories_vec`/`vec_chunks` (and the on-disk ANN index) are cleared automatically and `memories_vec` recreated at the new dimension if needed, so every memory falls through to the existing missing-embeddings path instead of silently serving wrong-model results.
- `remind_me_server_status` surfaces an explicit "Embedding model changed" warning distinct from the generic missing-embeddings message.

### 1.15.0 — 2026-07-21

Closes a data-safety gap flagged in the application capability review: there was no backup command anywhere, and schema migrations ran with no snapshot or safety net — a failed or buggy migration against the single SQLite file holding someone's entire memory store had no way back short of a manual file copy.

- **`remind_me_backup` tool** — on-demand backup via SQLite's WAL-safe `Connection.backup()` API (not a raw file copy, which could read a torn/mid-checkpoint page). Written under `MEMORY_DIR/backups/`.
- **Pre-migration snapshot guard** — `_migrate_schema()` snapshots the database before running any pending migration, so a migration that fails or completes but is semantically wrong can be rolled back. Skipped for a brand-new empty database; snapshot failure is logged, never blocks the migration.
- **Automatic retention** — only the most recent `REMIND_ME_BACKUP_RETENTION_COUNT` backups (default 10) are kept, pruned after each new backup.

### 1.14.0 — 2026-07-21

Closes a dashboard-usability gap flagged in the application capability review: `GET /api/memories/search` returned a flat, capped list with no way to page through results beyond the cap, and there was no bulk delete/tag/reclassify REST endpoint despite the equivalent single-item operations already existing.

- **Search pagination** — `GET /api/memories/search` gains `offset` and now returns the same pagination envelope (`total`/`count`/`offset`/`limit`/`has_more`) `GET /api/memories` already had.
- **Bulk REST endpoints** — `POST /api/memories/bulk/{delete,tag,reclassify}`, each taking an explicit id list (max 200) rather than a filter, so a request can never silently affect more than what a dashboard's list/search selection actually picked. `bulk/delete` mirrors single-delete's soft-delete-when-sync-configured behavior exactly; `bulk/tag` supports add/remove/set modes; `bulk/reclassify` mirrors `remind_me_reclassify`.
- `docs/openapi.yaml` updated with the new routes and pagination fields.

### 1.13.0 — 2026-07-21

Closes a multi-device data-loss gap flagged in the application capability review: `_upsert_one`'s whole-row last-write-wins meant two devices editing *different* fields of the same memory between sync cycles (one adds a tag, another edits content) had whichever write arrived second silently clobber the other's change entirely, not just the field that actually conflicted.

- **Field-level conflict merge for memory sync** — `tags` union-merge (dedup, order-preserving, same semantics entities already use for aliases) and `metadata` shallow-merges key by key (the LWW winner's value wins only on an actual key collision; keys unique to either side are kept), regardless of which side wins on `updated_at`. A record that loses LWW on `content`/other scalar fields still gets its tags/metadata folded in via a merge-only update that doesn't bump `updated_at`. Applies to both hub-pull and peer-pull, since both go through the same client-side upsert. The hub's own Postgres storage still does whole-row LWW for now — a documented scope decision, not an oversight.

### 1.12.0 — 2026-07-21

Closes a ranking gap flagged in the application capability review: every new memory started at a flat `base_weight=1.0` regardless of kind, so a throwaway aside competed evenly with a real decision until feedback/access signal accrued — and decisions are exactly the memories least likely to be re-queried immediately, so they'd lose that early ranking race.

- **Importance prior at write time** — `remind_me_decompose` seeds `base_weight` from its already-known `memory_type` (`decision`/`blocker` highest); `remind_me_add` seeds from `source` since `memory_type` isn't known yet (`manual` keeps the flat default, raw import sources start slightly lower). A fresh memory's `vitality` is set to match its seeded `base_weight` exactly. Purely additive — an unrecognized or absent source, or the `unclassified` type, still falls through to the original flat 1.0 default.

### 1.11.0 — 2026-07-21

Closes a vault-hygiene gap flagged in the application capability review: `merge_cluster` unioned raw content lines from clustered memories rather than summarizing them, so merged memories grew unbounded and stayed verbose instead of becoming genuinely consolidated. Its clustering step was also a Python-level O(n²) double loop.

- **Summarization instead of concatenation** — `remind_me_consolidate`'s auto-merge (`dry_run=False`) now requires an LLM-authored `summaries` entry (`{canonical_id: summary}`) per cluster, produced client-side after reviewing a `dry_run=True` report — the same pattern already used by `remind_me_decompose`/`remind_me_normalize_apply`. A found cluster with no matching summary is skipped and reported (`skipped_no_summary`), not silently merged with a raw concatenation. `merge_cluster` gained an optional `summary` parameter that replaces the union entirely when given; omitted, it falls back to the original union (kept for callers without an LLM in the loop, e.g. tests).
- **Bounded, vectorized clustering** — `find_clusters`'s pairwise similarity-threshold check is now a single vectorized numpy comparison instead of an O(n²) Python loop; a new `REMIND_ME_CONSOLIDATE_MAX_CANDIDATES` (default 1500) hard-caps the candidate pool so a large vault degrades gracefully (a logged truncation) instead of an unbounded comparison.

### 1.10.0 — 2026-07-21

Closes the biggest gap in the feedback loop flagged in the application capability review: `record_feedback` always adjusted `base_weight` globally, discarding `FeedbackInput`'s `query` field entirely — a memory marked unhelpful for one query got demoted for *every* future query, even a completely unrelated one.

- **Query-contextual feedback** — `remind_me_feedback` now has two modes. Without `query`: the original global `base_weight` adjustment, unchanged. With `query`: the event is logged (memory, query, signal, magnitude) to a new `memory_feedback` table instead of touching `base_weight`; at ranking time, a future query is compared against stored feedback queries via Jaccard token-overlap similarity, and matches above a threshold nudge that memory's RRF score up or down (capped at ±40%) before reranking. Purely local bookkeeping, not synced across devices — same scope as the dbs/MemPalace import dedup tables.

### 1.9.0 — 2026-07-21

Closes a query-routing gap flagged in the application capability review: the `strategy="auto"` heuristic router had no awareness of temporal expressions, even though `temporal-reasoning` is one of the two weakest query categories documented in `benchmarks/RESULTS.md`.

- **Temporal-expression query routing** — a new detector recognizes temporal expressions ("before I moved", "last summer", "when I lived in Seattle", a bare year) and boosts `w_recency` by 1.5x on top of whichever keyword/semantic profile the query's shape already picked. Composes with the existing routing rather than replacing it, so a temporal query gets recency-aware ranking regardless of whether it's also short/keyword-shaped or long/semantic-shaped. Always active under `strategy="auto"` — no separate toggle needed, same design as the existing keyword/semantic shape heuristics.
- `benchmarks/before_after.py` gains `--compare temporal` for isolated A/B measurement against `RESULTS.md`'s `temporal-reasoning` category.

### 1.8.0 — 2026-07-21

Closes a precision gap flagged in the application capability review: RRF fuses signals purely by ordinal rank, discarding the actual score magnitude — a 0.95-cosine match and a 0.55-cosine match tie if they land in adjacent rank positions.

- **Score-based RRF fusion (opt-in)** — `rank_rrf` gains a `fusion="score"` mode (`REMIND_ME_RRF_FUSION=score`) that min-max normalizes the real underlying magnitudes (`bm25` score, semantic distance, recency, vitality) into `[0, 1]` and sums weighted normalized scores, instead of `1/(k+rank)` terms. Preserves match-confidence information rank-only fusion throws away; `fusion="rank"` stays the default, so no existing behavior changes unless explicitly enabled.
- `remind_me_search`'s `verbose=True` debug signals surface the new `keyword_score`/`semantic_score`/`recency_score`/`vitality_score`/`fusion_mode` fields when score fusion is active.
- `benchmarks/runner.py --rrf-fusion score` and `benchmarks/before_after.py --compare score_fusion` added for A/B measurement.

### 1.7.0 — 2026-07-21

Ships the single most-cited unused retrieval-quality lever flagged in the application capability review: cross-encoder reranking was built, tested, and off by default.

- **Reranking on by default.** `REMIND_ME_RERANK` now defaults to `onnx` instead of unset — rescoring only ever touches the bounded `REMIND_ME_RERANK_TOP_K` (default 20) head, so the added latency is small and constant regardless of result-pool size. Disable with `REMIND_ME_RERANK=""` for latency-sensitive deployments.
- **Stronger default model.** `REMIND_ME_RERANK_MODEL` swaps from the 2019 `cross-encoder/ms-marco-MiniLM-L6-v2` to `BAAI/bge-reranker-base` (2023) — still small enough for CPU, meaningfully stronger.
- **Failure caching (PF-01).** The reranker now caches load failures the same way the embedder already does — a missing dependency or failed HuggingFace download is retried only after a cooldown instead of on every single search, now that reranking runs by default for everyone.

### 1.6.0 — 2026-07-21

Closes a retrieval-quality gap flagged in the application capability review: modern embedding models (`nomic-embed-text`, `bge-*`, `e5-*`) expect different instruction prefixes on search queries vs. indexed passages, but remind_me embedded both identically.

- **Query/document embedding prefix asymmetry.** The embed path gains a `role: Literal["query", "passage"]` parameter, applying the correct per-model-family prefix (e.g. `nomic-embed-text`'s `search_query:`/`search_document:`) before encoding, via a lookup table keyed by model name. Models with no such convention (the ONNX default) are unaffected. Every call site — document indexing, plain query search, and fused query+HyDE-passage search — now passes the correct role.

### 1.5.0 — 2026-07-21

Closes a gap in the living-memory model flagged in the application capability review: supersession only ever happened via similarity-merge, so a contradictory update never replaced an old fact it didn't textually resemble.

- **Contradiction-based supersession.** A new/updated SPO triple sharing an existing fact's subject+predicate but a different object now automatically supersedes it (`remind_me_add`, `remind_me_decompose`, `remind_me_annotate`) — deterministic, no LLM call, using the SPO columns and `superseded_by` mechanism that already exist. Deliberately narrow: a differently-worded predicate (e.g. "visited" vs. "lives in") never contradicts.

### 1.4.0 — 2026-07-21

Closes a real multi-device correctness bug flagged in the application capability review: sync had no delete semantics, so a memory deleted on one device silently resurrected on the next pull elsewhere.

- **Delete/tombstone propagation.** Deleting a memory (when sync is configured) is now a soft delete — a `deleted_at` UPDATE that rides the existing outbox trigger and LWW conflict resolution, instead of a hard DELETE that produced no sync signal at all. Every normal read path excludes tombstones; a background compaction pass (`REMIND_ME_TOMBSTONE_RETENTION_DAYS`, default 180) hard-deletes them once safely old. Hub schema/upsert/pull-wire columns updated for parity. A node with sync disabled keeps the old plain-hard-delete behavior unchanged.

### 1.3.1 — 2026-07-21

Defense-in-depth fix, not a new capability. `_embed_and_store_rows` now batches internally by `EMBED_BATCH_SIZE` regardless of how many rows a caller passes in one call — a single source of truth, instead of every bulk caller pre-slicing its own input. This fixes the one caller that never batched (sync's pulled-record embedding) without touching `sync.py`, and let the file/mempalace/dbs importers drop their now-redundant external batching loops.

### 1.3.0 — 2026-07-21

Closes the "semantic search degrades O(n) as the store grows" gap flagged in the application capability review.

- **Optional ANN index for semantic search.** A new `ann_index.py` module adds an HNSW approximate-nearest-neighbor index (via `usearch`, new `ann` extra) that kicks in once a store passes `REMIND_ME_ANN_MIN_CHUNKS` chunk vectors (default 5000) — below that, or without `usearch` installed, or on any ANN failure, search stays on the existing exact brute-force scan. Self-healing (rebuilds from `memories_vec` if the on-disk index is missing/stale) and reported in `remind_me_server_status`. ~11x faster than brute force at 20k vectors in benchmarking, identical top result.

### 1.2.0 — 2026-07-21

Gives the LLM Wiki (FT-08) a user-facing surface — previously Claude could read and write it via MCP tools, but the human owner had no way to browse it at all.

- **Wiki REST API.** Five read-only routes under `/api/wiki/*` (list, read-by-slug, search, load, status), mirroring the existing `remind_me_wiki_*` MCP tool read paths. Read-only by design — writing remains an MCP-tool-only, LLM-curated action.
- **Wiki dashboard view.** A new "Wiki" tab with a searchable page catalogue, rendered page body (clickable `[[Wikilinks]]`), and a links/backlinks panel, plus a pending-compile badge.

### 1.1.0 — 2026-07-21

Eight-phase capability expansion, closing the gaps identified in a comparison against [cognee](docs/cognee-capability-review-2026-07-20.md) and [Cerebras's internal knowledge system](docs/cerebras-knowledge-capability-review-2026-07-20.md). All additions are backward-compatible and opt-in or default-preserving — no breaking changes to existing tools, storage, or sync wire formats.

- **Phase 1 — Search feedback loop + IDF ranking signal.** `remind_me_feedback` records a helpful/unhelpful signal into `base_weight`/vitality; a new opt-in IDF (`bm25`-derived) RRF signal, off by default.
- **Phase 2 — Neighbor-aware chunk retrieval.** Every import-produced chunk carries a `doc_id`/`chunk_index`; opt-in `include_neighbors` search expansion surfaces adjacent chunks from the same source document.
- **Phase 3 — Typed entity-to-entity relations.** A new `entity_relations` table and `remind_me_entity_traverse` tool for multi-hop graph queries (e.g. "who introduced me to the person who recommended this tool"), fully synced across hub and peers.
- **Phase 4 — Pluggable import connector framework.** `chat`/`document` (and third-party kinds) are parser functions registered by kind string, not a hardcoded dispatch; `remind_me_list_connectors` reports the registry.
- **Phase 5 — Push/webhook ingestion + ingest-time normalization.** A bearer-authenticated `POST /ingest` endpoint accepts content directly, sharing the import pipeline's connector dispatch and hash dedup; `remind_me_normalize_batch`/`remind_me_normalize_apply` distill noisy raw imports into clean `{question, summary, resolution?}` memories client-side.
- **Phase 6 — Auto-routing retrieval strategy.** `remind_me_search` gains a `strategy` parameter (`auto`/`balanced`/`keyword_favored`/`semantic_favored`); a deterministic query-shape heuristic (no LLM call) rebalances RRF weights as relative multipliers on top of whatever profile is already configured.
- **Phase 7 — Optional OpenTelemetry tracing + benchmark comparison docs.** `maybe_span()` instruments tool calls, sync cycles, and watcher scans, zero-cost unless explicitly enabled; `benchmarks/RESULTS.md` documents why cognee's BEAM figures aren't directly comparable to remind_me's LongMemEval-S numbers, plus a new weekly non-blocking CI smoke check.
- **Phase 8 — Storage-interface prep, alternative hub deploy targets, OpenAPI spec.** `storage_interfaces.py` documents the storage layer as `Protocol`s (no new backend); `hub/deploy/` gained Docker Compose, Fly.io, and Railway templates alongside the existing Podman quadlets; `docs/openapi.yaml` publishes the REST API for client-SDK generation in any language. Multimodal ingestion and multi-tenant isolation were evaluated and explicitly deferred — see "Design Scope" above.

Tool count: 35 → 41. Full detail in each phase's merged PR (#19–#26).

### 1.0.0

Initial tagged baseline: hybrid FTS5 + semantic search with RRF rank fusion, ACT-R vitality/decay, structured SPO triples and entity graph (FT-04), chat/document import (FT-02) with folder watching (FT-03), JSON/JSONL export (FT-01), LLM Wiki (FT-08), distributed sync (hub + peer-to-peer), dashboard UI + REST API, and remote MCP connector support (FT-05/FT-07).
