# Remind Me MCP — Full Refactor

## What This Is

A full architectural refactor of Remind Me MCP, a personal memory server for Claude that persists facts, preferences, and conversations across sessions. The goal is to bring the existing working codebase into alignment with our software design principles — modular architecture, comprehensive tests, robust error handling, DRY code, and async-first patterns — while preserving all current functionality and the simple single-package install experience.

## Core Value

Every design principle from CLAUDE.md passes a green audit — modularity, tests, error handling, DRY, extensibility, and async-first — without breaking existing functionality.

## Requirements

### Validated

- ✓ MCP tool-based memory CRUD (add, search, list, get, update, delete) — existing
- ✓ Hybrid search (FTS5 keyword + semantic vector similarity) — existing
- ✓ Chat import from JSON, JSONL, Markdown formats — existing
- ✓ Auto-capture of conversations (dialog + summary linked by capture_id) — existing
- ✓ Optional HTTP dashboard with React UI for browsing/editing memories — existing
- ✓ Optional semantic search via ONNX embeddings + sqlite-vec — existing
- ✓ Pydantic input validation on all MCP tools — existing
- ✓ Environment-based configuration (no magic globals) — existing
- ✓ Graceful degradation when optional dependencies are missing — existing

### Active

- [ ] Modular project structure with clear separation of concerns (db, tools, api, embeddings, importer, dashboard)
- [ ] Comprehensive test suite (unit + integration tests for all modules)
- [ ] Robust error handling — no silent exception swallowing, proper error propagation
- [ ] DRY — eliminate duplicated directory import logic and any other repetition
- [ ] Async-first — wrap sync embedding/DB calls with asyncio.to_thread
- [ ] Multi-process concurrency — WAL mode + busy_timeout for simultaneous Claude Code + Desktop access
- [ ] All public functions and classes have docstrings
- [ ] Extensible module design — adding a new tool/feature doesn't require editing a monolithic file
- [ ] Fix known bugs: import embedding ID mismatch, LIKE-based capture_id lookup
- [ ] Extract dashboard JSX into separate files (keep Babel standalone, no build step)
- [ ] Schema migration system (PRAGMA user_version)
- [ ] Tag filtering in SQL (junction table) instead of post-fetch Python filtering
- [ ] Connection management — pool or singleton instead of connect-per-call
- [ ] Normalize _make_id semantics (deterministic or explicitly non-deterministic)

### Out of Scope

- Security hardening (CORS lockdown, API auth, import path restrictions) — deferred to separate pass
- Build tooling for dashboard (Vite/esbuild) — keeping Babel standalone for simplicity
- Splitting into separate installable packages — single package install preserved
- CI/CD pipeline setup — focus on code quality, not infrastructure
- New features — this is a refactor, not a feature release
- Performance optimization beyond fixing sync-in-async — no premature optimization

## Context

- Codebase is currently a single 2,500-line Python file (`remind_me_mcp.py`) containing all layers
- Zero existing tests — the refactor will establish testing patterns from scratch
- The project is a working MCP server used daily — refactor must not break existing functionality
- Dashboard is a React app embedded as a Python string, transpiled client-side by Babel standalone
- A separate `remind_me_dashboard.jsx` file exists as a reference/editing copy
- Known bug: imported memories never get embedded due to ID mismatch in `import_chat_file`
- Known bug: `remind_me_get_capture` uses fragile LIKE-based JSON metadata search
- Tag filtering happens in Python after SQL fetch, breaking pagination
- DB connections opened fresh on every call with schema check overhead
- Multi-process concurrency issue: Claude Code + Claude Desktop running simultaneously causes hanging due to SQLite default journal mode file-level locking
- Codebase map available at `.planning/codebase/` with detailed analysis

## Constraints

- **Packaging**: Must remain a single `pip install`-able package — internal module split only
- **Compatibility**: All existing MCP tool names and parameters must remain unchanged (clients depend on them)
- **Dashboard**: Extract JSX to separate files but keep Babel standalone transpilation (no build step)
- **Data**: Must be compatible with existing `~/.remind-me/memory.db` databases (migration, not recreation)
- **Python**: Requires Python 3.11+ (existing constraint)

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Refactor and test in parallel | Building tests on the new clean structure avoids writing tests for code that will change | — Pending |
| Single package, multiple modules | Preserves simple install while enabling separation of concerns | — Pending |
| Keep Babel standalone for dashboard | Avoids adding Node.js build tooling dependency for a simple dashboard | — Pending |
| Fix bugs during refactor | Bugs surface naturally when restructuring the affected code | — Pending |
| Defer security hardening | Separate concern — mixing security changes with structural refactor increases risk | — Pending |
| SQLite WAL mode over PostgreSQL | WAL fixes multi-process concurrency without adding dependencies; PostgreSQL is overkill for a personal tool | — Pending |

---
*Last updated: 2026-02-23 after scope expansion (WAL concurrency fix)*
