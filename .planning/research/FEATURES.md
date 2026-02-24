# Feature Research

**Domain:** Python MCP/Starlette memory server — v1.1 tech debt milestone
**Researched:** 2026-02-24
**Confidence:** HIGH (all findings from direct codebase analysis + established Python/Starlette patterns; web search unavailable)

---

## Context

This is a tech debt milestone, not a feature milestone. "Features" here means security hardening, pipeline infrastructure, correctness fixes, and code quality improvements on an already-working v1.0 server. The source is the 10-module `remind_me_mcp` package (3,680 lines, 190 tests passing). No new MCP tools, no new user-facing capabilities, no schema changes beyond what security/correctness requires.

All findings are grounded in direct code inspection:
- `remind_me_mcp/api.py` — CORS config, route handlers, import path handling
- `remind_me_mcp/tools.py` — reindex loop, embed_and_store call sites
- `remind_me_mcp/embeddings.py` — embed() batch method, broad except handlers
- `remind_me_mcp/importer.py` — sequential file processing loop
- `remind_me_mcp/pid.py` — broad except handler
- `remind_me_mcp/updater.py` — broad except handler
- Ruff output: 30 warnings, 26 auto-fixable

---

## Table Stakes

Features the v1.1 tech debt milestone must deliver. Missing any = known security gap, broken correctness, or no CI.

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| CORS lockdown (localhost-only) | `api.py:345` sets `allow_origins=["*"]` — any webpage can XHR read/delete all memories. Browser CORS policy is the only cross-origin protection here. | LOW | Change to `["http://localhost:{UI_PORT}", "http://127.0.0.1:{UI_PORT}"]`. `UI_PORT` already in `config.py`. One line change. |
| Import path restriction (home-directory boundary) | `api_import` at `api.py:299` does `Path(file_path).expanduser().resolve()` with no boundary check. Any path can be passed — including `/etc/passwd`, symlinks outside home, etc. | LOW | Add check after resolve: `if not str(p).startswith(str(Path.home())): return _json_err(...)`. ~5 lines. |
| ruff auto-fix (26 of 30 errors) | 30 ruff errors exist today. CI will fail on lint. Must clean before adding pipeline. 26 are `--fix`-able in one command. | LOW | `ruff check --fix remind_me_mcp/`. Fixes: F401 (unused imports), I001 (import order), F541 (bare f-strings), UP045/UP037/UP017 (type annotations). |
| ruff manual fixes (4 remaining errors) | SIM105 (use contextlib.suppress), TC002 (type-checking import block), F821 (undefined Starlette name), B007 (unused loop var) need manual edits. | LOW | 4 small targeted edits across api.py, db.py, importer.py. |
| Narrow broad `except Exception` | 5 call sites: `embeddings.py:82, 145, 164`, `updater.py:370`, `pid.py:102`. Bare `except Exception` swallows unexpected failures silently, masking bugs. | LOW | Replace with specific types: `(ImportError, OSError, RuntimeError)` in embeddings, `(OSError, PermissionError)` in pid, `(subprocess.SubprocessError, OSError)` in updater. |
| Remove monolith file | `remind_me_mcp_original.py` (2,495 lines) lives in project root. No code references it. Creates contributor confusion. | LOW | `git rm remind_me_mcp_original.py`. Zero risk. |
| API embedding parity (POST /api/memories) | `api_add` at `api.py:212-238` inserts a memory then commits without calling `_embed_and_store`. Memories created via dashboard are invisible to semantic search until manual reindex. The MCP `remind_me_add` tool calls `_embed_and_store` correctly (`tools.py:117`). | MEDIUM | Add `await asyncio.to_thread(_embed_and_store, db, mem_id, content)` after commit in `api_add`. The import `_embed_and_store` is already in `api.py:20` — currently flagged as unused (F401). This feature removes that ruff warning as a side effect. |
| API embedding parity (PUT/PATCH /api/memories/{id}) | `api_update` at `api.py:240-276` updates content and commits without re-embedding. Semantic index goes stale. MCP `remind_me_update` re-embeds correctly (`tools.py:360`). | MEDIUM | After commit, if `content` was in the update body: `await asyncio.to_thread(_embed_and_store, db, memory_id, body["content"])`. |
| GitHub Actions CI pipeline | No `.github/` directory exists. No automated validation on push. Every commit is unverified against the full test suite. | LOW | Single `.github/workflows/ci.yml`: checkout, setup Python 3.11+3.12 matrix, `pip install -e ".[semantic]" pytest-cov ruff`, `ruff check`, `pytest --cov=remind_me_mcp --cov-fail-under=80`. |
| Coverage enforcement (≥80%) | 190 tests exist but no gate prevents coverage regressions. With `pytest-cov`, CI can enforce a minimum. | LOW | `pytest --cov=remind_me_mcp --cov-fail-under=80 --cov-report=term-missing`. Add `pytest-cov` to `[project.optional-dependencies] dev` in `pyproject.toml`. |

---

## Differentiators

Features that improve correctness or performance beyond the baseline, but not strictly required for the stated v1.1 scope.

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Batch reindex (embed N at a time) | Current `remind_me_reindex` in `tools.py:766` embeds one memory per `asyncio.to_thread` call in a serial loop. For 500+ memories this causes excessive thread-pool round-trips. `embedder.embed()` already accepts `list[str]` and returns `ndarray`. Batching 32 at a time gives 5-10x speedup. | MEDIUM | Group missing memories into batches of 32. Call `await asyncio.to_thread(embedder.embed, texts)` once. Write all vectors with `db.executemany`. Commit per batch, not per memory. |
| Concurrent file import (`import_directory`) | `import_directory` in `importer.py:374` processes files sequentially. For 100+ file directories this is slow (pure I/O-bound). Use `ThreadPoolExecutor` with `max_workers=4` for parallel file reads. | MEDIUM | SQLite WAL mode already supports concurrent readers; bottleneck is file I/O not DB writes. Keep DB commit sequential. Use `executor.map(import_chat_file_sync, files)` pattern. |
| Optional API auth token (env-gated) | Users who port-forward the dashboard (e.g., WSL to Windows, SSH tunnel) have no auth protection. An optional `REMIND_ME_API_TOKEN` env var, checked by Starlette middleware, provides protection without affecting default local-only use. | MEDIUM | `BaseHTTPMiddleware` subclass. On each request: if token env var is set, check `Authorization: Bearer <token>` header. Skip check for `GET /` (dashboard HTML). Return 401 JSON if missing/wrong. Zero impact when env var is unset. |

---

## Anti-Features

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| Full HTTPS/TLS for dashboard | "More secure" | Self-signed cert = browser security warnings; real cert = DNS + renewal complexity. Personal localhost tool gains nothing from TLS since all traffic is loopback. | CORS lockdown covers cross-origin threats. Optional API token covers network-exposure scenarios. |
| PostgreSQL migration | "Production ready" | Out of scope per `PROJECT.md`. SQLite WAL is sufficient for personal use (single user, localhost). | SQLite with WAL + busy_timeout is already proven concurrency-safe in v1.0 tests. |
| Rate limiting | "Protect the API" | Single-user personal tool. No multi-tenant scenario. Rate limiting adds middleware complexity with zero real benefit. | Not needed. The real risks are CORS (addressed) and unauthenticated network access (addressed by optional token). |
| Vite/esbuild build step for dashboard | "Modern JS tooling" | Explicitly out of scope in `PROJECT.md`: "Keep Babel standalone transpilation (no build step)". Adds Node.js hard dependency. | Babel standalone continues to work. JSX already extracted to `dashboard/App.jsx` in v1.0. |
| Split into separate installable packages | "Cleaner architecture" | Explicitly out of scope in `PROJECT.md`: "Must remain a single pip install-able package". | Internal module boundaries are already clean: 10 modules, zero circular imports (verified v1.0). |
| Thread-per-embedding in batch reindex | "Maximum parallelism" | ONNX inference is CPU-bound and the ONNX session is not thread-safe for simultaneous calls without locking. Spawning a thread per embedding creates overhead without speedup. | Call `embedder.embed(batch_list)` once inside a single `asyncio.to_thread`. ONNX does internal SIMD batching for free. |
| mypy strict mode | "Type safety" | `pyproject.toml` has `disallow_untyped_defs = false`. Enabling strict requires annotating all 190 tests and many internal helpers — large scope increase with no user impact. | Keep current mypy config. Ruff UP045 fixes modernize type annotations without strict enforcement overhead. |

---

## Feature Dependencies

```
[CORS lockdown]
    — no dependencies —

[Import path restriction]
    — no dependencies —

[ruff auto-fix (26 errors)]
    — blocks —> [ruff manual fixes (4 errors)]
    — blocks —> [GitHub Actions CI]    (CI fails until lint is clean)

[ruff manual fixes]
    — blocks —> [GitHub Actions CI]

[Narrow except Exception]
    — no hard dependencies —
    — enhances —> [GitHub Actions CI] (cleaner, safer code)

[Remove monolith file]
    — no dependencies —

[API embedding parity — api_add]
    — depends on —> [_embed_and_store in db.py] (already exists)
    — resolves —>   [F401 unused import warning in api.py] (side effect)
    — compatible with —> [Batch reindex] (parity prevents future missing embeddings)

[API embedding parity — api_update]
    — depends on —> [API embedding parity — api_add] (same pattern, natural to do together)

[GitHub Actions CI]
    — requires —> [ruff auto-fix + manual fixes] (lint must pass first)
    — requires —> [pytest-cov installed]
    — validates —> [CORS lockdown, import path restriction, embedding parity, narrow except]

[Coverage enforcement]
    — part of —> [GitHub Actions CI]
    — requires —> [pytest-cov in dev dependencies]

[Batch reindex]
    — depends on —> [embedder.embed() list API] (already exists in embeddings.py:88)
    — independent of —> [API embedding parity] (solves existing missing embeddings; parity solves future ones)

[Concurrent file import]
    — no dependencies —
    — compatible with —> [Batch reindex] (orthogonal: file I/O vs embedding compute)

[Optional API auth token]
    — enhances —> [CORS lockdown] (defense in depth; CORS should be done first)
```

### Dependency Notes

- **CI must come after ruff is clean.** Adding CI before fixing 30 ruff errors creates a permanently red pipeline. Fix lint first, add CI second.
- **API embedding parity is purely additive.** No DB schema changes. No new imports needed (the import already exists at `api.py:20`, just flagged unused). Pattern is identical to `tools.py:117`.
- **Batch reindex is independent of everything else.** Can be delivered in any order. Only dependency is `embedder.embed()` which already exists.
- **Import path restriction is standalone.** Only touches the `api_import` route handler in `api.py`. ~5 lines. No schema, no config, no tests beyond the existing import test suite.

---

## MVP Definition

### v1.1 Launch With (in recommended order)

The order respects the CI-blocks-on-lint dependency:

- [ ] **ruff auto-fix** — `ruff check --fix remind_me_mcp/` clears 26 of 30 errors in one command
- [ ] **ruff manual fixes** — 4 remaining: SIM105, TC002, F821, B007
- [ ] **Narrow broad except Exception** — 5 call sites, specific exception types
- [ ] **Remove monolith file** — `git rm remind_me_mcp_original.py`
- [ ] **CORS lockdown** — change `allow_origins=["*"]` to localhost-only in `api.py:345`
- [ ] **Import path restriction** — add home-directory boundary check in `api_import`
- [ ] **API embedding parity (add + update)** — `asyncio.to_thread(_embed_and_store, ...)` after commit in both routes
- [ ] **GitHub Actions CI** — `.github/workflows/ci.yml` with pytest + ruff + coverage
- [ ] **Coverage enforcement** — add `pytest-cov` to dev deps; `--cov-fail-under=80` in CI

### Add After v1.1 Validation

These are real improvements but not stated v1.1 scope:

- [ ] **Batch reindex** — matters at 500+ memories; optimize when user hits the scale
- [ ] **Concurrent file import** — matters at 100+ file directories; most users won't hit this
- [ ] **Optional API auth token** — matters when dashboard is exposed outside localhost

### Future Consideration (v2+)

- [ ] **Semantic search in REST API search route** — `api_search` uses only FTS5; could call `_semantic_search` and merge like MCP tools do
- [ ] **mypy strict mode** — requires annotating all internal helpers and tests
- [ ] **REST API semantic search endpoint** — no `/api/memories/semantic-search` exists today

---

## Feature Prioritization Matrix

| Feature | User Value | Implementation Cost | Priority |
|---------|------------|---------------------|----------|
| CORS lockdown | HIGH (security) | LOW (1 line) | P1 |
| Import path restriction | HIGH (security) | LOW (~5 lines) | P1 |
| ruff auto-fix (26 errors) | MEDIUM (CI prereq) | LOW (1 command) | P1 |
| ruff manual fixes (4 errors) | MEDIUM (CI prereq) | LOW (4 targeted edits) | P1 |
| Narrow except Exception | MEDIUM (reliability) | LOW (5 call sites) | P1 |
| Remove monolith file | MEDIUM (clarity) | LOW (git rm) | P1 |
| API embedding parity (add) | HIGH (correctness) | MEDIUM (async pattern) | P1 |
| API embedding parity (update) | HIGH (correctness) | LOW (same pattern as add) | P1 |
| GitHub Actions CI | HIGH (quality gate) | LOW (single YAML) | P1 |
| Coverage enforcement | MEDIUM (regression prevention) | LOW (pytest-cov dep) | P1 |
| Batch reindex | LOW (only at scale) | MEDIUM (batch logic) | P2 |
| Concurrent file import | LOW (only at scale) | MEDIUM (thread pool) | P2 |
| Optional API auth token | LOW (niche: exposed dashboard) | MEDIUM (middleware) | P2 |

**Priority key:**
- P1: Must have for v1.1 (directly in stated scope per PROJECT.md)
- P2: Should have, add if time permits in v1.1 or defer to v1.2
- P3: Nice to have, future milestone

---

## Technical Implementation Notes

### CORS Lockdown (api.py:343-346)

Current:
```python
middleware = [
    Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]),
]
```

Target:
```python
from remind_me_mcp.config import UI_PORT

_allowed_origins = [
    f"http://localhost:{UI_PORT}",
    f"http://127.0.0.1:{UI_PORT}",
]
middleware = [
    Middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "Authorization"],
    ),
]
```

`UI_PORT` is already imported indirectly via `config.py`. No new config needed.

### Import Path Restriction (api.py:299-301)

Current:
```python
p = Path(file_path).expanduser().resolve()
if not p.exists():
    return _json_err(f"Path not found: {p}")
```

Target:
```python
p = Path(file_path).expanduser().resolve()
if not p.exists():
    return _json_err(f"Path not found: {p}")
home = Path.home()
if not str(p).startswith(str(home)):
    return _json_err(f"Import path must be within home directory: {p}", 403)
```

### API Embedding Parity (api.py:212-238 and api.py:240-276)

In `api_add`, after `db.commit()` (line 236):
```python
import asyncio
# _embed_and_store already imported at line 20 — removing the F401 warning
await asyncio.to_thread(_embed_and_store, db, mem_id, content)
```

In `api_update`, after `db.commit()` (line 275), conditionally:
```python
if "content" in body and body["content"]:
    new_content = body["content"]
    await asyncio.to_thread(_embed_and_store, db, memory_id, new_content)
```

### Batch Reindex (tools.py:760-773)

Current: one `asyncio.to_thread` per memory.

Target:
```python
BATCH_SIZE = 32
for i in range(0, len(missing), BATCH_SIZE):
    batch = missing[i:i + BATCH_SIZE]
    texts = [content[:2000] for _, _, content in batch]
    vecs = await asyncio.to_thread(embedder.embed, texts)  # returns ndarray (N, dim)
    rows = [(rowid, vecs[j].tobytes()) for j, (_, rowid, _) in enumerate(batch)]
    db.executemany("INSERT OR REPLACE INTO memories_vec(rowid, embedding) VALUES (?, ?)", rows)
    db.commit()
    created += len(batch)
```

`embedder.embed()` already takes `list[str]` (`embeddings.py:88`). `embed_one()` is just `embed([text])[0].tobytes()`.

### GitHub Actions CI (.github/workflows/ci.yml)

```yaml
name: CI
on: [push, pull_request]

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
      - name: Install dependencies
        run: pip install -e ".[semantic]" pytest-cov ruff
      - name: Lint
        run: ruff check remind_me_mcp/
      - name: Test with coverage
        run: pytest --cov=remind_me_mcp --cov-fail-under=80 --cov-report=term-missing
```

Note: `sqlite-vec` ships manylinux wheels and installs cleanly on `ubuntu-latest`. ONNX Runtime similarly has Linux x86_64 wheels. The `[semantic]` extras should work in CI without additional OS packages.

---

## Sources

- Direct codebase inspection: `/home/baileyrd/projects/remind_me/remind_me_mcp/api.py`
  (CORS config line 345, api_add lines 212-238, api_update lines 240-276, import path line 299)
- Direct codebase inspection: `/home/baileyrd/projects/remind_me/remind_me_mcp/tools.py`
  (reindex serial loop line 766, embed_and_store calls lines 117, 360, 638-639)
- Direct codebase inspection: `/home/baileyrd/projects/remind_me/remind_me_mcp/embeddings.py`
  (embed batch method line 88, broad except lines 82, 145, 164)
- Direct codebase inspection: `/home/baileyrd/projects/remind_me/remind_me_mcp/importer.py`
  (sequential file loop line 374)
- Direct codebase inspection: `pid.py:102`, `updater.py:370` (broad except)
- Ruff output (live run): 30 warnings total, 26 auto-fixable; codes I001, F401, F541, UP045, UP037, UP017, SIM105, B007, TC002, F821
- `/home/baileyrd/projects/remind_me/.planning/PROJECT.md` (v1.1 scope, out-of-scope constraints, existing validated features)
- Starlette `CORSMiddleware` `allow_origins`, `allow_methods`, `allow_headers` parameters — HIGH confidence (established API, unchanged across versions)
- GitHub Actions `actions/checkout@v4`, `actions/setup-python@v5`, pytest-cov `--cov-fail-under` — HIGH confidence (industry-standard Python CI pattern)
- Python `asyncio.to_thread` for sync-in-async wrapping — HIGH confidence (Python 3.9+ stdlib, used throughout codebase already)

---
*Feature research for: remind-me-mcp v1.1 tech debt milestone*
*Researched: 2026-02-24*
