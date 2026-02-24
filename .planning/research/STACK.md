# Stack Research — v1.1 Tech Debt

**Domain:** Python MCP server — security hardening, CI/CD, performance, code quality
**Researched:** 2026-02-24
**Confidence:** MEDIUM — based on direct codebase inspection (HIGH) and training knowledge through August 2025 (MEDIUM). WebFetch/WebSearch/Brave unavailable. Verify pinned versions with `pip index versions <package>` before implementation.

---

## Context: What Already Exists (Do Not Re-Add)

Read from `pyproject.toml` and codebase directly:

| Package | Current Declaration | Role |
|---------|--------------------|----|
| mcp[cli] | >=1.0.0 | MCP server framework |
| pydantic | >=2.0.0 | Input validation |
| httpx | >=0.25.0 | HTTP client (updater) |
| starlette | >=0.40.0 | HTTP API + middleware |
| uvicorn | >=0.30.0 | ASGI server |
| numpy | >=1.24.0 | Embedding math |
| pytest, pytest-asyncio, pytest-cov | dev tools | Already established in v1.0 |
| ruff, mypy | linting/typing | Already configured in pyproject.toml |
| hatchling | build backend | Already in use |

Existing security code: `CORSMiddleware` already imported in `api.py`. Current config: `allow_origins=["*"]` — the problem is configuration, not missing imports.

---

## New Stack Additions for v1.1

### 1. Security Hardening

**No new production packages required.** All security hardening is configuration-only changes to existing imports.

#### CORS Lockdown

```python
# api.py — change only the middleware configuration
middleware = [
    Middleware(
        CORSMiddleware,
        allow_origins=[
            f"http://127.0.0.1:{UI_PORT}",
            f"http://localhost:{UI_PORT}",
        ],
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    ),
]
```

`CORSMiddleware` is already imported from `starlette.middleware.cors`. `UI_PORT` is already in `config.py`. Zero new dependencies.

Why narrow origins: The dashboard is always `--ui-host 127.0.0.1` by default (see `__main__.py`). Wildcard CORS allows any origin to make credentialed requests — a security anti-pattern even for localhost tools, especially if `REMIND_ME_API_KEY` auth is added.

#### API Key Authentication

```python
# New: Starlette BaseHTTPMiddleware — zero new imports beyond stdlib
import secrets
from starlette.middleware.base import BaseHTTPMiddleware

class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        api_key = os.environ.get("REMIND_ME_API_KEY", "").strip()
        if not api_key:
            return await call_next(request)  # auth disabled when key not set
        if request.url.path == "/":
            return await call_next(request)  # dashboard HTML always accessible
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not secrets.compare_digest(token.encode(), api_key.encode()):
            return JSONResponse({"error": "Unauthorized"}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
        return await call_next(request)
```

`BaseHTTPMiddleware` is in `starlette.middleware.base` — already installed. `secrets.compare_digest()` is stdlib. `os.environ` is stdlib. Zero new dependencies.

Why static bearer token vs full auth library: The dashboard is a personal localhost tool. OAuth2/JWT brings key rotation, expiry, and library dependencies for zero additional security benefit at this threat model. A static env-var token is the established pattern for personal tool API protection (same pattern as `GITHUB_TOKEN`, `OPENAI_API_KEY` etc.).

Why `secrets.compare_digest()` over `==`: Prevents timing side-channel attacks. The stdlib function runs in constant time regardless of where strings diverge.

Why opt-in (only enforce when env var is set): Preserves zero-config default experience. Existing deployments do not break.

#### Import Path Restrictions

```python
# api.py api_import() — add after existing p.exists() check
ALLOWED_ROOTS_ENV = os.environ.get("REMIND_ME_IMPORT_ROOTS", "").strip()
if ALLOWED_ROOTS_ENV:
    allowed_roots = [Path(r).expanduser().resolve() for r in ALLOWED_ROOTS_ENV.split(":")]
else:
    allowed_roots = [Path.home()]

if not any(p.is_relative_to(root) for root in allowed_roots):
    return _json_err("Path outside allowed import roots", 403)
```

`Path.is_relative_to()` is stdlib (Python 3.9+). This project requires 3.11+. Zero new dependencies.

Why this specific check: The current `api_import()` does `p.resolve()` and `p.exists()` — it does NOT restrict what filesystem paths are accessible. An adversary who can POST to `/api/import` can read `/etc/passwd` if `allow_origins=["*"]` stays open. The combination of CORS lockdown + optional path restriction closes this.

---

### 2. CI/CD Pipeline

**No new Python packages beyond what already exists in dev dependencies.**

#### GitHub Actions Workflow

New file: `.github/workflows/ci.yml`

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.12"]

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
          cache: pip

      - name: Install dependencies
        run: |
          pip install -e ".[dev]"

      - name: Lint (ruff)
        run: ruff check remind_me_mcp/ tests/

      - name: Type check (mypy)
        run: mypy remind_me_mcp/ --ignore-missing-imports
        continue-on-error: true  # non-blocking until type coverage is complete

      - name: Tests with coverage
        run: |
          pytest --cov=remind_me_mcp --cov-fail-under=80 --cov-report=term-missing

      - name: Upload coverage report
        uses: actions/upload-artifact@v4
        if: always()
        with:
          name: coverage-${{ matrix.python-version }}
          path: htmlcov/
```

**Actions used (HIGH confidence — stable since 2023, current as of research date):**
- `actions/checkout@v4` — standard checkout action
- `actions/setup-python@v5` — Python environment setup with pip cache
- `actions/upload-artifact@v4` — artifact upload (coverage HTML)

**Why matrix `["3.11", "3.12"]` and not 3.13/3.14:**

The project requires `>=3.11`. Testing 3.11 (minimum) and 3.12 (one version ahead) provides forward-compatibility signal without the risk of `mcp[cli]`, `onnxruntime`, or `sqlite-vec` having incomplete 3.13/3.14 support. These optional dependencies frequently lag behind CPython releases.

**Why `continue-on-error: true` on mypy:**

The existing `pyproject.toml` mypy config has `disallow_untyped_defs = false` and `check_untyped_defs = true` — it is not in full strict mode. Running mypy in CI as non-blocking allows gradual tightening without failing PRs during cleanup work.

#### Coverage Enforcement

`pytest-cov` is already declared as a dev dependency (see existing `pyproject.toml` and prior STACK research). The `--cov-fail-under=80` flag enforces a minimum gate.

**Why 80%:**

Inspecting the 190-test suite across 10 modules, 80% is achievable without adding tests for the ONNX model-loading path (which requires the optional semantic extra). 90%+ would require either mocking the embedder download or excluding the embeddings hot path — both acceptable but adds work to the CI phase.

Add to `pyproject.toml` under `[tool.pytest.ini_options]`:
```toml
addopts = "--cov=remind_me_mcp --cov-fail-under=80"
```

This makes coverage enforcement automatic for every local `pytest` run, not just CI.

---

### 3. Performance Improvements

**No new production packages required.** The existing `asyncio` stdlib and the `_embed_and_store()` function already provide all necessary primitives.

#### Batch Reindex

New MCP tool: `reindex_memories` that re-embeds all memories missing vectors.

```python
# tools.py — new tool
import asyncio

@mcp.tool()
async def reindex_memories(batch_size: int = 50) -> dict:
    """Re-embed all memories that are missing vector entries."""
    db = _get_db()
    sem = asyncio.Semaphore(4)  # max 4 concurrent embed calls

    async def embed_one_memory(mem_id: str, content: str) -> bool:
        async with sem:
            return await asyncio.to_thread(_embed_and_store, db, mem_id, content)

    # Find memories with no embedding
    rows = db.execute("""
        SELECT m.id, m.content FROM memories m
        LEFT JOIN memories_vec mv ON m.rowid = mv.rowid
        WHERE mv.rowid IS NULL
    """).fetchall()

    tasks = [embed_one_memory(r["id"], r["content"]) for r in rows]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    succeeded = sum(1 for r in results if r is True)
    return {"total": len(rows), "reindexed": succeeded}
```

`asyncio.Semaphore` and `asyncio.gather()` are stdlib. `asyncio.to_thread()` is already used in the embeddings path. Zero new dependencies.

Why semaphore limit of 4: ONNX `CPUExecutionProvider` manages a thread pool internally. Sending too many concurrent `to_thread()` calls overloads it. 4 is a conservative default that allows meaningful parallelism without contention. Make it configurable via the batch_size parameter or a separate env var if needed.

#### Concurrent Directory Import

```python
# importer.py — import_directory() concurrent variant
async def import_directory_concurrent(
    directory: str,
    category: str = "chat_import",
    tags: list[str] | None = None,
    extract_mode: str = "assistant_messages",
    max_length: int = 10000,
    recursive: bool = True,
    concurrency: int = 4,
) -> dict[str, Any]:
    """Concurrent version of import_directory using asyncio.gather."""
    root = Path(directory)
    sem = asyncio.Semaphore(concurrency)
    files = ...  # same file discovery logic

    async def import_one(f: Path) -> dict:
        async with sem:
            return await asyncio.to_thread(
                import_chat_file, str(f), category, tags or [], extract_mode, max_length
            )

    results = await asyncio.gather(*[import_one(f) for f in files], return_exceptions=True)
    # aggregate results same as existing import_directory()
```

Why keep the sync `import_directory()` too: The MCP `import_chats` tool likely calls the sync version. The new async version is for the REST API endpoint `POST /api/import` when processing directories — it runs inside an async Starlette handler so `asyncio.gather()` is natural there.

---

### 4. API Embedding Parity

**No new packages.** The fix is two lines in `api.py`.

The REST API `api_add()` and `api_update()` handlers insert/update memories without calling `_embed_and_store()`. The MCP `add_memory` and `update_memory` tools do call it. This is the parity gap.

```python
# api.py api_add() — add after db.commit()
_embed_and_store(db, mem_id, content)

# api.py api_update() — add after final db.commit()
new_content = body.get("content") or db.execute(
    "SELECT content FROM memories WHERE id = ?", (memory_id,)
).fetchone()["content"]
_embed_and_store(db, memory_id, new_content)
```

`_embed_and_store()` already handles the unavailable-embedder case by returning `False` silently. No error handling needed.

---

### 5. Code Quality

**No new packages.** All fixes are in the existing ruff + mypy setup.

#### Ruff Warning Fixes

Current `pyproject.toml` ruff config already selects `["E", "F", "W", "I", "N", "UP", "B", "SIM", "TCH"]`. The warnings to fix:

| Warning Class | Where | Fix |
|--------------|-------|-----|
| F401 (unused imports) | Various modules | Remove unused `from __future__ import annotations` where unused, remove dead imports |
| TCH (type-checking imports) | Tools using Pydantic models | Move type-only imports inside `if TYPE_CHECKING:` blocks |
| UP (pyupgrade) | Python 3.9 patterns | Ruff auto-fixes these with `ruff check --fix` |

Run `ruff check --fix remind_me_mcp/` to apply safe auto-fixes. Review remaining warnings manually.

#### Narrow `except Exception`

Two known locations per PROJECT.md:

1. `embeddings.py` line 82 — `except Exception as e:` in `_ensure_loaded()`
   Replace with: `except (ort.OrtException, OSError, RuntimeError) as e:`
   (ONNX raises `OrtException` for model load failures; `OSError` covers file access)

2. `pid.py` line 102 — `except Exception:` in `_check_ui_server_health()`
   Replace with: `except (OSError, TimeoutError, urllib.error.URLError) as e:`
   (urllib raises `URLError` for connection failures, `OSError` for socket errors, `TimeoutError` for timeouts)

**Verify the exact ONNX exception class name** — ONNX Runtime may expose it as `onnxruntime.backend.backend.OrtException` or simply as a subclass of `RuntimeError`. Check the installed package before committing the narrowed exception.

#### Monolith Removal

Delete `remind_me_mcp_original.py` from the repo root. This file has no imports pointing to it — confirmed by absence of any `from remind_me_mcp_original` references in the codebase.

```bash
git rm remind_me_mcp_original.py
```

No code changes required.

---

## Recommended Stack Summary

### Core Technologies (unchanged from v1.0)

| Technology | Version | Purpose | Status |
|------------|---------|---------|--------|
| Python | 3.11+ | Runtime | No change |
| FastMCP via mcp[cli] | >=1.0.0 | MCP server | No change |
| Starlette | >=0.40.0 | HTTP middleware + API | Config change only |
| SQLite WAL | stdlib | Storage | No change |
| Pydantic | >=2.0.0 | Input validation | No change |

### Supporting Libraries (v1.1 additions — dev only)

No new production dependencies.
No new dev dependencies (pytest-cov already established in v1.0 dev setup).

### Development Tools (new — CI/CD)

| Tool | Purpose | Notes |
|------|---------|-------|
| GitHub Actions | Automated CI on push/PR | `.github/workflows/ci.yml` — new file |
| actions/checkout@v4 | Git checkout in CI | No version change needed |
| actions/setup-python@v5 | Python env in CI with pip cache | No version change needed |
| actions/upload-artifact@v4 | Coverage report artifact | No version change needed |

---

## Installation

```bash
# No new packages to install for production

# For CI (already in dev deps from v1.0 setup):
pip install -e ".[dev]"

# Run coverage locally (with enforcement):
pytest --cov=remind_me_mcp --cov-fail-under=80 --cov-report=term-missing --cov-report=html
```

Add to `pyproject.toml` to make coverage enforcement automatic:
```toml
[tool.pytest.ini_options]
addopts = "--cov=remind_me_mcp --cov-fail-under=80"
testpaths = ["tests"]
asyncio_mode = "auto"
```

---

## Alternatives Considered

| Recommended | Alternative | When to Use Alternative |
|-------------|-------------|-------------------------|
| Static bearer token via env var | `python-jose` JWT | If auth must have expiry/rotation (not needed for personal localhost tool) |
| Static bearer token via env var | HTTP Basic Auth (`starlette.middleware.authentication`) | If you prefer username/password UX; adds no security benefit vs bearer token for localhost |
| `asyncio.Semaphore(4)` + `asyncio.gather()` | `concurrent.futures.ProcessPoolExecutor` | If embedding were CPU-bound AND you needed true multi-core parallelism; ONNX CPUExecutionProvider already manages its own thread pool internally |
| `asyncio.to_thread()` | `loop.run_in_executor()` | `asyncio.to_thread()` is the modern stdlib idiom since Python 3.9; `run_in_executor()` is the older form; prefer to_thread for consistency with existing codebase |
| `Path.is_relative_to()` | `str.startswith()` on resolved path | `is_relative_to()` is the correct method; string prefix matching fails for paths like `/home/user2` matching `/home/user` |
| `secrets.compare_digest()` | `hmac.compare_digest()` | Both are correct; `secrets.compare_digest()` is the modern alias; either works |
| actions/setup-python@v5 | actions/setup-python@v4 | v5 added pip caching improvements; use v5 unless you need v4 behavior |

---

## What NOT to Use

| Avoid | Why | Use Instead |
|-------|-----|-------------|
| `authlib` / `python-jose` | Full OAuth2/JWT library; 3+ MB of code for a static API key check | `secrets.compare_digest()` from stdlib |
| `bandit` standalone | Already partially covered by ruff's S rules; separate bandit run adds CI time and produces overlapping (and sometimes contradictory) reports | Fix the specific known issues from PROJECT.md |
| `pre-commit` hooks | Active refactor work creates high commit churn; pre-commit hooks add friction during implementation; add after v1.1 is stable | CI gates (ruff + pytest) provide equivalent protection without blocking local commits |
| `tox` | Multi-environment orchestration adds complexity; GitHub Actions matrix handles Python version matrix directly | GitHub Actions matrix strategy |
| `coverage` CLI standalone | Requires separate invocation; pytest-cov already wraps it and integrates the fail-under gate | `pytest-cov` with `--cov-fail-under` |
| `asyncio.gather()` without semaphore for embedding | ONNX InferenceSession is not designed for unbounded concurrent calls; leads to internal mutex contention | `asyncio.Semaphore(4)` guards each `asyncio.to_thread()` call |
| `ProcessPoolExecutor` for embedding batch | Cross-process serialization overhead exceeds embedding time for short texts; ONNX already manages threading | `asyncio.to_thread()` with semaphore |

---

## Stack Patterns by Variant

**If `REMIND_ME_API_KEY` is not set (default install):**
- API key middleware skips all checks — zero change to existing UX
- CORS is still locked to localhost origins — dashboard still works
- Import path restriction defaults to `Path.home()` as allowlist

**If `REMIND_ME_API_KEY` is set (hardened mode):**
- All `/api/*` routes require `Authorization: Bearer <key>` header
- Dashboard HTML at `/` is excluded from auth (browser can load the React app)
- React dashboard must include the key in `fetch()` calls — update `App.jsx` fetch headers

**For the CI matrix Python version choice:**
- Test `["3.11", "3.12"]` — both are active supported releases as of 2026-02
- Do not add 3.13 yet until `mcp[cli]` and `onnxruntime` publish 3.13 wheels on PyPI
- Do not add 3.10 — the project requires 3.11 minimum (`match` syntax and `tomllib` usage)

**For coverage threshold:**
- Start at 80% — achievable with 190 existing tests
- Increase to 85% after embedding parity tests are added
- The ONNX model-loading path in `embeddings.py` will always be hard to cover without the `[semantic]` extra installed in CI; exclude it explicitly: `--cov-config=pyproject.toml` with `[tool.coverage.run] omit = ["remind_me_mcp/embeddings.py"]` if needed, or use the existing `FakeEmbedder` fixture

---

## Version Compatibility

| Package | Compatible With | Notes |
|---------|----------------|-------|
| starlette>=0.40.0 | BaseHTTPMiddleware — stable API | No breaking changes to middleware interface in 0.40.x series |
| starlette>=0.40.0 | Python 3.11, 3.12 | Fully supported |
| pytest-cov>=5.0 | pytest>=8.0, coverage>=7.0 | pytest-cov 5.x requires coverage 7.x — both already in training data as stable |
| asyncio.to_thread() | Python 3.9+ | Available since 3.9; project requires 3.11+ so no compatibility issue |
| Path.is_relative_to() | Python 3.9+ | Available since 3.9; safe to use |

---

## Sources

- Direct codebase inspection:
  - `/home/baileyrd/projects/remind_me/pyproject.toml` — declared dependencies, existing tool config — HIGH confidence
  - `/home/baileyrd/projects/remind_me/remind_me_mcp/api.py` — current CORS config (`allow_origins=["*"]`), import route, add/update handlers — HIGH confidence
  - `/home/baileyrd/projects/remind_me/remind_me_mcp/config.py` — env-based config pattern — HIGH confidence
  - `/home/baileyrd/projects/remind_me/remind_me_mcp/db.py` — `_embed_and_store()` signature and error handling — HIGH confidence
  - `/home/baileyrd/projects/remind_me/remind_me_mcp/embeddings.py` — broad `except Exception` location (line 82) — HIGH confidence
  - `/home/baileyrd/projects/remind_me/remind_me_mcp/pid.py` — broad `except Exception` location (line 102) — HIGH confidence
  - `/home/baileyrd/projects/remind_me/remind_me_mcp/importer.py` — sequential `import_directory()` structure — HIGH confidence
  - `/home/baileyrd/projects/remind_me/.planning/PROJECT.md` — active requirements, out-of-scope list — HIGH confidence

- Training knowledge (cutoff August 2025):
  - `starlette.middleware.base.BaseHTTPMiddleware` API — MEDIUM confidence
  - `secrets.compare_digest()` stdlib pattern — HIGH confidence (official Python security guidance)
  - GitHub Actions `actions/checkout@v4`, `actions/setup-python@v5`, `actions/upload-artifact@v4` — MEDIUM confidence (stable since 2023–2024)
  - `asyncio.Semaphore` + `asyncio.gather()` + `asyncio.to_thread()` concurrency pattern — HIGH confidence (stable stdlib since 3.9)
  - `Path.is_relative_to()` — HIGH confidence (stable stdlib since 3.9)
  - ONNX Runtime exception types — LOW confidence (verify the exact class name in installed onnxruntime before narrowing `except Exception`)

**Verification required before implementation:**
```bash
# Confirm ONNX exception class name
python -c "import onnxruntime; help(onnxruntime)" 2>&1 | grep -i exception

# Confirm actions version pinning is current (check github.com/actions/* releases)

# Confirm pytest-cov >=5.0 is still current
pip index versions pytest-cov

# Confirm starlette BaseHTTPMiddleware API is unchanged
python -c "from starlette.middleware.base import BaseHTTPMiddleware; help(BaseHTTPMiddleware)"
```

---

*Stack research for: remind_me_mcp v1.1 tech debt milestone*
*Researched: 2026-02-24*
