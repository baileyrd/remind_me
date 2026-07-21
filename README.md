# Remind Me MCP Server

[![CI](https://github.com/baileyrd/remind_me/actions/workflows/ci.yml/badge.svg)](https://github.com/baileyrd/remind_me/actions/workflows/ci.yml)

Persistent, searchable memory that works across **Claude.ai**, **Claude Code**, and **Claude Desktop** — with intelligent retrieval, multi-machine sync, and a built-in dashboard UI.

## Features

**Capture & import**
- **Chat export import** — ingest JSON, JSONL, or Markdown exports from Claude, ChatGPT, or custom formats
- **Document ingestion** — import Markdown notes and plain-text files, chunked per-section (heading context preserved) or per-paragraph; `kind=auto` detects chat vs document per file
- **Bulk directory import** — point at a folder of exports/notes and import them all
- **Watched folders** — set `REMIND_ME_WATCH_DIRS` and new or changed files auto-ingest in the background; changed files supersede their previous import
- **Push/webhook ingestion** — set `REMIND_ME_WEBHOOK_SECRET` and `POST /ingest` accepts content directly over the network, no filesystem staging required
- **Ingest-time normalization** — `remind_me_normalize_batch`/`remind_me_normalize_apply` distill noisy raw imports into clean `{question, summary, resolution?}` memories, non-destructively linked back to the source
- **Auto-capture** — store a full conversation dialog plus a distilled summary as two linked memories
- **Deduplication** — re-importing the same content is a safe no-op (tracked by content hash)

**Organize: entity knowledge graph**
- **Atomic decomposition** — Claude-driven extraction of atomic facts from conversations, linked to parent memories
- **Structured triples** — subject/predicate/object columns written by add/decompose/annotate for precise query routing
- **Entity graph** — entities with kinds and aliases, deterministic ids, mention links from memories; backfill via `remind_me_extract_batch` + `remind_me_annotate`, look up with `remind_me_entity`
- **Tagging & categorization** — organize memories with categories and tags
- **Memory classification** — 7 memory types with single and batch reclassification tools

**Synthesise: LLM Wiki**
- **LLM Wiki layer** (Karpathy pattern) — a *synthesis* layer over the raw memory store: Claude distils memories into a small set of interlinked markdown pages you can load directly into context instead of retrieving fragments
- **Files are the source of truth** — pages live as plain `.md` files (`REMIND_ME_WIKI_DIR`), with `[[wikilinks]]` + backlinks, an auto-generated `index.md`, an append-only `log.md`, and a seeded `SCHEMA.md` maintainer contract; the database is just a reconcile-from-files search index
- **Compile workflow** — `remind_me_wiki_compile` surfaces pending raw memories plus the current wiki state and schema, then advances a watermark once the batch is integrated

**Evolve & maintain**
- **ACT-R vitality model** — cognitive-science-inspired memory decay with per-category rates, access-based reinforcement, and bridge protection for high-value memories
- **Vault consolidation** — semantic clustering with Union-Find, canonical selection, and dry-run merge previews

**Search & retrieval**
- **Full-text search** via SQLite FTS5 — fast, offline, no external services
- **Hybrid semantic search** — FTS5 keyword matching + vector similarity via `sqlite-vec` and a local ONNX embedding model
- **RRF rank fusion** — Reciprocal Rank Fusion merges keyword, semantic, recency, vitality, and an opt-in IDF signal for best-match retrieval
- **Auto-routing retrieval strategy** — `strategy=auto` (default) heuristically rebalances keyword vs. semantic weight by query shape (short/quoted/wildcard queries favor keyword, long/question-shaped queries favor semantic); pin `balanced`/`keyword_favored`/`semantic_favored` explicitly to A/B test
- **Structured queries** — `subject:`, `predicate:`, and `entity:"..."` filters route straight to indexed lookups; opt-in 1-hop graph expansion surfaces related memories
- **Neighbor-aware chunk retrieval** — opt-in expansion surfaces a result's adjacent chunks from the same source document, so context split apart by chunking isn't lost
- **Token budget** — search results are trimmed to fit within an 800-token default cap (configurable), preventing context overflow
- **Search transparency** — debug signals, tier breakdown, and dormant exclusion counts in search results
- **Search feedback** — `remind_me_feedback` records a helpful/unhelpful signal on a memory, adjusting its `base_weight` (and therefore vitality and future ranking) up or down

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
`GET /health` is an unauthenticated liveness probe.

### What It Does

- **Browse & search** — full-text search with `⌘K` shortcut, category sidebar with counts, clickable tag filters
- **View stats** — bar charts for categories, sources, and top tags; database size and server info
- **Add memories** — modal form with content editor, color-coded category picker, and tag input
- **Edit & delete** — inline controls on every memory card with confirmation dialogs
- **Expand/collapse** — long memories truncate at 200 characters with a click to expand
- **Live data** — the dashboard reads and writes your real SQLite database; changes appear immediately

### REST API

The dashboard is powered by a REST API you can also use directly:

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Liveness probe (no auth) |
| `GET` | `/api/stats` | Memory statistics, categories, tags, DB info |
| `GET` | `/api/memories?category=&tags=&limit=&offset=` | List memories with filters |
| `GET` | `/api/memories/search?q=&category=&tags=` | Full-text search |
| `GET` | `/api/memories/{id}` | Get a single memory |
| `POST` | `/api/memories` | Add a memory (JSON body: `{content, category, tags, source, metadata}`) |
| `PUT`/`PATCH` | `/api/memories/{id}` | Update a memory |
| `DELETE` | `/api/memories/{id}` | Delete a memory |
| `POST` | `/api/import` | Import a chat/document file or directory (JSON body: `{file_path, kind, extract_mode, category, tags, max_length}`; paths must be inside `REMIND_ME_IMPORT_ROOTS`) |
| `GET` | `/api/export?format=&category=&tags=&file_path=&include_graph=` | Export memories (+ entity graph by default) as JSON/JSONL — streamed as the response body, or written server-side when `file_path` (inside `REMIND_ME_EXPORT_ROOTS`) is given |
| `GET` | `/api/entity?name=&limit=` | Look up a knowledge-graph entity by name or alias (404 if unknown) |

All `/api/*` routes require the bearer token described above (`GET /health` does not).

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
| `remind_me_search` | Hybrid search with RRF rank fusion, auto-routed or pinned ranking `strategy`, token budget, dormant exclusion, structured `subject:`/`predicate:`/`entity:` queries, opt-in `expand_entities` graph expansion, and opt-in `include_neighbors` sibling-chunk expansion |
| `remind_me_entity` | Look up a knowledge-graph entity by name or alias: canonical record, facts, and linked memories |
| `remind_me_entity_traverse` | Multi-hop traversal of the typed entity-relation graph (1-3 hops, both directions, optional relation filter) — for questions that require chaining relations, not just co-mention |
| `remind_me_feedback` | Mark a memory helpful/unhelpful for a search result — a signed signal into `base_weight`/vitality (and therefore future ranking), distinct from the always-positive reinforcement of a plain access |

### CRUD

| Tool | Description |
|------|-------------|
| `remind_me_add` | Store a new memory with content, category, tags, metadata, optional SPO triple, and entity mentions |
| `remind_me_list` | List memories with filters (category, tags, source) and pagination |
| `remind_me_get` | Retrieve a single memory by ID |
| `remind_me_update` | Update a memory's content, category, tags, or metadata |
| `remind_me_delete` | Permanently delete a memory |

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
| `remind_me_normalize_apply` | Write a distilled `{question, summary, resolution?, refs?}` as a new memory, non-destructively linked back to the raw import |

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
| `remind_me_consolidate` | Find semantically similar memories, preview clusters, and merge duplicates |

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
| `remind_me_import_chat` | Import a single chat export or document file (`kind`: auto/chat/document) |
| `remind_me_import_directory` | Bulk import all exports/documents from a directory |
| `remind_me_import_mempalace` | Bulk-import memories from a MemPalace ChromaDB store, one page at a time (requires the optional `mempalace` extra) |
| `remind_me_list_connectors` | List every registered import connector (built-in and third-party) and which are valid `remind_me_import_chat` kinds |
| `remind_me_export_memories` | Export memories (+ entity graph by default) to JSON/JSONL, inline or to a file inside the export roots |
| `remind_me_stats` | View statistics: counts, categories, recent activity |
| `remind_me_reindex` | Build vector embeddings for any memories missing them |
| `remind_me_server_status` | Check dashboard, embedding, folder-watcher, and remote-connector state and verify DB connectivity |
| `remind_me_watch_status` | Folder watcher status: watched dirs, scan counters, recent errors |
| `remind_me_webhook_status` | Push/webhook ingestion status: bind/port, request counters, recent errors |
| `remind_me_revoke_clients` | List OAuth connector clients, or revoke one (with all of its tokens) |
| `remind_me_check_update` | Check if a newer version is available on origin/main |
| `remind_me_self_update` | Pull latest changes from origin and reinstall the package |

41 tools + 4 resources (`memory://stats`, `memory://categories`, `wiki://schema`, `wiki://index`).

### Auto-Capture: Persisting Full Conversations

The `remind_me_auto_capture` tool stores **two linked memories** from each conversation:

1. **Dialog** (category: `dialog`) — the full verbatim conversation, every turn preserved
2. **Summary** (category: `conversation`) — a concise distillation of key topics, decisions, facts, and preferences

Both memories share a `capture_id` in their metadata, so you can retrieve them together with `remind_me_get_capture`.

**To use automatically**, add this to your Claude Desktop or Claude.ai custom instructions:

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

### Reindexing Existing Memories

If you enable semantic search after already having memories stored, run reindex to backfill embeddings:

```
Use remind_me_reindex
```

Or ask Claude: "Reindex my memories for semantic search."

This only generates embeddings for memories that don't have them yet — existing embeddings are preserved.

### Checking Status

Use `remind_me_server_status` to see how many memories have embeddings and whether the model is loaded.

## Importing Chats & Documents

The import tools (`remind_me_import_chat`, `remind_me_import_directory`, `POST /api/import`) share one pipeline: hash-based deduplication (re-importing the same file content is a no-op), batched embedding, and a `kind` parameter that controls parsing.

### Import Kinds

| Kind | Behavior |
|------|----------|
| `auto` *(default)* | `.json`/`.jsonl` always parse as chat. `.md`/`.markdown`/`.txt` are content-sniffed: files with chat role markers (`**User:**`, `## Assistant`, …) import as chat, everything else as a document |
| `chat` | Force the chat-export parser (chunked per-message) |
| `document` | Force document chunking (`.md`/`.markdown`/`.txt` only) |

Document imports chunk Markdown per-section (the heading context is kept with each chunk and stored in metadata) and plain text per-paragraph. They get `source: document_import` and default to category `document`.

Imports are restricted to paths inside `REMIND_ME_IMPORT_ROOTS` (default: your home directory) — enforced by both the MCP tools and the HTTP API.

### Pluggable Connectors

The `chat` and `document` kinds are plain parser functions registered by kind string in `remind_me_mcp/importer.py` (`register_connector(kind, connector)`), not a hardcoded dispatch — `remind_me_import_chat`/`remind_me_import_directory` resolve the effective kind exactly as before, then look it up in the registry. A third-party module can register more connectors without touching `importer.py`; `remind_me_mcp/mempalace_import.py` does this for its own `_parse_frontmatter` step, registered under `"mempalace"` purely for discovery (its real ingestion path, `remind_me_import_mempalace`, keeps its own bespoke per-drawer dedup/paging loop — MemPalace drawers arrive individually from a paginated ChromaDB read, not as one raw file). Call `remind_me_list_connectors` to see every registered connector and which are valid `remind_me_import_chat` kinds.

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

Set `REMIND_ME_WATCH_DIRS` (colon-separated, each directory must lie inside `REMIND_ME_IMPORT_ROOTS`) and the server polls those folders in the background, auto-ingesting new or changed `.md`, `.markdown`, `.txt`, `.json`, and `.jsonl` files through the same import pipeline (`kind=auto`, hash dedup applies):

```bash
REMIND_ME_WATCH_DIRS=~/notes:~/Downloads/exports remind-me-mcp
```

- **Polling, not inotify** — directories are scanned every `REMIND_ME_WATCH_INTERVAL` seconds (default 60); no extra dependencies.
- **Debounce** — a file whose mtime is younger than `REMIND_ME_WATCH_GRACE` seconds (default 5) is deferred until a later scan observes the same (mtime, size) signature, so partially-written files are never ingested mid-write.
- **Changed files supersede** — a changed file has a new hash, so it imports fresh; the watcher then marks every memory from the file's previous import as superseded (`superseded_by` = the new import id). Stale chunks drop out of search results (which filter `superseded_by IS NULL`) but remain in the database for audit.
- **Status** — the `remind_me_watch_status` tool reports watched dirs, scan counters, ingest/skip/supersede counts, and recent errors; `remind_me_server_status` includes a watcher summary too.
- **Wiki is downstream, not automatic** — the watcher feeds the **memory store**, not the wiki. Synthesis into wiki pages is a separate LLM-driven step (`remind_me_wiki_compile`). So both status tools also report `pending_wiki_compile` — the count of non-superseded memories created since the last compile watermark — as a nudge that newly ingested files are waiting to be folded into the wiki.

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

## Ingest-Time Normalization

Raw imports (chat/document, from a file import, the watcher, or a webhook push) are often verbatim and noisy. `remind_me_normalize_batch` surfaces un-normalized `document_import`/`chat_import` chunks for the calling agent to distill into `{question, summary, resolution?, refs?}` — the LLM work happens client-side, exactly like `remind_me_decompose` already does for atomic-fact extraction, so the server itself has no LLM dependency. `remind_me_normalize_apply` then writes each distillation as a new memory (category `normalized`), non-destructively linked back to the raw row via a `normalized_from` metadata pointer — the raw memory is kept, not replaced, and `remind_me_normalize_batch` skips it on the next call.

## Observability (OpenTelemetry)

Off by default and zero-cost when unset. Enable tracing to see where time goes across three boundaries — every MCP tool call, each sync cycle, and each folder-watcher scan pass — exported to whatever OTLP collector you already run (Jaeger, Tempo, Honeycomb, ...). remind_me never bundles or manages a collector itself, which would conflict with the zero-ops, local-first design.

```bash
uv pip install "remind-me-mcp[otel]"
REMIND_ME_OTEL_ENABLED=1 REMIND_ME_OTEL_ENDPOINT=http://localhost:4318/v1/traces remind-me-mcp
```

- **Graceful degradation** — if `REMIND_ME_OTEL_ENABLED=1` is set but the `otel` extra isn't installed (or setup fails for any other reason), tracing silently no-ops after a one-time warning in the log — it can never break the server it's observing.
- **Status** — `remind_me_server_status` reports whether tracing is enabled and actually active.

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

- **`remind_me_entity_traverse`** walks this edge graph breadth-first from a starting entity, up to `hops` (1-3) steps in both directions, with an optional exact-match `relation` filter and a result cap. This answers questions that require chaining relations rather than co-mention — e.g. "who introduced me to the person who recommended this tool" (`Alice --introduced--> Bob`, `Bob --recommended--> tool`).
- Relation edges have a **deterministic id** (hash of the subject/relation/object triple, same convergence property as entity ids) and are **immutable** — insert-or-ignore, like memory↔entity links, so re-recording the same triple is a no-op.

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

```bash
# Point the wiki somewhere git-friendly (optional; defaults to ~/.remind-me/wiki)
export REMIND_ME_WIKI_DIR=~/notes/wiki
# Cap the default whole-wiki load (estimated tokens; 0 = unlimited)
export REMIND_ME_WIKI_LOAD_TOKEN_BUDGET=12000
```

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
3. Incoming records are upserted with last-write-wins on `updated_at`
4. Records pulled from the hub are marked as already-sent in the outbox to prevent echo
5. Optionally, peers discover each other via Tailscale and sync directly
6. The **entity graph syncs too**: entity and link records carry a `record_type` discriminator on the wire; entities upsert with alias union-merge, links are insert-or-ignore (see [Entity Knowledge Graph](#entity-knowledge-graph))

#### Peer Server Endpoints

Each node runs a small HTTP server (default port 8766, bind via `REMIND_ME_PEER_BIND`) for direct peer sync. Every request requires `Authorization: Bearer <REMIND_ME_SYNC_SECRET>`:

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Node liveness + node_id |
| `GET` | `/sync/pull?since=&since_id=&exclude_node=&limit=` | Pull memory records (keyset cursor on `(updated_at, id)` when `since_id` is sent) |
| `POST` | `/sync/push` | Push records (responds with `processed_ids`) |
| `GET` | `/sync/pull_entities?since=&since_id=&limit=` | Pull entity records (404 on pre-entity-graph peers is treated as "no entity support") |
| `GET` | `/sync/pull_links?since=&since_id=&limit=` | Pull memory-entity link records |

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

## Remote Access via HTTP Transport

The `--serve-mcp` flag runs the MCP server over Streamable HTTP transport, making it accessible remotely without spawning a subprocess.

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

### Auto-Routing Retrieval Strategy

`remind_me_search`'s `strategy` parameter picks the RRF weight profile used to fuse keyword and semantic ranking:

| Value | Behavior |
|-------|----------|
| `auto` (default) | Deterministic heuristic on query shape: quoted phrases, `prefix*` wildcards, or queries of 2 words or fewer favor keyword relevance; long (6+ word) or question-shaped (ending in `?`) natural-language queries favor semantic similarity; everything else is balanced |
| `balanced` | Pins the tuned RRF defaults (equivalent to not overriding anything) |
| `keyword_favored` | Always favors keyword relevance, regardless of query shape |
| `semantic_favored` | Always favors semantic similarity, regardless of query shape |

This is a deterministic heuristic, not an LLM planner call — no extra latency or opacity on the search hot path, consistent with keeping server-side synthesis out of scope. `keyword_favored`/`semantic_favored` are relative *multipliers* on top of whatever RRF weights are already configured (`REMIND_ME_RRF_W_*` env vars), not fixed replacements — a signal you've deliberately zeroed stays zeroed. `strategy` only affects the hybrid ranking path; structured `subject:`/`predicate:`/`entity:` lookups bypass RRF entirely. Pass `verbose=true` to see the resolved `strategy` and `weights_used` in each result's `debug_signals`.

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
| `REMIND_ME_MCP_HTTP_SECRET` | *(unset)* | Bearer token for MCP HTTP transport authentication |
| `REMIND_ME_REMOTE_MCP` | `false` | Run the remote MCP connector (Streamable HTTP behind a secret URL path) for claude.ai custom connectors |
| `REMIND_ME_REMOTE_PORT` | `8768` | Port for the remote MCP connector |
| `REMIND_ME_REMOTE_HOST` | `127.0.0.1` | Host to bind the remote MCP connector (keep localhost; let the tunnel do the exposing) |
| `REMIND_ME_REMOTE_TOKEN` | *(auto-generated)* | Connector token (doubles as the secret URL path and the OAuth owner credential). When unset, generated on first run and stored at `~/.remind-me/connector_token` (0600). Delete the file to rotate |
| `REMIND_ME_REMOTE_ISSUER` | *(unset)* | Public HTTPS origin of the remote connector (e.g. the tunnel hostname). Setting it activates the single-user OAuth 2.1 authorization server; unset falls back to the secret-path mode with a warning |
| `REMIND_ME_EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | HuggingFace model for semantic embeddings (ONNX backend) |
| `REMIND_ME_EMBEDDING_BACKEND` | `onnx` | Embedding backend: `onnx` (in-process) or `ollama` (local daemon) |
| `REMIND_ME_EMBEDDING_DIM` | `384` | Embedding dimension — must match the model (nomic-embed-text=768, bge-m3=1024). Changing it requires recreating the vector table + `remind_me_reindex` |
| `REMIND_ME_OLLAMA_URL` | `http://localhost:11434` | Ollama daemon URL (when backend is `ollama`) |
| `REMIND_ME_OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Ollama embedding model name |
| `REMIND_ME_EMBED_CHUNK_CHARS` | `1600` | Character window size for sliding-window embedding of long content |
| `REMIND_ME_EMBED_CHUNK_OVERLAP` | `200` | Overlap between embedding windows |
| `REMIND_ME_EMBED_MAX_CHUNKS` | `16` | Max embedding chunks per memory |
| `REMIND_ME_EMBED_BATCH_SIZE` | `32` | Memories embedded per batch during reindex and import |
| `REMIND_ME_EMBED_FORWARD_BATCH` | `32` | Chunks per ONNX forward pass inside the embedder — the hard ceiling on embedding memory per call |
| `REMIND_ME_MEMPALACE_PATH` | `~/.mempalace/palace` | Path to a MemPalace ChromaDB persistent store, read (read-only) by `remind_me_import_mempalace` |
| `REMIND_ME_API_KEY` | *(auto-generated)* | Bearer token for `/api/*` routes. When unset, a key is generated on first run and stored at `~/.remind-me/api_key` (0600) — check the server log or that file for the value. Set to `disabled` to explicitly turn dashboard auth off |
| `REMIND_ME_IMPORT_ROOTS` | `$HOME` | Colon-separated allowed filesystem roots for import operations (enforced by both the HTTP API and the MCP import tools) |
| `REMIND_ME_EXPORT_ROOTS` | `$HOME` | Colon-separated allowed filesystem roots for export destinations (enforced by both the HTTP API and the MCP export tool) |
| `REMIND_ME_WATCH_DIRS` | *(unset)* | Colon-separated directories for the folder watcher to auto-ingest. Empty = watcher disabled. Each directory must lie inside `REMIND_ME_IMPORT_ROOTS` |
| `REMIND_ME_WATCH_INTERVAL` | `60` | Seconds between folder watcher scan passes |
| `REMIND_ME_WATCH_GRACE` | `5` | Debounce grace period in seconds — files modified more recently than this are deferred until a scan sees a stable (mtime, size) |
| `REMIND_ME_WEBHOOK_SECRET` | *(unset)* | Bearer token for the push/webhook ingestion server. Empty = disabled — the server refuses to start without it |
| `REMIND_ME_WEBHOOK_PORT` | `8769` | Port for the push/webhook ingestion server |
| `REMIND_ME_WEBHOOK_BIND` | `127.0.0.1` | Bind address for the push/webhook ingestion server. Widen deliberately (e.g. a Tailscale IP) since it writes arbitrary pushed content directly into memory |
| `REMIND_ME_OTEL_ENABLED` | `false` | Enable OpenTelemetry tracing of tool calls, sync cycles, and watcher scans. Requires the `otel` extra (`pip install remind-me-mcp[otel]`); degrades gracefully to a no-op if missing |
| `REMIND_ME_OTEL_ENDPOINT` | *(unset)* | OTLP/HTTP collector endpoint (e.g. `http://localhost:4318/v1/traces`). Unset uses the OTLP exporter's own default |
| `REMIND_ME_OTEL_SERVICE_NAME` | `remind-me-mcp` | `service.name` resource attribute reported to the collector |
| `REMIND_ME_AUTO_UPDATE_CHECK` | `true` | Set to `false` to skip the background `git fetch` update check at server startup (the manual check/update tools keep working) |
| `REMIND_ME_RRF_K` | `60` | Smoothing constant for Reciprocal Rank Fusion scoring |
| `REMIND_ME_RRF_W_KEYWORD` | `1.0` | RRF weight for the keyword (FTS5) signal |
| `REMIND_ME_RRF_W_SEMANTIC` | `1.0` | RRF weight for the semantic (vector) signal |
| `REMIND_ME_RRF_W_RECENCY` | `1.0` | RRF weight for the recency signal (set `0` for a pure-retrieval profile) |
| `REMIND_ME_RRF_W_VITALITY` | `1.0` | RRF weight for the vitality signal (set `0` for a pure-retrieval profile) |
| `REMIND_ME_RRF_W_IDF` | `0.0` | RRF weight for the IDF signal (derived from FTS5's `bm25()` score). Off by default — set a positive value to opt in |
| `REMIND_ME_RERANK` | *(unset)* | Set to `onnx` to rerank the top search candidates with a cross-encoder |
| `REMIND_ME_RERANK_MODEL` | `cross-encoder/ms-marco-MiniLM-L6-v2` | HuggingFace cross-encoder repo (must ship `onnx/model.onnx`) |
| `REMIND_ME_RERANK_TOP_K` | `20` | How many top RRF candidates the reranker rescores |
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
- **RRF rank fusion** (k=60) — merges keyword, semantic, recency, vitality, and an opt-in IDF signal without score normalization
- **Auto-routing retrieval strategy** — a deterministic query-shape heuristic (no LLM call) picks a keyword-favored, semantic-favored, or balanced RRF weight profile; presets are relative multipliers on the live weights, so a deliberately-zeroed signal (e.g. a benchmark's `--rrf-profile`) is never resurrected
- **Token budget** — search results are trimmed to an 800-token default cap to prevent LLM context overflow
- **ACT-R vitality model** — cognitive-science decay with per-category rates, access reinforcement, signed helpful/unhelpful feedback, and bridge protection
- **Structured triples** — subject/predicate/object columns with indexed query routing
- **Entity knowledge graph** — `entities` and `memory_entities` tables with deterministic name-derived ids, alias union-merge, and 1-hop search expansion
- **Union-Find clustering** — transitive semantic similarity grouping for vault consolidation
- **Section-aware document chunking** — Markdown imports split per heading section, plain text per paragraph
- **Pluggable connectors** — `chat`/`document` (and third-party kinds like `mempalace`) are parser functions registered by kind string, not a hardcoded dispatch — `remind_me_list_connectors` reports the registry
- **Neighbor-aware chunk retrieval** — every import-produced chunk carries a `doc_id`/`chunk_index`; opt-in search expansion surfaces adjacent chunks from the same source document
- **Polling folder watcher** — mtime/size scans with a debounce grace window and changed-file supersession (no inotify dependency)
- **Push/webhook ingestion** — a bearer-authenticated `POST /ingest` endpoint accepts content directly (no filesystem staging), sharing the same connector pipeline and hash dedup as file import
- **Client-side ingest normalization** — `remind_me_normalize_batch`/`remind_me_normalize_apply` distill noisy raw imports into `{question, summary, resolution?}` memories; the LLM work happens in the calling agent, not the server, same as `remind_me_decompose`
- **Outbox-based sync** — local writes (memories, entities, links) are captured in `sync_outbox`, pushed to hub/peers in background
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
- **Multimodal ingestion (images, audio)** — deferred entirely. Ingestion, chunking, embedding, FTS5, and the wiki are all text-native end to end; images would need a second embedding pipeline, binary storage, and a UI story, none of which serve the text-first "personal memory for Claude clients" center. Revisit only if a concrete use case emerges.
- **Multi-tenant / cross-agent isolation** — deferred. remind_me is explicitly single-owner by design: one OAuth owner token, one SQLite file per node. Multi-tenancy is an architecture change orthogonal to "personal memory," not a gap in the current design — worth revisiting only if the project's scope deliberately shifts toward shared/team memory infrastructure.
- **Client SDKs beyond MCP** — no hand-written TS/Rust/etc. SDKs (maintenance surface disproportionate to a single-user local tool whose real client is Claude via MCP). Instead, the existing `GET /api/*` REST surface is published as an [OpenAPI 3.0 spec](docs/openapi.yaml) so any language can generate a thin client for free.
- **Cloud/managed & serverless hosting** — no managed hosting product. The per-user SQLite node is designed to stay local; the one component that's natural to host centrally (the sync hub) already had a Podman quadlet deploy path, and now also has [Docker Compose, Fly.io, and Railway templates](hub/deploy/) — deliberately still self-hosted, not a one-click managed service.

## Changelog

See [`RELEASE_NOTES.md`](RELEASE_NOTES.md) for a per-version feature breakdown with PR references; this section summarizes the same history phase-by-phase.

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
