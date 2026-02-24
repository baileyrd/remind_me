# Architecture Research: v1.1 Tech Debt Features

**Domain:** Python MCP server — security, CI/CD, performance, code quality integration
**Researched:** 2026-02-24
**Confidence:** HIGH (direct codebase analysis of existing 10-module package)

---

## Standard Architecture

### System Overview — Current v1.0 State

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Entry Points                                     │
├────────────────────────┬────────────────────────────────────────────┤
│  MCP stdio (default)   │  HTTP dashboard (--serve-ui)               │
│  FastMCP / stdio       │  uvicorn + Starlette                       │
└──────────┬─────────────┴───────────────┬────────────────────────────┘
           │                             │
┌──────────▼─────────────────────────────▼────────────────────────────┐
│                     remind_me_mcp package                            │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐            │
│  │ __main__ │  │ server   │  │ tools    │  │ api      │            │
│  │ (CLI)    │  │ (FastMCP)│  │ (15 MCP) │  │ (REST)   │            │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘            │
│       │             │             │             │                   │
│  ┌────▼─────────────▼─────────────▼─────────────▼────────────────┐  │
│  │              Core Services Layer                               │  │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐      │  │
│  │  │ config   │  │ db       │  │embeddings│  │ importer │      │  │
│  │  │ (env)    │  │ (SQLite) │  │ (ONNX)   │  │ (parsers)│      │  │
│  │  └──────────┘  └──────────┘  └──────────┘  └──────────┘      │  │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐      │  │
│  │  │ models   │  │formatting│  │ pid      │  │ updater  │      │  │
│  │  │ (Pydantic)│  │ (output) │  │ (server) │  │ (git)    │      │  │
│  │  └──────────┘  └──────────┘  └──────────┘  └──────────┘      │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                    dashboard/ subpackage                             │
│                    App.jsx (React/Babel)                            │
└─────────────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────▼───────────────────────────────────────┐
│                     Storage Layer                                    │
│  SQLite WAL (memories, chat_imports, memories_fts, memories_vec,    │
│              memory_tags, PRAGMA user_version=2)                     │
└─────────────────────────────────────────────────────────────────────┘
```

### System Overview — Target v1.1 State

The v1.1 changes are additive overlays on the existing architecture. No new layers are introduced. The primary mutations are:

1. **config.py** — gains security-scoped env vars (CORS origins, API key, import root)
2. **api.py** — gains auth middleware and CORS lockdown; `api_add`/`api_update`/`api_import` gain embedding calls
3. **importer.py** — gains concurrent file processing via `asyncio.gather` or `ThreadPoolExecutor`
4. **tools.py / db.py** — `remind_me_reindex` gains batch embedding logic
5. **embeddings.py / pid.py** — narrow broad `except Exception` handlers
6. New file: `.github/workflows/ci.yml` — GitHub Actions pipeline (no Python module)
7. Deleted: `remind_me_mcp_original.py` — root-level monolith removed

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Entry Points (unchanged)                         │
├────────────────────────┬────────────────────────────────────────────┤
│  MCP stdio             │  HTTP dashboard                            │
└──────────┬─────────────┴───────────────┬────────────────────────────┘
           │                             │
┌──────────▼─────────────────────────────▼────────────────────────────┐
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────────────────┐  │
│  │ __main__ │  │ server   │  │ tools    │  │ api [MODIFIED]     │  │
│  │(unchanged)│  │(unchanged)│  │(unchanged)│  │ + auth middleware  │  │
│  └──────────┘  └──────────┘  └──────────┘  │ + CORS lockdown    │  │
│                                             │ + embed on add/upd │  │
│                                             └────────────────────┘  │
│  ┌────────────────────┐  ┌──────────────────────────────────────┐   │
│  │ config [MODIFIED]  │  │ db [MODIFIED]                        │   │
│  │ + REMIND_ME_CORS_  │  │ + _batch_embed_and_store()           │   │
│  │   ORIGINS          │  │   (new helper, used by reindex tool) │   │
│  │ + REMIND_ME_API_KEY│  └──────────────────────────────────────┘   │
│  │ + REMIND_ME_IMPORT_│  ┌──────────────────────────────────────┐   │
│  │   ROOT             │  │ importer [MODIFIED]                  │   │
│  └────────────────────┘  │ + concurrent file processing         │   │
│                           │   (ThreadPoolExecutor in             │   │
│                           │    import_directory)                 │   │
│                           └──────────────────────────────────────┘   │
│  ┌────────────────────┐  ┌──────────────────────────────────────┐   │
│  │ embeddings         │  │ pid [MODIFIED]                       │   │
│  │ [MODIFIED]         │  │ narrow except → OSError, ValueError  │   │
│  │ narrow except →    │  └──────────────────────────────────────┘   │
│  │ specific types     │                                              │
│  └────────────────────┘                                              │
└─────────────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────────────┐
│  CI/CD (NEW — not a Python module)                                   │
│  .github/workflows/ci.yml                                            │
│  pytest + coverage + ruff + mypy on every push/PR                   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Component Responsibilities — v1.1 Changes

### New vs Modified Components

| Component | New/Modified | v1.1 Change | Touches What |
|-----------|-------------|-------------|--------------|
| `config.py` | Modified | Add `REMIND_ME_CORS_ORIGINS`, `REMIND_ME_API_KEY`, `REMIND_ME_IMPORT_ROOT` env vars | Consumed by `api.py` |
| `api.py` | Modified | CORS lockdown (specific origins), optional API key middleware, `api_add`/`api_update` embed parity | Reads new config vars |
| `db.py` | Modified | Add `_batch_embed_and_store(ids_and_contents)` helper for reindex performance | Called by tools.py reindex |
| `importer.py` | Modified | Concurrent file processing in `import_directory()` via `ThreadPoolExecutor` | Unchanged interface |
| `embeddings.py` | Modified | Narrow `except Exception` to specific types (`ImportError`, `RuntimeError`, `OSError`) | No interface change |
| `pid.py` | Modified | Narrow `except Exception` in `_check_ui_server_health` to `urllib.error.URLError`, `OSError`, `TimeoutError` | No interface change |
| `tools.py` | Modified | `remind_me_reindex` uses `_batch_embed_and_store` for efficiency | Calls new db helper |
| `.github/workflows/ci.yml` | New | GitHub Actions: pytest, coverage ≥80%, ruff, mypy on push/PR | External — no Python imports |
| `remind_me_mcp_original.py` | Deleted | Remove root-level monolith file | No module dependencies |

### Unchanged Components

| Component | Why Unchanged |
|-----------|---------------|
| `server.py` | FastMCP lifecycle unaffected by security/perf changes |
| `models.py` | Pydantic models complete; no new input shapes needed |
| `formatting.py` | Output formatting unrelated to v1.1 scope |
| `updater.py` | Self-update logic complete; no v1.1 changes |
| `__main__.py` | CLI dispatch complete; no new modes |
| `dashboard/App.jsx` | UI complete; no build tooling changes (out of scope) |

---

## Integration Points

### Security Hardening Integration

**CORS lockdown in `api.py`:**

The current `_build_api_app()` uses `allow_origins=["*"]`. The fix is localised to the `middleware` list construction at the bottom of `_build_api_app`. No route handlers change.

```python
# config.py addition
CORS_ORIGINS: list[str] = [
    o.strip()
    for o in os.environ.get("REMIND_ME_CORS_ORIGINS", "http://127.0.0.1:5199,http://localhost:5199").split(",")
    if o.strip()
]

# api.py change — only the middleware list
middleware = [
    Middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,   # was ["*"]
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    ),
]
```

**API key middleware in `api.py`:**

Starlette middleware reads `REMIND_ME_API_KEY` from config. If the env var is unset (empty string), middleware is not added — preserving backward compatibility for users who don't set it. Middleware sits between CORS and routes.

```
Request → CORSMiddleware → [ApiKeyMiddleware if key set] → Route handlers
```

The middleware pattern: check `Authorization: Bearer <token>` header; return 401 JSON on mismatch; pass through on match or when key is unset.

**Import path restriction in `api.py`:**

`api_import` currently accepts any filesystem path. The fix adds an `IMPORT_ROOT` config var (default: user home dir `~`). The handler resolves the incoming path and checks `path.is_relative_to(IMPORT_ROOT)` before proceeding. This is a single-line guard in `api_import`, with a corresponding config constant.

```
config.py: IMPORT_ROOT = Path(os.environ.get("REMIND_ME_IMPORT_ROOT", "~")).expanduser()
api.py api_import: if not p.is_relative_to(IMPORT_ROOT): return _json_err("Path outside allowed root", 403)
```

### CI/CD Pipeline Integration

The CI pipeline has zero Python module dependencies. It is a pure GitHub Actions workflow file. Integration points are:

| CI Check | What It Runs | Pass Condition |
|----------|-------------|----------------|
| Tests | `pytest tests/ -x --tb=short` | All 190+ tests pass |
| Coverage | `pytest --cov=remind_me_mcp --cov-report=term-missing --cov-fail-under=80` | ≥80% line coverage |
| Lint | `ruff check remind_me_mcp/` | Zero warnings after cleanup |
| Type check | `mypy remind_me_mcp/` | No new errors (existing config) |

The workflow triggers on `push` and `pull_request` to `main`. Python matrix: `3.11`, `3.12` (matches `.python-version` file which specifies 3.12, plus minimum supported 3.11).

Dependencies: installs `pip install -e ".[semantic]"` to test the full optional stack. Alternatively, run two jobs: base deps only, then with `[semantic]`.

### Performance — Batch Reindex Integration

Current `remind_me_reindex` (in `tools.py`) embeds one memory at a time in a loop. The batch improvement has two sub-changes:

**1. New `_batch_embed_and_store` helper in `db.py`:**

Takes a list of `(memory_id, content)` pairs. Calls `embedder.embed(batch_of_texts)` once for the entire batch (ONNX handles batching natively), then inserts all vectors in a single transaction.

```
db.py: _batch_embed_and_store(db, pairs: list[tuple[str, str]]) -> int
```

Interface: returns count of successfully stored embeddings. Called from `tools.py` reindex only. `_embed_and_store` (single-item) remains unchanged — still used by `memory_add`, `memory_update`, `remind_me_auto_capture`.

**2. `remind_me_reindex` in `tools.py`:**

Replaces the per-item `asyncio.to_thread` loop with a single `asyncio.to_thread(_batch_embed_and_store, db, missing_pairs)`. The tool's user-facing output format stays identical.

### Performance — Concurrent File Import Integration

`import_directory` in `importer.py` currently processes files serially (`for f in sorted(files)`). The concurrent version uses `concurrent.futures.ThreadPoolExecutor` to process files in parallel, with the existing `import_chat_file` as the unit of work.

```
importer.py: import_directory() — uses ThreadPoolExecutor(max_workers=4)
```

Constraints:
- `import_chat_file` calls `_get_db()` (singleton, `check_same_thread=False`) — safe for multi-thread access since SQLite WAL allows concurrent writers with serialization on commit.
- Each file's INSERT + commit is atomic per `import_chat_file`. Concurrent commits may serialize under WAL busy_timeout (5 seconds already set), which is acceptable.
- The public interface and return shape of `import_directory` do not change.

### API Embedding Parity Integration

Currently, `api_add` in `api.py` inserts a memory but never calls `_embed_and_store`. The MCP `memory_add` tool in `tools.py` does call it. This is the parity gap.

Fix: `api_add` and `api_update` must call `_embed_and_store` after their respective `db.commit()`. Since `api.py` route handlers are async (Starlette), this should use `asyncio.to_thread`:

```python
# api_add — after db.commit()
import asyncio
await asyncio.to_thread(_embed_and_store, db, mem_id, content)

# api_update — after db.commit(), if content was updated
if "content" in body:
    await asyncio.to_thread(_embed_and_store, db, memory_id, body["content"])
```

`_embed_and_store` is already imported in `api.py` (visible in current import list). No new imports needed.

### Code Quality — Ruff Cleanup Integration

The ruff warnings are in existing modules. No new files. The fixes are mechanical:

- Remove unused imports flagged by `F401`
- Fix type annotation issues flagged by `UP` (modern union syntax `X | Y` vs `Optional[X]`)
- Fix `B` (bugbear) and `SIM` (simplify) warnings in place

These are pure edits within existing files. No interface changes.

### Code Quality — Exception Narrowing Integration

Two modules need narrowing:

**`embeddings.py` — `_ensure_loaded`:**

Current: bare `except Exception` that swallows all errors. Replace with:
- `except ImportError` — missing optional dependencies (onnxruntime, tokenizers, etc.)
- `except (RuntimeError, OSError, ValueError)` — model load failures

**`pid.py` — `_check_ui_server_health`:**

Current: bare `except Exception` in the urllib call. Replace with:
- `except (urllib.error.URLError, OSError, TimeoutError)` — network failures expected here
- The `import urllib.request` is already local; add `import urllib.error`

---

## Recommended Project Structure — v1.1 Delta

The project structure is unchanged from v1.0 except:

```
remind_me/
├── .github/
│   └── workflows/
│       └── ci.yml                    # NEW — GitHub Actions CI/CD pipeline
├── pyproject.toml                    # version bump: "1.0.0" → "1.1.0"
├── remind_me_mcp_original.py         # DELETED
├── remind_me_mcp/
│   ├── config.py                     # MODIFIED — add CORS_ORIGINS, API_KEY, IMPORT_ROOT
│   ├── api.py                        # MODIFIED — CORS lockdown, auth middleware, embed parity
│   ├── db.py                         # MODIFIED — add _batch_embed_and_store()
│   ├── embeddings.py                 # MODIFIED — narrow except Exception
│   ├── importer.py                   # MODIFIED — concurrent import_directory()
│   ├── pid.py                        # MODIFIED — narrow except Exception
│   ├── tools.py                      # MODIFIED — reindex uses batch embedding
│   │   (all others unchanged)
│   └── dashboard/
│       └── (unchanged)
└── tests/
    ├── test_api.py                   # EXTENDED — auth tests, embed parity tests
    ├── test_db.py                    # EXTENDED — _batch_embed_and_store tests
    ├── test_importer.py              # EXTENDED — concurrent import tests
    └── (all others unchanged)
```

---

## Architectural Patterns

### Pattern 1: Security as Config + Middleware (not Route-Level)

**What:** Security controls (CORS, API key) live in middleware, not inside route handlers. Config constants in `config.py` drive middleware behavior.

**When to use:** When multiple routes need the same security policy — centralizing in middleware is DRY and consistent.

**Trade-offs:** Middleware applies uniformly; if some routes need different auth (e.g., public health check), the middleware must explicitly whitelist them. For this project: all API routes require auth when the key is set. The dashboard root (`/`) can be excluded from auth checks since it serves static HTML.

**Example:**
```python
# api.py — middleware ordering matters: CORS before auth
middleware = [
    Middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, ...),
    Middleware(ApiKeyMiddleware) if API_KEY else None,  # conditional
]
middleware = [m for m in middleware if m is not None]
```

### Pattern 2: Additive Config Constants for Security Opt-In

**What:** New security env vars default to permissive/disabled behavior so existing deployments are not broken on upgrade.

**When to use:** Any time a new security control is added to an existing deployed system.

**Trade-offs:** Backward compatible but requires users to explicitly enable security. For a personal tool, this is the right call — hard-enabling would break existing setups.

**Defaults:**
- `REMIND_ME_CORS_ORIGINS` defaults to `http://127.0.0.1:5199,http://localhost:5199` (same as the default dashboard URL — more secure than `*` but not breaking)
- `REMIND_ME_API_KEY` defaults to `""` (empty = auth disabled, preserves current behavior)
- `REMIND_ME_IMPORT_ROOT` defaults to `~` (user home — restricts to user's own files, reasonable default)

### Pattern 3: Batch vs Single Embed — Two Helpers

**What:** Keep `_embed_and_store(db, memory_id, content) -> bool` for single-item use (add, update, auto_capture) and add `_batch_embed_and_store(db, pairs) -> int` for bulk use (reindex). Do not merge them into one function.

**When to use:** Separate helpers when the calling contexts have fundamentally different performance profiles. Single-item helpers keep the common case simple; batch helpers optimize the uncommon bulk case.

**Trade-offs:** Two functions to maintain, but each is small and focused. The alternative (making `_embed_and_store` accept a list) would complicate all callers that use it for single items.

### Pattern 4: ThreadPoolExecutor for I/O-Bound Concurrent Import

**What:** `import_directory()` uses `concurrent.futures.ThreadPoolExecutor` with a bounded worker count (4) to parallelize `import_chat_file` calls.

**When to use:** CPU-bound work needs `ProcessPoolExecutor`; I/O-bound work (file reads, DB writes) benefits from threads even under the GIL. Import is mostly file I/O and SQLite writes — threads are correct.

**Trade-offs:** SQLite WAL handles concurrent writes with busy_timeout. If many large files are imported simultaneously, the commit serialization bottleneck is SQLite, not Python. Worker count of 4 is a reasonable bound that avoids flooding the DB with lock contention.

---

## Data Flow

### Security Request Flow (After v1.1)

```
HTTP Request
    ↓
CORSMiddleware
  (check Origin header against CORS_ORIGINS)
    ↓ (origin allowed or non-CORS request)
[ApiKeyMiddleware] (only if REMIND_ME_API_KEY is set)
  (check Authorization: Bearer <token>)
    → 401 JSON if mismatch
    ↓ (auth passed)
Route Handler
  (api_import: check path.is_relative_to(IMPORT_ROOT))
    → 403 JSON if path escapes root
    ↓ (path allowed)
DB operation + embed + response
```

### Embedding Parity Flow (api_add / api_update After v1.1)

```
POST /api/memories (body: {content, category, tags, ...})
    ↓
api_add handler
    ↓
db.execute(INSERT INTO memories ...) + db.commit()
    ↓
asyncio.to_thread(_embed_and_store, db, mem_id, content)
  [runs embedding in thread pool, non-blocking]
    ↓
return JSONResponse(memory_dict, status=201)
```

Before v1.1, the `asyncio.to_thread` step was missing from `api_add` and `api_update`.

### Batch Reindex Flow (After v1.1)

```
remind_me_reindex tool called
    ↓
Fetch all memory rowids, fetch existing vec rowids
    ↓
missing_pairs = [(id, content), ...] for rowids not in vec table
    ↓
asyncio.to_thread(_batch_embed_and_store, db, missing_pairs)
  ↓ (in thread)
  embedder.embed([text for _, text in missing_pairs])  # one ONNX call
  ↓
  for each (rowid, vec_bytes): INSERT OR REPLACE INTO memories_vec
  db.commit()  # single commit for all
    ↓
return summary string (unchanged format)
```

Before v1.1: one `asyncio.to_thread` per memory, N ONNX inference calls.
After v1.1: one `asyncio.to_thread` for all, one ONNX batch call.

### Concurrent Import Flow (After v1.1)

```
import_directory(directory, ...)
    ↓
discover files (unchanged rglob logic)
    ↓
ThreadPoolExecutor(max_workers=4)
  ├── Thread: import_chat_file(file1, ...)
  ├── Thread: import_chat_file(file2, ...)
  ├── Thread: import_chat_file(file3, ...)
  └── Thread: import_chat_file(file4, ...)
  (remaining files queued, processed as workers free)
    ↓
collect results (futures.as_completed or executor.map)
    ↓
return summary dict (unchanged shape)
```

---

## Internal Boundaries

### Module Dependency Map (v1.1 Changes Only)

| Boundary | Communication | v1.1 Change |
|----------|---------------|-------------|
| `config.py` → `api.py` | Import constants | api.py reads 3 new config vars |
| `config.py` → internal modules | Import constants | embeddings.py, pid.py unchanged |
| `db.py` → `tools.py` | Import `_batch_embed_and_store` | New function added to db.py exports |
| `db.py` → `api.py` | Import `_embed_and_store` (already imported) | api.py now actually calls it |
| `importer.py` → `ThreadPoolExecutor` | stdlib only | No new package deps |

### Circular Import Risk

Zero risk. The existing module graph is:
```
config → (nothing)
db → config, embeddings
embeddings → config
models → (nothing)
formatting → (nothing)
importer → db
pid → config, db
server → config, db
tools → db, formatting, importer, models, pid, server, updater
api → config, db, importer
__main__ → api, config, pid, server, tools, updater
```

No v1.1 change introduces a new import direction. The graph stays acyclic.

---

## Anti-Patterns

### Anti-Pattern 1: Putting Security Logic in Route Handlers

**What people do:** Add `if "Authorization" not in request.headers: return 401` inside each route handler.

**Why it's wrong:** Easily forgotten on new routes, violates DRY, makes the common auth path visible in every handler.

**Do this instead:** Put auth in Starlette middleware registered once in `_build_api_app`. All routes get it automatically.

### Anti-Pattern 2: allow_origins=["*"] in Production

**What people do:** Leave `allow_origins=["*"]` because "it's just a local server."

**Why it's wrong:** Any webpage the user visits can make cross-origin requests to `http://127.0.0.1:5199` and read/write their memories. CSRF becomes trivial.

**Do this instead:** Default to `http://127.0.0.1:5199,http://localhost:5199` — same-origin for the dashboard, nothing else. Configurable via env var for users who need different origins.

### Anti-Pattern 3: Embedding One at a Time in Batch Operations

**What people do:** `for id, content in missing: await asyncio.to_thread(_embed_and_store, db, id, content)`.

**Why it's wrong:** N separate ONNX inference calls for N memories. ONNX Runtime's batch inference amortizes tokenization and matrix multiply overhead across the entire batch — 100 memories in one call is ~5-10x faster than 100 sequential calls.

**Do this instead:** Collect all (id, content) pairs first, then call `embedder.embed(all_texts)` once, then write all vectors in one transaction.

### Anti-Pattern 4: Path Traversal via Unsanitized User Input

**What people do:** `p = Path(file_path).expanduser().resolve()` then directly pass to file operations without checking that p is within an expected root.

**Why it's wrong:** A user (or the MCP client acting on their behalf) can pass `../../etc/passwd` or any absolute path on the filesystem.

**Do this instead:** After resolving, assert `p.is_relative_to(IMPORT_ROOT)`. For this project, IMPORT_ROOT defaults to `~` — users can import from their home directory, not from `/etc` or other system paths.

### Anti-Pattern 5: Broad `except Exception` That Silences Real Errors

**What people do:** `except Exception: return None` — catches everything, makes failures invisible.

**Why it's wrong:** Hides bugs. A `TypeError` from a logic error is indistinguishable from an expected `ImportError` from missing dependencies. Debugging becomes guesswork.

**Do this instead:** Catch only the specific exception types you expect and handle. Let unexpected exceptions propagate (or log them at ERROR before re-raising). In `embeddings.py`: `except ImportError` and `except (RuntimeError, OSError)` are the expected cases; `ValueError` and `TypeError` should surface.

---

## Build Order for v1.1 Phases

The following order minimizes risk by addressing dependencies first:

### Recommended Phase Sequence

**Phase 1: Code Quality and Cleanup (no deps, lowest risk)**

Scope: ruff warnings, narrow exceptions, remove monolith. These are mechanical edits with no behavioral change. Do first because:
- Zero risk of breaking existing tests
- Leaves the codebase cleaner before adding features
- ruff cleanup verifies CI will pass before CI is set up

Files touched: `embeddings.py`, `pid.py`, any file with ruff warnings, delete `remind_me_mcp_original.py`

**Phase 2: CI/CD Pipeline (depends on Phase 1 passing)**

Scope: `.github/workflows/ci.yml`. Do second because:
- Phase 1 ensures ruff and existing tests pass cleanly
- CI validates all subsequent phases automatically
- Once CI is live, any regression in Phase 3-5 is caught immediately

Files touched: `.github/workflows/ci.yml` (new), `pyproject.toml` (version bump to 1.1.0)

**Phase 3: Security Hardening (depends on CI from Phase 2)**

Scope: `config.py` env vars, `api.py` CORS lockdown + auth middleware + import path restriction. Do third because:
- CI from Phase 2 validates the security changes automatically
- Security changes are additive (backward compatible defaults) — low breaking risk
- Must be done before performance changes that also touch `api.py` (avoid conflicts)

Files touched: `config.py`, `api.py`, `tests/test_api.py`

**Phase 4: API Embedding Parity (depends on Phase 3 touching api.py)**

Scope: `api_add`, `api_update` in `api.py` get `asyncio.to_thread(_embed_and_store, ...)`. Do fourth because:
- Phase 3 already modifies `api.py` — combining with Phase 3 is a valid alternative, but separating keeps diffs small and reviewable
- Requires `_embed_and_store` (already imported in `api.py`) — no new dependencies

Files touched: `api.py` (within `api_add` and `api_update`), `tests/test_api.py`

**Phase 5: Performance Improvements (depends on Phase 4 for full test coverage)**

Scope: `_batch_embed_and_store` in `db.py`, batch reindex in `tools.py`, concurrent import in `importer.py`. Do last because:
- Performance changes carry the most behavioral risk (concurrency, DB write patterns)
- CI from Phase 2 catches regressions
- Security and parity are more critical correctness issues than performance

Files touched: `db.py`, `tools.py`, `importer.py`, `tests/test_db.py`, `tests/test_importer.py`

---

## Scaling Considerations

This is a personal-use server. Scaling is not a concern. The architecture is appropriate for single-user operation.

| Concern | Current State | v1.1 Impact |
|---------|--------------|-------------|
| DB write concurrency | WAL + busy_timeout=5s handles concurrent writers | Concurrent import adds writer threads; SQLite serializes commits; acceptable |
| Embedding throughput | Sequential per-item | Batch reindex: 5-10x faster for cold start; ongoing adds unchanged |
| API auth overhead | None (no auth) | Per-request header check: microseconds; negligible |
| Import speed | Sequential file-by-file | 4 concurrent workers; I/O-bound so ~3-4x faster on multi-file imports |

---

## Sources

- Direct codebase analysis: `/home/baileyrd/projects/remind_me/remind_me_mcp/*.py` (all 11 modules)
- `pyproject.toml` for dependency constraints and ruff/mypy configuration
- `tests/conftest.py` for test patterns and fixture architecture
- `.planning/PROJECT.md` for v1.1 requirements and constraints
- Starlette documentation (middleware ordering): https://www.starlette.io/middleware/
- Python `concurrent.futures` stdlib documentation: standard thread safety with SQLite WAL confirmed from project's existing WAL + `check_same_thread=False` setup

---

*Architecture research for: Remind Me MCP v1.1 tech debt features*
*Researched: 2026-02-24*
