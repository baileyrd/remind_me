# Pitfalls Research

**Domain:** Adding security hardening, CI/CD, performance improvements, API embedding parity, and code quality cleanup to an existing Python MCP server with 190 passing tests
**Researched:** 2026-02-24
**Confidence:** HIGH (direct codebase analysis + established patterns for each change type)

---

## Critical Pitfalls

### Pitfall 1: CORS Lockdown Breaks the Dashboard's Own Fetch Calls

**What goes wrong:**
The current `CORSMiddleware` uses `allow_origins=["*"]` (line 345 in `api.py`). Locking it down to `["http://127.0.0.1:5199"]` breaks the dashboard when the user accesses it via `http://localhost:5199` — because `localhost` and `127.0.0.1` are treated as different origins by browsers. The dashboard's React code calls `/api/*` routes; if the origin header doesn't match, preflight OPTIONS requests return 403 and all API calls silently fail in the browser. The MCP tools and CLI continue working because they bypass CORS entirely.

**Why it happens:**
CORS origin matching is exact string comparison. `http://127.0.0.1:5199` and `http://localhost:5199` are different origins despite resolving to the same address. Developers test the lockdown with one URL and don't notice the other is broken.

**How to avoid:**
Include both variants in the allowed origins list:
```python
allow_origins=["http://127.0.0.1:5199", "http://localhost:5199"]
```
Or make the allowed origins configurable via an environment variable so users who bind to a non-default host/port can add their origin without code changes. Test dashboard fetch calls from both `127.0.0.1` and `localhost` URLs after any CORS change.

**Warning signs:**
- Dashboard loads (HTML served) but shows blank memory list with no error message.
- Browser DevTools Network tab shows failed OPTIONS preflight requests returning 403.
- `api/stats` returns data when curl'd directly but not from the browser dashboard.

**Phase to address:** Security hardening phase — CORS change must include both URL variants.

---

### Pitfall 2: API Authentication Breaks Existing MCP Tool Calls If Applied to Wrong Layer

**What goes wrong:**
Adding authentication (e.g., a Bearer token or API key header check) to the Starlette API app protects the REST/dashboard layer — but nothing in the MCP path goes through Starlette. MCP tools use `_get_db()` directly, not via HTTP. If a developer confuses the two paths and adds auth middleware to `_build_api_app()`, the MCP tools are unaffected. But if they mistakenly move the `_get_db()` singleton into the Starlette layer (to "centralize" the auth check), they break MCP tool handlers that call `_get_db()` directly from `tools.py`.

**Why it happens:**
The dual-path architecture (MCP stdio transport + HTTP REST) is easy to misread. The Starlette app and the MCP server share the same database singleton but are completely separate entrypoints. Auth changes to one have no effect on the other.

**How to avoid:**
Auth for the dashboard API applies only to `_build_api_app()` middleware. MCP tools are inherently protected by the MCP transport layer (Claude client auth). Never move the DB singleton inside the Starlette app — keep `_get_db()` in `db.py` as a module-level function accessible from both paths. Write a test after auth is added that exercises MCP tool handlers directly (not via HTTP) and confirms they still work without an auth token.

**Warning signs:**
- MCP tools stop responding after "dashboard security" changes.
- Auth works in curl tests but Claude reports MCP tools unavailable.
- `_get_db()` starts getting called inside request handlers instead of being imported at module level.

**Phase to address:** Security hardening phase — auth changes must be isolated to the HTTP layer.

---

### Pitfall 3: Import Path Restriction Using `Path.resolve()` Blocks Symlinks and Home-Dir Aliases

**What goes wrong:**
The current `api_import` handler (line 299 in `api.py`) does `Path(file_path).expanduser().resolve()`. Adding a path restriction like `if not p.is_relative_to(Path.home())` to prevent directory traversal attacks will reject legitimate symlinks that resolve outside `~`. For example, a user who stores chat exports at `/data/claude_exports` with a symlink at `~/claude_exports` — the symlink resolves to `/data/claude_exports`, which is not relative to `~`, so the import is blocked even though the user intentionally placed it there.

**Why it happens:**
`Path.resolve()` follows symlinks before restriction checks. A restriction on the resolved path is more aggressive than intended — it blocks symlinked paths the user explicitly set up.

**How to avoid:**
Restrict to a configurable `ALLOWED_IMPORT_DIRS` list (env var) rather than a hard-coded rule. Default to `["~/"]` for personal installs. If no allowed dirs are configured, apply the restriction only against obvious traversal patterns (`..` components in the raw path before expansion). Test with symlinks as well as direct paths.

**Warning signs:**
- Import API returns "path not allowed" for files that exist and the user can read.
- Users with non-standard home dir layouts (symlinked data drives) cannot import.
- CI passes (uses direct `tmp_path`) but real-world users report path restriction errors.

**Phase to address:** Security hardening phase — path restriction implementation.

---

### Pitfall 4: CI Pipeline Fails Nondeterministically Due to Embedding Model Download

**What goes wrong:**
The ONNX embedding model is downloaded from HuggingFace Hub on first use. If CI runs tests without the `[semantic]` extras installed, embedding tests are skipped correctly. But if someone adds a CI job that installs `[semantic]` to test embedding parity, the first run downloads ~90MB from HuggingFace Hub. This is slow, fails on rate-limited CI environments, and varies by network conditions — creating nondeterministic test timing and occasional `ConnectionError` failures.

**Why it happens:**
The model download is implicitly triggered by `_ensure_loaded()` inside `_Embedder`. Tests with `mock_embedder` fixture avoid this, but any test that uses the real embedder (e.g., integration tests for embedding parity) will trigger a download on cold CI.

**How to avoid:**
Always use `mock_embedder` fixture in CI. Never use the real `_Embedder` in automated tests — the `FakeEmbedder` in `conftest.py` already exists for this purpose. For the embedding parity feature tests, test that `_embed_and_store` is called (via mock assertion), not that real vectors are produced. Cache the HuggingFace model in CI if real embedding tests are truly needed (use `actions/cache` keyed on model name).

**Warning signs:**
- CI jobs take 2-3x longer on first run than subsequent runs.
- Intermittent `huggingface_hub.utils._errors.EntryNotFoundError` or `ConnectionError` in CI logs.
- Test suite passes locally (model already cached) but fails in CI on pull requests.

**Phase to address:** CI/CD setup phase — model caching strategy must be decided before embedding tests run in CI.

---

### Pitfall 5: Coverage Enforcement Breaks When Tests Are Excluded Inconsistently

**What goes wrong:**
Adding `--cov-fail-under=90` to CI without carefully configuring `[tool.coverage.omit]` in `pyproject.toml` causes failures from modules that are legitimately hard to cover in unit tests (e.g., `updater.py` with its subprocess git calls, `pid.py` with its `os.kill()` process checks, `__main__.py` with its CLI dispatch). The existing 190 tests were written against the v1.0 scope — adding new modules (security auth, CI helpers) without new tests drops coverage below the threshold and blocks the pipeline.

**Why it happens:**
Coverage thresholds are set optimistically for existing code and then new untested code is added. The threshold that was "safe" yesterday becomes a daily blocker tomorrow.

**How to avoid:**
Set the initial threshold to match actual current coverage (measure first, enforce second). Use `[tool.coverage.omit]` to exclude paths that require subprocess mocking (`updater.py`, CLI entrypoints) and are tested separately. Add a `make coverage` target that shows per-file coverage so regressions are visible before CI catches them. When adding new modules (e.g., auth middleware), write the tests in the same PR as the code.

**Warning signs:**
- `FAIL Required test coverage of X% not reached. Total coverage: Y%` on PRs that add non-test code.
- Developers add modules to `omit` to make CI green rather than writing tests.
- Coverage report shows 0% on new modules because they were merged without tests.

**Phase to address:** CI/CD setup phase — threshold must be measured before being enforced.

---

### Pitfall 6: Ruff Cleanup Introduces Runtime Bugs by Removing "Unused" Imports That Are Side-Effect Registrations

**What goes wrong:**
`pyproject.toml` has `select = ["F", ...]` which includes `F401` (unused imports). The line in `__main__.py`:
```python
import remind_me_mcp.tools  # noqa: F401 — ensure tools are registered before mcp.run()
```
This import exists solely for its side effect: registering all `@mcp.tool()` decorated functions onto the `mcp` FastMCP instance. Ruff flags it as unused (`F401`). The existing `# noqa: F401` suppresses it. But during "ruff cleanup" if a developer removes this suppression comment (or the import itself), all MCP tools vanish silently — the server starts but reports zero tools to Claude.

**Why it happens:**
The registration side effect is invisible to static analysis. The comment explains the intent, but cleanup work often treats `# noqa` suppressions as "technical debt to fix" rather than as intentional suppressions.

**How to avoid:**
Before removing any `# noqa` suppression, understand why it was added. Change the comment format to make it unmissable:
```python
import remind_me_mcp.tools  # noqa: F401 — CRITICAL: side-effect import, registers all MCP tools
```
Add a test that asserts the tool count after import: `assert len(registered_tools) == 15`. This test fails loudly if the import is removed, giving immediate feedback.

**Warning signs:**
- Claude reports no memory tools available after "cleanup" changes.
- `len(mcp._tool_manager._tools)` drops from 15 to 0.
- CI passes (no Python syntax errors) but real usage is broken.

**Phase to address:** Code quality phase — review all existing `# noqa` suppressions before touching them.

---

### Pitfall 7: API Path Embedding Parity Breaks Existing API Response Shape

**What goes wrong:**
Currently, `api_add` (line 212 in `api.py`) inserts a memory and returns the row from the database without embedding. Adding `_embed_and_store()` after the insert changes the response timing (embedding takes 50-200ms on CPU) and may change the response shape if not handled carefully. Specifically: if `_embed_and_store` is called synchronously in the request handler, it blocks the Starlette async event loop. If it's called in a background task, the response returns before embedding is complete — which is fine, but the test must not assert that embeddings exist immediately after the API response.

**Why it happens:**
Embedding is slow and async embedding parity is added as an afterthought. Developers add a synchronous `_embed_and_store()` call to the async handler without wrapping it in `asyncio.to_thread()`, blocking the event loop and degrading API responsiveness for all concurrent requests.

**How to avoid:**
Call `_embed_and_store()` via `asyncio.to_thread()` inside the async handler, or use Starlette's `BackgroundTask`:
```python
from starlette.background import BackgroundTask
return JSONResponse(result, background=BackgroundTask(_embed_and_store, db, mem_id, content))
```
The background task approach returns the response immediately and embeds asynchronously. Write tests that verify embedding is eventually stored (not immediately after the response), using a small `asyncio.sleep()` or by checking the vector table in a follow-up query.

**Warning signs:**
- API response times for `POST /api/memories` increase by 100-500ms after parity is added.
- Starlette event loop shows blocking in profiling output.
- Tests for embedding parity pass only because they assert on the mock embedder call, not on actual vector storage.

**Phase to address:** API embedding parity phase — must use `to_thread` or `BackgroundTask` pattern.

---

### Pitfall 8: Broad `except Exception` Narrowing Breaks Graceful Degradation

**What goes wrong:**
The existing broad `except Exception` clauses in `embeddings.py` and `pid.py` are intentional: they catch `ImportError`, `OSError`, `MemoryError`, `RuntimeError`, and other unexpected failures from optional subsystems, logging a warning and returning a fallback value. Narrowing these to specific exception types (e.g., `except (ImportError, RuntimeError)`) risks missing exception types that legitimately occur in production — for example, `ort.InferenceSession` can raise `onnxruntime.capi.onnxruntime_pybind11_state.Fail` (a custom ONNX exception) that is not in the standard exception hierarchy. If that exception escapes the embedder, it propagates to the MCP tool handler and surfaces as an unhandled error to Claude.

**Why it happens:**
Code review feedback says "broad except is bad" and the fix is to narrow it — but narrowing requires knowing every exception type the subsystem can raise, which requires reading the optional dependency's documentation (ONNX Runtime, huggingface_hub), not just Python stdlib.

**How to avoid:**
Keep `except Exception` in `_ensure_loaded()` and `_get_embedder()` — these are the graceful degradation boundaries. Narrow exceptions only at specific DB operation sites where the exception type is fully known (e.g., `except sqlite3.OperationalError`). For `pid.py`'s `_check_ui_server_health`, the current `except Exception` is correct because `urllib.request.urlopen` can raise many things (SSL errors, socket errors, timeout errors) and all should produce `False` rather than propagate.

Add a comment explaining the intentional breadth:
```python
except Exception:  # intentionally broad: ONNX/HF exceptions are not in stdlib hierarchy
    return None
```

**Warning signs:**
- After narrowing, `AttributeError` or custom ONNX exception types escape to MCP tool handlers.
- Semantic search goes from "gracefully unavailable" to "crashing with unhandled exception".
- Users see unexpected error messages in Claude when embedder fails to load.

**Phase to address:** Code quality phase — audit each broad `except` for intent before narrowing.

---

## Technical Debt Patterns

Shortcuts that seem reasonable but create long-term problems.

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|----------|-------------------|----------------|-----------------|
| Setting coverage threshold below actual coverage | CI passes immediately | Team ignores coverage; real regressions go undetected | Never — measure first, set threshold to actual value |
| Hardcoding `allow_origins=["http://127.0.0.1:5199"]` in CORS | Fast security fix | Breaks users with custom ports or hosts | Only if `UI_PORT` and `UI_HOST` are read from config and included dynamically |
| Using `BackgroundTask` for embeddings without retry | Parity ships quickly | Failed embeddings are silently lost — memories exist without vectors | Acceptable for v1.1; add retry in later milestone |
| Adding auth token as plain env var | Simple to configure | Token logged to stderr if config module logs env vars | Acceptable; ensure token is never logged |
| Keeping `remind_me_mcp_original.py` in repo | Reference during migration | Confuses contributors; coverage tool includes it | Never past v1.1 — remove with tests that verify nothing imports it |
| Running all 190 tests on every push in CI | Full regression coverage | CI is slow for trivial changes | Acceptable at current scale; split slow/fast if CI exceeds 3 min |

---

## Integration Gotchas

Common mistakes when connecting the new features to the existing system.

| Integration | Common Mistake | Correct Approach |
|-------------|----------------|------------------|
| GitHub Actions + pytest | Caching `.venv/` without invalidating on `pyproject.toml` changes | Cache key must include `pyproject.toml` hash: `hashFiles('pyproject.toml')` |
| GitHub Actions + SQLite | Tests write to `~/.remind-me/` because config module imports at test collection time | Ensure `conftest.py` session fixture runs before any module-level DB access — verify with `REMIND_ME_MCP_DIR` env var set in CI |
| Starlette CORSMiddleware + CORS lockdown | `allow_credentials=True` requires exact origins (not `["*"]`) — Starlette raises an error if both are set | If adding credentials support, set exact origins AND `allow_credentials=True` together |
| Concurrent file processing + SQLite WAL | asyncio.gather over file imports each calling `_get_db()` — WAL allows multiple readers but only one writer at a time | Serialize writes with an asyncio.Lock; reads can proceed concurrently |
| Ruff + `# noqa` suppressions | Ruff `--fix` mode removes `# noqa` comments it considers "unnecessary" if the violation no longer exists in that ruff version | Pin ruff version in CI; review all `# noqa` suppressions after upgrading ruff |
| GitHub Actions + optional deps (`[semantic]`) | `pip install -e ".[semantic]"` installs ONNX; first test run downloads model from HuggingFace | Use `mock_embedder` fixture for all CI tests; never instantiate real `_Embedder` in tests |

---

## Performance Traps

Patterns that work at current scale but are traps to avoid during v1.1 optimization work.

| Trap | Symptoms | Prevention | When It Breaks |
|------|----------|------------|----------------|
| Synchronous embedding in async API handler | `POST /api/memories` blocks for 100-500ms per request; concurrent imports block each other | Wrap `_embed_and_store` in `asyncio.to_thread` or `BackgroundTask` | Any concurrent dashboard usage |
| N+1 embedding calls in batch reindex | Reindexing 1,000 memories makes 1,000 individual ONNX inference calls | Batch memories into groups of 32-64 for `embedder.embed(batch)` | Immediately — reindex of >100 memories takes minutes not seconds |
| FTS5 `rebuild` during server uptime | `INSERT INTO memories_fts(memories_fts) VALUES('rebuild')` holds a write lock for the full duration | Run rebuild only in maintenance mode or as a CLI-only operation, not during request handling | Any concurrent search during rebuild |
| `asyncio.gather` for file import without bounded concurrency | Importing a directory of 500 files launches 500 concurrent tasks — overwhelms I/O and hits SQLite busy lock | Use `asyncio.Semaphore(8)` to cap concurrent file readers | Directories with >50 files |
| `Path.stat().st_size` in API stats on every request | `db_size_mb` computation calls `stat()` on every `/api/stats` hit — not cached | Cache the stat result with a 30-second TTL or compute only on-demand | Not a correctness issue; cache if stats endpoint is called frequently |

---

## Security Mistakes

Domain-specific security issues beyond general web security.

| Mistake | Risk | Prevention |
|---------|------|------------|
| `allow_origins=["*"]` on local-only dashboard | Low risk for personal tool (localhost only), but allows any page the user visits to read/write their memories via CORS | Lock to `["http://127.0.0.1:{port}", "http://localhost:{port}"]` dynamically from config |
| Import path accepts any filesystem path from API | Attacker with API access (if auth is weak) can read any file the server process can read | Restrict to configurable `ALLOWED_IMPORT_DIRS`; reject paths not under allowed roots |
| API auth token in response body of any endpoint | Token leakage via logs or browser history | Never include auth config in API responses; token check should be middleware-only |
| Dashboard served over HTTP (not HTTPS) | Traffic is readable on network for non-localhost binds | Acceptable for localhost-only; document clearly that remote bind requires a reverse proxy with TLS |
| `db_path` exposed in `/api/stats` response | Reveals filesystem layout to anyone who can call the stats endpoint | Acceptable for personal tool; consider omitting if auth is added and dashboard becomes semi-public |

---

## "Looks Done But Isn't" Checklist

Things that appear complete but are missing critical pieces.

- [ ] **CORS lockdown:** Verify both `http://127.0.0.1:{port}` AND `http://localhost:{port}` work — test dashboard fetch calls from both URLs, not just server-side curl.
- [ ] **CI pipeline:** Verify tests run against a fresh temp database, not `~/.remind-me/memory.db` — check that the session-scoped `tmp_memory_dir` fixture activates before CI test collection.
- [ ] **Coverage enforcement:** Verify the threshold reflects actual coverage before enforcing — run `pytest --cov` without `--cov-fail-under` first; set threshold to (actual - 2%) to allow headroom.
- [ ] **Embedding parity:** Verify that memories added via REST API appear in semantic search results — not just that `_embed_and_store` was called, but that the vector table has the row.
- [ ] **Monolith removal:** Verify nothing in the codebase imports from `remind_me_mcp_original.py` — grep for any reference before deleting the file.
- [ ] **Ruff cleanup:** Verify all `# noqa` suppressions that were not removed still have their original justification comment intact — don't silently drop the explanation.
- [ ] **Broad exception narrowing:** Verify that graceful degradation still works after narrowing — test with optional dependencies absent (uninstalled from venv) and confirm server starts without error.
- [ ] **Concurrent file processing:** Verify the semaphore-bounded import doesn't deadlock with the existing SQLite WAL busy_timeout — test with a directory of 50+ files.

---

## Recovery Strategies

When pitfalls occur despite prevention, how to recover.

| Pitfall | Recovery Cost | Recovery Steps |
|---------|---------------|----------------|
| CORS lockdown breaks dashboard | LOW | Add missing origin to `allow_origins` list; restart server; no data loss |
| CI downloads ONNX model on every run | LOW | Add `actions/cache` step for `~/.cache/huggingface`; or switch to `mock_embedder` in that job |
| Coverage threshold blocks all PRs | LOW | Temporarily lower threshold; add missing tests in a follow-up PR |
| Broad `except` narrowed, graceful degradation broken | MEDIUM | Revert narrowing; re-narrow with full list of exception types from ONNX/HF docs |
| Side-effect import removed, all MCP tools gone | MEDIUM | Restore `import remind_me_mcp.tools` with `# noqa: F401` comment; Claude sessions need server restart |
| Embedding parity blocks event loop | MEDIUM | Move `_embed_and_store` to `BackgroundTask` or `asyncio.to_thread`; no data loss but response times were degraded |
| `remind_me_mcp_original.py` deleted before all imports migrated | HIGH | Restore from git; audit all imports; plan systematic removal |
| Auth middleware accidentally applied to MCP path | HIGH | Revert auth change; re-apply only to `_build_api_app()`; verify MCP tool calls work again |

---

## Pitfall-to-Phase Mapping

How roadmap phases should address these pitfalls.

| Pitfall | Prevention Phase | Verification |
|---------|------------------|--------------|
| CORS lockdown breaks dashboard (Pitfall 1) | Security hardening | Test dashboard API calls from both `127.0.0.1` and `localhost` URLs |
| Auth applied to wrong layer — MCP path broken (Pitfall 2) | Security hardening | Run MCP tool handler tests (not HTTP) after auth changes |
| Path restriction blocks symlinks (Pitfall 3) | Security hardening | Test import with symlinked path before declaring done |
| ONNX model download in CI (Pitfall 4) | CI/CD setup | Verify CI logs show no HuggingFace HTTP requests; all embedding tests use `mock_embedder` |
| Coverage threshold set wrong (Pitfall 5) | CI/CD setup | Run `pytest --cov` without threshold first; set threshold to measured value |
| `# noqa` side-effect import removed (Pitfall 6) | Code quality | Assert tool count after any `__main__.py` changes |
| Embedding parity blocks event loop (Pitfall 7) | API embedding parity | Profile `/api/memories` POST under concurrent load; p95 latency must not increase >50ms |
| Broad except narrowed, degradation broken (Pitfall 8) | Code quality | Test with `[semantic]` extras uninstalled; server must start without error |

---

## Sources

- Direct codebase analysis of `remind_me_mcp/api.py`, `embeddings.py`, `pid.py`, `__main__.py`, `db.py`, `config.py` — HIGH confidence
- `tests/conftest.py` analysis for fixture patterns and CI isolation strategy — HIGH confidence
- `pyproject.toml` for ruff config, pytest config, and dependency declarations — HIGH confidence
- Starlette CORS middleware documentation: exact origin matching behavior — HIGH confidence (well-established web standard)
- Python asyncio documentation: `to_thread` thread pool executor requirements — HIGH confidence
- SQLite WAL mode documentation: single-writer multiple-reader behavior — HIGH confidence
- ONNX Runtime exception hierarchy: custom exception types not in stdlib — MEDIUM confidence (based on training knowledge; verify against current ONNX Runtime docs)
- GitHub Actions caching: `hashFiles()` syntax for dependency invalidation — HIGH confidence
- FastMCP registration model: side-effect import pattern — HIGH confidence (direct code inspection)

---
*Pitfalls research for: Adding security hardening, CI/CD, performance, embedding parity, and code quality to existing Python MCP server*
*Researched: 2026-02-24*
