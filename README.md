# Remind Me MCP Server

Persistent, searchable memory that works across **Claude.ai**, **Claude Code**, and **Claude Desktop** — with multi-machine sync support and a built-in dashboard UI.

## Features

- **Full-text search** via SQLite FTS5 — fast, offline, no external services
- **Dashboard UI** — browse, search, add, edit, and delete memories from a web interface
- **Chat export import** — ingest JSON, JSONL, or Markdown exports from Claude, ChatGPT, or custom formats
- **Bulk directory import** — point at a folder of exports and import them all
- **Deduplication** — re-importing the same file is a safe no-op (tracked by file hash)
- **Tagging & categorization** — organize memories with categories and tags
- **Multi-machine sync** — database lives in `~/.remind-me/` — sync it with Syncthing, Dropbox, git, or any file sync tool
- **WAL mode** — SQLite Write-Ahead Logging ensures safe concurrent reads

## Quick Start

### 1. Install

```bash
# Clone or copy the server
git clone <your-repo-url> ~/remind-me-mcp
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
      "command": "uv",
      "args": ["run", "--directory", "/path/to/remind-me-mcp", "python", "remind_me_mcp.py"],
      "env": {
        "REMIND_ME_MCP_DIR": "~/.remind-me"
      }
    }
  }
}
```

Or if installed as a package:

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

### 3. Configure for Claude Desktop

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS, `%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "remind-me": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/remind-me-mcp", "python", "remind_me_mcp.py"],
      "env": {
        "REMIND_ME_MCP_DIR": "~/.remind-me"
      }
    }
  }
}
```

### 4. Configure for Claude.ai (via Claude in Chrome)

If using the Claude in Chrome extension with MCP support, add the same server configuration to your extension's MCP settings.

## Dashboard UI

The server includes a built-in web dashboard for browsing, searching, and managing your memories visually.

### Starting the Dashboard

```bash
# Option A: environment variable
REMIND_ME_MCP_SERVE_UI=true python remind_me_mcp.py

# Option B: command-line flag
python remind_me_mcp.py --serve-ui

# Option C: custom port and host
python remind_me_mcp.py --serve-ui --ui-port 8080 --ui-host 0.0.0.0
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

The entire memory database lives in a single directory (default: `~/.remind-me/`). To sync across machines:

### Option A: Syncthing (recommended — real-time, no cloud)

1. Install Syncthing on both machines
2. Share `~/.remind-me/` between them
3. SQLite WAL mode handles concurrent access safely

### Option B: Git

```bash
cd ~/.remind-me
git init
git add -A
git commit -m "sync"
git remote add origin <your-repo>
git push

# On other machine:
git clone <your-repo> ~/.remind-me
```

Add a cron job or alias for periodic sync.

### Option C: Dropbox / Google Drive / OneDrive

Symlink the memory directory into your cloud sync folder:

```bash
# Example with Dropbox
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

## Project Structure

```
remind-me-mcp/
├── remind_me_mcp.py         # MCP server — tools, import engine, SQLite storage
├── remind_me_dashboard.jsx  # React dashboard UI (Claude artifact or standalone)
├── pyproject.toml           # Package configuration and dependencies
└── README.md                # This file

~/.remind-me/                # Data directory (synced across machines)
└── memory.db                # SQLite database with FTS5 full-text search
```

## Architecture

The server uses:
- **SQLite FTS5** for fast full-text search
- **WAL journal mode** for safe concurrent access
- **Content-based hashing** for deduplication
- **stdio transport** for MCP compatibility with all Claude interfaces
- **Starlette + Uvicorn** for the optional HTTP dashboard and REST API
- **Self-contained HTML** — the dashboard is served as a single inline page with no build step