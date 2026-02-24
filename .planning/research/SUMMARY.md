# Project Research Summary

**Project:** remind_me_mcp v1.1 tech debt milestone
**Domain:** Python MCP server — security hardening, CI/CD pipeline, API correctness, performance, code quality
**Researched:** 2026-02-24
**Confidence:** HIGH

## Executive Summary

This is a tech debt milestone on a working v1.0 system — 3,680 lines of Python across 10 modules, 190 passing tests, no CI pipeline. The v1.1 scope is tightly bounded: no new user-facing features, no schema changes, no new production dependencies. Every improvement is either a security fix, a correctness fix, a quality gate, or a performance optimization on existing primitives. All four research files drew from direct codebase analysis and confirm the same execution path: clean the code first, add CI second, harden security third, fix embedding parity fourth, and optimize performance last.

The recommended approach requires zero new production packages. Security hardening uses Starlette's existing `BaseHTTPMiddleware` and stdlib `secrets.compare_digest()`. CI uses GitHub Actions with the already-established `pytest-cov` dev dependency. The critical correctness gap — memories added via the REST API are invisible to semantic search — is fixed by calling `_embed_and_store` (already imported in `api.py`) via `asyncio.to_thread` after each commit. Performance improvements use `asyncio.gather` with a bounded semaphore and `concurrent.futures.ThreadPoolExecutor`, both stdlib.

The most significant risks are ordering-related rather than technical. CI will permanently fail if added before ruff warnings are cleared (30 warnings exist today). CORS lockdown will break the dashboard if only `127.0.0.1` is included without `localhost`. Broad `except Exception` in the embeddings path must not be naively narrowed — those clauses implement intentional graceful degradation for optional ONNX dependencies that raise non-stdlib exceptions. The recommended phase sequence resolves all dependency chains and lets each phase be validated by the CI pipeline established in Phase 2.

## Key Findings

### Recommended Stack

The v1.1 milestone requires no new production dependencies. All security and performance primitives are already available in the installed stack: `starlette.middleware.base.BaseHTTPMiddleware`, `asyncio.to_thread()`, `asyncio.Semaphore`, `concurrent.futures.ThreadPoolExecutor`, and `secrets.compare_digest()` are all stdlib or already-installed packages. CI infrastructure is GitHub Actions with `actions/checkout@v4`, `actions/setup-python@v5`, and `actions/upload-artifact@v4` — stable since 2023-2024. The `pytest-cov` dev dependency already exists in `pyproject.toml`.

**Core technologies:**
- Python 3.11+ / stdlib asyncio — concurrent embedding and import via `to_thread` and `Semaphore`; no new packages
- Starlette `>=0.40.0` — `BaseHTTPMiddleware` for API key auth; `CORSMiddleware` config change only
- GitHub Actions — CI/CD pipeline (zero Python module deps; YAML config only)
- pytest-cov — coverage enforcement at `--cov-fail-under=80`; already in dev deps
- ruff + mypy — lint and type checking; already configured in `pyproject.toml`

**Do not add:**
- `authlib` / `python-jose` — static bearer token via env var is the correct pattern for a localhost personal tool
- `pre-commit` — adds friction during active refactor; CI gates provide equivalent protection
- `tox` — GitHub Actions matrix handles Python version testing directly

See `.planning/research/STACK.md` for full rationale, code samples, and alternatives considered.

### Expected Features

All v1.1 features are internal improvements, not user-facing capabilities.

**Must have (table stakes — P1):**
- CORS lockdown — `allow_origins=["*"]` replaced with explicit `localhost` and `127.0.0.1` variants
- Import path restriction — `api_import` boundary check preventing filesystem traversal
- Ruff auto-fix (26 of 30 warnings) — prerequisite for any CI pipeline
- Ruff manual fixes (4 remaining warnings) — SIM105, TC002, F821, B007
- Narrow broad `except Exception` — 5 call sites in `embeddings.py`, `pid.py`, `updater.py`
- Remove `remind_me_mcp_original.py` — 2,495-line monolith with zero active imports
- API embedding parity (POST /api/memories) — `api_add` must call `_embed_and_store` after commit
- API embedding parity (PUT/PATCH /api/memories/{id}) — `api_update` must re-embed on content change
- GitHub Actions CI pipeline — ruff + pytest + coverage on push/PR
- Coverage enforcement at 80% threshold

**Should have (P2 — defer to v1.2 if time is short):**
- Batch reindex — 5-10x speedup for cold-start reindex by calling `embedder.embed(batch_list)` instead of one-at-a-time; matters at 500+ memories
- Concurrent file import — `ThreadPoolExecutor(max_workers=4)` in `import_directory`; matters at 100+ file directories
- Optional API auth token — `REMIND_ME_API_KEY` env var checked by `ApiKeyMiddleware`; matters only for users who expose the dashboard outside localhost

**Defer (v2+):**
- Semantic search in REST API `/api/search` (currently FTS5 only)
- mypy strict mode (requires annotating all 190 tests and internal helpers)
- HTTPS/TLS for dashboard (self-signed certs add browser warnings; CORS lockdown covers the actual threat)

See `.planning/research/FEATURES.md` for full feature dependency graph and implementation notes.

### Architecture Approach

The v1.1 architecture is additive overlays on the existing 10-module system. No new layers, no new module boundaries, no DB schema changes. Seven existing modules receive targeted edits; one new file is created (`.github/workflows/ci.yml`); one file is deleted (`remind_me_mcp_original.py`). The existing module dependency graph is acyclic and stays acyclic — all v1.1 changes follow existing import directions. New security config env vars default to backward-compatible values so existing deployments are not broken on upgrade.

**Major components and their v1.1 changes:**
1. `api.py` — CORS lockdown (middleware config), optional `ApiKeyMiddleware`, `_embed_and_store` calls in `api_add`/`api_update`, path restriction in `api_import`
2. `config.py` — three new env vars: `REMIND_ME_CORS_ORIGINS`, `REMIND_ME_API_KEY`, `REMIND_ME_IMPORT_ROOT`; all default to backward-compatible values
3. `db.py` — new `_batch_embed_and_store(pairs)` helper for bulk reindex; existing `_embed_and_store` unchanged
4. `importer.py` — `import_directory()` gains `ThreadPoolExecutor(max_workers=4)` for concurrent file processing
5. `embeddings.py` / `pid.py` — narrow `except Exception` to specific types where safe; preserve broad clauses at graceful-degradation boundaries
6. `tools.py` — `remind_me_reindex` replaces per-item loop with `_batch_embed_and_store` single-call pattern
7. `.github/workflows/ci.yml` — new file; Python 3.11 + 3.12 matrix; ruff, pytest with coverage enforcement

See `.planning/research/ARCHITECTURE.md` for full data flow diagrams and integration points.

### Critical Pitfalls

1. **CORS lockdown breaks dashboard** — `localhost` and `127.0.0.1` are different browser origins. Always include both in `allow_origins`. Test dashboard fetch calls from both URL variants after any CORS change.

2. **Auth applied to wrong layer breaks MCP tools** — Starlette middleware applies only to the HTTP REST path. MCP tools call `_get_db()` directly and bypass Starlette entirely. Keep auth in `_build_api_app()` only; never move DB singleton inside the Starlette app.

3. **CI fails nondeterministically due to ONNX model download** — `pip install -e ".[semantic]"` in CI will trigger a ~90MB HuggingFace download on cold runs. Always use the `mock_embedder` fixture in CI; never instantiate the real `_Embedder` in automated tests.

4. **Side-effect import removed during ruff cleanup** — `import remind_me_mcp.tools` in `__main__.py` registers all 15 MCP tools. It is flagged `F401` (unused). Removing it silently empties the MCP tool registry. Strengthen the noqa comment; add a test asserting tool count.

5. **Broad `except Exception` in embeddings path is intentional** — ONNX Runtime raises custom exceptions outside the stdlib hierarchy. Narrowing `except Exception` in `_ensure_loaded()` and `_get_embedder()` risks letting ONNX errors escape to MCP tool handlers, turning graceful degradation into server crashes. Narrow only at DB operation sites with known exception types; preserve the broad clause at the embedder boundary.

See `.planning/research/PITFALLS.md` for all 8 critical pitfalls with recovery strategies and phase-to-pitfall mapping.

## Implications for Roadmap

Based on all four research files, the dependency chain is clear: lint must be clean before CI can be added; CI must be live before security changes are validated automatically; security must be stable before touching `api.py` for embedding parity; performance optimizations carry the most concurrency risk and belong last. The following five-phase structure is the direct output of that analysis.

### Phase 1: Code Quality and Cleanup

**Rationale:** Zero behavioral change, zero risk. Run `ruff check --fix` (26 auto-fixes), make 4 manual edits, narrow exception handlers at safe sites, delete the monolith file. This phase is required before CI is added — adding CI to a repo with 30 ruff warnings creates a permanently red pipeline. Starting here also produces a clean baseline and verifies the test suite passes before adding any new code.

**Delivers:** Clean ruff output, no unused imports, no broad swallowed exceptions at known sites, no contributor confusion from the dead monolith file.

**Addresses:** ruff auto-fix, ruff manual fixes, narrow broad except, remove monolith file (all P1 from FEATURES.md).

**Avoids:** Pitfall 4 (side-effect import removed during ruff cleanup) — audit all existing `# noqa` suppressions before touching them. Pitfall 5 (graceful degradation broken) — preserve broad except at embedder boundaries where ONNX exception types are non-stdlib.

### Phase 2: CI/CD Pipeline

**Rationale:** After Phase 1 makes all checks green locally, codify those checks in GitHub Actions. From this point forward, every subsequent phase gets automatic regression validation. CI is the force multiplier for all remaining work.

**Delivers:** `.github/workflows/ci.yml` with Python 3.11 + 3.12 matrix, ruff lint gate, pytest with 80% coverage enforcement, coverage artifact upload.

**Addresses:** GitHub Actions CI pipeline, coverage enforcement (both P1 from FEATURES.md).

**Avoids:** Pitfall 3 (CI fails from ONNX model download) — all embedding tests use `mock_embedder` fixture; never instantiate real `_Embedder` in CI. Pitfall 5 (coverage threshold set wrong) — measure actual coverage first with `pytest --cov` (no `--cov-fail-under`), then set threshold at measured value.

### Phase 3: Security Hardening

**Rationale:** Security fixes are additive and backward compatible (all new env vars default to safe permissive values). Must happen before API embedding parity changes to `api.py` to avoid merge conflicts and to get CI validation of security tests. CORS lockdown is the highest-severity open issue.

**Delivers:** CORS locked to localhost origins, optional API key middleware in Starlette, import path restriction defaulting to `~`.

**Addresses:** CORS lockdown, import path restriction (both P1 from FEATURES.md). Optional API auth token (P2 — include if scope allows).

**Avoids:** Pitfall 1 (CORS breaks dashboard) — include both `127.0.0.1` and `localhost` in allow_origins. Pitfall 2 (auth on wrong layer) — middleware applied only to `_build_api_app()`; MCP tool handlers untouched. Pitfall 3 (symlink blocking) — make import root configurable via `REMIND_ME_IMPORT_ROOT` env var, defaulting to `~`.

### Phase 4: API Embedding Parity

**Rationale:** This is the critical correctness fix: memories added or updated via the dashboard are invisible to semantic search. The fix is two `asyncio.to_thread(_embed_and_store, ...)` calls — one in `api_add`, one in `api_update`. Separated from Phase 3 to keep `api.py` diffs reviewable, and because CI from Phase 2 will validate the new test coverage automatically.

**Delivers:** REST API memories are immediately embedded on creation and update, reaching full parity with MCP tool behavior. As a side effect, resolves the F401 unused-import ruff warning for `_embed_and_store` in `api.py`.

**Addresses:** API embedding parity for POST and PUT/PATCH routes (both P1 from FEATURES.md).

**Avoids:** Pitfall 7 (event loop blocking) — wrap `_embed_and_store` in `asyncio.to_thread()` or Starlette `BackgroundTask`; never call synchronously in async handler. Tests must verify vector table row exists, not just that the function was called.

### Phase 5: Performance Improvements

**Rationale:** Performance changes carry the highest behavioral risk (concurrency, DB write serialization patterns) and are rated P2 — real improvements but not blocking correctness. Doing them last means CI is fully established and the security/parity surface is stable. Batch reindex matters at 500+ memories; concurrent import matters at 100+ file directories. Most users will not hit these limits in the v1.1 timeframe.

**Delivers:** `_batch_embed_and_store()` in `db.py` for 5-10x reindex speedup; `ThreadPoolExecutor(max_workers=4)` in `import_directory()` for approximately 3-4x import speedup on large directories.

**Addresses:** Batch reindex (P2), concurrent file import (P2) from FEATURES.md.

**Avoids:** Avoid unbounded `asyncio.gather` for file imports — use `Semaphore(8)` cap. Avoid `ProcessPoolExecutor` for embedding — ONNX manages its own thread pool internally. SQLite WAL + `busy_timeout=5s` handles concurrent import writes via serialization — acceptable, not a bug.

### Phase Ordering Rationale

- Lint must precede CI: 30 existing ruff warnings guarantee a permanently red pipeline if CI is added first.
- CI must precede security: CI validates every subsequent change automatically; adding it early is the force multiplier.
- Security must precede embedding parity: both touch `api.py`; sequential changes keep diffs small and reviewable.
- Performance is last: highest behavioral risk (concurrency), lowest correctness priority, most isolated from other phases.
- This five-phase structure directly reflects the feature dependency graph in FEATURES.md, the build order prescribed in ARCHITECTURE.md, and the pitfall-to-phase mapping in PITFALLS.md.

### Research Flags

Phases with standard patterns (no additional `/gsd:research-phase` needed):
- **Phase 1 (Code Quality):** ruff fixes and exception narrowing are mechanical; well-understood Python idioms
- **Phase 2 (CI/CD):** GitHub Actions Python CI is extensively documented; matches existing project setup
- **Phase 3 (Security):** Starlette middleware patterns are stable and established; zero new packages
- **Phase 4 (Embedding Parity):** `asyncio.to_thread` pattern already used in `tools.py`; no new territory
- **Phase 5 (Performance):** `ThreadPoolExecutor` I/O-bound concurrency is a standard Python idiom

No phase requires a research-phase invocation. All implementation patterns are directly grounded in the existing codebase or well-documented stdlib/Starlette behavior.

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | Direct `pyproject.toml` inspection; no new packages needed; all tools already installed and in use |
| Features | HIGH | Full codebase analysis; ruff output confirmed live; all findings grounded in actual file inspection with line numbers |
| Architecture | HIGH | 10-module graph fully mapped; dependency directions confirmed; no circular import risk from any proposed change |
| Pitfalls | HIGH | CORS origin matching, ONNX exception hierarchy, side-effect import, event loop blocking — all grounded in direct code inspection or established web standards |

**Overall confidence:** HIGH

### Gaps to Address

- **ONNX exception class name (LOW confidence):** ONNX Runtime may expose its custom exception as `onnxruntime.capi.onnxruntime_pybind11_state.Fail` or as a `RuntimeError` subclass. Verify with `python -c "import onnxruntime; help(onnxruntime)" 2>&1 | grep -i exception` before narrowing `except Exception` in `embeddings.py`. If the type is non-stdlib, keep the broad clause and add a comment documenting the intent.

- **Coverage threshold calibration:** Set threshold after measuring, not before. Run `pytest --cov=remind_me_mcp --cov-report=term-missing` against the clean Phase 1 codebase and read the actual percentage. Start enforcement at (measured - 2%) to allow headroom for new code added in Phases 3-5.

- **GitHub Actions version pinning:** `actions/checkout@v4`, `actions/setup-python@v5`, `actions/upload-artifact@v4` are MEDIUM confidence (training knowledge through August 2025). Confirm these are still current before adding the CI workflow.

- **Dashboard fetch headers for optional API auth:** If `REMIND_ME_API_KEY` is set, `dashboard/App.jsx` must include `Authorization: Bearer <token>` in all fetch calls. The dashboard is out of scope for v1.1 JS changes. Document this as a known gap: users who set `REMIND_ME_API_KEY` will need to manually update their dashboard bundle or access the API via curl or MCP tools only.

## Sources

### Primary (HIGH confidence)

- `/home/baileyrd/projects/remind_me/remind_me_mcp/api.py` — CORS config (line 345), `api_add` (lines 212-238), `api_update` (lines 240-276), `api_import` path handling (line 299)
- `/home/baileyrd/projects/remind_me/remind_me_mcp/tools.py` — reindex serial loop (line 766), `_embed_and_store` call sites (lines 117, 360, 638)
- `/home/baileyrd/projects/remind_me/remind_me_mcp/embeddings.py` — `embed()` batch method (line 88), broad except locations (lines 82, 145, 164)
- `/home/baileyrd/projects/remind_me/remind_me_mcp/importer.py` — sequential file loop (line 374)
- `/home/baileyrd/projects/remind_me/remind_me_mcp/pid.py` — broad except (line 102)
- `/home/baileyrd/projects/remind_me/pyproject.toml` — declared dependencies, ruff/mypy config, pytest config
- `/home/baileyrd/projects/remind_me/tests/conftest.py` — fixture architecture, `mock_embedder` pattern
- `/home/baileyrd/projects/remind_me/.planning/PROJECT.md` — v1.1 scope, out-of-scope constraints
- Ruff live output — 30 warnings (26 auto-fixable): I001, F401, F541, UP045, UP037, UP017, SIM105, B007, TC002, F821

### Secondary (MEDIUM confidence)

- Training knowledge: Starlette `CORSMiddleware` exact-origin matching behavior — established web standard, HIGH confidence in correctness
- Training knowledge: GitHub Actions `actions/checkout@v4`, `actions/setup-python@v5` — stable since 2023; verify current version pinning before use
- Training knowledge: `asyncio.to_thread()` / `asyncio.Semaphore` / `concurrent.futures.ThreadPoolExecutor` — stdlib since Python 3.9; HIGH confidence in API stability

### Tertiary (LOW confidence)

- ONNX Runtime exception hierarchy — custom exception types (`onnxruntime_pybind11_state.Fail`) not in stdlib; verify with installed package before narrowing exception handlers

---
*Research completed: 2026-02-24*
*Ready for roadmap: yes*
