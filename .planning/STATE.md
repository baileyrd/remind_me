# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-02-22)

**Core value:** Every design principle from CLAUDE.md passes a green audit without breaking existing functionality
**Current focus:** Phase 2 — Test Infrastructure

## Current Position

Phase: 2 of 3 (Test Infrastructure)
Plan: 4 of 4 in current phase
Status: In progress
Last activity: 2026-02-24 — Completed 02-04 (API integration tests)

Progress: [████████░░] 73%

## Performance Metrics

**Velocity:**
- Total plans completed: 4
- Average duration: 4min
- Total execution time: 0.25 hours

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| 01-package-structure | 3/3 | 13min | 4min |
| 02-test-infrastructure | 4/4 | 10min | 2.5min |

**Recent Trend:**
- Last 5 plans: 01-01 (3min), 01-02 (8min), 01-03 (2min), 02-01 (2min), 02-04 (2min)
- Trend: stable

*Updated after each plan completion*
| Phase 02-test-infrastructure P02 | 2 | 2 tasks | 4 files |

## Accumulated Context

### Decisions

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions affecting current work:

- [Init]: Refactor and test in parallel — build modules first (Phase 1), then tests (Phase 2), then fixes (Phase 3)
- [Init]: Single package, multiple modules — preserves simple install while enabling separation of concerns
- [Init]: Keep Babel standalone for dashboard — avoids Node.js build tooling dependency
- [Init]: Fix bugs during Phase 3, after test coverage exists as a safety net
- [01-01]: Pure extraction only — no logic changes in 01-01; all signatures and docstrings preserved verbatim
- [01-01]: SERVE_UI and UI_PORT extracted to config.py (not HTTP layer) because they are environment configuration
- [01-01]: uv venv created for project — uv pip install -e '.[semantic]' used for dependency setup
- [01-02]: server.py must NOT import tools.py — tools.py imports mcp from server to prevent circular imports
- [01-02]: Lazy Starlette imports inside _build_api_app() — prevents heavy web framework load in stdio-only mode
- [01-02]: JSX loaded via Path(__file__).parent / 'dashboard' / 'App.jsx' at runtime — simpler than importlib.resources
- [01-02]: Inline Babel-compatible JSX extracted from _get_dashboard_script() — NOT the ES module reference file
- [01-03]: __init__.py imports tools module as side effect — ensures @mcp.tool decorators fire before mcp.run() via entry point
- [01-03]: Entry point keep as remind_me_mcp:mcp.run — FastMCP handles run loop; __main__.py for python -m usage
- [01-03]: Monolith renamed to remind_me_mcp_original.py — eliminates Python import ambiguity with package directory
- [02-01]: Session-scoped monkeypatch uses pytest.MonkeyPatch() directly — function-scoped monkeypatch fixture cannot be injected into session-scoped fixtures
- [02-01]: FakeEmbedder seeds np.random.default_rng on hash(text) for deterministic per-text vectors without ML model dependency
- [02-01]: db_conn monkeypatches both remind_me_mcp.db._get_db and remind_me_mcp.api._get_db since api.py imports _get_db directly
- [02-02]: Direct import of private pure functions — no MCP server context needed; all 75 tests run in 0.04s
- [02-02]: FTS5 trigger tests use distinct unique words per test to avoid cross-test interference without requiring separate db_conn instances
- [02-03]: db_conn fixture must patch remind_me_mcp.tools._get_db and remind_me_mcp.importer._get_db — both use 'from remind_me_mcp.db import _get_db' creating separate bindings not covered by the module attribute patch
- [02-03]: server_status test monkeypatches remind_me_mcp.tools.get_server_status (not pid module) because tools.py imports it directly, creating a local binding
- [02-04]: db_conn fixture uses check_same_thread=False — Starlette TestClient runs async handlers in a worker thread separate from pytest main thread
- [02-04]: client fixture patches remind_me_mcp.importer._get_db directly because importer uses 'from ... import _get_db' local binding not affected by module attribute patch

### Pending Todos

None yet.

### Blockers/Concerns

- [Research]: asyncio.to_thread + SQLite threading interaction needs prototype test before finalizing pattern (see SUMMARY.md gaps)
- [Research]: ruff ASYNC rule codes may have changed since August 2025 cutoff — verify before configuring pyproject.toml
- [Research]: FTS5 memories_fts rebuild behavior during migration needs validation against actual SQLite behavior

## Session Continuity

Last session: 2026-02-24
Stopped at: Completed 02-03-PLAN.md — integration tests for all 13 MCP tool handlers
Resume file: None
