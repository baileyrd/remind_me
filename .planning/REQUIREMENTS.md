# Requirements: Remind Me MCP — Full Refactor

**Defined:** 2026-02-22
**Core Value:** Every design principle from CLAUDE.md passes a green audit without breaking existing functionality

## v1 Requirements

Requirements for the refactored release. Each maps to roadmap phases.

### Module Architecture

- [x] **ARCH-01**: Project is structured as a `remind_me_mcp/` package with separate modules for each concern (config, db, embeddings, models, formatting, importer, pid, server, tools, api, dashboard)
- [x] **ARCH-02**: `__init__.py` re-exports `mcp` from `server.py` so existing `pyproject.toml` entry point works unchanged
- [x] **ARCH-03**: `__main__.py` handles CLI argument parsing and mode dispatch (MCP stdio vs HTTP dashboard)
- [x] **ARCH-04**: Each module has its own logger via `logging.getLogger("remind_me_mcp.<module>")`
- [x] **ARCH-05**: Each module defines `__all__` to declare its explicit public surface
- [x] **ARCH-06**: No circular imports exist between any modules

### Testing

- [x] **TEST-01**: pytest test suite exists with unit tests for all pure-function modules (importer parsers, chunker, formatting, models)
- [x] **TEST-02**: Integration tests exist for all 13 MCP tool handlers using in-memory SQLite
- [x] **TEST-03**: Integration tests exist for all Starlette HTTP API routes via TestClient or httpx
- [x] **TEST-04**: `conftest.py` provides shared fixtures: in-memory SQLite db with schema, mock embedder, sample memory factory
- [x] **TEST-05**: All async tests run correctly via pytest-asyncio with `asyncio_mode = "auto"`
- [x] **TEST-06**: Tests use in-memory SQLite (not mocks) for database operations to validate FTS5 triggers and SQL correctness

### Error Handling

- [ ] **ERRH-01**: No exceptions are silently swallowed — all caught exceptions are logged with appropriate level before handling
- [ ] **ERRH-02**: Error handling uses specific exception types instead of bare `except Exception` where the failure mode is known
- [ ] **ERRH-03**: MCP tool handlers return clear, user-facing error messages rather than opaque sentinel values

### Async & Performance

- [ ] **ASYN-01**: All sync embedding computations in async MCP tool handlers are wrapped with `asyncio.to_thread`
- [ ] **ASYN-02**: DB connection is managed as a lazy lifespan-scoped singleton instead of opening a new connection per call
- [ ] **ASYN-03**: SQLite thread-safety is handled correctly — no `ProgrammingError` under concurrent async operations
- [ ] **ASYN-04**: SQLite database opens in WAL journal mode (`PRAGMA journal_mode=WAL`) to support concurrent multi-process access (e.g., Claude Code + Claude Desktop running simultaneously)
- [ ] **ASYN-05**: Database connection sets `busy_timeout` (e.g., 5000ms) so brief lock contention retries gracefully instead of hanging or erroring

### Bug Fixes

- [ ] **BUGF-01**: Imported memories are embedded correctly at import time (fix ID mismatch in `import_chat_file` by collecting `(mem_id, chunk)` pairs)
- [ ] **BUGF-02**: `remind_me_get_capture` uses a proper indexed `capture_id` column instead of fragile LIKE-based JSON metadata search

### Data Layer

- [x] **DATA-01**: Schema migration system using `PRAGMA user_version` supports safe column additions and table changes on existing databases
- [x] **DATA-02**: Tag filtering happens in SQL via a `memory_tags` junction table, not post-fetch in Python, so pagination works correctly with tag filters
- [ ] **DATA-03**: A single `import_directory()` function is shared between the MCP tool handler and the HTTP API handler (DRY)
- [ ] **DATA-04**: `_make_id` semantics are normalized — either truly deterministic (content-hash only) or explicitly documented as non-deterministic with an appropriate name

### Code Quality

- [ ] **QUAL-01**: All public functions and classes have docstrings
- [ ] **QUAL-02**: All function signatures have complete type hints
- [x] **QUAL-03**: Dashboard JSX is extracted to a separate `App.jsx` file in the `dashboard/` directory (no longer embedded as Python string)
- [x] **QUAL-04**: ruff is configured in `pyproject.toml` for linting and formatting
- [x] **QUAL-05**: mypy is configured in `pyproject.toml` for type checking
- [x] **QUAL-06**: `pyproject.toml` has test configuration (pytest settings, asyncio mode)

## v2 Requirements

Deferred to future release. Tracked but not in current roadmap.

### Performance

- **PERF-01**: `remind_me_reindex` processes memories in batches of 100-500 rows instead of loading all into memory
- **PERF-02**: Bulk directory import uses concurrent file processing via thread pool

### Security

- **SECR-01**: CORS locked down to localhost origins only
- **SECR-02**: Basic API token authentication on HTTP dashboard
- **SECR-03**: Import path restricted to configured allowed directories

### Infrastructure

- **INFR-01**: CI/CD pipeline with automated test runs
- **INFR-02**: Coverage enforcement gate (minimum threshold)
- **INFR-03**: Version-pinned optional dependencies

## Out of Scope

| Feature | Reason |
|---------|--------|
| New MCP tools or parameter changes | Clients depend on existing interface — refactor only |
| Dashboard build step (Vite/esbuild) | PROJECT.md constraint — keep Babel standalone |
| Multiple PyPI packages | Single `pip install` constraint preserved |
| `aiosqlite` or ORM | Correct fix is `asyncio.to_thread` + raw `sqlite3` |
| PostgreSQL migration | Out of scope per PROJECT.md |
| Node.js build tooling | Unnecessary complexity for simple dashboard |
| pre-commit hooks | Disruptive during active refactor churn |

## Traceability

Which phases cover which requirements. Updated during roadmap creation.

| Requirement | Phase | Status |
|-------------|-------|--------|
| ARCH-01 | Phase 1 | Complete (01-01) |
| ARCH-02 | Phase 1 | Complete (01-03) |
| ARCH-03 | Phase 1 | Complete (01-03) |
| ARCH-04 | Phase 1 | Complete (01-01) |
| ARCH-05 | Phase 1 | Complete (01-01) |
| ARCH-06 | Phase 1 | Complete (01-03) |
| TEST-01 | Phase 2 | Complete |
| TEST-02 | Phase 2 | Complete |
| TEST-03 | Phase 2 | Complete |
| TEST-04 | Phase 2 | Complete |
| TEST-05 | Phase 2 | Complete |
| TEST-06 | Phase 2 | Complete |
| ERRH-01 | Phase 3 | Pending |
| ERRH-02 | Phase 3 | Pending |
| ERRH-03 | Phase 3 | Pending |
| ASYN-01 | Phase 3 | Pending |
| ASYN-02 | Phase 3 | Pending |
| ASYN-03 | Phase 3 | Pending |
| BUGF-01 | Phase 3 | Pending |
| BUGF-02 | Phase 3 | Pending |
| DATA-01 | Phase 3 | Pending |
| DATA-02 | Phase 3 | Pending |
| DATA-03 | Phase 3 | Pending |
| DATA-04 | Phase 3 | Pending |
| QUAL-01 | Phase 3 | Pending |
| QUAL-02 | Phase 3 | Pending |
| QUAL-03 | Phase 1 | Complete |
| QUAL-04 | Phase 1 | Complete (01-03) |
| QUAL-05 | Phase 1 | Complete (01-03) |
| QUAL-06 | Phase 1 | Complete (01-03) |
| ASYN-04 | Phase 3 | Pending |
| ASYN-05 | Phase 3 | Pending |

**Coverage:**
- v1 requirements: 32 total
- Mapped to phases: 32
- Unmapped: 0

---
*Requirements defined: 2026-02-22*
*Last updated: 2026-02-24 after 01-03 completion (ARCH-02, ARCH-03, ARCH-06, QUAL-04, QUAL-05, QUAL-06 marked complete)*
