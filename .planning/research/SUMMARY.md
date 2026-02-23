# Project Research Summary

**Project:** Remind Me MCP — Monolith-to-Package Refactor
**Domain:** Python async MCP server modularization
**Researched:** 2026-02-22
**Confidence:** HIGH

## Executive Summary

This is a structural refactoring project, not a feature project. The goal is to transform a working 2,500-line single-file Python MCP server (`remind_me_mcp.py`) into a well-structured, testable Python package (`remind_me_mcp/`) without changing any user-visible behavior. The research is grounded almost entirely in direct codebase analysis and well-established Python packaging patterns — not speculative domain research — which gives all four research areas HIGH confidence.

The recommended approach is dependency-driven: build modules in topological order starting from `config.py` (no dependencies), then `db.py`, `embeddings.py`, `models.py`, and up through `tools.py` and `api.py`. Tests are written incrementally alongside each module extraction rather than in a single batch at the end. This sequencing prevents the most dangerous pitfall: writing tests against the monolith's internal interfaces before the modular structure is defined, which creates migration overhead that erodes test suite confidence. The test stack (pytest + pytest-asyncio + httpx + in-memory SQLite) is fully determined and carries no technology uncertainty.

The key risks are all operational, not architectural: circular imports from naive module splitting, entry point path breakage when the `pyproject.toml` script reference is not updated atomically with the package restructure, SQLite thread-safety violations from incorrect `asyncio.to_thread` placement, and schema migration failures on existing user databases when `IF NOT EXISTS` guards are assumed sufficient for column additions. Every risk has a concrete prevention pattern documented in PITFALLS.md. The schema migration system (`PRAGMA user_version`) must be the first deliverable of any phase that touches schema, because existing user data is at stake.

## Key Findings

### Recommended Stack

The dev tooling stack is fully determined with no significant alternatives in contention. ruff handles linting and formatting in a single binary, replacing flake8, black, and isort entirely. mypy provides static type checking with a permissive starting configuration that tightens per-module as refactoring progresses. pytest with pytest-asyncio (auto mode) is the unambiguous test runner choice for a pure-asyncio codebase. All of these are already in the dependency tree or trivially addable. The one area requiring version verification before committing is exact version pins — ruff evolves rapidly, and the cutoff date for research was August 2025.

See `.planning/research/STACK.md` for complete rationale and configuration templates.

**Core technologies:**
- **pytest >= 8.0**: Test runner — industry standard with the fixture system required for clean async + DB test setup
- **pytest-asyncio >= 0.23** with `asyncio_mode = "auto"`: Async test support — eliminates `@pytest.mark.asyncio` boilerplate across the entire test suite; pure-asyncio project has no reason to use anyio
- **pytest-cov >= 5.0**: Coverage measurement — standard integration; run locally, not enforced in CI (CI is out of scope)
- **pytest-mock >= 3.12**: Mock fixture — cleaner than `unittest.mock.patch` context managers; auto-resets between tests
- **ruff >= 0.4**: Linting and formatting — replaces flake8/black/isort in one binary; ASYNC rule set detects sync calls inside async handlers, which is self-enforcing for the async-first refactor goal
- **mypy >= 1.10**: Type checking — `strict = false` starting point, tighten per-module; `ignore_missing_imports = true` required for mcp, sqlite-vec, onnxruntime stubs
- **httpx >= 0.27**: HTTP integration testing — already a project dependency; use `httpx.AsyncClient` with `ASGITransport` for async Starlette tests
- **uv >= 0.4**: Package manager — already in use; add `uv.lock` for reproducible installs during refactor
- **hatchling >= 1.24**: Build backend — already in use; no migration needed

**What NOT to install:** flake8, black, isort, bandit, pylint, pre-commit (disruptive during active refactor churn), tox, nose.

### Expected Features

This is a refactoring project. "Features" are structural and quality capabilities. Research identified 15 table-stakes items (gaps make the project unsafe to extend), 10 differentiators (improvements beyond minimum clean structure), and a clear list of anti-features to avoid.

See `.planning/research/FEATURES.md` for full lists with complexity ratings and dependencies.

**Must have (table stakes):**
- Module split into `config.py`, `db.py`, `embeddings.py`, `models.py`, `formatting.py`, `importer.py`, `pid.py`, `server.py`, `tools.py`, `api.py`, `dashboard/` — the foundation that enables everything else
- `__init__.py` with stable public API (`mcp` re-export only)
- pytest test suite (unit + integration) — zero tests currently is a blocker for safe refactoring
- In-memory SQLite pytest fixtures — every DB test needs isolation; tests must use real SQLite, not mocks
- `asyncio.to_thread` wrapping for all sync DB/embedding calls in async handlers — fixes event loop blockage
- Connection management singleton (lazy, lifespan-scoped) — replaces connect-per-call with schema check
- Schema migration with `PRAGMA user_version` — required for column additions on existing user databases
- Bug fix: import embedding ID mismatch (collect `(mem_id, chunk)` pairs; use them directly for embedding pass)
- Bug fix: `remind_me_get_capture` LIKE lookup (requires `capture_id` column via migration)
- SQL-level tag filtering via `memory_tags` junction table (fixes broken pagination)
- DRY: single `import_directory()` function (replaces duplicate MCP tool + HTTP handler implementations)
- Docstrings on all public functions/classes — CLAUDE.md requirement; ruff D rules enforce this
- Type hints on all signatures — CLAUDE.md requirement; mostly complete, fill gaps

**Should have (differentiators):**
- `conftest.py` shared fixtures (db, mock embedder, sample memory factory)
- Mock embedder fixture — decouples unit tests from ONNX model download; CI-safe
- Per-module loggers with consistent naming (`logging.getLogger("remind_me_mcp.db")`, etc.)
- `__all__` on each module — defines explicit public surface
- `pyproject.toml` test configuration block

**Defer to later:**
- Batched reindex (100-row pages) — medium benefit, low urgency for personal tool
- Version-pinned optional dependencies — do after refactor stabilizes to avoid churn
- `pytest-cov` enforcement gate — CI/CD is out of scope per PROJECT.md
- Security hardening (CORS, API auth) — explicitly deferred in PROJECT.md
- PostgreSQL migration — explicitly deferred in PROJECT.md

**Anti-features (explicitly prohibited):**
- New user-facing MCP tools or parameter changes — out of scope; clients depend on existing interface
- Build step for dashboard (Vite, esbuild) — PROJECT.md requires Babel standalone only
- Split into multiple PyPI packages — single `pip install` constraint preserved
- `aiosqlite` or ORM — correct fix is `asyncio.to_thread`; keep raw `sqlite3`

### Architecture Approach

The package replaces the single file: `remind_me_mcp.py` becomes `remind_me_mcp/` directory with `__init__.py`. The pyproject.toml entry point (`remind-me-mcp = "remind_me_mcp:mcp.run"`) continues to work unchanged because `__init__.py` re-exports `mcp` from `server.py`. The FastMCP instance is created in `server.py`; `tools.py` imports it to register decorators; `__init__.py` imports `tools` for side effects after importing `server` — this is the correct pattern to avoid the server/tools circular import. All external behavior is preserved: same tool names, same parameters, same MCP protocol.

See `.planning/research/ARCHITECTURE.md` for the full dependency graph, data flow diagrams, and 13-step build order.

**Major components:**
1. **`config.py`** — all env-var constants and path resolution; no project-internal imports; foundation for every other module
2. **`db.py`** — SQLite connection singleton, schema DDL, `PRAGMA user_version` migration system, FTS5, CRUD helpers, `memory_tags` junction table
3. **`embeddings.py`** — `_Embedder` class, lazy model loading, `embed_one()`, `_get_embedder()` singleton factory, `_embed_and_store()`, `_semantic_search()`; all optional-dep imports stay lazy inside `_ensure_loaded()`
4. **`models.py`** — all Pydantic `BaseModel` subclasses and `ResponseFormat` enum; no project-internal imports
5. **`formatting.py`** — `_fmt_memory_md()`, `_fmt_memories()`; pure functions, no I/O
6. **`importer.py`** — `import_chat_file()`, `import_directory()` (extracted shared function), `_chunk_text()`, all parsers; fixes embedding ID mismatch bug
7. **`pid.py`** — PID file management, server status detection; depends only on `config.py`
8. **`server.py`** — FastMCP instance creation, `app_lifespan` async context manager; owns DB connection lifecycle
9. **`tools.py`** — all 13 `@mcp.tool()` handlers and 2 `@mcp.resource()` definitions; all sync DB/embed calls wrapped in `asyncio.to_thread`
10. **`api.py`** — Starlette app builder, all REST route handlers; Starlette imports remain lazy inside `_build_api_app()`
11. **`dashboard/`** — `html.py` (HTML wrapper assembly) + `App.jsx` (React source, moved from monolith string); no build step
12. **`__init__.py` + `__main__.py`** — entry point wiring; exports only `mcp`

### Critical Pitfalls

1. **Circular imports from naive module splitting** — Map all dependencies explicitly before writing any import statements. Use the topological build order (config → db → embeddings → models → importer → tools → api). Extract shared utilities to `utils.py` (no project imports). Use `TYPE_CHECKING` guard for type-only imports. Detection: `ImportError: cannot import name X from partially initialized module Y` at startup.

2. **Module-level constants not patchable by `monkeypatch.setenv`** — `MEMORY_DIR`, `DB_PATH`, `PID_FILE` are computed at import time. Tests that call `monkeypatch.setenv("REMIND_ME_MCP_DIR", ...)` after import hit stale values and silently write to `~/.remind-me/memory.db` (the developer's real database). Fix: convert constants to lazy functions or a `Config` dataclass instantiated at runtime. Must be solved before writing any integration test.

3. **Entry point path breaks atomically with module split** — `pyproject.toml` script reference `remind_me_mcp:mcp.run` must be updated in the same commit that moves the `mcp` instance. The error (`AttributeError: module 'remind_me_mcp' has no attribute 'mcp'`) only surfaces at runtime. Smoke test: `pip install -e . && remind-me-mcp --help` after every structural commit.

4. **Schema migration `IF NOT EXISTS` insufficient for column additions** — Adding `capture_id TEXT` or creating `memory_tags` junction table requires `ALTER TABLE`, which is not handled by `CREATE TABLE IF NOT EXISTS`. Existing user databases hit `OperationalError: table memories has no column named capture_id`. The `PRAGMA user_version` migration system must be the first deliverable of any phase that touches schema.

5. **SQLite thread-safety violation from incorrect `asyncio.to_thread` placement** — Wrapping `_get_db()` in `to_thread` then using the returned connection on the event loop thread triggers `ProgrammingError: SQLite objects created in a thread can only be used in that same thread`. Correct pattern: wrap only CPU-bound embedding computation in `to_thread`; either open the connection with `check_same_thread=False` explicitly, or keep the connection on a single thread. Write async concurrency tests with `asyncio.gather` to surface this during the async-wrapping phase.

## Implications for Roadmap

The dependency graph from ARCHITECTURE.md and feature ordering from FEATURES.md converge on a 3-phase structure. Each phase has a clear prerequisite gate: Phase 1 must complete before tests can be written, Phase 2 must complete before bugs can be safely fixed, Phase 3 requires the migration system as its first task.

### Phase 1: Package Structure and Entry Point

**Rationale:** The module split is the prerequisite for everything else. Tests import from specific modules (`from remind_me_mcp.db import ...`); until those modules exist, no meaningful tests can be written. Extracting the dashboard JSX and updating the entry point are bundled here because they are structural, not behavioral, changes. This phase has the highest density of critical pitfalls — circular imports, entry point breakage, optional-dep lazy import violations — and must be executed in strict dependency order.

**Delivers:** A working, installable `remind_me_mcp/` package with all existing behavior preserved. `remind-me-mcp --help` works. All 13 tools register correctly. No behavioral regressions.

**Addresses (from FEATURES.md):** Module split, `__init__.py` stable API, single pip install preserved, extract dashboard JSX, per-module loggers, `__all__` on each module.

**Avoids (from PITFALLS.md):** Circular imports (Pitfall 1), embedder singleton binding (Pitfall 2), entry point path break (Pitfall 3), lazy import violation (Pitfall 12), silent tool unregistration (Pitfall 7), MCP tool description regression (Pitfall 15).

**Build order within phase:** config.py → db.py → embeddings.py + models.py (parallel) → formatting.py → importer.py + pid.py (parallel) → server.py → tools.py → api.py → dashboard/ → `__init__.py` + `__main__.py`.

### Phase 2: Test Infrastructure and Baseline Coverage

**Rationale:** Tests must be written against the final modular interface (Phase 1 output), not the monolith. Writing them in Phase 2 means the import paths are already correct. The in-memory SQLite fixture must solve the `monkeypatch.setenv` timing problem (Pitfall 10) before any integration test can run safely. Baseline coverage established here is the regression net that makes Phase 3 changes safe.

**Delivers:** pytest test suite with unit tests for `db.py`, `embeddings.py`, `importer.py`, `models.py`; integration tests for all 13 MCP tool handlers against in-memory SQLite; API tests for all Starlette routes via Starlette `TestClient`/`httpx.AsyncClient`.

**Uses (from STACK.md):** pytest, pytest-asyncio (`asyncio_mode = "auto"`), pytest-mock, pytest-cov, httpx, in-memory SQLite fixtures.

**Implements:** `conftest.py` with `db` fixture (in-memory SQLite with schema), `mock_embedder` fixture (bypasses ONNX download), sample memory factory, `tmp_path`-based path fixtures.

**Avoids (from PITFALLS.md):** Monolith interface lock-in (Pitfall 6), pytest-asyncio not executing async tests (Pitfall 16), module-level constants not patchable (Pitfall 10), event loop starvation in tests (Pitfall 9).

### Phase 3: Quality, Async Safety, and Bug Fixes

**Rationale:** Bug fixes and quality changes are safe only once tests exist. The schema migration system must be the first deliverable of this phase — it is the prerequisite for the `capture_id` column fix and the `memory_tags` junction table. `asyncio.to_thread` wrapping follows the migration work because it requires a stable connection strategy. Docstrings and `_make_id` normalization are mechanical changes safe to batch at the end.

**Delivers:** Event-loop-safe async operation (no sync blocking in tool handlers); schema migration system supporting existing user databases; two critical bug fixes (import embedding ID mismatch, `capture_id` lookup); SQL-level tag filtering with correct pagination; full docstring coverage; DRY `import_directory()` function; normalized `_make_id` semantics; complete `pyproject.toml` dev tooling configuration.

**Addresses (from FEATURES.md):** `asyncio.to_thread` wrapping, connection management singleton, schema migration, both bug fixes, SQL-level tag filtering, DRY `import_directory()`, `_make_id` normalization, docstrings on all public functions, type hints on all signatures.

**Avoids (from PITFALLS.md):** Schema migration `IF NOT EXISTS` bug (Pitfall 5), FTS5 trigger divergence (Pitfall 8), SQLite thread-safety violation (Pitfall 4), `_make_id` non-determinism amplifying bugs (Pitfall 14), duplicate import logic divergence (Pitfall 11), docstring gaps (Pitfall 13).

### Phase Ordering Rationale

- Phase 1 before Phase 2: tests import from specific module paths that don't exist until Phase 1 completes. Writing tests against the monolith (before Phase 1) would create the Pitfall 6 migration overhead.
- Phase 2 before Phase 3: bug fixes without tests are unsafely speculative. The migration system modifies existing user data; test coverage of the migration path is mandatory before shipping.
- Schema migration first within Phase 3: `capture_id` column and `memory_tags` junction table both require it; attempting either fix without it causes `OperationalError` on existing databases.
- `asyncio.to_thread` after migration: the thread-safe connection strategy must be established first (connection singleton from Phase 1 already provides the handle; Phase 3 wraps calls to it correctly).

### Research Flags

Phases with well-documented patterns (skip `/gsd:research-phase` — standard Python practice):
- **Phase 1 (module split):** All patterns (flat layout, FastMCP import side-effect registration, lazy optional imports) are directly derived from codebase analysis. No external research needed.
- **Phase 2 (test infrastructure):** pytest, pytest-asyncio, in-memory SQLite, httpx/Starlette TestClient are all established patterns with clear configuration documented in STACK.md.

Phases that may benefit from targeted research during planning:
- **Phase 3 (asyncio.to_thread + SQLite threading):** The interaction between `asyncio.to_thread`, SQLite `check_same_thread=False`, and the connection singleton is nuanced. Pitfall 4 identifies the risk but the precise correct implementation (wrapping only embed vs. wrapping DB writes, connection scoping) warrants a focused spike or prototype test before committing to the pattern.
- **Phase 3 (ruff ASYNC rule codes):** ruff evolves rapidly. The specific ASYNC rule codes referenced in STACK.md should be verified against the current ruff version before finalizing the `pyproject.toml` configuration.

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | MEDIUM-HIGH | Tool choices (pytest, ruff, mypy, uv) are unambiguous; exact version numbers need PyPI verification since research cutoff was August 2025 and ruff in particular releases monthly |
| Features | HIGH | Grounded in direct codebase inspection + PROJECT.md constraints + CONCERNS.md bugs; all findings are observable facts, not domain speculation |
| Architecture | HIGH | All patterns derived from direct code analysis of working system; dependency graph verified against actual import structure; well-established Python packaging conventions |
| Pitfalls | HIGH | All 16 pitfalls derived from codebase analysis + Python language semantics; no domain-specific speculative risks; prevention patterns are concrete and testable |

**Overall confidence:** HIGH

### Gaps to Address

- **Version pins:** All version numbers in STACK.md reflect the August 2025 research cutoff. Verify with `uv pip index versions <package>` before committing to pyproject.toml. Priority: ruff (fastest-moving), then pytest-asyncio (strict mode behavior is version-dependent).
- **`asyncio.to_thread` + SQLite threading interaction:** The exact placement of `to_thread` boundaries (wrap only embedding CPU work vs. wrap DB calls too, and with which `check_same_thread` setting) needs a concrete test in Phase 3 before finalizing. Pitfall 4 describes the risk but does not fully prescribe the solution — a prototype concurrent test (`asyncio.gather` on multiple tool calls) should be written and green before the pattern is standardized.
- **`[dependency-groups]` uv support:** PEP 735 `[dependency-groups]` in `pyproject.toml` was actively rolling out at research cutoff. Verify that the installed uv version supports it; fall back to `[project.optional-dependencies]` if not.
- **FTS5 `memories_fts` rebuild after migration:** Pitfall 8 specifies not to DROP the virtual table, but the correct FTS5 rebuild command (`INSERT INTO memories_fts(memories_fts) VALUES('rebuild')`) and when to invoke it during migration (specifically for the `capture_id` column addition) should be validated against actual SQLite behavior before the migration is written.

## Sources

### Primary (HIGH confidence)
- Direct codebase analysis: `/home/baileyrd/projects/remind_me/remind_me_mcp.py` (2,500 lines) — all architecture, feature, and pitfall findings
- `.planning/PROJECT.md` — requirements, constraints, scope boundaries
- `.planning/codebase/CONCERNS.md` — identified bugs and tech debt
- `.planning/codebase/ARCHITECTURE.md` — layer analysis (pre-existing)
- `.planning/codebase/CONVENTIONS.md` — error handling and import patterns
- `/home/baileyrd/projects/remind_me/pyproject.toml` — dependency and entry-point constraints

### Secondary (MEDIUM confidence)
- Training data (cutoff August 2025) — pytest, pytest-asyncio, ruff, mypy ecosystem choices
- Python stdlib docs: `asyncio.to_thread`, `sqlite3`, `contextlib.asynccontextmanager` — standard patterns
- SQLite official documentation: `PRAGMA user_version`, FTS5 virtual table rebuild behavior
- Python packaging guide: flat vs. src layout, entry point specification

### Tertiary (LOW confidence)
- Exact version numbers for all dev dependencies — require PyPI verification before use
- ruff ASYNC rule codes — evolve rapidly; verify against current ruff changelog

---
*Research completed: 2026-02-22*
*Ready for roadmap: yes*
