# Architecture Patterns: Python MCP Server Package Refactor

**Project:** Remind Me MCP
**Domain:** Python async MCP server — monolith-to-package refactor
**Researched:** 2026-02-22
**Overall confidence:** HIGH (based on direct codebase analysis + established Python packaging patterns)

---

## Recommended Architecture

Split the single `remind_me_mcp.py` file into a proper Python package named `remind_me_mcp/`
(directory replaces the file). The package exposes the same `mcp.run` entry point and the same
MCP tool names — external behavior is unchanged. Internal structure becomes modular.

### Target Directory Layout

```
remind_me/
├── pyproject.toml                    # Update entry point: remind_me_mcp:mcp.run → still works
├── remind_me_mcp/                    # Package replaces the single file
│   ├── __init__.py                   # Exports: mcp (FastMCP instance), run()
│   ├── config.py                     # All env-var constants (MEMORY_DIR, DB_PATH, etc.)
│   ├── db.py                         # SQLite connection, schema, migration, CRUD helpers
│   ├── embeddings.py                 # _Embedder class, _get_embedder(), embed/search ops
│   ├── models.py                     # All Pydantic input models + ResponseFormat enum
│   ├── formatting.py                 # _fmt_memory_md(), _fmt_memories()
│   ├── importer.py                   # import_chat_file(), parsers, chunker, dir import
│   ├── server.py                     # FastMCP instance, lifespan, app_lifespan
│   ├── tools.py                      # All @mcp.tool() and @mcp.resource() definitions
│   ├── api.py                        # Starlette app builder, all REST route handlers
│   ├── pid.py                        # PID file management, server status detection
│   └── dashboard/
│       ├── __init__.py               # Exports: build_dashboard_html()
│       ├── html.py                   # _build_dashboard_html() — assembles HTML wrapper
│       └── App.jsx                   # Dashboard React source (moved from remind_me_dashboard.jsx)
├── tests/
│   ├── __init__.py
│   ├── conftest.py                   # Shared fixtures: tmp_db, in-memory SQLite, sample data
│   ├── test_db.py                    # Schema, CRUD, FTS5, migration tests
│   ├── test_embeddings.py            # Embedder unit tests (mock ONNX session)
│   ├── test_importer.py              # import_chat_file, parsers, chunker — pure function tests
│   ├── test_models.py                # Pydantic validator tests
│   ├── test_tools.py                 # Async MCP tool handler integration tests
│   └── test_api.py                   # Starlette REST API tests (TestClient)
└── remind_me_dashboard.jsx           # Keep as reference (can be removed after dashboard/ is live)
```

### Package Entry Point Update

`pyproject.toml` currently points at `remind_me_mcp:mcp.run` (the module attribute).
After refactoring, this still works if `remind_me_mcp/__init__.py` exports `mcp`:

```python
# remind_me_mcp/__init__.py
from remind_me_mcp.server import mcp

__all__ = ["mcp"]
```

The installed script `remind-me-mcp` continues to work with zero user-visible change.

---

## Component Boundaries

### What Each Module Is Responsible For

| Module | Owns | Does NOT Own |
|--------|------|--------------|
| `config.py` | Env-var constants, path resolution, MEMORY_DIR creation | Any I/O beyond mkdir |
| `db.py` | SQLite connection, schema DDL, migration versioning, row CRUD, FTS5, `_row_to_dict` | Embedding logic, business rules |
| `embeddings.py` | `_Embedder` class, model loading, vector math, `embed_one()`, `_get_embedder()` singleton, `_embed_and_store()`, `_semantic_search()` | DB connection management |
| `models.py` | Pydantic `BaseModel` subclasses, `ResponseFormat` enum, field validators | Any business logic |
| `formatting.py` | Memory-to-Markdown conversion, JSON serialization for tool responses | DB access, any I/O |
| `importer.py` | `import_chat_file()`, `import_directory()`, `_chunk_text()`, `_extract_messages_from_json()`, `_filter_messages()`, `_parse_markdown_chat()`, `_file_hash()` | MCP tool registration, HTTP routing |
| `pid.py` | `_read_pid_file()`, `_write_pid_file()`, `_remove_pid_file()`, `_check_ui_server_health()`, `get_server_status()` | DB, MCP, HTTP — pure filesystem + network |
| `server.py` | FastMCP instance creation, `app_lifespan` async context manager | Tool definitions (those live in `tools.py`) |
| `tools.py` | All `@mcp.tool()` and `@mcp.resource()` async functions | DB access details (delegates to `db.py`, `importer.py`, etc.) |
| `api.py` | Starlette `_build_api_app()`, all REST route handlers | MCP tool protocol, static assets |
| `dashboard/html.py` | `build_dashboard_html()` — HTML wrapper with `<script>` tags | React logic (that lives in `App.jsx`) |

### Communicates With (Dependency Graph)

```
config.py      (no dependencies on other project modules)
    ^
    |
db.py ──────── imports: config.py
    ^
    |
embeddings.py ─ imports: config.py, db.py (for _embed_and_store rowid lookup)
    ^
    |
importer.py ─── imports: config.py, db.py, embeddings.py
    ^
    |
models.py ─────  imports: (nothing from project — only pydantic + stdlib)
    ^
    |
formatting.py ─  imports: models.py (ResponseFormat enum)
    ^             imports: (nothing else from project)
    |
tools.py ───────  imports: server.py (mcp instance), models.py, db.py,
    ^              embeddings.py, importer.py, formatting.py, pid.py, config.py
    |
server.py ──────  imports: config.py, db.py
    |
    └─> tools.py (tools.py imports server.py for mcp instance — circular avoided by
                  importing mcp at module level in server.py, then tools.py imports
                  the already-constructed mcp object)

api.py ─────────  imports: config.py, db.py, importer.py, dashboard/html.py
    ^
    |
pid.py ─────────  imports: config.py
    ^
    |
__init__.py ────  imports: server.py (re-exports mcp for entry point)
```

**Key constraint:** `tools.py` imports `server.py` for the `mcp` instance, but `server.py`
must NOT import `tools.py` (that would create a circular import). The FastMCP pattern handles
this: `mcp` is created in `server.py`, then `tools.py` imports it and registers decorators.
The `__init__.py` ties both together at package import time.

**Recommended import order at package init:**

```python
# remind_me_mcp/__init__.py
from remind_me_mcp.server import mcp   # creates FastMCP instance
import remind_me_mcp.tools             # noqa: F401 — registers all @mcp.tool decorators
import remind_me_mcp.resources         # noqa: F401 — if resources split out later

__all__ = ["mcp"]
```

This pattern is used by FastMCP-based projects: the `mcp` object exists first, then modules
that decorate it are imported for their side effects.

---

## Data Flow

### MCP Tool Invocation (add a memory)

```
Claude client (stdio)
  → FastMCP deserializes → MemoryAddInput (models.py validates)
  → tools.memory_add()
  → asyncio.to_thread(db.insert_memory, ...)     [sync DB call wrapped async]
  → asyncio.to_thread(embeddings._embed_and_store, ...)
  → formatting._fmt_confirmation()
  → str returned to Claude client
```

### Search Flow

```
Claude client (stdio)
  → FastMCP → MemorySearchInput (models.py)
  → tools.memory_search()
  → asyncio.to_thread(db.fts_search, query, limit)    [FTS5]
  → asyncio.to_thread(embeddings._semantic_search, query, limit)
  → merge + deduplicate in tools layer
  → formatting._fmt_memories(results, fmt)
  → str returned
```

### Chat Import Flow

```
tools.memory_import_chat(ChatImportInput)
  → importer.import_chat_file(file_path, ...)
      → db._get_db()              [connection]
      → _file_hash()              [duplicate check]
      → _extract_messages_from_json() / _parse_markdown_chat()
      → _filter_messages()
      → _chunk_text()
      → db.insert_memory() × N   [batch insert]
      → embeddings._embed_and_store() × N  [bug fix: use collected (id, chunk) pairs]
      → db.record_import()
  → str result returned
```

### HTTP Dashboard Flow

```
Browser → GET /
  → api._build_api_app() (Starlette)
  → dashboard.html.build_dashboard_html()    [serves HTML+JSX]

Browser → GET /api/memories
  → api.api_list(request)
  → asyncio.to_thread(db.list_memories, ...)
  → JSONResponse

Browser → PUT /api/memories/{id}
  → api.api_update(request)
  → asyncio.to_thread(db.update_memory, ...)
  → asyncio.to_thread(embeddings._embed_and_store, ...)
  → JSONResponse
```

### Entry Point Decision (CLI)

```
remind_me_mcp/__main__.py  (or __init__ __main__ block)
  → argparse
  → if --serve-ui: api._build_api_app() → uvicorn.run()
  → else: mcp.run()    [stdio transport]
```

---

## Patterns to Follow

### Pattern 1: Async Wrapper for All Sync I/O

Every synchronous DB call and embedding operation inside an `async def` tool handler
must be wrapped with `asyncio.to_thread`. This is the fix for the performance bottleneck
documented in CONCERNS.md.

```python
# tools.py — correct pattern
import asyncio
from remind_me_mcp import db, embeddings

async def memory_add(params: MemoryAddInput) -> str:
    """Store a new memory."""
    conn = await asyncio.to_thread(db.get_connection)
    mem_id = await asyncio.to_thread(db.insert_memory, conn, params)
    await asyncio.to_thread(embeddings.embed_and_store, conn, mem_id, params.content)
    return f"Memory stored with id `{mem_id}`."
```

### Pattern 2: DB Connection Singleton per Process

Replace connect-per-call with a module-level singleton in `db.py`. The lifespan
context manager in `server.py` owns the lifecycle.

```python
# db.py
import sqlite3
from remind_me_mcp.config import DB_PATH

_connection: sqlite3.Connection | None = None

def get_connection() -> sqlite3.Connection:
    """Return the process-scoped SQLite connection, initializing if needed."""
    global _connection
    if _connection is None:
        _connection = _open_connection()
    return _connection

def close_connection() -> None:
    global _connection
    if _connection:
        _connection.close()
        _connection = None
```

```python
# server.py
from contextlib import asynccontextmanager
from remind_me_mcp import db

@asynccontextmanager
async def app_lifespan(app):
    conn = await asyncio.to_thread(db.get_connection)
    log.info("Remind Me MCP started — db at %s", config.DB_PATH)
    yield {"db": conn}
    await asyncio.to_thread(db.close_connection)
```

### Pattern 3: Schema Migration with PRAGMA user_version

Replace `CREATE IF NOT EXISTS` only with a versioned migration system.

```python
# db.py
SCHEMA_VERSION = 2  # increment when schema changes

def _migrate(conn: sqlite3.Connection) -> None:
    """Apply pending migrations in version order."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current < 1:
        _apply_v1(conn)   # initial schema
    if current < 2:
        _apply_v2(conn)   # add capture_id column + memory_tags junction table
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
```

### Pattern 4: Collected ID Pairs in Import (Bug Fix)

Fix the import embedding ID mismatch by collecting `(mem_id, chunk)` tuples during
the insert loop and reusing them for the embedding pass.

```python
# importer.py
inserted: list[tuple[str, str]] = []   # (mem_id, chunk) pairs

for chunk in chunks:
    mem_id = _make_id(chunk)
    conn.execute("INSERT OR IGNORE INTO memories ...", (mem_id, chunk, ...))
    inserted.append((mem_id, chunk))

conn.commit()

# Embedding pass uses the exact same IDs — no reconstruction
for mem_id, chunk in inserted:
    embeddings.embed_and_store(conn, mem_id, chunk)
```

### Pattern 5: Shared import_directory() Function (DRY Fix)

Extract directory-import logic into `importer.py` to eliminate the duplicate
implementations in `tools.py` and `api.py`.

```python
# importer.py
def import_directory(
    directory: str,
    category: str,
    tags: list[str],
    extract_mode: str,
    max_length: int,
    recursive: bool,
) -> list[dict]:
    """Shared directory import — called by both MCP tool and HTTP endpoint."""
    extensions = {".json", ".jsonl", ".md", ".markdown", ".txt"}
    glob_pattern = "**/*" if recursive else "*"
    files = [
        p for p in Path(directory).glob(glob_pattern)
        if p.suffix.lower() in extensions
    ]
    return [import_chat_file(str(f), category, tags, extract_mode, max_length)
            for f in sorted(files)]
```

### Pattern 6: Tag Junction Table (Bug Fix for Broken Pagination)

The `memory_tags` junction table enables SQL-level tag filtering with correct pagination.

```python
# db.py schema (v2 migration)
"""
CREATE TABLE IF NOT EXISTS memory_tags (
    memory_id  TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    tag        TEXT NOT NULL,
    PRIMARY KEY (memory_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_memory_tags_tag ON memory_tags(tag);
"""
```

Query with tag filter:

```python
# db.py
def list_memories_by_tags(conn, tags: list[str], limit: int, offset: int) -> list[dict]:
    placeholders = ",".join("?" * len(tags))
    rows = conn.execute(f"""
        SELECT m.* FROM memories m
        WHERE m.id IN (
            SELECT memory_id FROM memory_tags
            WHERE tag IN ({placeholders})
            GROUP BY memory_id
            HAVING COUNT(DISTINCT tag) = ?
        )
        ORDER BY m.created_at DESC
        LIMIT ? OFFSET ?
    """, (*tags, len(tags), limit, offset)).fetchall()
    return [_row_to_dict(r) for r in rows]
```

---

## Anti-Patterns to Avoid

### Anti-Pattern 1: Circular Module Imports

**What:** `server.py` importing `tools.py` and `tools.py` importing `server.py`.
**Why bad:** Python will raise `ImportError: cannot import name 'mcp'` or partially
initialized modules.
**Instead:** `server.py` creates `mcp`. `tools.py` imports `mcp` from `server.py`.
`__init__.py` imports `tools` (for side effects) after importing `server`.

### Anti-Pattern 2: Module-Level DB Connect on Import

**What:** Calling `sqlite3.connect()` at module level in `config.py` or `db.py`.
**Why bad:** Runs on every test import, breaks tests that set `REMIND_ME_MCP_DIR` via
`monkeypatch.setenv` (env var already consumed), creates file in wrong location.
**Instead:** Lazy singleton in `db.get_connection()`. Only opens connection when first called.

### Anti-Pattern 3: Inlining Starlette Imports at Module Top Level

**What:** `from starlette.applications import Starlette` at top of `api.py`.
**Why bad:** Forces Starlette import even in MCP-only (stdio) mode. Starlette is always
installed, but the pattern also applies to optional dependencies.
**Instead:** Keep the current lazy-import-inside-function pattern from the original code.
Starlette and uvicorn imports belong inside `_build_api_app()` or a conditional block.
For optional embeddings: still lazy-import inside `_Embedder._ensure_loaded()`.

### Anti-Pattern 4: God Module `tools.py`

**What:** Stuffing all 13 tools into one 800-line `tools.py`.
**Why bad:** This recreates the monolith problem one level up. The file is still
the only place to add tools.
**Instead:** For this project's size (13 tools, 800 lines), a single `tools.py` is
acceptable — it's smaller than the original and tools are the natural atomic unit.
If tool count grows past ~20, split into `tools/memory.py`, `tools/import.py`,
`tools/system.py`. Do NOT pre-split prematurely for 13 tools.

### Anti-Pattern 5: Re-exporting Everything in `__init__.py`

**What:** `from remind_me_mcp.db import *` in `__init__.py`.
**Why bad:** Exposes internal helpers as the package's public API. Creates maintenance
burden when internal names change.
**Instead:** `__init__.py` exports only `mcp` (the FastMCP instance). Everything else
is accessed via explicit submodule imports within the package.

---

## Component Build Order (Dependency-Driven Sequence)

This sequence reflects the dependency graph: each module can only be built after its
dependencies. This IS the recommended phase/task ordering for the refactor.

```
Step 1: config.py
  - No dependencies on other project modules
  - Move all env-var constants + path setup from remind_me_mcp.py top
  - Foundation for every other module

Step 2: db.py
  - Depends on: config.py
  - Move: _get_db(), _ensure_schema(), _row_to_dict(), _now_iso(), _make_id()
  - Add: singleton connection pattern, PRAGMA user_version migration system
  - Add: memory_tags junction table migration
  - Fix: LIKE-based capture_id lookup (add capture_id column in v2 migration)

Step 3: embeddings.py
  - Depends on: config.py, db.py
  - Move: _Embedder class, _get_embedder(), _embed_and_store(), _semantic_search()
  - Unchanged behavior; just relocated

Step 4: models.py
  - Depends on: nothing (stdlib + pydantic only)
  - Move: all Pydantic BaseModel subclasses, ResponseFormat enum
  - Can be done in parallel with Step 3

Step 5: formatting.py
  - Depends on: models.py (ResponseFormat)
  - Move: _fmt_memory_md(), _fmt_memories()
  - Pure functions; no I/O

Step 6: importer.py
  - Depends on: config.py, db.py, embeddings.py
  - Move: import_chat_file(), _chunk_text(), _extract_messages_from_json(),
           _filter_messages(), _parse_markdown_chat(), _file_hash()
  - Add: import_directory() (extracted from duplicate implementations)
  - Fix: embedding ID mismatch (use collected (mem_id, chunk) pairs)

Step 7: pid.py
  - Depends on: config.py
  - Move: _read_pid_file(), _write_pid_file(), _remove_pid_file(),
           _check_ui_server_health(), get_server_status()
  - Can be done in parallel with Step 6

Step 8: server.py
  - Depends on: config.py, db.py
  - Create: FastMCP instance, app_lifespan
  - This is the moment the mcp object is created for tools.py to import

Step 9: tools.py
  - Depends on: server.py, models.py, db.py, embeddings.py, importer.py, formatting.py, pid.py
  - Move: all 13 @mcp.tool() handlers and 2 @mcp.resource() definitions
  - Wrap all sync DB/embed calls in asyncio.to_thread
  - Fix: tag filtering now delegates to db.list_memories_by_tags() (SQL junction table)

Step 10: dashboard/html.py + dashboard/App.jsx
  - Depends on: nothing (static content generation)
  - Move: _build_dashboard_html() → dashboard/html.py
  - Move: _get_dashboard_script() content → dashboard/App.jsx
  - Can be done in parallel with Step 9

Step 11: api.py
  - Depends on: config.py, db.py, importer.py, dashboard/html.py
  - Move: _build_api_app() and all route handlers
  - Fix: tag filtering (same SQL pattern as tools.py)
  - Fix: api_import now calls importer.import_directory() (shared function)
  - Wrap sync DB calls in asyncio.to_thread for async route handlers

Step 12: __init__.py + __main__.py
  - Depends on: everything
  - __init__.py: export mcp, import tools for side effects
  - __main__.py: move argparse CLI block from remind_me_mcp.py __main__
  - Update pyproject.toml entry point if needed

Step 13: tests/ (written alongside each module, not after)
  - Recommended: write tests for each module immediately after extracting it
  - tests/test_db.py: written after Step 2
  - tests/test_importer.py: written after Step 6
  - tests/test_tools.py: written after Step 9
  - tests/test_api.py: written after Step 11
```

**Why this order:** Steps 1-7 have no dependency on the MCP framework (FastMCP), making
them independently testable pure-Python modules. Steps 8-9 introduce FastMCP. Steps 10-11
are independent of each other and of tools.py. Step 12 wires everything together. Tests
written incrementally prevent the entire test suite from being written in one risky batch.

---

## Scalability Considerations

| Concern | Current (1 file) | After Refactor | If 10x growth |
|---------|-----------------|----------------|---------------|
| Adding a new tool | Edit 2500-line file | Edit tools.py only | Split into tools/ subpackage |
| Adding a new DB column | Edit _ensure_schema | Add migration in db.py | Migration system handles it |
| Replacing embeddings | Buried in 2500 lines | Replace embeddings.py | Abstract behind Embedder protocol |
| Adding new import format | Edit importer section | Add to importer.py | Format registry pattern |
| Testing | Impossible without mocks threading | Per-module unit tests | Same |
| Separate process for API | Not possible (coupled) | api.py is standalone | Deploy api.py separately |

---

## Key Architectural Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Package layout | Flat (`remind_me_mcp/*.py`) not `src/` layout | No separate distribution; single pip install; simpler import paths |
| Tool organization | Single `tools.py` (not split by domain) | 13 tools fit in ~800 lines; premature split adds nav overhead without benefit |
| DB connection | Module-level singleton | Avoids per-call PRAGMA overhead; safe for single-process MCP server |
| Async wrapping | `asyncio.to_thread` | Existing sync SQLite + ONNX don't require rewrite; `to_thread` is the correct pattern for CPU-bound sync work in async contexts |
| Dashboard assets | `dashboard/App.jsx` file (no build step) | Preserves Babel standalone constraint from PROJECT.md; JSX still transpiled in-browser |
| Migration system | `PRAGMA user_version` | Standard SQLite versioning; no external migration library needed |
| Tag storage | Add `memory_tags` junction table | Fixes broken pagination; keep JSON column as denormalized read cache |
| Entry point | `remind_me_mcp/__init__.py` re-exports `mcp` | `pyproject.toml` entry point `remind_me_mcp:mcp.run` continues to work |

---

## Sources and Confidence

| Claim | Source | Confidence |
|-------|--------|------------|
| FastMCP `mcp` object imported by tools.py as side-effect pattern | Direct code analysis of existing working pattern in remind_me_mcp.py line 823 | HIGH |
| `asyncio.to_thread` for sync I/O in async handlers | Python 3.9+ stdlib docs; directly addresses CONCERNS.md performance bottleneck | HIGH |
| `PRAGMA user_version` for SQLite migration versioning | SQLite official documentation — standard pattern | HIGH |
| Flat package layout over src/ for single-distribution project | Python packaging guide; hatchling default behavior with single package | HIGH |
| `memory_tags` junction table SQL pattern | Standard relational normalization; directly fixes CONCERNS.md tag pagination bug | HIGH |
| Build order based on dependency graph | Derived from code analysis — each module's actual import dependencies | HIGH |
| Single `tools.py` vs split `tools/` subpackage | Judgment based on tool count (13) and line count (~800); standard Python practice | MEDIUM |

All findings based on direct codebase analysis of `remind_me_mcp.py` and planning documents.
No web research performed (tools unavailable). Patterns are well-established Python conventions
with no recency risk.

---

*Architecture research: 2026-02-22*
*Informs roadmap phase structure and build order*
