# Roadmap: Remind Me MCP — Full Refactor

## Overview

Transform a 2,500-line Python monolith into a well-structured, testable package in three dependency-driven phases. Phase 1 establishes the module skeleton (no behavior changes). Phase 2 builds the test suite against the clean interfaces. Phase 3 fixes bugs, enforces async safety, and completes the code quality audit — all protected by Phase 2 coverage.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [x] **Phase 1: Package Structure** - Split the monolith into modules, wire entry points, extract dashboard JSX, configure dev tooling
- [x] **Phase 2: Test Infrastructure** - Build the full pytest suite against Phase 1 module interfaces (completed 2026-02-24)
- [ ] **Phase 3: Quality and Bug Fixes** - Fix known bugs, enforce async safety, apply data-layer improvements, complete docstring coverage

## Phase Details

### Phase 1: Package Structure
**Goal**: The monolith is replaced by a properly organized `remind_me_mcp/` package that installs and runs identically to the current single-file version
**Depends on**: Nothing (first phase)
**Requirements**: ARCH-01, ARCH-02, ARCH-03, ARCH-04, ARCH-05, ARCH-06, QUAL-03, QUAL-04, QUAL-05, QUAL-06
**Success Criteria** (what must be TRUE):
  1. `pip install -e . && remind-me-mcp --help` works without error after the restructure
  2. All 13 MCP tools and 2 resources register correctly and respond to Claude with the same names and parameters as before
  3. The HTTP dashboard serves correctly when launched with the dashboard flag, with JSX loaded from `dashboard/App.jsx` instead of an embedded Python string
  4. No circular imports exist: `python -c "import remind_me_mcp"` exits cleanly
  5. ruff and mypy are runnable against the codebase via `pyproject.toml` configuration (zero configuration errors, even if lint warnings exist)
**Plans:** 3/3 plans complete

Plans:
- [x] 01-01-PLAN.md — Create package skeleton (config.py, models.py, formatting.py, db.py, embeddings.py)
- [x] 01-02-PLAN.md — Extract behavioral modules (importer.py, pid.py, server.py, tools.py, api.py, dashboard/)
- [x] 01-03-PLAN.md — Wire __init__.py, __main__.py, update pyproject.toml entry point, configure ruff/mypy/pytest

### Phase 2: Test Infrastructure
**Goal**: A pytest suite with full unit and integration coverage exists, written against Phase 1 module interfaces, providing the regression net required to safely change behavior in Phase 3
**Depends on**: Phase 1
**Requirements**: TEST-01, TEST-02, TEST-03, TEST-04, TEST-05, TEST-06
**Success Criteria** (what must be TRUE):
  1. `pytest` runs without collection errors and all tests pass
  2. Every pure-function module (importer parsers, chunker, formatting, models) has unit tests
  3. All 13 MCP tool handlers are covered by integration tests using in-memory SQLite (not mocks) — FTS5 triggers and SQL correctness are exercised
  4. All Starlette HTTP API routes have integration tests via httpx AsyncClient or TestClient
  5. A test that imports a chat file and then calls a search tool confirms end-to-end behavior without touching the developer's real `~/.remind-me/memory.db`
**Plans:** 3/4 plans executed

Plans:
- [ ] 02-01-PLAN.md — Shared pytest fixtures (in-memory db, mock embedder, memory factory, config isolation, smoke tests)
- [ ] 02-02-PLAN.md — Unit tests for pure-function modules (importer parsers, chunker, formatting, models, db utilities)
- [ ] 02-03-PLAN.md — Integration tests for all 13 MCP tool handlers and 2 resource handlers
- [ ] 02-04-PLAN.md — Integration tests for all Starlette HTTP API routes via TestClient

### Phase 3: Quality and Bug Fixes
**Goal**: The codebase passes a green audit on every CLAUDE.md design principle — async safety, robust error handling, DRY data layer, SQL-correct tag filtering, schema migration, full docstring coverage — while the two known bugs are fixed and verified by tests
**Depends on**: Phase 2
**Requirements**: ERRH-01, ERRH-02, ERRH-03, ASYN-01, ASYN-02, ASYN-03, ASYN-04, ASYN-05, BUGF-01, BUGF-02, DATA-01, DATA-02, DATA-03, DATA-04, QUAL-01, QUAL-02
**Success Criteria** (what must be TRUE):
  1. Importing a chat file then calling `remind_me_search` returns embedded results for the imported memories (BUGF-01 fixed: no ID mismatch)
  2. `remind_me_get_capture` returns the correct capture record via an indexed `capture_id` column lookup, not a LIKE-based JSON scan (BUGF-02 fixed)
  3. Searching memories filtered by tag returns correctly paginated results — a tag-filtered query with `limit=5` returns exactly 5 matches, not 5 pre-filter rows truncated by Python (DATA-02 fixed)
  4. No `asyncio` event loop blockage occurs under concurrent tool calls — `asyncio.gather` on multiple MCP tool invocations completes without `ProgrammingError` or event loop starvation
  5. Two separate processes opening the same database file simultaneously can both read and write without hanging (WAL mode + busy_timeout verified)
  6. `pydoc` on any public function or class in any module returns a non-empty docstring
**Plans**: TBD

Plans:
- [ ] 03-01: Schema migration system (PRAGMA user_version), capture_id column, memory_tags junction table
- [ ] 03-02: Fix BUGF-01 (import embedding ID mismatch) and BUGF-02 (capture_id lookup)
- [ ] 03-03: Async safety — asyncio.to_thread for embedding calls, connection singleton, thread-safety
- [ ] 03-04: Error handling — specific exception types, error propagation, user-facing tool messages
- [ ] 03-05: DRY import_directory(), _make_id normalization, docstrings, type hints completion

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2 → 3

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Package Structure | 3/3 | Complete    | 2026-02-24 |
| 2. Test Infrastructure | 3/4 | In Progress|  |
| 3. Quality and Bug Fixes | 0/5 | Not started | - |
