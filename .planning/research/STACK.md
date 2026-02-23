# Technology Stack — Research

**Project:** Remind Me MCP — Modular Refactor
**Dimension:** Testing, linting, formatting, and package layout
**Researched:** 2026-02-22
**Confidence note:** Web search and bash execution were unavailable during this research session.
Version numbers are drawn from training data (cutoff August 2025). Verify all pinned
versions with `uv pip index versions <package>` or PyPI before committing to pyproject.toml.

---

## Recommended Stack

### Test Runner

| Technology | Version (verify) | Purpose | Why |
|------------|-----------------|---------|-----|
| pytest | >=8.0 | Test runner and assertion engine | Industry standard; superior fixture system; best plugin ecosystem; `assert` rewriting produces readable diffs without boilerplate |
| pytest-asyncio | >=0.23 | Async test support | This codebase is async-first (`async def` MCP tools, Starlette handlers). pytest-asyncio is the dominant choice for asyncio codebases; it integrates with pytest fixtures cleanly. anyio is the alternative (see below). |
| pytest-cov | >=5.0 | Coverage measurement | Thin wrapper over coverage.py; integrates with pytest via `--cov` flag; generates HTML, XML, and terminal reports |
| pytest-mock | >=3.12 | Mock fixture | Provides `mocker` fixture — cleaner than `unittest.mock.patch` as context managers; auto-resets mocks between tests |

**Confidence:** HIGH — pytest's dominance is unambiguous; versions reflect stable releases known before August 2025 cutoff. Verify exact latest with PyPI.

### Async Testing Strategy

**Use `pytest-asyncio` with `asyncio_mode = "auto"` in `pyproject.toml`.**

Rationale: The MCP tool handlers are all `async def`. With `asyncio_mode = "auto"`, every `async def test_*` function is automatically treated as an asyncio test without requiring `@pytest.mark.asyncio` decorators on each one. This reduces boilerplate across what will be a large test suite.

```toml
# pyproject.toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

**Why not anyio over pytest-asyncio:** anyio is the right choice when the codebase supports multiple async backends (asyncio + trio). This project uses pure asyncio throughout — FastMCP, Starlette, and uvicorn all run asyncio. Introducing anyio would add a backend abstraction layer with no payoff. pytest-asyncio directly targets asyncio; use it.

**Why not standard `unittest.IsolatedAsyncioTestCase`:** Incompatible with pytest's fixture system. Since fixtures are central to clean async test setup (temp SQLite databases, mock embedders), pytest-asyncio is the right choice.

**Confidence:** HIGH — pytest-asyncio with auto mode is the established pattern for pure-asyncio Python projects.

### Integration Testing — Starlette HTTP API

| Technology | Version (verify) | Purpose | Why |
|------------|-----------------|---------|-----|
| httpx | >=0.27 | HTTP client for Starlette TestClient | Already a project dependency (`httpx>=0.25.0` in pyproject.toml). Starlette's `TestClient` is built on `requests`, but `httpx` is required for async test clients. Use `httpx.AsyncClient` with Starlette's `ASGITransport` for async HTTP tests. |

```python
# Async HTTP integration test pattern
import pytest
import httpx
from starlette.testclient import TestClient

# For sync tests (simpler, covers most API surface)
def test_api_stats(api_app):
    with TestClient(api_app) as client:
        response = client.get("/api/stats")
    assert response.status_code == 200

# For async tests
async def test_api_stats_async(api_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as client:
        response = await client.get("/api/stats")
    assert response.status_code == 200
```

**Confidence:** HIGH — `httpx` is already present in the dependency tree; Starlette's docs recommend this exact pattern.

### Database Test Fixtures

Use in-memory SQLite (`:memory:`) for unit and integration tests. Do NOT mock the database at the unit level — the SQL queries and FTS5 triggers are core logic and must be tested against a real SQLite engine.

```python
# conftest.py pattern
import sqlite3
import pytest

@pytest.fixture
def db():
    """In-memory SQLite with full schema applied."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)   # call the actual schema function
    yield conn
    conn.close()
```

**Confidence:** HIGH — in-memory SQLite is the canonical approach; no additional libraries needed.

### Linter

| Technology | Version (verify) | Purpose | Why |
|------------|-----------------|---------|-----|
| ruff | >=0.4 | Linting AND formatting | Replaces flake8, isort, pyupgrade, pydocstyle, and black in a single binary. ~100x faster than the tools it replaces. Written in Rust. Active development by Astral (same team as uv). As of mid-2025, ruff is the de facto standard linter/formatter for new Python projects. |

**What ruff replaces — do NOT install these separately:**

| Tool | Replaced By | Why Not Both |
|------|------------|--------------|
| flake8 | ruff (E, W, F rules) | Redundant; slower; would conflict on rule sets |
| black | ruff format | ruff format is black-compatible; one tool is simpler |
| isort | ruff (I rules) | ruff handles import sorting natively |
| pyupgrade | ruff (UP rules) | ruff handles syntax modernization |
| pydocstyle | ruff (D rules) | ruff handles docstring style checking |
| bandit | ruff (S rules) | ruff has basic security rules (S prefix) |

**Recommended ruff configuration for this project:**

```toml
# pyproject.toml
[tool.ruff]
target-version = "py311"
line-length = 100         # matches existing ~100 char convention in codebase

[tool.ruff.lint]
select = [
    "E",   # pycodestyle errors
    "W",   # pycodestyle warnings
    "F",   # pyflakes
    "I",   # isort
    "B",   # flake8-bugbear (catches common mistakes)
    "UP",  # pyupgrade (modernize syntax for Python 3.11+)
    "N",   # pep8 naming
    "D",   # pydocstyle (enforce docstrings — project requirement)
    "S",   # flake8-bandit security
    "ASYNC", # flake8-async (catches sync calls in async functions — relevant here)
]
ignore = [
    "D100", # Missing docstring in public module — modules have file-level docstrings
    "D104", # Missing docstring in public package — __init__.py files don't need docs
    "S101", # Use of assert — pytest uses assert
    "S603", # subprocess calls — not applicable
]

[tool.ruff.lint.pydocstyle]
convention = "google"     # Google-style docstrings match existing codebase pattern

[tool.ruff.format]
quote-style = "double"    # matches existing codebase convention
```

**Why the ASYNC rule set matters for this project specifically:** The `ASYNC` rule set (flake8-async) detects patterns like `time.sleep()` called inside `async def` functions. This project has a known performance concern: embedding and DB calls are synchronous and block the async event loop. The ASYNC rules will flag these during linting, making the async-first refactor goal self-enforcing via the linter.

**Confidence:** MEDIUM-HIGH — ruff's dominance as of August 2025 is clear from training data. The ASYNC rule availability and exact rule codes should be verified against the current ruff changelog since ruff evolves rapidly.

### Type Checker

| Technology | Version (verify) | Purpose | Why |
|------------|-----------------|---------|-----|
| mypy | >=1.10 | Static type checking | The project already uses type hints throughout with `from __future__ import annotations`. mypy is the reference implementation; most IDE integrations and CI tooling target it. pyright is the alternative (see below). |

**Why mypy over pyright:** pyright is faster and has better inference in some cases, but mypy is better integrated with the pytest ecosystem (via `pytest-mypy` plugins), has broader third-party stub coverage via `typeshed`, and is the default expectation when projects say "type-checked." For a refactor project where the goal is clean type hygiene, mypy's stricter defaults are valuable. Pyright would be a valid alternative if IDE speed is a priority.

**Recommended mypy configuration:**

```toml
# pyproject.toml
[tool.mypy]
python_version = "3.11"
strict = false                # Start permissive; tighten per-module during refactor
warn_return_any = true
warn_unused_configs = true
warn_redundant_casts = true
no_implicit_reexport = true
ignore_missing_imports = true # Required: mcp, sqlite-vec, onnxruntime have no stubs

# Per-module overrides as refactor progresses
[[tool.mypy.overrides]]
module = "remind_me.*"
disallow_untyped_defs = true  # All functions must be typed
```

**Confidence:** MEDIUM — mypy >=1.10 is well-established as of August 2025. The `strict = false` starting point is deliberately pragmatic: the existing code has type hints but not full mypy-clean coverage. Tightening incrementally per module is the correct refactor strategy.

### Package Layout

**Use flat layout (not src layout) — stay with existing convention.**

The existing project puts `remind_me_mcp.py` at the project root. After modularization, the new structure should be:

```
remind_me/                     # git repo root
├── pyproject.toml
├── README.md
├── remind_me_dashboard.jsx    # reference JSX (unchanged)
├── tests/                     # NEW — test suite
│   ├── __init__.py
│   ├── conftest.py            # shared fixtures: db, api_app, mock_embedder
│   ├── unit/
│   │   ├── __init__.py
│   │   ├── test_db.py
│   │   ├── test_embeddings.py
│   │   ├── test_importer.py
│   │   ├── test_models.py
│   │   └── test_utils.py
│   ├── integration/
│   │   ├── __init__.py
│   │   ├── test_api.py        # Starlette TestClient tests
│   │   └── test_tools.py     # MCP tool handler end-to-end tests
│   └── fixtures/
│       ├── sample_claude_export.json
│       ├── sample_chat.jsonl
│       └── sample_chat.md
└── remind_me_mcp/             # NEW — package directory (renamed from single file)
    ├── __init__.py            # re-exports mcp instance; entry point compatibility
    ├── config.py              # environment variable constants
    ├── db.py                  # SQLite helpers, schema, _get_db, _ensure_schema
    ├── embeddings.py          # _Embedder class, _get_embedder, _embed_and_store
    ├── models.py              # Pydantic input models
    ├── importer.py            # import_chat_file, _chunk_text, parsers
    ├── tools.py               # @mcp.tool decorated async handlers
    ├── api.py                 # Starlette route handlers, _build_api_app
    ├── dashboard.py           # _build_dashboard_html, _get_dashboard_script
    ├── utils.py               # _make_id, _now_iso, _row_to_dict, _fmt_memory_md
    └── server.py              # PID management, get_server_status, lifespan
```

**Why flat layout over src layout:**

The src layout (`src/remind_me_mcp/`) adds the `src/` prefix and requires explicit editable installs. The project uses `hatchling` as the build backend, which supports both. Flat layout is simpler and matches the existing convention — the pyproject.toml entry point `remind_me_mcp:mcp.run` works unchanged. No reason to add friction.

**Why keep the package name `remind_me_mcp` (not `remind_me`):**

The existing `[project.scripts]` entry is `remind-me-mcp = "remind_me_mcp:mcp.run"`. The MCP config JSON files used by Claude Code, Claude Desktop, and Claude.ai reference the installed script `remind-me-mcp`. Renaming the importable package would break existing installations unless done carefully. Keep `remind_me_mcp` as the package name.

**Confidence:** HIGH — flat layout for this specific migration is clearly correct given the existing tooling and entry point constraints.

### Build Backend

| Technology | Version (verify) | Purpose | Why |
|------------|-----------------|---------|-----|
| hatchling | >=1.24 | Build backend | Already in use. No reason to change. Hatchling handles the package correctly with flat layout. Works with uv. |

**Confidence:** HIGH — already in use; no migration needed.

### Dependency Management

| Technology | Version (verify) | Purpose | Why |
|------------|-----------------|---------|-----|
| uv | >=0.4 | Package manager and virtual env | Already the recommended tool per README. uv replaces pip + virtualenv + pip-tools in one binary. Speed advantage is meaningful in CI. Add `uv.lock` to the repo for reproducible installs. |

**Why add `uv.lock`:** The project currently has no lockfile. During a refactor where test infrastructure is being built from scratch, a lockfile prevents "worked on my machine" failures caused by transitive dependency drift.

**Confidence:** HIGH — uv is explicitly referenced throughout the existing README; adding a lockfile is a clear improvement.

---

## Dev Dependency Declaration

The dev dependencies below should be added to `pyproject.toml` under `[project.optional-dependencies]` or a `[dependency-groups]` (uv supports both):

```toml
[dependency-groups]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-cov>=5.0",
    "pytest-mock>=3.12",
    "ruff>=0.4",
    "mypy>=1.10",
]
```

**Note on `[dependency-groups]` vs `[project.optional-dependencies]`:** uv introduced native support for `[dependency-groups]` (PEP 735) in 2024. It is the preferred way to declare dev dependencies with uv. If pip compatibility is required, use `[project.optional-dependencies]` with `dev = [...]` instead. Since this project is a personal tool primarily run with uv, `[dependency-groups]` is fine.

**Confidence:** MEDIUM — PEP 735 and `[dependency-groups]` support was actively rolling out during the training window. Verify that the installed uv version supports this syntax; fall back to `[project.optional-dependencies]` if not.

---

## Test Invocation

```bash
# Run all tests
uv run pytest

# Run with coverage report
uv run pytest --cov=remind_me_mcp --cov-report=term-missing --cov-report=html

# Run only unit tests
uv run pytest tests/unit/

# Run linter
uv run ruff check remind_me_mcp/ tests/

# Run formatter (check mode, no writes)
uv run ruff format --check remind_me_mcp/ tests/

# Apply formatting
uv run ruff format remind_me_mcp/ tests/

# Run type checker
uv run mypy remind_me_mcp/
```

---

## Alternatives Considered

| Category | Recommended | Alternative | Why Not |
|----------|-------------|-------------|---------|
| Test runner | pytest | unittest | pytest fixtures are essential for clean async + DB test setup; unittest's `TestCase` class model is more verbose and less composable |
| Async test support | pytest-asyncio | anyio (pytest-anyio) | anyio is for multi-backend projects; this project is pure asyncio; anyio adds indirection with no benefit |
| Async test support | pytest-asyncio | `unittest.IsolatedAsyncioTestCase` | Incompatible with pytest fixtures; forces TestCase subclassing |
| Linter | ruff | flake8 + black + isort | Three separate tools, 100x slower, no ASYNC rule set, stale development pace |
| Formatter | ruff format | black | ruff format is black-compatible; one fewer tool to install and configure |
| Type checker | mypy | pyright | mypy has broader third-party stub support; IDE-neutral; appropriate strictness for incremental adoption |
| Type checker | mypy | basedmypy | Niche; less documentation; unnecessary for a refactor project |
| Package layout | flat (`remind_me_mcp/` at root) | src layout (`src/remind_me_mcp/`) | src layout adds no benefit here; would require updating the hatchling config and editable install commands |
| Build backend | hatchling | flit, setuptools | hatchling already in use; no reason to migrate |
| Lockfile | uv.lock | requirements.txt | uv.lock is more complete (includes all transitive deps and hashes); compatible with uv workflows |
| HTTP test client | httpx | requests | httpx supports async; already a project dependency; Starlette explicitly supports both |

---

## What to Explicitly NOT Install

| Tool | Reason |
|------|--------|
| flake8 | Replaced entirely by ruff; installing both creates config conflicts |
| black | Replaced entirely by ruff format; black-compatible output |
| isort | Replaced entirely by ruff (I rules) |
| bandit | Covered by ruff S rules for basic security checks |
| pylint | Too opinionated for a refactor; noisy; slower than ruff |
| nose / nose2 | Dead project; pytest superseded it |
| tox | Overkill for a single-package personal tool; adds complexity with no gain |
| pre-commit | Valuable eventually, but not during an active refactor — the churn on every commit would be disruptive |

---

## Installation Order for New Dev Environment

```bash
# 1. Create and activate virtual environment
uv venv

# 2. Install package in editable mode with all dependencies
uv pip install -e ".[semantic]"

# 3. Install dev dependencies
uv pip install pytest>=8.0 pytest-asyncio>=0.23 pytest-cov>=5.0 pytest-mock>=3.12 ruff>=0.4 mypy>=1.10

# 4. Verify test runner works
uv run pytest --collect-only

# 5. Verify linter works
uv run ruff check remind_me_mcp/

# 6. Verify type checker works
uv run mypy remind_me_mcp/
```

---

## Version Verification Required

**Before committing these versions to pyproject.toml, verify on PyPI:**

| Package | Claimed Min | Verify Command |
|---------|------------|----------------|
| pytest | >=8.0 | `uv pip index versions pytest` |
| pytest-asyncio | >=0.23 | `uv pip index versions pytest-asyncio` |
| pytest-cov | >=5.0 | `uv pip index versions pytest-cov` |
| pytest-mock | >=3.12 | `uv pip index versions pytest-mock` |
| ruff | >=0.4 | `uv pip index versions ruff` |
| mypy | >=1.10 | `uv pip index versions mypy` |
| hatchling | >=1.24 | `uv pip index versions hatchling` |

Web search was unavailable during this research session. All version numbers reflect the state of the ecosystem as of August 2025. Ruff in particular evolves rapidly (monthly releases); the latest version as of February 2026 may be significantly higher than 0.4.

---

## Sources

- Training data (cutoff August 2025) — MEDIUM confidence for ecosystem choices, LOW confidence for exact version numbers
- pytest documentation structure: https://docs.pytest.org/en/stable/
- pytest-asyncio documentation: https://pytest-asyncio.readthedocs.io/
- ruff documentation: https://docs.astral.sh/ruff/
- mypy documentation: https://mypy.readthedocs.io/en/stable/
- Existing project constraints: `/home/baileyrd/projects/remind_me/pyproject.toml` (read during research — HIGH confidence)
- Existing codebase conventions: `/home/baileyrd/projects/remind_me/.planning/codebase/CONVENTIONS.md` (read during research — HIGH confidence)
- Project goals: `/home/baileyrd/projects/remind_me/.planning/PROJECT.md` (read during research — HIGH confidence)
