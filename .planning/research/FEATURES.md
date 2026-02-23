# Feature Landscape

**Domain:** Python MCP server refactoring — monolith-to-modules restructure
**Project:** Remind Me MCP
**Researched:** 2026-02-22
**Confidence:** HIGH (grounded in codebase analysis + established Python idioms; WebSearch unavailable, findings drawn from codebase inspection and well-established Python community standards)

---

## Context

This is a refactoring project, not a feature project. "Features" here means structural and quality capabilities that a well-structured Python package is expected to have. The source is a working, 2,500-line single-file MCP server. The destination is a modular Python package that passes a design-principles audit without breaking any existing functionality.

Findings are grounded in:
- Direct inspection of `remind_me_mcp.py` and `pyproject.toml`
- Codebase concerns documented in `.planning/codebase/CONCERNS.md`
- Project requirements in `.planning/PROJECT.md`
- Established Python packaging and async patterns (Python 3.11+, pytest, asyncio)

---

## Table Stakes

Features a well-structured Python project is expected to have. Absence makes the project feel incomplete or unsafe to extend.

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Module split by layer | Industry standard for any multi-layer Python package | Medium | `db.py`, `embeddings.py`, `importer.py`, `tools.py`, `api.py`, `dashboard.py`, `server.py`. Boundaries already clear from ARCHITECTURE.md layers. |
| `__init__.py` with stable public API | Required for installable package imports | Low | Entry points in `pyproject.toml` must resolve after split; `mcp.run` reference changes |
| Single `pip install` preserved | Constraint from PROJECT.md — all modules remain in one package | Low | Internal split only; no new top-level packages |
| pytest test suite (unit + integration) | Zero tests currently is a blocker for safe refactoring | High | Must establish patterns from scratch; in-memory SQLite fixture is the key enabler |
| pytest fixtures for in-memory DB | Every DB test needs an isolated SQLite database | Low | `@pytest.fixture` returning `sqlite3.connect(":memory:")` with schema applied |
| Docstrings on all public functions/classes | CLAUDE.md requirement; enables IDE tooling | Medium | Numerous undocumented helpers across all layers; high volume but mechanical |
| Type hints on all function signatures | CLAUDE.md requirement; `from __future__ import annotations` already present | Low | Most signatures already typed; gaps in helper functions |
| `asyncio.to_thread` wrapping sync DB/embedding calls | Async-first principle; sync calls currently block the event loop | Medium | `_embed_and_store`, `_semantic_search`, `_get_db` calls inside async tool handlers all need wrapping |
| Connection management (singleton or context manager) | `_get_db()` currently opens+schema-checks on every call | Medium | Thread-local singleton or lifespan-scoped connection; must be safe across MCP stdio and HTTP ASGI contexts |
| Schema migration with `PRAGMA user_version` | `_ensure_schema` uses only `CREATE IF NOT EXISTS`; cannot evolve schema on existing DBs | Medium | Numbered migration steps, applied conditionally; required to add `capture_id` column and tags junction table |
| DRY: single `import_directory()` function | Directory import logic duplicated in `memory_import_directory` tool and `api_import` HTTP handler | Low | Extract shared function, call from both sites |
| Bug fix: import embedding ID mismatch | Known bug: imported memories never embedded due to `mem_id_check` reconstruction | Low | Collect `(mem_id, chunk)` pairs in insert loop; pass to embedding pass directly |
| Bug fix: `remind_me_get_capture` LIKE lookup | Fragile JSON string matching instead of indexed column | Medium | Requires schema migration to add `capture_id` column |
| Normalize `_make_id` semantics | Docstring says "deterministic" but timestamp is included; misleading | Low | Rename to `_new_id` and document as intentionally non-deterministic, OR truly deduplicate by content hash |
| SQL-level tag filtering | Tag filtering in Python post-fetch breaks pagination | Medium | Requires `memory_tags` junction table + schema migration |
| Extract dashboard JSX to separate file | React app embedded as Python string is unmaintainable | Low | `dashboard/App.jsx` file; Babel standalone kept, no build step needed |
| Logging to `sys.stderr` only | Already correct; must be preserved after module split | Low | Each module gets its own `logging.getLogger("remind_me_mcp.[module]")` |

---

## Differentiators

Features that go beyond minimum well-structured Python. Valuable additions but not expected for a clean refactor.

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| `pytest-asyncio` for async tool tests | Enables testing async MCP tool handlers directly without mocking the event loop | Low | Add `asyncio_mode = "auto"` in `pyproject.toml`; minimal overhead |
| `conftest.py` shared fixtures | Centralizes test fixtures so every test module has access without duplication | Low | DB fixture, mock embedder fixture, sample memory factory function |
| Mock embedder fixture | Decouples unit tests from ONNX model download; CI-safe | Low | `unittest.mock.patch` on `_get_embedder` returning a minimal fake with predictable `embed_one` output |
| Starlette `TestClient` for API tests | Tests HTTP routes in-process without a real server | Low | Starlette ships its own `TestClient`; no additional dependency |
| Batched reindex (100-row pages) | Current `remind_me_reindex` loads all rows into memory at once | Medium | `LIMIT`/`OFFSET` cursor loop; low risk, high value at scale |
| `capture_id` as indexed column | Replaces LIKE-based lookup with O(log n) index scan | Medium | Part of schema migration; prerequisite for the capture bug fix |
| Version-pinned optional dependencies | `mcp[cli]>=1.0.0,<2.0.0`, `sqlite-vec>=0.1.0,<0.2.0` | Low | Prevents surprise breakage on fresh installs |
| `pyproject.toml` test configuration | `[tool.pytest.ini_options]` block with `asyncio_mode`, `testpaths`, `markers` | Low | Eliminates need for separate `pytest.ini` or `setup.cfg` |
| Per-module loggers with consistent naming | `logging.getLogger("remind_me_mcp.db")`, `...embeddings`, `...tools`, etc. | Low | Allows per-module log level control at runtime |
| `__all__` on each module | Defines the public surface of each module explicitly | Low | Prevents accidental import of private helpers |

---

## Anti-Features

Things to deliberately NOT do during this refactoring pass. Doing these would violate the project constraints or introduce unnecessary risk.

| Anti-Feature | Why Avoid | What to Do Instead |
|--------------|-----------|-------------------|
| Add new user-facing MCP tools or parameters | PROJECT.md: "New features" is explicitly out of scope; clients depend on existing tool names/params | Touch only structure and quality; all existing tool names/params remain unchanged |
| Build step for the dashboard (Vite, esbuild, webpack) | Adds Node.js toolchain dependency; PROJECT.md explicitly keeps Babel standalone | Extract JSX to `dashboard/App.jsx`; Babel standalone continues to transpile in-browser |
| Split into multiple PyPI packages | PROJECT.md: single `pip install` preserved | Internal module split within one package only |
| Security hardening (CORS lockdown, API auth) | Explicitly deferred to a separate pass in PROJECT.md | Document security issues in CONCERNS.md; leave CORS wildcard and no-auth as-is |
| `aiosqlite` or async SQLite driver | Adds a dependency; the correct fix is `asyncio.to_thread` around sync sqlite3 calls | Wrap sync DB calls with `await asyncio.to_thread(...)` |
| ORM (SQLAlchemy, Tortoise) | Heavyweight abstraction over a well-understood schema; overkill for SQLite | Keep raw `sqlite3` with named parameters |
| `pytest-cov` coverage enforcement in CI | CI/CD setup is out of scope per PROJECT.md | Run coverage locally as a development tool; no enforcement gate |
| Rewrite tool logic during refactoring | Risk of behavior regression; violates "preserve functionality" constraint | Move functions as-is; fix only the explicitly listed bugs |
| PostgreSQL migration | Scaling consideration explicitly deferred; single-user personal tool | SQLite remains; WAL mode is sufficient |
| Performance optimization beyond sync-in-async fix | "No premature optimization" per PROJECT.md | Fix only the async event-loop blockage; no other perf work |
| `__init__.py` re-exporting everything | Defeats the module separation; makes boundaries implicit | Import from specific modules (`from remind_me_mcp.db import ...`) |
| Parallel imports via `asyncio.gather` in bulk import | Out of scope for this refactor; adds complexity and risk | Fix the ID mismatch bug only; concurrency improvement can come later |

---

## Feature Dependencies

Dependencies between table-stakes items (order matters for phased implementation):

```
Schema migration system (PRAGMA user_version)
  → Bug fix: capture_id column (requires ALTER TABLE)
  → SQL-level tag filtering (requires memory_tags junction table)

In-memory DB pytest fixture
  → All DB unit tests
  → FTS5 trigger correctness tests
  → Import extraction tests

Connection management singleton
  → asyncio.to_thread wrapping (needs stable connection reference across await boundaries)

Module split (db.py, embeddings.py, importer.py, tools.py, api.py)
  → All tests (tests import from specific modules)
  → DRY import_directory() extraction (natural seam once importer.py exists)
  → Normalize _make_id (isolated in db.py)

Bug fix: import embedding ID mismatch
  → No dependencies; self-contained change in importer.py
  → Prerequisite: module split (importer.py must exist)

Extract dashboard JSX
  → No code dependencies; file extraction only
  → Must update _build_dashboard_html() to load from file path
```

---

## MVP Recommendation

For the refactoring milestone, prioritize in this order:

**Phase 1 — Structure first (enables everything else):**
1. Module split: create `remind_me_mcp/` package with `db.py`, `embeddings.py`, `importer.py`, `tools.py`, `api.py`, `dashboard.py`, `server.py`, `__init__.py`
2. Update `pyproject.toml` entry point from `remind_me_mcp:mcp.run` to new location
3. Extract dashboard JSX (low-risk, self-contained)

**Phase 2 — Tests (validates structure is correct):**
4. In-memory DB pytest fixture in `conftest.py`
5. Unit tests: schema + FTS5 triggers, `_chunk_text`, `_extract_messages_from_json`, `_filter_messages`, `_parse_markdown_chat`
6. Integration tests: each MCP tool handler against in-memory DB
7. API tests: Starlette `TestClient` against HTTP routes

**Phase 3 — Quality and bug fixes (safe now that tests exist):**
8. `asyncio.to_thread` wrapping for sync embedding/DB calls
9. Connection management (singleton scoped to lifespan)
10. Schema migration with `PRAGMA user_version`
11. Bug fix: import embedding ID mismatch
12. Bug fix: `capture_id` column + indexed lookup
13. SQL-level tag filtering (junction table)
14. DRY: extract `import_directory()`
15. Normalize `_make_id` semantics
16. Docstrings on all public functions/classes

**Defer:**
- Batched reindex: medium benefit, low urgency for a personal tool
- `__all__` exports: nice to have, not blocking quality audit
- Version-pinned optional dependencies: do at the end to avoid churn during refactor

---

## Sources

- Direct codebase inspection: `/home/baileyrd/projects/remind_me/remind_me_mcp.py` (2,500 lines)
- `.planning/PROJECT.md` — requirements and constraints (HIGH confidence)
- `.planning/codebase/ARCHITECTURE.md` — layer analysis (HIGH confidence)
- `.planning/codebase/CONCERNS.md` — bugs and tech debt (HIGH confidence)
- `/home/baileyrd/projects/remind_me/pyproject.toml` — dependency and entry-point constraints (HIGH confidence)
- Python 3.11+ stdlib: `asyncio.to_thread`, `sqlite3`, `contextlib.asynccontextmanager` — standard patterns (HIGH confidence, training data corroborated by direct code inspection)
- pytest, pytest-asyncio, Starlette TestClient — established testing patterns for async Python servers (MEDIUM confidence — training data only; WebSearch unavailable to verify current versions)
