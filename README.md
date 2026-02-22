# Claude Memory MCP Server

Persistent, searchable memory that works across **Claude.ai**, **Claude Code**, and **Claude Desktop** — with multi-machine sync support and a built-in dashboard UI.

## Features

- **Full-text search** via SQLite FTS5 — fast, offline, no external services
- **Dashboard UI** — browse, search, add, edit, and delete memories from a web interface
- **Chat export import** — ingest JSON, JSONL, or Markdown exports from Claude, ChatGPT, or custom formats
- **Bulk directory import** — point at a folder of exports and import them all
- **Deduplication** — re-importing the same file is a safe no-op (tracked by file hash)
- **Tagging & categorization** — organize memories with categories and tags
- **Multi-machine sync** — database lives in `~/.claude-memory/` — sync it with Syncthing, Dropbox, git, or any file sync tool
- **WAL mode** — SQLite Write-Ahead Logging ensures safe concurrent reads

## Quick Start

### 1. Install

```bash
# Clone or copy the server
git clone <your-repo-url> ~/claude-memory-mcp
cd ~/claude-memory-mcp

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
    "memory": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/claude-memory-mcp", "python", "memory_mcp.py"],
      "env": {
        "MEMORY_MCP_DIR": "~/.claude-memory"
      }
    }
  }
}
```

Or if installed as a package:

```json
{
  "mcpServers": {
    "memory": {
      "command": "claude-memory-mcp",
      "env": {
        "MEMORY_MCP_DIR": "~/.claude-memory"
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
    "memory": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/claude-memory-mcp", "python", "memory_mcp.py"],
      "env": {
        "MEMORY_MCP_DIR": "~/.claude-memory"
      }
    }
  }
}
```

### 4. Configure for Claude.ai (via Claude in Chrome)

If using the Claude in Chrome extension with MCP support, add the same server configuration to your extension's MCP settings.

## Dashboard UI

The memory dashboard (`memory_dashboard.jsx`) is a React artifact that provides a full visual interface to your memory store.

### What It Does

- **Browse & search** — full-text search with `⌘K` shortcut, category sidebar with counts, clickable tag filters
- **View stats** — bar charts for categories, sources, and top tags; total counts and server info
- **Add memories** — modal form with content editor, color-coded category picker, and tag input
- **Edit & delete** — inline controls on every memory card with confirmation dialogs
- **Expand/collapse** — long memories truncate at 200 characters with a click to expand

### How to Use It

**Option A — Open directly in Claude.ai:**
Upload or paste `memory_dashboard.jsx` as an artifact in any Claude conversation. It renders immediately with sample data so you can explore the interface.

**Option B — Connect to your real database:**
The dashboard is designed to swap its data layer. Replace the `useMemoryStore` hook's mock data with calls to a REST API wrapper around the MCP server. To add a lightweight HTTP API:

1. Set `MEMORY_MCP_SERVE_UI=true` in your environment
2. Run the server — it will start an HTTP endpoint on `localhost:5199` alongside the stdio transport
3. Point the dashboard's fetch calls at `http://localhost:5199/api/`

> This HTTP bridge is optional. The core MCP server works independently over stdio.

**Option C — Embed in your own app:**
The component is a single self-contained React file with no external dependencies beyond React and Tailwind-compatible inline styles. Drop it into any React project.

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
| `memory_add` | Store a new memory with content, category, tags, and metadata |
| `memory_search` | Full-text search with FTS5 syntax (AND, OR, NOT, "phrases", prefix*) |
| `memory_list` | List memories with filters (category, tags, source) and pagination |
| `memory_get` | Retrieve a single memory by ID |
| `memory_update` | Update a memory's content, category, tags, or metadata |
| `memory_delete` | Permanently delete a memory |
| `memory_import_chat` | Import a single chat export file |
| `memory_import_directory` | Bulk import all exports from a directory |
| `memory_stats` | View statistics: counts, categories, recent activity |

## Importing Chat Exports

### Claude Export Format

Export your Claude conversations from claude.ai (Settings → Export Data), then:

```
Use memory_import_directory with:
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

The entire memory database lives in a single directory (default: `~/.claude-memory/`). To sync across machines:

### Option A: Syncthing (recommended — real-time, no cloud)

1. Install Syncthing on both machines
2. Share `~/.claude-memory/` between them
3. SQLite WAL mode handles concurrent access safely

### Option B: Git

```bash
cd ~/.claude-memory
git init
git add -A
git commit -m "sync"
git remote add origin <your-repo>
git push

# On other machine:
git clone <your-repo> ~/.claude-memory
```

Add a cron job or alias for periodic sync.

### Option C: Dropbox / Google Drive / OneDrive

Symlink the memory directory into your cloud sync folder:

```bash
# Example with Dropbox
mv ~/.claude-memory ~/Dropbox/claude-memory
ln -s ~/Dropbox/claude-memory ~/.claude-memory
```

### Custom Location

Set `MEMORY_MCP_DIR` to any path:

```bash
export MEMORY_MCP_DIR="/mnt/synced-drive/claude-memory"
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
| `MEMORY_MCP_DIR` | `~/.claude-memory` | Directory for the SQLite database |
| `MEMORY_MCP_SERVE_UI` | `false` | Start HTTP API for the dashboard UI |

## Project Structure

```
claude-memory-mcp/
├── memory_mcp.py           # MCP server — tools, import engine, SQLite storage
├── memory_dashboard.jsx    # React dashboard UI (Claude artifact or standalone)
├── pyproject.toml          # Package configuration and dependencies
└── README.md               # This file

~/.claude-memory/           # Data directory (synced across machines)
└── memory.db               # SQLite database with FTS5 full-text search
```

## Architecture

The server uses:
- **SQLite FTS5** for fast full-text search
- **WAL journal mode** for safe concurrent access
- **Content-based hashing** for deduplication
- **stdio transport** for compatibility with all Claude interfaces
- **React artifact** for the dashboard UI, renderable in Claude.ai or any React host