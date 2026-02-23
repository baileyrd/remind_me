# Codebase Structure

**Analysis Date:** 2026-02-22

## Directory Layout

```
remind_me/
├── remind_me_mcp.py       # Entire application: MCP server, HTTP API, dashboard UI
├── remind_me_dashboard.jsx # Standalone JSX source (reference only — not executed)
├── pyproject.toml          # Project metadata, dependencies, package script entry
├── .python-version         # Python version pin (3.12)
├── README.md               # User-facing setup and usage documentation
├── .gitignore              # Git exclusions
├── .claude/                # Claude Code project settings
│   └── settings.local.json
└── .planning/              # GSD planning artifacts (not application code)
    └── codebase/           # Codebase analysis documents
```

## Directory Purposes

**Project root:**
- Purpose: All application code lives directly at the root — this is a single-module project
- Contains: One Python source file, one JSX reference file, one config file
- Key files: `remind_me_mcp.py` is the entire application

**.planning/codebase/:**
- Purpose: GSD codebase analysis documents consumed by planning/execution agents
- Contains: STACK.md, ARCHITECTURE.md, STRUCTURE.md
- Generated: By GSD map-codebase commands
- Committed: Yes

**.claude/:**
- Purpose: Claude Code project-level settings
- Contains: `settings.local.json`
- Committed: Partially (settings.local.json typically gitignored)

## Key File Locations

**Entry Points:**
- `remind_me_mcp.py` (line 2440, `__main__` block): Primary entry — MCP stdio or HTTP dashboard mode
- `pyproject.toml` `[project.scripts]`: `remind-me-mcp` → `remind_me_mcp:mcp.run` (installed package entry)

**Configuration:**
- `pyproject.toml`: Python version requirement (`>=3.11`), core and optional dependencies, build system (hatchling)
- `.python-version`: Pins development Python to 3.12
- Runtime config via environment variables (no config files): `REMIND_ME_MCP_DIR`, `REMIND_ME_EMBEDDING_MODEL`, `REMIND_ME_MCP_SERVE_UI`, `REMIND_ME_MCP_UI_PORT`

**Core Logic (all within `remind_me_mcp.py`):**
- Lines 1–48: Module docstring, imports, module-level constants (`MEMORY_DIR`, `DB_PATH`, `IMPORT_LOG`, `PID_FILE`)
- Lines 53–119: Server instance detection (PID file management)
- Lines 122–379: Database helpers — schema, FTS5, vector embeddings, `_Embedder` class
- Lines 383–542: Pydantic input models for all MCP tools
- Lines 547–577: Formatting helpers (`_fmt_memory_md`, `_fmt_memories`)
- Lines 580–806: Chat import engine (`import_chat_file`, parsers, chunker)
- Lines 812–823: FastMCP server instantiation with lifespan
- Lines 827–1601: MCP tool and resource definitions (13 tools, 2 resources)
- Lines 1603–1871: HTTP REST API (Starlette app and route handlers)
- Lines 1874–2433: Dashboard HTML/React UI (inlined into Python string)
- Lines 2440–2496: CLI entry point (`__main__`)

**Generated Runtime Files (not in repo):**
- `~/.remind-me/memory.db`: SQLite database (configurable path)
- `~/.remind-me/server.pid`: UI server PID tracking
- `~/.remind-me/import_log.json`: Import log reference path (defined but not heavily used)
- `~/.remind-me/models/`: ONNX embedding model cache

**Documentation:**
- `README.md`: Installation, MCP config for Claude Code/Desktop/Claude.ai, tool reference, dashboard usage

**Reference:**
- `remind_me_dashboard.jsx`: Standalone JSX source of the dashboard component (the inlined version in `remind_me_mcp.py` is the executed version; this file is for readability/editing)

## Naming Conventions

**Files:**
- `snake_case.py`: Python source files
- `snake_case.jsx`: JSX source files
- `UPPERCASE.md`: Important project documents (README)
- `kebab-case.md`: GSD planning documents

**Functions:**
- `snake_case`: All Python functions
- Prefix `_` (single underscore): Private/internal helpers (e.g., `_get_db`, `_ensure_schema`, `_fmt_memory_md`, `_build_api_app`)
- No prefix: Public business logic and MCP tool handlers (e.g., `import_chat_file`, `get_server_status`, `memory_add`)

**Classes:**
- `PascalCase`: Pydantic models (e.g., `MemoryAddInput`, `ChatImportInput`)
- `_PascalCase` with underscore prefix: Internal implementation classes (e.g., `_Embedder`)
- `PascalCase` Enum: `ResponseFormat`

**MCP Tool Names:**
- `remind_me_{action}` pattern: `remind_me_add`, `remind_me_search`, `remind_me_list`, `remind_me_get`, `remind_me_update`, `remind_me_delete`, `remind_me_import_chat`, `remind_me_import_directory`, `remind_me_stats`, `remind_me_auto_capture`, `remind_me_get_capture`, `remind_me_reindex`, `remind_me_server_status`

**HTTP API Routes:**
- `/api/{resource}` flat structure: `/api/stats`, `/api/memories`, `/api/memories/search`, `/api/memories/{memory_id}`, `/api/import`

**Database:**
- `snake_case` table and column names: `memories`, `chat_imports`, `memories_fts`, `memories_vec`
- `idx_{table}_{column}` for indexes: `idx_memories_category`, `idx_memories_source`, `idx_memories_created`

## Where to Add New Code

**New MCP Tool:**
- Tool handler: Add `@mcp.tool(name="remind_me_{action}", annotations={...})` async function in `remind_me_mcp.py` near line 827–1581
- Input model: Add `{Action}Input(BaseModel)` class in `remind_me_mcp.py` near line 383–542
- If the tool needs new DB fields: add migration logic in `_ensure_schema()` (lines 253–310)
- Document in `README.md` tool reference section

**New HTTP API Endpoint:**
- Route handler: Add async function inside `_build_api_app()` (lines 1610–1871)
- Register in the `routes` list near line 1855

**New Database Table or Column:**
- Add `CREATE TABLE IF NOT EXISTS` or `CREATE INDEX IF NOT EXISTS` in `_ensure_schema()` (lines 253–310)
- SQLite's `IF NOT EXISTS` guards make schema changes safe without migrations

**New Chat Import Format:**
- Add parser function (follow pattern of `_extract_messages_from_json`, `_parse_markdown_chat`)
- Add branch in `import_chat_file()` format dispatch block (lines 735–762)
- Add file extension to `extensions` sets in `memory_import_directory` and `api_import`

**New Configuration Option:**
- Add `os.environ.get("REMIND_ME_...", default)` module-level constant
- Add `--flag` to `argparse` in `__main__` block if it affects startup mode

**Dashboard UI Changes:**
- Edit `remind_me_dashboard.jsx` for readability, then copy the transpiled logic into `_get_dashboard_script()` in `remind_me_mcp.py`
- The JSX in `remind_me_mcp.py` is transpiled at runtime by Babel standalone in the browser

## Special Directories

**`~/.remind-me/` (runtime, not in repo):**
- Purpose: All persistent application data
- Source: Created by `MEMORY_DIR.mkdir(parents=True, exist_ok=True)` at module import
- Committed: No — user data directory

**`.planning/` (in repo):**
- Purpose: GSD planning artifacts — not application code
- Source: Created by GSD planning commands
- Committed: Yes

---

*Structure analysis: 2026-02-22*
*Update when directory structure changes*
