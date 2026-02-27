# Remind Me MCP Server

[![CI](https://github.com/baileyrd/remind_me/actions/workflows/ci.yml/badge.svg)](https://github.com/baileyrd/remind_me/actions/workflows/ci.yml)

Persistent, searchable memory that works across **Claude.ai**, **Claude Code**, and **Claude Desktop** — with multi-machine sync support and a built-in dashboard UI.

## Features

- **Full-text search** via SQLite FTS5 — fast, offline, no external services
- **Hybrid semantic search** — FTS5 keyword matching + vector similarity via `sqlite-vec` and a local ONNX embedding model
- **Distributed sync** — offline-first with outbox pattern, Postgres hub, and peer-to-peer sync over Tailscale
- **Dashboard UI** — browse, search, add, edit, and delete memories from a web interface
- **Chat export import** — ingest JSON, JSONL, or Markdown exports from Claude, ChatGPT, or custom formats
- **Bulk directory import** — point at a folder of exports and import them all
- **Deduplication** — re-importing the same file is a safe no-op (tracked by file hash)
- **Tagging & categorization** — organize memories with categories and tags
- **WAL mode** — SQLite Write-Ahead Logging ensures safe concurrent reads

## Quick Start

### 1. Install

```bash
# Clone the repository
git clone https://github.com/baileyrd/remind_me.git ~/remind-me-mcp
cd ~/remind-me-mcp

# Install with uv (recommended)
uv pip install -e .

# Or with pip
pip install -e .
```

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
| `GET` | `/api/stats` | Memory statistics, categories, tags, DB info |
| `GET` | `/api/memories?category=&tags=&limit=&offset=` | List memories with filters |
| `GET` | `/api/memories/search?q=&category=&tags=` | Full-text search |
| `GET` | `/api/memories/{id}` | Get a single memory |
| `POST` | `/api/memories` | Add a memory (JSON body: `{content, category, tags}`) |
| `PUT` | `/api/memories/{id}` | Update a memory |
| `DELETE` | `/api/memories/{id}` | Delete a memory |
| `POST` | `/api/import` | Import a chat file (JSON body: `{file_path, extract_mode, tags}`) |

### Standalone Artifact

The project also includes `remind_me_dashboard.jsx` — a standalone React artifact with mock data that can be uploaded directly into Claude.ai for previewing the UI without running the server.

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

| Tool | Description |
|------|-------------|
| `remind_me_add` | Store a new memory with content, category, tags, and metadata |
| `remind_me_search` | Full-text search with FTS5 syntax (AND, OR, NOT, "phrases", prefix*) |
| `remind_me_list` | List memories with filters (category, tags, source) and pagination |
| `remind_me_get` | Retrieve a single memory by ID |
| `remind_me_update` | Update a memory's content, category, tags, or metadata |
| `remind_me_delete` | Permanently delete a memory |
| `remind_me_import_chat` | Import a single chat export file |
| `remind_me_import_directory` | Bulk import all exports from a directory |
| `remind_me_stats` | View statistics: counts, categories, recent activity |
| `remind_me_auto_capture` | Capture a full conversation dialog + distilled summary as two linked memories |
| `remind_me_get_capture` | Retrieve a linked dialog/summary pair by their shared capture_id |
| `remind_me_server_status` | Check if the dashboard UI is running, get its URL, and verify DB connectivity |
| `remind_me_reindex` | Build vector embeddings for any memories missing them (run after enabling semantic search) |
| `remind_me_check_update` | Check if a newer version is available on origin/main |
| `remind_me_self_update` | Pull latest changes from origin and reinstall the package |

16 tools + 2 resources (`stats` and `categories`).

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

## Importing Chat Exports

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
- **Markdown**: Headings or bold markers for roles (`## Human`, `**Assistant:**`, etc.)

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

The sync hub is a FastAPI server backed by Postgres. Deploy it with Podman or Docker:

1. Run a Postgres instance (e.g., via Podman Quadlet or Docker Compose)
2. Deploy the sync hub container pointing at the Postgres instance
3. Ensure the hub is reachable from all machines (e.g., via Tailscale)

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

#### How It Works

1. Every local write (add, update, delete) is recorded in a `sync_outbox` table
2. The background sync thread pushes outbox entries to the hub and pulls new records
3. Incoming records are upserted with last-write-wins on `updated_at`
4. Records pulled from the hub are marked as already-sent in the outbox to prevent echo
5. Optionally, peers discover each other via Tailscale and sync directly

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

## Search Syntax

The search tool uses SQLite FTS5. Examples:

| Query | Matches |
|-------|---------|
| `python async` | Memories containing both "python" AND "async" |
| `python OR rust` | Memories containing either word |
| `python NOT django` | Python memories excluding Django |
| `"exact phrase"` | Memories with the exact phrase |
| `deploy*` | Prefix matching: deploy, deployment, deployed… |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `REMIND_ME_MCP_DIR` | `~/.remind-me` | Directory for the SQLite database |
| `REMIND_ME_MCP_SERVE_UI` | `false` | Start the HTTP dashboard server instead of stdio MCP |
| `REMIND_ME_MCP_UI_PORT` | `5199` | Port for the dashboard server |
| `REMIND_ME_EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | HuggingFace model for semantic embeddings |
| `REMIND_ME_API_KEY` | *(unset)* | Bearer token for `/api/*` routes (auth disabled when unset) |
| `REMIND_ME_IMPORT_ROOTS` | `$HOME` | Colon-separated allowed filesystem roots for import operations |
| `REMIND_ME_NODE_ID` | *(unset)* | Unique identifier for this machine (enables sync when set with HUB_URL and SYNC_SECRET) |
| `REMIND_ME_HUB_URL` | *(unset)* | URL of the sync hub (e.g., `http://100.x.x.x:8765`) |
| `REMIND_ME_SYNC_SECRET` | *(unset)* | Shared bearer token for hub and peer authentication |
| `REMIND_ME_SYNC_INTERVAL` | `60` | Seconds between sync cycles |
| `REMIND_ME_PEER_PORT` | `8766` | Local port for the peer-to-peer sync server |
| `REMIND_ME_STATIC_PEERS` | `[]` | JSON array of static peer configs (for environments without Tailscale) |
| `REMIND_ME_TAILSCALE_SOCKET` | *(unset)* | Path to Tailscale socket for peer discovery (auto-detected if empty) |

## Project Structure

```
remind-me-mcp/
├── remind_me_mcp/              # Main package
│   ├── __init__.py             # Package exports, version
│   ├── __main__.py             # CLI entry point, mode dispatch
│   ├── server.py               # FastMCP instance, app lifespan
│   ├── tools.py                # 16 MCP tools + 2 resources
│   ├── models.py               # Pydantic input models
│   ├── config.py               # Environment configuration, constants
│   ├── db.py                   # SQLite schema, migrations, helpers
│   ├── api.py                  # Starlette HTTP API + dashboard HTML
│   ├── importer.py             # Chat export parser & import engine
│   ├── embeddings.py           # ONNX embedding engine
│   ├── formatting.py           # Memory markdown/JSON formatters
│   ├── pid.py                  # PID file management, instance detection
│   ├── updater.py              # Version checking, self-update logic
│   ├── sync.py                 # Background sync engine (hub + peer push/pull)
│   ├── peer_server.py          # Lightweight HTTP server for peer-to-peer sync
│   └── dashboard/
│       └── App.jsx             # React dashboard component
├── tests/                      # Test suite (pytest + pytest-asyncio)
├── remind_me_dashboard.jsx     # Standalone React artifact for Claude.ai preview
├── pyproject.toml              # Package configuration and dependencies
└── README.md                   # This file

~/.remind-me/                   # Data directory (synced across machines)
├── memory.db                   # SQLite database with FTS5 + sqlite-vec
├── models/                     # Cached ONNX embedding model (~80MB, auto-downloaded)
└── server.pid                  # PID file when dashboard is running
```

## CLI Reference

```bash
remind-me-mcp                        # MCP stdio mode (default)
remind-me-mcp --serve-ui             # Start dashboard UI server
remind-me-mcp --serve-ui --ui-port 8080 --ui-host 0.0.0.0
remind-me-mcp --status               # Check if dashboard is running
remind-me-mcp --version              # Print installed version
remind-me-mcp --check-update         # Check for available updates
remind-me-mcp --update               # Pull latest and reinstall
```

You can also run via `python -m remind_me_mcp` with the same flags.

## Architecture

The server uses:
- **SQLite FTS5** for keyword full-text search (inverted index, boolean queries)
- **sqlite-vec** for semantic vector search (cosine similarity on embeddings)
- **all-MiniLM-L6-v2** via ONNX Runtime for local embedding generation (~80MB model, no API keys)
- **Hybrid ranking** merges keyword and semantic results with deduplication and score fusion
- **Outbox-based sync** — local writes are captured in `sync_outbox`, pushed to hub/peers in background
- **Postgres hub** — central sync point with last-write-wins conflict resolution
- **Peer-to-peer sync** — direct machine-to-machine sync via Tailscale peer discovery
- **WAL journal mode** for safe concurrent access
- **Content-based hashing** for deduplication
- **stdio transport** for MCP compatibility with all Claude interfaces
- **Starlette + Uvicorn** for the optional HTTP dashboard and REST API
- **Self-contained HTML** — the dashboard is served as a single inline page with no build step
- **Graceful degradation** — semantic search and distributed sync are both optional; core functionality works with just FTS5 and local storage