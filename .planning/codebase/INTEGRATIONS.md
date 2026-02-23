# External Integrations

**Analysis Date:** 2026-02-22

## APIs & External Services

**AI/ML Model Hosting:**
- HuggingFace Hub - Downloads the `all-MiniLM-L6-v2` ONNX embedding model (~80MB) on first use
  - SDK/Client: `huggingface-hub` >=0.20.0 (`hf_hub_download`)
  - Auth: None required (public model)
  - Files fetched: `onnx/model.onnx`, `tokenizer.json`
  - Cache: `~/.remind-me/models/` (local after first download)
  - Optional: Only used when `[semantic]` extras are installed

## Data Storage

**Databases:**
- SQLite - Primary and only data store
  - Location: `~/.remind-me/memory.db` (configurable via `REMIND_ME_MCP_DIR`)
  - Client: Python stdlib `sqlite3` module
  - Features: FTS5 full-text search, WAL journal mode, foreign keys enabled
  - Migrations: Schema created/upgraded inline via `_ensure_schema()` in `remind_me_mcp.py`

**Vector Storage:**
- `sqlite-vec` - SQLite extension for vector similarity search (optional)
  - Loaded as a SQLite extension at connection time
  - Stores 384-dimension float32 embeddings alongside FTS5 index
  - Graceful fallback: if not installed, FTS5 keyword search still works
  - Extension loaded via `sqlite_vec.load(db)` in `_get_db()` in `remind_me_mcp.py`

**File Storage:**
- Local filesystem only
  - Data directory: `~/.remind-me/` (default) or path in `REMIND_ME_MCP_DIR`
  - Import log: `~/.remind-me/import_log.json` (deduplication tracking by file hash)
  - PID file: `~/.remind-me/server.pid` (instance detection for dashboard)
  - Model cache: `~/.remind-me/models/` (ONNX model files from HuggingFace)

**Caching:**
- None (all reads are direct SQLite queries; no Redis or in-memory cache layer)

## Authentication & Identity

**Auth Provider:**
- None - No user authentication system
- The server is single-tenant (one user's personal memory store)
- Access controlled entirely by filesystem permissions on `~/.remind-me/`

## Monitoring & Observability

**Error Tracking:**
- None

**Analytics:**
- None

**Logs:**
- Python stdlib `logging` module - stderr only (stdout reserved for MCP stdio transport)
  - Format: `%(levelname)s | %(message)s`
  - Level: INFO by default
  - Destination: `sys.stderr` (never interferes with MCP stdio communication)

## CI/CD & Deployment

**Hosting:**
- No hosting platform - runs locally on the user's machine
- Two execution modes:
  - **stdio MCP mode**: invoked by Claude Code/Desktop/claude.ai as a subprocess
  - **HTTP dashboard mode**: `--serve-ui` flag, listens on `localhost:5199` by default

**CI Pipeline:**
- Not detected (no `.github/workflows/`, no CI config files)

## Environment Configuration

**Development:**
- Required env vars: None (all have defaults)
- Optional env vars:
  - `REMIND_ME_MCP_DIR` - Override data directory (default: `~/.remind-me`)
  - `REMIND_ME_MCP_SERVE_UI` - Enable dashboard mode (default: `false`)
  - `REMIND_ME_MCP_UI_PORT` - Dashboard port (default: `5199`)
  - `REMIND_ME_EMBEDDING_MODEL` - Override embedding model (default: `sentence-transformers/all-MiniLM-L6-v2`)
- Secrets location: No secrets required; no `.env` file present

**Production:**
- Secrets management: Not applicable (no external API keys required for core functionality)
- Multi-machine sync: User-managed via Syncthing, git, Dropbox, or symlinks on `~/.remind-me/`

## Webhooks & Callbacks

**Incoming:**
- None (the REST API at `localhost:5199` is a dashboard API, not a webhook endpoint)

**Outgoing:**
- None

## MCP Protocol Integration

**Claude Interfaces:**
- Claude Code - Configured via `~/.claude/claude_code_config.json` or project `.mcp.json`
- Claude Desktop - Configured via platform-specific `claude_desktop_config.json`
- Claude.ai - Configured via Chrome extension MCP settings
- Transport: stdio (stdout/stdin pipes between Claude host and Python process)
- Tools exposed: `remind_me_add`, `remind_me_search`, `remind_me_list`, `remind_me_get`, `remind_me_update`, `remind_me_delete`, `remind_me_import_chat`, `remind_me_import_directory`, `remind_me_stats`, `remind_me_auto_capture`, `remind_me_get_capture`, `remind_me_server_status`, `remind_me_reindex`

---

*Integration audit: 2026-02-22*
*Update when adding/removing external services*
