---
phase: 01-package-structure
plan: "02"
subsystem: api
tags: [python, fastmcp, starlette, react, jsx, mcp-tools, mcp-resources, modular-package]

# Dependency graph
requires:
  - phase: 01-package-structure
    provides: "config.py, models.py, formatting.py, embeddings.py, db.py from plan 01-01"
provides:
  - remind_me_mcp/importer.py — chat import engine with JSON/JSONL/Markdown parsers and chunker
  - remind_me_mcp/pid.py — PID file management and UI server status detection
  - remind_me_mcp/server.py — FastMCP instance (mcp) and app_lifespan context manager
  - remind_me_mcp/tools.py — all 13 MCP tool handlers and 2 resource handlers registered on mcp
  - remind_me_mcp/api.py — Starlette REST API builder and dashboard HTML builder (reads JSX from file)
  - remind_me_mcp/dashboard/App.jsx — full React dashboard component extracted from monolith
affects:
  - 01-package-structure (plan 03 will wire up __main__.py entry point)
  - 02-test-coverage (tests will import and exercise all 6 new modules)
  - 03-bug-fixes (bug fixes will modify these modules)

# Tech tracking
tech-stack:
  added: [mcp/fastmcp, starlette, react-cdn-babel-standalone]
  patterns:
    - "Circular import avoidance: server.py defines mcp instance; tools.py imports mcp from server"
    - "Lazy Starlette imports: all web framework imports inside _build_api_app() to avoid loading in stdio mode"
    - "JSX extracted to file: _build_dashboard_html reads App.jsx via Path(__file__).parent / 'dashboard' / 'App.jsx'"
    - "Lazy embedder imports: _get_embedder imported inside tool functions to avoid top-level load"

key-files:
  created:
    - remind_me_mcp/importer.py
    - remind_me_mcp/pid.py
    - remind_me_mcp/server.py
    - remind_me_mcp/tools.py
    - remind_me_mcp/api.py
    - remind_me_mcp/dashboard/__init__.py
    - remind_me_mcp/dashboard/App.jsx
  modified: []

key-decisions:
  - "server.py must NOT import tools.py — tools.py imports mcp from server to prevent circular imports"
  - "Lazy Starlette imports inside _build_api_app() — prevents heavy web framework load in stdio-only mode"
  - "JSX loaded via pathlib at runtime — Path(__file__).parent / 'dashboard' / 'App.jsx' — simpler than importlib.resources"
  - "embeddings._get_embedder imported locally in tool functions — avoids model load at module import time"
  - "Pure extraction — all function signatures, docstrings, and behavior preserved verbatim from monolith"

patterns-established:
  - "Import chain: tools.py imports from server, db, formatting, models, importer, pid (no upward dependencies)"
  - "All private helpers (_file_hash, _chunk_text, etc.) included in __all__ per plan spec"
  - "config imports (DB_PATH, EMBEDDING_MODEL) done locally inside functions in tools.py to keep module-level imports minimal"

requirements-completed: [ARCH-01, ARCH-04, ARCH-05, QUAL-03]

# Metrics
duration: 8min
completed: 2026-02-24
---

# Phase 1 Plan 2: Behavioral Module Extraction Summary

**Six behavioral modules extracted from the 2,500-line monolith — FastMCP instance, 13 MCP tools, 2 resources, Starlette REST API, PID management, and chat import engine — with React dashboard JSX decoupled from Python string**

## Performance

- **Duration:** 8 min
- **Started:** 2026-02-24T03:26:55Z
- **Completed:** 2026-02-24T03:34:14Z
- **Tasks:** 2
- **Files modified:** 7

## Accomplishments

- Extracted chat import engine (JSON/JSONL/Markdown parsers, chunker, dedup) into `importer.py`
- Extracted PID file management and server status detection into `pid.py`
- Created `server.py` with FastMCP instance and lifespan — no circular imports with `tools.py`
- Extracted all 13 MCP tool handlers and 2 resource handlers into `tools.py` (registered on `mcp` from `server.py`)
- Extracted Starlette REST API into `api.py` with lazy imports; dashboard HTML builder reads JSX from file
- Extracted React dashboard component from Python triple-quoted string into `dashboard/App.jsx`

## Task Commits

Each task was committed atomically:

1. **Task 1: Create importer.py and pid.py** - `1d0f0c7` (feat)
2. **Task 2: Create server.py, tools.py, api.py, and extract dashboard JSX** - `89a74ff` (feat)

**Plan metadata:** _(docs commit below)_

## Files Created/Modified

- `remind_me_mcp/importer.py` — import_chat_file, _chunk_text, _extract_messages_from_json, _filter_messages, _parse_markdown_chat, _file_hash
- `remind_me_mcp/pid.py` — _read_pid_file, _write_pid_file, _remove_pid_file, _check_ui_server_health, get_server_status
- `remind_me_mcp/server.py` — FastMCP instance (mcp = FastMCP("remind_me_mcp", lifespan=app_lifespan)), app_lifespan
- `remind_me_mcp/tools.py` — 13 tool handlers (memory_add, memory_search, memory_list, memory_get, memory_update, memory_delete, memory_import_chat, memory_import_directory, memory_stats, remind_me_auto_capture, remind_me_get_capture, remind_me_reindex, remind_me_server_status) + 2 resources (resource_stats, resource_categories)
- `remind_me_mcp/api.py` — _build_api_app (Starlette with 9 routes), _build_dashboard_html (reads App.jsx at runtime)
- `remind_me_mcp/dashboard/__init__.py` — empty subpackage marker
- `remind_me_mcp/dashboard/App.jsx` — full React component (~540 lines) using React.createElement, CDN-loaded React 18

## Decisions Made

- `server.py` must not import from `tools.py` — instead `tools.py` imports `mcp` from `server.py`. This is the canonical circular-import avoidance pattern for FastMCP modular architectures.
- All Starlette imports are kept lazy inside `_build_api_app()` — same as monolith — to prevent loading the web framework when running in stdio MCP mode.
- Used `Path(__file__).parent / "dashboard" / "App.jsx"` to locate the JSX file (simpler than `importlib.resources`, no Python version concerns).
- The inline Babel-compatible React component (using `React.createElement`) was extracted from `_get_dashboard_script()` in the monolith — NOT from the `remind_me_dashboard.jsx` reference file which uses ES module imports incompatible with Babel standalone.

## Deviations from Plan

None — plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None — no external service configuration required.

## Next Phase Readiness

- All 10 modules (`config`, `models`, `formatting`, `embeddings`, `db`, `importer`, `pid`, `server`, `tools`, `api`) import cleanly with no circular dependency errors
- `mcp` instance in `server.py` has all 13 tools and 2 resources registered (side effect of importing `tools`)
- Plan 01-03 can now create `__main__.py` entry point wiring everything together
- Full integration test (running the MCP server end-to-end) is deferred to Phase 2

## Self-Check: PASSED

All 7 files found on disk. Both task commits (1d0f0c7, 89a74ff) confirmed in git log.

---
*Phase: 01-package-structure*
*Completed: 2026-02-24*
