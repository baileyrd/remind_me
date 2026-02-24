# Remind Me MCP

## What This Is

A personal memory server for Claude that persists facts, preferences, and conversations across sessions. Works with Claude.ai, Claude Code, and Claude Desktop via MCP (Model Context Protocol). Features hybrid search (FTS5 + semantic vectors), auto-capture of conversations, chat import, and an optional web dashboard. Structured as a well-tested 10-module Python package with async safety and schema migration support.

## Core Value

Persistent, searchable memory that works seamlessly across all Claude interfaces — with a codebase that's modular, tested, and maintainable.

## Requirements

### Validated

- ✓ MCP tool-based memory CRUD (add, search, list, get, update, delete) — v1.0
- ✓ Hybrid search (FTS5 keyword + semantic vector similarity) — v1.0
- ✓ Chat import from JSON, JSONL, Markdown formats — v1.0
- ✓ Auto-capture of conversations (dialog + summary linked by capture_id) — v1.0
- ✓ Optional HTTP dashboard with React UI for browsing/editing memories — v1.0
- ✓ Optional semantic search via ONNX embeddings + sqlite-vec — v1.0
- ✓ Pydantic input validation on all MCP tools — v1.0
- ✓ Environment-based configuration (no magic globals) — v1.0
- ✓ Graceful degradation when optional dependencies are missing — v1.0
- ✓ Modular project structure with clear separation of concerns — v1.0
- ✓ Comprehensive test suite (172 unit + integration tests) — v1.0
- ✓ Robust error handling with specific exception types — v1.0
- ✓ DRY shared import_directory() function — v1.0
- ✓ Async-first: asyncio.to_thread for embeddings, WAL + busy_timeout for concurrency — v1.0
- ✓ Full docstring and type hint coverage — v1.0
- ✓ Schema migration system via PRAGMA user_version — v1.0
- ✓ Tag filtering in SQL via memory_tags junction table — v1.0
- ✓ DB singleton connection (lazy, lifespan-scoped) — v1.0
- ✓ Both known bugs fixed (import embedding ID mismatch, capture_id LIKE scan) — v1.0
- ✓ Dashboard JSX extracted to separate file — v1.0
- ✓ Self-update feature with background check and CLI flags — post-v1.0

### Active

- [ ] Security hardening (CORS lockdown, API auth, import path restrictions)
- [ ] CI/CD pipeline with automated test runs and coverage enforcement
- [ ] Performance: batch reindex, concurrent file processing
- [ ] API path embedding parity (REST API memories lack semantic embeddings)
- [ ] Clean up ruff warnings (unused imports, type annotations)
- [ ] Remove original monolith file (remind_me_mcp_original.py)

### Out of Scope

- Build tooling for dashboard (Vite/esbuild) — keeping Babel standalone for simplicity
- Splitting into separate installable packages — single package install preserved
- PostgreSQL migration — SQLite with WAL is sufficient for personal use
- Node.js build tooling — unnecessary complexity for simple dashboard

## Context

Shipped v1.0 with 7,215 lines of Python (3,680 package + 3,535 tests).
Tech stack: Python 3.11+, FastMCP, SQLite (WAL), Pydantic, Starlette, ONNX Runtime (optional), React/Babel (dashboard).
10-module package: config, db, embeddings, models, formatting, importer, pid, server, tools, api, plus dashboard/ subpackage.
190 tests passing (172 v1.0 + 18 updater tests).
15 MCP tools + 2 resource handlers registered.
Schema at version 2 (PRAGMA user_version) with migration support.
Self-update feature added post-v1.0 with `--version`, `--check-update`, `--update` CLI flags.

## Constraints

- **Packaging**: Must remain a single `pip install`-able package — internal module split only
- **Compatibility**: All existing MCP tool names and parameters must remain unchanged (clients depend on them)
- **Dashboard**: Keep Babel standalone transpilation (no build step)
- **Data**: Must be compatible with existing `~/.remind-me/memory.db` databases (migration, not recreation)
- **Python**: Requires Python 3.11+

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Refactor and test in parallel | Building tests on the new clean structure avoids writing tests for code that will change | ✓ Good — clean interfaces made test writing fast |
| Single package, multiple modules | Preserves simple install while enabling separation of concerns | ✓ Good — 10 modules, zero circular imports |
| Keep Babel standalone for dashboard | Avoids adding Node.js build tooling dependency for a simple dashboard | ✓ Good — dashboard works, no build step needed |
| Fix bugs during refactor | Bugs surface naturally when restructuring the affected code | ✓ Good — both bugs found and fixed with tests |
| Defer security hardening | Separate concern — mixing security changes with structural refactor increases risk | — Pending (next milestone candidate) |
| SQLite WAL mode over PostgreSQL | WAL fixes multi-process concurrency without adding dependencies | ✓ Good — concurrent access verified by tests |
| Entry point via __main__:main | CLI flags (--version, --check-update, --update) need argparse | ✓ Good — replaced mcp.run entry point |

---
*Last updated: 2026-02-24 after v1.0 milestone*
