# Domain Pitfalls: Python MCP Server Refactoring

**Domain:** Python monolith-to-package refactor — modularization, test introduction, error handling revision, sync-to-async wrapping
**Researched:** 2026-02-22
**Overall confidence:** HIGH (direct codebase analysis + established Python refactoring patterns)

---

## Critical Pitfalls

Mistakes that cause rewrites, silent data corruption, or behavioral regressions.

---

### Pitfall 1: Circular Imports from Naive Module Splitting

**What goes wrong:** The monolith has a flat dependency structure — everything imports everything inline. When split into `db.py`, `tools.py`, `api.py`, `importer.py`, etc., circular import chains emerge because the split follows layer names rather than actual dependency direction. Example: `tools.py` imports from `db.py`, `db.py` imports from `embeddings.py`, and `embeddings.py` imports a helper that was in `db.py`. Python raises `ImportError: cannot import name X from partially initialized module`.

**Why it happens:** In a monolith, helpers defined in one section are freely used by all later sections because Python resolves them at runtime within the same module namespace. When you move them to separate files, the implicit ordering disappears and cycles surface.

**Specific risk in this codebase:** `_get_db()` and `_ensure_schema()` are used by tool handlers, the HTTP API, and the embedder. `_Embedder` is used by both `db.py` helpers and tool handlers. If `embeddings.py` imports any typing or helper from `db.py` and vice versa, a cycle forms. The `import_chat_file` function in `importer.py` needs both `db.py` (`_get_db`, `_make_id`) and `embeddings.py` (`_embed_and_store`) — a star topology that requires careful ordering.

**Consequences:** Import errors at startup that are unrelated to the refactored logic, often hard to trace because the `ImportError` message points to the wrong file.

**Prevention:**
- Map dependencies explicitly before writing any `import` statements: draw or list what each module needs from every other module.
- Use the dependency inversion rule: leaf modules (no dependencies on other project modules) are `db.py`, `embeddings.py`, `models.py`. Modules that depend on leaves: `importer.py`, `tools.py`, `api.py`. The entry point `__init__.py` or `server.py` depends on everything. Never reverse these arrows.
- For shared utilities (`_now_iso`, `_make_id`, `_row_to_dict`), extract them to a `utils.py` that imports only stdlib — zero project-internal imports.
- Use TYPE_CHECKING guard for type-only imports: `if TYPE_CHECKING: from db import Connection` avoids runtime cycles caused by type hints.

**Detection (warning signs):**
- `ImportError: cannot import name X from partially initialized module Y` at startup.
- `AttributeError: module Y has no attribute X` (module partially loaded when import ran).
- Tests pass in isolation but fail when run together (import order-dependent failures).

**Phase:** Module split phase (first structural change). Address immediately before any logic changes.

---

### Pitfall 2: Module-Level State Migration Breaks the Singleton Embedder

**What goes wrong:** `_embedder: _Embedder | None = None` and `_get_embedder()` live at module level in the monolith. When moved to `embeddings.py`, the singleton reference is now scoped to that module. If any other module accidentally imports `_embedder` directly (e.g., `from embeddings import _embedder`) and caches that reference, it holds `None` forever — the singleton update in `_get_embedder()` modifies `embeddings._embedder` but the cached reference in the caller's namespace is stale.

**Why it happens:** Python module-level variables are mutable, but binding a variable name in another namespace via `from X import Y` creates a local binding — it does not stay linked to `X.Y`.

**Specific risk in this codebase:** `_embed_and_store` and `_semantic_search` both call `_get_embedder()` internally. If during refactoring a developer rewrites them to accept the embedder as a parameter and passes it via `from embeddings import _embedder`, the lazy-load pattern silently breaks. The model never loads, semantic search returns nothing, no exception is raised (the code gracefully degrades).

**Consequences:** Silent semantic search degradation. `remind_me_server_status` may report embedder as None even when the model is installed. The bug is invisible without explicit status checks.

**Prevention:**
- Always access singletons via the factory function: `get_embedder()` (public, not the variable directly).
- Make `_embedder` genuinely private — prefix with `__` at module level in `embeddings.py` to cause `AttributeError` if anyone tries to import it directly.
- Integration test: after refactoring, assert that `get_embedder()` called from `tools.py` context returns a non-None `_Embedder` when dependencies are installed.

**Detection:**
- `remind_me_server_status` reports `embedder: None` after refactor even with optional deps installed.
- Semantic search returns zero results for content that was previously findable.

**Phase:** Module split phase — embeddings module extraction.

---

### Pitfall 3: Entry Point Package Path Breaks After Module Split

**What goes wrong:** `pyproject.toml` declares `remind-me-mcp = "remind_me_mcp:mcp.run"`. After splitting into a package (`remind_me_mcp/` directory with `__init__.py`), the entry point path must change to reflect the new location of the `mcp` FastMCP instance. If `mcp` is defined in `remind_me_mcp/server.py`, the entry point becomes `remind_me_mcp.server:mcp.run`. Forgetting to update this causes `pip install -e .` to succeed but `remind-me-mcp` command to fail with `AttributeError: module 'remind_me_mcp' has no attribute 'mcp'`.

**Why it happens:** `pyproject.toml` entry points are not validated at install time — they are resolved at runtime. The error only appears when the command is actually executed.

**Specific risk in this codebase:** This project is used daily via MCP config JSON in Claude Code/Desktop which invokes `remind-me-mcp` directly. A broken entry point silently disables the entire memory system — Claude starts but the MCP server fails to launch, returning no tool responses.

**Consequences:** Total loss of MCP server functionality. Claude sessions lose all memory tool access silently (MCP initialization failure is not always surfaced to the user).

**Prevention:**
- Update `pyproject.toml` entry point in the same commit that moves the `mcp` instance.
- After every structural change, run `pip install -e . && remind-me-mcp --help` as a smoke test.
- Add an entry point smoke test to the test suite: `subprocess.run(["remind-me-mcp", "--help"], check=True)`.

**Detection:**
- `AttributeError` when running `remind-me-mcp`.
- MCP tools silently unavailable in Claude session after install.

**Phase:** Module split phase — immediately when creating the package directory.

---

### Pitfall 4: asyncio.to_thread Wrapping Masks Sync Exceptions Differently

**What goes wrong:** Wrapping `_embed_and_store(id, content)` in `await asyncio.to_thread(_embed_and_store, id, content)` changes exception propagation. Exceptions from the sync function are re-raised in the async context as-is, but the traceback is truncated at the thread boundary. More critically: if the sync function catches all exceptions and returns `False` (existing pattern), wrapping it in `to_thread` does not change that behavior — but developers often assume the wrapper "adds async exception safety" and stop wrapping the outer `await` call with try/except, leaving the async handler unguarded against unexpected thread exceptions (e.g., `MemoryError`, `SystemExit`).

**Why it happens:** The existing code uses "return False on failure" as its error contract for embedding helpers. Wrapping in `to_thread` preserves this contract but doesn't address the deeper anti-pattern. Developers conflate "async-safe" with "exception-safe."

**Specific risk in this codebase:** `_embed_and_store` already swallows most exceptions. The real danger is in wrapping `_get_db()` calls in `to_thread` — SQLite connections are not thread-safe unless `check_same_thread=False`. If `_get_db()` returns a connection created on thread A and it's used on thread B (the event loop thread after `to_thread` returns), SQLite raises `ProgrammingError: SQLite objects created in a thread can only be used in that same thread`.

**Consequences:** Intermittent `ProgrammingError` in production that doesn't reproduce in tests (test runs are single-threaded).

**Prevention:**
- For embedding calls: wrap only the CPU-bound `_Embedder.embed()` call in `to_thread`, not the DB write that follows. Keep DB writes on the event loop thread via a dedicated connection strategy.
- Use `sqlite3.connect(..., check_same_thread=False)` explicitly if a connection will be shared across threads, and document the choice.
- Alternatively: use `aiosqlite` for all DB operations, which handles thread safety internally.
- Write an async integration test with `asyncio.gather` running multiple tool calls concurrently to surface thread-safety issues early.

**Detection:**
- `ProgrammingError: SQLite objects created in a thread can only be used in that same thread` under concurrent load.
- Intermittent failures that only appear when multiple MCP tool calls happen within the same event loop tick.

**Phase:** Async wrapping phase.

---

### Pitfall 5: Schema Migration IF NOT EXISTS Guard Prevents Column Additions

**What goes wrong:** The existing `_ensure_schema()` uses `CREATE TABLE IF NOT EXISTS` guards. When a new column is needed (e.g., `capture_id TEXT` on the `memories` table, or a `memory_tags` junction table with a foreign key), the `IF NOT EXISTS` guard means the DDL does nothing on existing databases — only new databases get the schema. Users running the refactored server against their existing `~/.remind-me/memory.db` silently run against the old schema with missing columns.

**Why it happens:** `IF NOT EXISTS` is idempotent for table creation but does not handle column additions or constraint changes. `ALTER TABLE ADD COLUMN` must be run separately, but there's no migration system to track which alterations have been applied.

**Specific risk in this codebase:** The refactor plan explicitly includes: adding a `capture_id` column (to fix `remind_me_get_capture`), creating a `memory_tags` junction table (to fix pagination), and schema versioning via `PRAGMA user_version`. All three require migration logic. Without it, the refactored code references columns that don't exist on existing databases → `OperationalError: table memories has no column named capture_id`.

**Consequences:** `OperationalError` crashes on existing user databases. Users must manually run DDL or delete their database. Since this is a personal daily-use tool, data loss risk is real.

**Prevention:**
- Implement `PRAGMA user_version` migration system before making any schema changes. Pattern:
  ```python
  current = db.execute("PRAGMA user_version").fetchone()[0]
  if current < 1:
      db.execute("ALTER TABLE memories ADD COLUMN capture_id TEXT")
      db.execute("PRAGMA user_version = 1")
  ```
- Migration system must be the first deliverable in any phase that touches schema.
- Test migrations against a SQLite database seeded with the old schema (fixture file in `tests/fixtures/`).

**Detection:**
- `OperationalError: table X has no column named Y` on startup against existing databases.
- New features silently fail for existing users while working for fresh installs.

**Phase:** Schema changes phase — migration system must precede all schema modifications.

---

### Pitfall 6: Introducing Tests to Untested Code Before Refactoring Locks In Bad Interfaces

**What goes wrong:** Writing tests against the monolith (`from remind_me_mcp import _get_db, memory_add`) before refactoring creates tests tightly coupled to the current internal structure. When the refactor moves `_get_db` to `db.py`, all those tests break — not because of a bug, but because of the import path change. This creates false test failures during refactoring that erode confidence in the test suite.

**Why it happens:** The natural instinct is "write tests first, then refactor." But for a monolith refactor, writing unit tests against unexported internal helpers before the module boundary is defined creates migration overhead, not safety.

**Consequences:** Test maintenance overhead during refactor. Developers disable or skip failing tests "temporarily" during the structural changes — and they stay disabled. The test suite becomes unreliable.

**Prevention:**
- Write tests against the intended final interface, not the current one. If `memory_add` will live in `tools.py`, write `from remind_me.tools import memory_add` even before the module exists — let the tests drive the interface definition.
- For helpers that are being extracted, write integration tests that go through the public interface (MCP tool call → assert DB state) rather than unit tests on internal helpers. These tests survive module renames.
- Use `pytest.importorskip` or `importlib` to allow tests to be written incrementally against not-yet-existing modules.
- Write one "golden path" integration test per MCP tool first, using an in-memory SQLite database. These are the regression safety net during refactoring.

**Detection:**
- Tests that import from `remind_me_mcp` directly (the old monolith path) will break during module split. Count of such imports in test files is a measure of migration debt.

**Phase:** Testing phase — strategy must be decided before writing first test.

---

## Moderate Pitfalls

Mistakes that cause bugs, unexpected behavior, or significant rework — but not rewrites.

---

### Pitfall 7: MCP Tool Names Must Not Change — But Imports of the Handler Functions May

**What goes wrong:** FastMCP registers tools by the `name=` argument in `@mcp.tool(name="remind_me_add")`. The actual Python function name (`memory_add`) is implementation-level. During refactoring, if a developer renames the handler function for clarity (e.g., `add_memory` or `tool_add_memory`) and forgets the `name=` argument is still `"remind_me_add"`, the tool still works. But if the developer also forgets to register the new function with `@mcp.tool()` in the new module, the tool silently disappears.

**Why it happens:** The decorator on the function is the registration act. Moving functions to a new file without ensuring they are decorated and imported into the FastMCP context causes silent unregistration.

**Specific risk in this codebase:** The `mcp` FastMCP instance must be imported or passed to wherever tools are registered. If `tools.py` defines `@mcp.tool()` decorated functions, it must import `mcp` from `server.py`. If `server.py` then imports from `tools.py` (to trigger the registration), this is a star import pattern — circular unless structured carefully.

**Prevention:**
- Keep all `@mcp.tool()` registrations in one file (or use explicit `mcp.tool()(handler_fn)` calls in the server module).
- After every refactor step, assert the tool count: `len(mcp._tool_manager._tools) == EXPECTED_TOOL_COUNT`.
- Write a test that lists all registered tool names and asserts against the expected set.

**Detection:**
- Claude reports "tool not found" or the tool list is shorter than expected.
- `remind_me_server_status` returns a tool count lower than the expected 13.

**Phase:** Module split phase — tool registration restructuring.

---

### Pitfall 8: FTS5 Triggers Silently Diverge if Schema Migration Recreates the Table

**What goes wrong:** If `_ensure_schema()` is refactored and a developer uses `DROP TABLE IF EXISTS memories_fts` followed by `CREATE VIRTUAL TABLE memories_fts` to "clean up" the virtual table, all existing full-text search index entries are lost. The `memories` table still has all records, but FTS5 returns no results. The triggers will re-sync going forward, but historical data is gone until `INSERT INTO memories_fts(memories_fts) VALUES('rebuild')` is run.

**Why it happens:** Developers see the FTS5 trigger code as boilerplate to clean up and may consolidate or recreate it during schema refactoring without realizing the virtual table holds a live index.

**Specific risk in this codebase:** The triggers are currently in `_ensure_schema()` and will be moved to a migration function. If the migration naively drops and recreates the virtual table rather than using `IF NOT EXISTS`, existing user data becomes unsearchable.

**Prevention:**
- Never `DROP` the `memories_fts` virtual table in a migration. Only use `IF NOT EXISTS` guards.
- To change FTS5 configuration (e.g., add a column), use FTS5 rebuild: `INSERT INTO memories_fts(memories_fts) VALUES('rebuild')` after the structural change.
- Add a test that inserts memories, then runs `_ensure_schema()` again (simulating server restart), and asserts FTS search still returns those memories.

**Detection:**
- `remind_me_search` returns no results for known content after a restart.
- `SELECT count(*) FROM memories_fts` returns fewer rows than `SELECT count(*) FROM memories`.

**Phase:** Schema migration phase and any subsequent schema touch.

---

### Pitfall 9: asyncio.to_thread with Blocking DB Calls Causes Event Loop Starvation Under Test

**What goes wrong:** Tests using `pytest-asyncio` with the default event loop run synchronously within a single thread. When production code uses `asyncio.to_thread()`, it works correctly in production (spawns a thread). But in tests with a mocked `asyncio.to_thread` (or without properly configured async test infrastructure), the wrapping either: (a) runs the function synchronously without threading, masking thread-safety bugs, or (b) deadlocks if the event loop is not running with the correct executor.

**Why it happens:** `asyncio.to_thread` requires a running event loop with a thread pool executor. Tests that use `asyncio.run()` or a minimal event loop may not have the executor configured, causing subtle failures.

**Prevention:**
- Use `pytest-asyncio` with `asyncio_mode = "auto"` in `pyproject.toml`.
- Test async functions with `@pytest.mark.asyncio` on every async test.
- Do not mock `asyncio.to_thread` itself — instead mock the underlying sync function being wrapped, so the threading behavior is preserved in tests.
- Use `anyio` as an alternative to `asyncio.to_thread` if cross-backend test compatibility is needed.

**Detection:**
- Tests pass but production sees `RuntimeError: no running event loop` or deadlocks under concurrent calls.
- `asyncio.to_thread()` calls in tests silently execute synchronously when `asyncio` is not properly initialized.

**Phase:** Async wrapping phase and test introduction phase.

---

### Pitfall 10: Test Fixtures Using Real File Paths Depend on Test Execution Order

**What goes wrong:** The codebase uses `~/.remind-me/memory.db` as the database path, determined at module import time via `os.environ.get("REMIND_ME_MCP_DIR", ...)`. Tests that rely on `monkeypatch.setenv("REMIND_ME_MCP_DIR", ...)` to redirect to a temp directory fail silently if the module was already imported before `monkeypatch` ran — the module-level constants were evaluated at import time and point to `~/.remind-me/`.

**Why it happens:** Python module-level constants are computed once at import time. Monkey-patching environment variables after import does not affect already-computed constants.

**Specific risk in this codebase:** `MEMORY_DIR`, `DB_PATH`, `IMPORT_LOG`, `PID_FILE` are all computed at the module top level. Any test that tries to redirect these via `monkeypatch.setenv` will appear to work (the env var changes) but the constants won't update.

**Consequences:** Tests write to the real `~/.remind-me/memory.db`, corrupting the developer's actual memory database. Or tests read from the real database and produce false positives because real memories satisfy the assertions.

**Prevention:**
- Convert module-level constants to lazy functions or class properties: `def db_path() -> Path: return Path(os.environ.get("REMIND_ME_MCP_DIR", "~/.remind-me")) / "memory.db"`. This is called at runtime, not import time.
- Alternatively, use a `Config` dataclass that is instantiated in `__init__.py` or `server.py` and passed as a dependency — testable without environment manipulation.
- In tests, always use `tmp_path` pytest fixture for SQLite databases.
- Write a test that asserts `DB_PATH` is not `~/.remind-me/memory.db` when `REMIND_ME_MCP_DIR` env var is set (catches the constant-vs-lazy distinction).

**Detection:**
- Tests that call `monkeypatch.setenv("REMIND_ME_MCP_DIR", ...)` but DB writes still go to `~/.remind-me/`.
- Test failures that only occur when running the full suite (previous test imported the module with the real path).

**Phase:** Test introduction phase — this must be solved before any integration tests can run safely.

---

### Pitfall 11: Duplicate Import Logic Diverges During Refactoring to a Shared Function

**What goes wrong:** The plan is to extract a shared `import_directory()` function from the two existing implementations (MCP tool handler and HTTP API handler). But the two implementations have subtle behavioral differences: the MCP version validates the directory path using `BulkImportDirInput.validate_dir()`, while the HTTP handler uses inline validation without Pydantic. When merged into one function, the developer must choose one validation approach — and if they choose the Pydantic version, the HTTP API behavior changes; if they choose inline, Pydantic validation gets bypassed for MCP calls.

**Why it happens:** DRY extraction across two different validation contexts (Pydantic MCP vs. manual HTTP) requires explicit interface decisions that weren't needed when the code was duplicated.

**Prevention:**
- Extract the core directory traversal and import logic as a pure function that takes `path: Path, extensions: set[str], ...` — no validation inside.
- Keep validation at the boundary: MCP handler validates via Pydantic before calling the shared function; HTTP handler validates manually before calling the same shared function.
- Write a test that exercises the shared function directly with both valid and invalid inputs (confirms it doesn't secretly validate internally).

**Detection:**
- HTTP import endpoint starts enforcing Pydantic validation rules that weren't there before (behavioral change for API clients).
- Or: MCP import silently accepts paths that should have been rejected (validation gap).

**Phase:** Importer module extraction phase.

---

### Pitfall 12: Moving Lazy Imports Inside Functions to Module Top Level Breaks Optional Dependency Handling

**What goes wrong:** The current code uses lazy imports inside functions for optional dependencies:
```python
from huggingface_hub import hf_hub_download  # inside _ensure_loaded()
from starlette.applications import Starlette   # inside _build_api_app()
```
During refactoring, a developer may "clean up" by moving all imports to the module top level of the new files (`embeddings.py`, `api.py`). This causes `ImportError` at server startup if the optional dependencies are not installed — breaking the graceful degradation that the original code carefully preserved.

**Why it happens:** Top-level imports are convention ("clean" code). Developers unfamiliar with the optional dependency pattern assume all imports should be at the top.

**Specific risk in this codebase:** `onnxruntime`, `tokenizers`, `huggingface_hub`, `sqlite_vec`, `starlette`, `uvicorn` are all optional or context-specific. Moving any of them to module top level in their extracted files causes mandatory import requirements.

**Prevention:**
- Add a comment on every lazy import explaining why it is lazy: `# lazy import — optional dependency, must not fail at module load`.
- In `embeddings.py`, keep all optional dependency imports inside `_ensure_loaded()`.
- In `api.py`, keep Starlette imports inside `get_app()` or use a try/except at module level that sets a flag: `_STARLETTE_AVAILABLE = False`.
- Test: import each module with optional dependencies absent (use a venv without them) and assert no `ImportError`.

**Detection:**
- `ImportError: No module named 'onnxruntime'` at startup even when user didn't install semantic extras.
- `remind-me-mcp` fails to start without `starlette` when dashboard is not being used.

**Phase:** Module split phase — every file that touches optional dependencies.

---

## Minor Pitfalls

Mistakes that cause friction or require small fixes, but don't block functionality.

---

### Pitfall 13: Docstring Coverage Gaps When Moving Functions Between Modules

**What goes wrong:** Private helpers in the monolith (prefixed `_`) have minimal or no docstrings. When moved to their own module and exposed to other modules (even if still underscore-prefixed), they become effectively cross-module API. Refactoring without adding docstrings leaves the new module structure with unexplained functions that future developers (and linters) flag.

**Prevention:**
- Treat module extraction as the trigger for docstring addition. Any function that crosses a module boundary gets a full docstring at extraction time.
- Run `pydocstyle` or configure `ruff` with `D` rules to enforce docstring coverage on all public symbols.

**Detection:** `ruff check` with `D` rules reports missing docstrings.

**Phase:** Every module extraction.

---

### Pitfall 14: `_make_id` Non-Determinism Is a Hidden Bug Amplifier

**What goes wrong:** The plan is to either make `_make_id` truly deterministic (content-hash only) or rename it to `_new_id`. If renamed to `_new_id` but the import side keeps using the old `_make_id` function reference, the rename doesn't propagate everywhere. More subtly: if made deterministic (content hash only) without adding a `UNIQUE` constraint on `content`, deduplication still doesn't work — the same content gets a stable ID, but `INSERT OR IGNORE` is not used in `memory_add`, so a duplicate `INSERT` fails with `IntegrityError` rather than silently deduplicating.

**Prevention:**
- Pair the `_make_id` semantics decision with the correct `INSERT` strategy: deterministic ID → `INSERT OR IGNORE`; non-deterministic ID → `INSERT` (duplicates allowed).
- Search-and-replace all usages of `_make_id` after any rename: `grep -r "_make_id" .` across the codebase.
- Write a test that inserts the same content twice and asserts either deduplication or two separate records — based on the chosen semantics.

**Detection:**
- `IntegrityError: UNIQUE constraint failed` if `_make_id` is made deterministic but `INSERT OR IGNORE` not used.
- Duplicate memories in the database if `_new_id` (non-deterministic) is used but user expects deduplication.

**Phase:** DB module extraction — fix semantics during extraction, not after.

---

### Pitfall 15: MCP Tool Parameter Descriptions Are Part of the User Contract

**What goes wrong:** FastMCP uses the `description=` fields in Pydantic `Field()` definitions to tell Claude how to use each tool. During refactoring, if `MemoryAddInput` is moved to `models.py` and the developer "cleans up" verbosity in the `Field(description=...)` values, Claude's ability to correctly infer parameters degrades. Changes that look like documentation cleanup are actually behavioral changes to the AI interface.

**Prevention:**
- Treat all `Field(description=...)` strings as user-facing API. Do not modify them during structural refactoring — only during deliberate tool interface revisions.
- Add a test that asserts key description strings are present on the Pydantic models.

**Detection:**
- Claude starts providing wrong parameters to tools or asking for clarification on previously obvious parameters.

**Phase:** Models module extraction.

---

### Pitfall 16: pytest-asyncio Strict Mode Requires Explicit Event Loop Scope

**What goes wrong:** `pytest-asyncio>=0.21` deprecated implicit event loop creation and now requires `asyncio_mode = "auto"` in config or explicit `@pytest.mark.asyncio` on every async test. Without configuration, async tests silently run as coroutine objects (not awaited), passing trivially and testing nothing. This is the most common way a "test suite" for async code provides false confidence.

**Prevention:**
- Add to `pyproject.toml`:
  ```toml
  [tool.pytest.ini_options]
  asyncio_mode = "auto"
  ```
- First test to write: an async test that deliberately fails (asserts `False`) to confirm async tests actually execute.

**Detection:**
- Async tests all pass immediately with zero duration (they are not being awaited).
- Coverage shows async function bodies as uncovered despite "passing" tests.

**Phase:** Test infrastructure setup — day one of the testing phase.

---

## Phase-Specific Warnings

| Phase Topic | Likely Pitfall | Mitigation |
|-------------|---------------|------------|
| Create package directory structure | Circular imports (Pitfall 1) | Map dependencies before writing imports |
| Extract `embeddings.py` | Singleton state binding (Pitfall 2) | Always use factory function `get_embedder()` |
| Update `pyproject.toml` entry point | Entry point path breaks (Pitfall 3) | Update in same commit; smoke-test install |
| Extract `importer.py` | Duplicate logic divergence (Pitfall 11) | Extract validation-free core; validate at boundary |
| Move optional dep imports | Graceful degradation broken (Pitfall 12) | Keep all optional imports lazy inside functions |
| Move Pydantic models | Tool description regression (Pitfall 15) | Treat `description=` as API contract |
| Add schema changes | Migration IF NOT EXISTS bug (Pitfall 5) | Implement `PRAGMA user_version` system first |
| Add schema changes | FTS5 trigger divergence (Pitfall 8) | Never DROP `memories_fts`; use rebuild only |
| Set up test infrastructure | Module-level constants not patchable (Pitfall 10) | Convert to lazy functions before first test run |
| Set up test infrastructure | pytest-asyncio not executing async tests (Pitfall 16) | Configure `asyncio_mode = "auto"` on day one |
| Write first tests | Tests lock in monolith interface (Pitfall 6) | Write against final interface, not current one |
| Wrap sync embedding in async | SQLite thread-safety violation (Pitfall 4) | Wrap only CPU-bound embed; keep DB on event loop thread |
| Wrap sync embedding in async | Test event loop starvation (Pitfall 9) | Use `pytest-asyncio` with proper configuration |
| Register MCP tools in new module | Silent tool unregistration (Pitfall 7) | Assert tool count after every registration change |
| Rename `_make_id` | Rename doesn't propagate (Pitfall 14) | Search all usages; pair with INSERT strategy choice |
| Extract functions to modules | Docstring gaps (Pitfall 13) | Add docstrings at extraction time |

---

## Sources

- Direct codebase analysis of `remind_me_mcp.py` (2,500 lines) — HIGH confidence
- `.planning/codebase/CONCERNS.md` — identified bugs, fragile areas, and tech debt — HIGH confidence
- `.planning/codebase/ARCHITECTURE.md` — layer dependencies and data flow — HIGH confidence
- `.planning/codebase/CONVENTIONS.md` — error handling patterns and import conventions — HIGH confidence
- Python packaging docs: entry point specification behavior — HIGH confidence
- Python `asyncio` docs: `to_thread` thread safety guarantees — HIGH confidence
- SQLite FTS5 documentation: virtual table rebuild behavior — HIGH confidence
- `pytest-asyncio` changelog: strict mode introduced in 0.21 — MEDIUM confidence (based on training knowledge, version details should be verified against current docs)
- Python module system: import-time constant evaluation, `from X import Y` binding semantics — HIGH confidence
