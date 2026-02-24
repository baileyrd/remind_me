---
phase: 01-package-structure
plan: "03"
subsystem: api
tags: [python, fastmcp, argparse, ruff, mypy, pytest, entry-point, modular-package]

# Dependency graph
requires:
  - phase: 01-package-structure
    provides: "server.py, tools.py, api.py, pid.py, config.py from plans 01-01 and 01-02"
provides:
  - remind_me_mcp/__init__.py — re-exports mcp from server.py, triggers tool registration via tools import
  - remind_me_mcp/__main__.py — argparse CLI with stdio/ui-server/status modes
  - pyproject.toml — hatchling wheel target, ruff config, mypy config, pytest config
  - remind_me_mcp_original.py — original monolith renamed to prevent import shadowing
affects:
  - 02-test-coverage (pytest config already in place; tests can run with asyncio_mode=auto)
  - 03-bug-fixes (ruff and mypy configs define lint/type baseline to improve)

# Tech tracking
tech-stack:
  added: [ruff, mypy, pytest-asyncio]
  patterns:
    - "__init__.py as tool-registration trigger: imports tools module as side effect to fire @mcp.tool decorators"
    - "Entry point pattern: pyproject.toml scripts -> remind_me_mcp:mcp.run (FastMCP direct run)"
    - "__main__.py as CLI dispatch: argparse parser with --serve-ui / --status / default stdio modes"
    - "Rename monolith to *_original.py to prevent Python import shadowing by same-named .py file"

key-files:
  created:
    - remind_me_mcp/__main__.py
    - remind_me_mcp_original.py
  modified:
    - remind_me_mcp/__init__.py
    - pyproject.toml

key-decisions:
  - "__init__.py imports tools module as side effect — ensures @mcp.tool decorators fire before mcp.run() via entry point"
  - "Entry point keep as remind_me_mcp:mcp.run — FastMCP handles the run loop directly; __main__.py is for python -m usage"
  - "Monolith renamed to remind_me_mcp_original.py — Python package directory takes import priority but same-name .py would cause confusion; rename eliminates ambiguity"
  - "ruff ASYNC rules excluded per STATE.md blocker — rule codes changed after August 2025, verify in Phase 3"

patterns-established:
  - "Tool registration via __init__.py side-effect import — never register tools lazily, always at package import time"
  - "CLI modes: stdio (default), --serve-ui (uvicorn), --status (health check + exit)"

requirements-completed: [ARCH-02, ARCH-03, ARCH-06, QUAL-04, QUAL-05, QUAL-06]

# Metrics
duration: 2min
completed: 2026-02-24
---

# Phase 1 Plan 3: Entry Point Wiring Summary

**Package fully installed with `pip install -e .` — FastMCP entry point, argparse CLI, and ruff/mypy/pytest tooling configured; monolith renamed to prevent Python import shadowing**

## Performance

- **Duration:** 2 min
- **Started:** 2026-02-24T03:37:19Z
- **Completed:** 2026-02-24T03:39:18Z
- **Tasks:** 2
- **Files modified:** 4

## Accomplishments

- Updated `__init__.py` to re-export `mcp` from `server.py` and trigger `@mcp.tool` registration by importing `tools` module as a side effect
- Created `__main__.py` with full argparse CLI — three execution modes: MCP stdio (default), `--serve-ui` (Starlette + uvicorn), and `--status` (health check)
- Updated `pyproject.toml` with hatchling wheel target pointing at `remind_me_mcp/` package directory, plus `[tool.ruff]`, `[tool.mypy]`, and `[tool.pytest.ini_options]` sections
- Renamed `remind_me_mcp.py` to `remind_me_mcp_original.py` to prevent Python from resolving `import remind_me_mcp` to the old monolith file instead of the package directory

## Task Commits

Each task was committed atomically:

1. **Task 1: Create __init__.py, __main__.py, and update pyproject.toml entry point** - `e6f2301` (feat)
2. **Task 2: Configure ruff, mypy, and pytest in pyproject.toml, verify no circular imports** - `18291f6` (feat)

**Plan metadata:** _(docs commit below)_

## Files Created/Modified

- `remind_me_mcp/__init__.py` — Re-exports `mcp` from `server.py`; imports `remind_me_mcp.tools` to trigger `@mcp.tool` decorator registration at package import time
- `remind_me_mcp/__main__.py` — Argparse CLI: `--serve-ui` (uvicorn dashboard), `--status` (PID health check), default (MCP stdio `mcp.run()`)
- `pyproject.toml` — Added `[tool.hatch.build.targets.wheel]`, `[tool.ruff]`, `[tool.mypy]`, `[tool.pytest.ini_options]`
- `remind_me_mcp_original.py` — Renamed from `remind_me_mcp.py` (original 2,495-line monolith, kept for reference)

## Decisions Made

- `__init__.py` imports `remind_me_mcp.tools` as a side effect to ensure `@mcp.tool` decorators fire before `mcp.run()` is called via the `remind_me_mcp:mcp.run` entry point.
- The `remind-me-mcp` entry point uses `mcp.run` directly (not `__main__.main`) because FastMCP's `run()` handles the MCP protocol lifecycle. The `__main__.py` extends this with a CLI for dashboard mode and status checks.
- Monolith file renamed to `remind_me_mcp_original.py` — Python would prefer a same-named `.py` file over a package directory in some edge cases; renaming eliminates all ambiguity.
- ruff ASYNC rules not configured per STATE.md blocker (rule codes changed after August 2025 cutoff).

## Deviations from Plan

None — plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None — no external service configuration required.

## Next Phase Readiness

- Phase 1 complete: all 10 modules import cleanly, package installs and runs
- `python -m remind_me_mcp --help` shows CLI usage
- `from remind_me_mcp import mcp` returns FastMCP instance with all 13 tools registered
- `ruff check remind_me_mcp/` runs (28 lint warnings — Phase 3 will address)
- `mypy remind_me_mcp/` runs (9 type errors — Phase 3 QUAL-02 will tighten)
- Phase 2 (test coverage) can begin: pytest is configured with `asyncio_mode = "auto"` and `testpaths = ["tests"]`

## Self-Check: PASSED
