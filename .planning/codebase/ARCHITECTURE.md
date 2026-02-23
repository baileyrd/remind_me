# Architecture

**Analysis Date:** 2026-02-22

## Pattern Overview

**Overall:** Single-file MCP Server with optional HTTP Dashboard

**Key Characteristics:**
- Single Python module (`remind_me_mcp.py`) serving dual transport modes: MCP stdio and HTTP REST
- File-based persistence — all state in `~/.remind-me/memory.db` (SQLite)
- Async-first tool handlers via `FastMCP`, synchronous DB helpers
- Self-contained: no external services required; semantic search is an optional dependency tier

## Layers

**MCP Tool Layer:**
- Purpose: Expose memory operations to Claude as callable tools via the MCP protocol
- Contains: Async tool handler functions decorated with `@mcp.tool()`
- Location: `remind_me_mcp.py` lines ~827–1581 (tool definitions)
- Depends on: Business logic helpers, Pydantic input models, DB helpers
- Used by: Claude clients (Claude.ai, Claude Code, Claude Desktop) over stdio

**HTTP API Layer (optional):**
- Purpose: REST API for the browser dashboard UI
- Contains: Starlette route handlers (`api_stats`, `api_list`, `api_search`, `api_get`, `api_add`, `api_update`, `api_delete`, `api_import`), Starlette app builder `_build_api_app()`
- Location: `remind_me_mcp.py` lines ~1603–1871
- Depends on: DB helpers, `import_chat_file()` business logic
- Used by: Browser dashboard served at `/`

**Dashboard UI Layer:**
- Purpose: Single-page React web app for browsing, searching, editing memories
- Contains: Inlined React 18 JavaScript (transpiled via Babel standalone), a `useMemoryStore` hook, component tree
- Location: `remind_me_mcp.py` lines ~1874–2433 (`_build_dashboard_html()`, `_get_dashboard_script()`)
- Depends on: HTTP API layer at `window.location.origin + "/api"`
- Used by: Browser connecting to the UI server

**Business Logic Layer:**
- Purpose: Core domain operations for memory management and chat import
- Contains: `import_chat_file()`, `_chunk_text()`, `_extract_messages_from_json()`, `_filter_messages()`, `_parse_markdown_chat()`
- Location: `remind_me_mcp.py` lines ~580–806
- Depends on: DB helpers, embedding helpers
- Used by: MCP tool layer, HTTP API layer

**Input Validation Layer:**
- Purpose: Typed, validated inputs for all MCP tools
- Contains: Pydantic `BaseModel` subclasses: `MemoryAddInput`, `MemorySearchInput`, `MemoryListInput`, `MemoryUpdateInput`, `MemoryDeleteInput`, `ChatImportInput`, `MemoryStatsInput`, `BulkImportDirInput`, `AutoCaptureInput`
- Location: `remind_me_mcp.py` lines ~383–542
- Depends on: Pydantic v2
- Used by: MCP tool handlers (FastMCP passes validated models automatically)

**Data / Persistence Layer:**
- Purpose: SQLite access, schema management, FTS5 search, vector embeddings
- Contains: `_get_db()`, `_ensure_schema()`, `_embed_and_store()`, `_semantic_search()`, `_row_to_dict()`, `_Embedder` class, `_get_embedder()`
- Location: `remind_me_mcp.py` lines ~122–379
- Depends on: `sqlite3` stdlib, optionally `sqlite-vec`, `onnxruntime`, `tokenizers`, `huggingface-hub`, `numpy`
- Used by: All layers above

**Server Management Layer:**
- Purpose: PID file tracking, UI server health checks, graceful shutdown
- Contains: `_read_pid_file()`, `_write_pid_file()`, `_remove_pid_file()`, `_check_ui_server_health()`, `get_server_status()`
- Location: `remind_me_mcp.py` lines ~53–119
- Depends on: stdlib `os`, `json`
- Used by: Entry point (`__main__`), `remind_me_server_status` tool

## Data Flow

**MCP Tool Invocation (e.g., add a memory):**

1. Claude client sends MCP tool call `remind_me_add` over stdio
2. FastMCP deserializes and validates params into `MemoryAddInput` via Pydantic
3. `memory_add()` handler invoked with validated model
4. `_get_db()` opens/initializes SQLite connection with WAL mode and FTS5 triggers
5. `INSERT` executed into `memories` table; FTS5 trigger auto-indexes content
6. `_embed_and_store()` optionally generates ONNX embedding and stores in `memories_vec`
7. Confirmation string returned to Claude client

**Hybrid Search Flow:**

1. `remind_me_search` tool receives `MemorySearchInput`
2. FTS5 keyword search executes: `JOIN memories_fts WHERE memories_fts MATCH ?`
3. Semantic search executes (if embedder available): query embedded, vector similarity via `sqlite-vec`
4. Results merged and deduplicated; FTS matches boosted, hybrid matches double-boosted
5. Optional category/tag filters applied in Python
6. Formatted as Markdown or JSON and returned

**Chat Import Flow:**

1. `remind_me_import_chat` or `remind_me_import_directory` invoked
2. File hash computed; duplicate check against `chat_imports` table (idempotent)
3. File parsed by format: JSON (`_extract_messages_from_json()`), JSONL (line-by-line JSON), Markdown (`_parse_markdown_chat()`)
4. Messages filtered by `extract_mode` via `_filter_messages()`
5. Long content chunked at paragraph/sentence boundaries by `_chunk_text()`
6. Each chunk inserted into `memories`, FTS5 trigger fires
7. Batch embedding pass after commit
8. Import record written to `chat_imports` table

**HTTP Dashboard Flow:**

1. Browser requests `/` → Starlette serves self-contained HTML with inlined React app
2. React `useMemoryStore` hook fetches `/api/memories` and `/api/stats` on mount
3. User search triggers `GET /api/memories/search?q=...`
4. User edits trigger `PUT /api/memories/{id}`
5. All mutations call `store.refresh()` to reload state

**Auto-Capture Flow (conversation end):**

1. `remind_me_auto_capture` called with full dialog + summary
2. Two memories created and linked via shared `capture_id` in metadata
3. Dialog stored with category `dialog`; summary stored with caller-specified category (default: `conversation`)
4. Both embedded for semantic search
5. `remind_me_get_capture` retrieves linked pair by `capture_id`

**State Management:**
- Persistent state: SQLite file at `~/.remind-me/memory.db` (configurable via `REMIND_ME_MCP_DIR` env var)
- No in-memory application state between tool calls
- Embedder singleton `_embedder` is module-level (survives within a process lifetime)
- UI server PID tracked in `~/.remind-me/server.pid`

## Key Abstractions

**Memory:**
- Purpose: The core data entity — any text content to persist across sessions
- Schema: `id` (12-char SHA256 hash), `content`, `category`, `tags` (JSON array), `source`, `metadata` (JSON object), `created_at`, `updated_at`
- FTS5 virtual table `memories_fts` auto-synced via INSERT/UPDATE/DELETE triggers
- Optional: `memories_vec` virtual table for vector embeddings (sqlite-vec)

**MCP Tool:**
- Purpose: Named callable exposed to Claude clients
- Pattern: Async function decorated with `@mcp.tool(name=..., annotations={...})`, parameter as single validated Pydantic model
- Examples: `memory_add`, `memory_search`, `memory_list`, `memory_get`, `memory_update`, `memory_delete`, `memory_import_chat`, `memory_import_directory`, `memory_stats`, `remind_me_auto_capture`, `remind_me_get_capture`, `remind_me_reindex`, `remind_me_server_status`

**Pydantic Input Model:**
- Purpose: Type-safe, validated tool parameters with rich field descriptions (consumed by Claude for parameter inference)
- Pattern: `BaseModel` with `ConfigDict(extra="forbid")`, Field descriptors with `description`, `min_length`, `max_length`
- Examples: `MemoryAddInput`, `ChatImportInput`, `AutoCaptureInput`

**_Embedder:**
- Purpose: Lazy-loading ONNX-based embedding engine for semantic search
- Pattern: Singleton via `_get_embedder()` module-level factory; loads model from HuggingFace Hub on first use
- Graceful degradation: returns `None` if dependencies missing; FTS5 search continues without it

**MCP Resource:**
- Purpose: Read-only data exposed to Claude without tool invocation overhead
- Examples: `memory://stats`, `memory://categories`

## Entry Points

**MCP stdio entry (primary):**
- Location: `remind_me_mcp.py`, `__main__` block → `mcp.run()`
- Triggers: `python remind_me_mcp.py` (no `--serve-ui` flag), or installed script `remind-me-mcp`
- Responsibilities: Initialize MCP server, register all tools and resources, run stdio event loop

**HTTP dashboard entry:**
- Location: `remind_me_mcp.py`, `__main__` block → `uvicorn.run(_build_api_app(), ...)`
- Triggers: `python remind_me_mcp.py --serve-ui` or `REMIND_ME_MCP_SERVE_UI=true`
- Responsibilities: Build Starlette ASGI app, write PID file, serve on `127.0.0.1:5199` (configurable)

**Package script entry:**
- Location: `pyproject.toml` → `[project.scripts]` → `remind-me-mcp = "remind_me_mcp:mcp.run"`
- Triggers: Calling `remind-me-mcp` after `pip install`
- Responsibilities: Same as MCP stdio entry

**App lifespan:**
- Location: `app_lifespan()` asynccontextmanager, passed to `FastMCP`
- Triggers: Server startup/shutdown
- Responsibilities: Open DB connection at startup, log path, close on shutdown

## Error Handling

**Strategy:** Log and degrade gracefully; never crash the MCP server due to optional feature failure

**Patterns:**
- Optional dependencies (embedding, sqlite-vec) caught with `try/except ImportError` — server continues with FTS5-only search
- DB operations in tool handlers: exceptions propagate to FastMCP which returns error to Claude client
- FTS5 query syntax errors caught with `sqlite3.OperationalError` — falls through to semantic-only search
- Chat import errors per-file: caught and recorded in results dict with `"status": "error"`
- PID file corruption: caught and cleaned up silently

## Cross-Cutting Concerns

**Logging:**
- `logging` stdlib, configured to `sys.stderr` only (stdout reserved for MCP stdio protocol)
- Module-level logger: `log = logging.getLogger("remind_me_mcp")`
- Level: `INFO` by default

**Validation:**
- Pydantic v2 models on all MCP tool inputs; `extra="forbid"` prevents unexpected fields
- Path validation in `ChatImportInput.validate_path()` and `BulkImportDirInput.validate_dir()` field validators
- HTTP API performs manual validation (no Pydantic — lighter weight for REST layer)

**Concurrency:**
- SQLite WAL mode (`PRAGMA journal_mode=WAL`) for safe concurrent readers
- Single-process design; no shared-memory concurrency issues
- MCP and HTTP server are separate processes (not co-hosted in same process)

**Configuration:**
- Environment variables only: `REMIND_ME_MCP_DIR`, `REMIND_ME_EMBEDDING_MODEL`, `REMIND_ME_MCP_SERVE_UI`, `REMIND_ME_MCP_UI_PORT`
- Defaults hardcoded as module-level constants

---

*Architecture analysis: 2026-02-22*
*Update when major patterns change*
