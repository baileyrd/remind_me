# Phase 4: Code Quality and Cleanup - Research

**Researched:** 2026-02-24
**Domain:** Python static analysis (ruff), exception handling patterns, dead code removal
**Confidence:** HIGH

## Summary

Phase 4 is a purely mechanical cleanup phase with no new feature risk. The work falls into three independent streams: (1) running ruff to auto-fix linting warnings, (2) manually editing exception handlers in three files to narrow bare `Exception` clauses at safe call sites, and (3) deleting the legacy monolith file. All 190 existing tests already pass and must continue to pass after every change.

The current ruff state is **85 total warnings**: 61 in active code (excluding `remind_me_mcp_original.py`) and 24 in the monolith. After the monolith is deleted (QUAL-03), 58 of the remaining 61 warnings are auto-fixable (43 safe + 15 unsafe); the remaining 3 require manual edits (F821, SIM105, B007). QUAL-02 (narrow exception handlers) is purely manual and affects 5 `except Exception` clauses across `embeddings.py`, `pid.py`, and `updater.py` — the STATE.md blockers list explicitly which to preserve.

The success criteria state "26 auto-fix and 4 manual warnings" — these counts predate full discovery. The actual counts (detailed below) supersede the roadmap numbers; the goal remains the same: `ruff check .` produces zero errors after all fixes are applied.

**Primary recommendation:** Execute in four ordered steps: (1) delete monolith, (2) `ruff --fix` for safe fixes, (3) `ruff --fix --unsafe-fixes` for TC/F841 fixes (safe with `from __future__ import annotations`), (4) three manual edits for F821 + SIM105 + B007, then validate QUAL-02 exception narrowing independently.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| QUAL-01 | All ruff warnings resolved (26 auto-fix + 4 manual per roadmap; actual: 58 auto-fix + 3 manual in active code) | Full ruff warning inventory documented below; fix strategy per warning code |
| QUAL-02 | Broad `except Exception` narrowed to specific types in `embeddings.py`, `pid.py`, `updater.py` at safe call sites; graceful-degradation boundaries preserved | Per-clause analysis with disposition (narrow vs. preserve) documented; STATE.md blockers honored |
| QUAL-03 | `remind_me_mcp_original.py` deleted from repository | No references to this file exist anywhere in active code; safe to `git rm` |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| ruff | 0.14.14 (installed) | Python linter + formatter | Project's chosen linter; configured in `pyproject.toml` |
| pytest | (project venv) | Test runner | Project's existing test framework; 190 tests |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| contextlib | stdlib | `contextlib.suppress()` for SIM105 fix | Replacing try-except-pass pattern in db.py |
| typing.TYPE_CHECKING | stdlib | Fixing F821 in api.py | Adding Starlette to TYPE_CHECKING block at module level |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `ruff --fix --unsafe-fixes` | Manual fixes for TC003/TC002 | `--unsafe-fixes` is correct here since all affected files have `from __future__ import annotations`; manual fixes would be more verbose with no benefit |
| `contextlib.suppress()` | Keep try-except-pass with `# noqa: SIM105` | The SIM105 fix IS the right pattern; suppress is cleaner than suppressing the warning |

**Run commands:**
```bash
# Check current state
ruff check .

# Apply safe auto-fixes
ruff --fix .

# Apply unsafe fixes (safe here due to `from __future__ import annotations` everywhere)
ruff --fix --unsafe-fixes .

# Verify clean
ruff check .

# Verify tests still pass
pytest --tb=short -q
```

## Architecture Patterns

### Ruff Warning Inventory (Active Code Only, Excluding Original Monolith)

**Total: 61 warnings** (as of 2026-02-24, ruff 0.14.14)

#### Safe Auto-Fixable (`ruff --fix`): 43 warnings
| Count | Code | Description | Files Affected |
|-------|------|-------------|----------------|
| 16 | I001 | Unsorted/unformatted import blocks | `__init__.py`, `__main__.py`, `embeddings.py`, `conftest.py`, plus 5 test files |
| 9 | UP045 | `Optional[X]` → `X \| None` | `models.py` (all 9) |
| 9 | F541 | f-string without placeholders (remove f-prefix) | `tools.py` (7), `updater.py` (2) |
| 7 | F401 | Unused imports | `api.py`, `db.py`, `importer.py`, `test_api.py`, `test_db.py`, `test_formatting.py` |
| 1 | UP037 | Quoted type annotation | `api.py:73` (part of F821 pattern) |
| 1 | UP017 | `timezone.utc` → `datetime.UTC` | `db.py` |

#### Unsafe Auto-Fixable (`ruff --fix --unsafe-fixes`): 15 warnings
| Count | Code | Description | Files Affected | Safety Note |
|-------|------|-------------|----------------|-------------|
| 9 | TC003 | Move stdlib import to `TYPE_CHECKING` | `conftest.py`, `test_api.py`, `test_db.py`, `test_importer.py`, `test_smoke.py`, `test_tools.py` | Safe: all files have `from __future__ import annotations` |
| 3 | TC002 | Move third-party import to `TYPE_CHECKING` | `api.py` (Request), `test_importer.py` (pytest), `test_tools.py` (pytest) | Safe: annotations are strings at runtime |
| 3 | F841 | Assigned but unused local variables | `test_async.py`, `test_updater.py`, `test_tools.py` | Safe: removes dead assignment |

#### Manual (No Auto-Fix): 3 warnings
| Code | File:Line | Description | Fix Strategy |
|------|-----------|-------------|--------------|
| F821 | `api.py:73` | `Starlette` undefined in return annotation | Add `TYPE_CHECKING` guard; import `Starlette` into it at module level |
| SIM105 | `db.py:215` | `try`-`except`-`pass` → `contextlib.suppress()` | Add `import contextlib` to db.py; rewrite the try-except block |
| B007 | `tools.py:180` | Unused loop variable `i` in `for i, m in enumerate(sem_memories)` | Change to `for _, m in enumerate(sem_memories)` |

### Monolith File (QUAL-03)

**File:** `remind_me_mcp_original.py` — 2495 lines, no references in active code

Verification: `grep -r "remind_me_mcp_original" . --include="*.py" --include="*.toml"` returns no results.

The file contributes **24 ruff warnings** (2 SIM105, 9 UP045, W292, plus others in the original). Deleting it removes these without any code change.

```bash
git rm remind_me_mcp_original.py
```

### Exception Handler Disposition (QUAL-02)

**Files checked:** `embeddings.py`, `pid.py`, `updater.py`

| File | Line | Handler | Disposition | Rationale |
|------|------|---------|-------------|-----------|
| `embeddings.py` | 82 | `except Exception as e` (in `_ensure_loaded`, after `ImportError`) | **PRESERVE** | ONNX Runtime raises non-stdlib exceptions (e.g., `onnxruntime.capi.onnxruntime_pybind11_state.InvalidGraph`); narrowing would risk uncaught crashes. STATE.md Blocker explicitly flagged. |
| `embeddings.py` | 145 | `except Exception` (in `.available` property) | **PRESERVE** | Graceful-degradation boundary: returns `False` on any failure. ONNX may raise unexpected exception types. STATE.md Blocker explicitly flagged. |
| `embeddings.py` | 164 | `except Exception` (in `_get_embedder` singleton) | **PRESERVE** | Returns `None` on failure. Same ONNX concern. STATE.md Blocker explicitly flagged. |
| `pid.py` | 102 | `except Exception` (in `_check_ui_server_health`) | **NARROW** | `urllib.request.urlopen` raises `urllib.error.URLError` (subclass of `OSError`). Safe to narrow to `except OSError`. |
| `updater.py` | 370 | `except Exception` (in `_background_check`) | **PRESERVE** | Explicit comment: "Background check should never crash the server." Intentional broad catch at graceful-degradation boundary. STATE.md Blocker explicitly flagged. |

**Narrowing fix for `pid.py` line 102:**
```python
# Before
except Exception:
    return False

# After
import urllib.error
except (urllib.error.URLError, OSError):
    return False
```

Note: `urllib.error.URLError` is already a subclass of `OSError`, so `except OSError` alone would also be correct. Using both is explicit and communicates intent.

### Critical: Side-Effect Import Preservation (noqa: F401)

`__init__.py` and `__main__.py` contain intentional side-effect imports with `# noqa: F401` comments:

```python
# __init__.py — registers MCP tools on the server instance
import remind_me_mcp.tools  # noqa: F401

# __main__.py — ensures tools are registered before mcp.run()
import remind_me_mcp.tools  # noqa: F401 — ensure tools are registered before mcp.run()
```

The ruff I001 auto-fix **reorders** these imports (moves `import remind_me_mcp.tools` to sort alphabetically before `from remind_me_mcp.server import mcp`). This is **safe** because `tools.py` itself imports `mcp` from `server.py` directly — it does not depend on `server.py` being imported first by `__init__.py`.

The `# noqa: F401` comments are preserved by ruff's I001 fix; it only reorders, not removes.

### F821 Fix Pattern for `api.py`

The `_build_api_app()` function uses `"Starlette"` as a string annotation in its return type, but `Starlette` is never imported at module level (it's imported lazily inside the function body). The TC002 unsafe fix for `api.py` will add `TYPE_CHECKING` to the `typing` import and create a module-level `if TYPE_CHECKING:` block for `Request`. The F821 manual fix extends this same block to include `Starlette`:

```python
# In api.py, add at module level (after `from typing import Any`):
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from starlette.applications import Starlette
    from starlette.requests import Request  # TC002 unsafe fix adds this
```

The TC002 unsafe fix (`--unsafe-fixes`) and the F821 manual fix must be applied together to avoid a partially broken state.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Suppressing known exception in migration | try-except-pass | `contextlib.suppress()` | More readable; explicit about which exception is expected; SIM105 fix |
| Import sorting | Manual reordering | `ruff --fix` (I001) | Ruff handles all isort rules; manual sorting is fragile |
| `Optional[X]` → `X \| None` migration | Sed/find-replace | `ruff --fix` (UP045) | Ruff handles these transformations correctly and safely |

**Key insight:** For ruff auto-fixes, always apply `--fix` first (safe), then verify, then apply `--unsafe-fixes`. Don't apply both at once without verification — separating them isolates any test failures.

## Common Pitfalls

### Pitfall 1: Breaking Side-Effect Imports with noqa Removal
**What goes wrong:** Applying ruff auto-fix removes the `import remind_me_mcp.tools` lines in `__init__.py` and `__main__.py` (thinking they are unused), silently emptying the MCP tool registry.
**Why it happens:** ruff sees no direct usage of the imported module and would flag F401 — but the `# noqa: F401` comments prevent ruff from auto-removing them. The fix only reorders these imports.
**How to avoid:** Verify the `# noqa: F401` comments remain on both lines after applying ruff auto-fix.
**Warning signs:** After fixing, running `python -c "from remind_me_mcp.server import mcp; print(mcp.list_tools())"` would return empty list.

### Pitfall 2: Applying unsafe TC Fixes to Files Without `from __future__ import annotations`
**What goes wrong:** If a file does NOT have `from __future__ import annotations`, moving imports to `TYPE_CHECKING` blocks breaks runtime code that uses those imports at runtime.
**Why it happens:** Without the future import, Python evaluates annotations eagerly at class/function definition time.
**How to avoid:** Verified: ALL source and test files in this project have `from __future__ import annotations`. The unsafe fixes are safe here.
**Warning signs:** `NameError` at import time for moved symbols.

### Pitfall 3: Narrowing ONNX Exception Boundaries
**What goes wrong:** Narrowing the `except Exception` clauses in `embeddings.py` to specific types causes server crashes when ONNX raises non-stdlib exceptions like `onnxruntime.capi.onnxruntime_pybind11_state.InvalidGraph`.
**Why it happens:** ONNX Runtime has its own exception hierarchy outside stdlib. The broad catch is intentional at these boundaries.
**How to avoid:** Leave lines 82, 145, 164 in `embeddings.py` exactly as-is. Only narrow `pid.py` line 102.
**Warning signs:** Test suite reports `onnxruntime` import failures or embedding-related crashes.

### Pitfall 4: Partial Fix Creating New F821 via TC002 Fix
**What goes wrong:** Applying the TC002 unsafe fix for `api.py` (which moves `Request` to a TYPE_CHECKING block and removes the local `Request` import) WITHOUT also adding `Starlette` to the TYPE_CHECKING block leaves `F821` still present.
**Why it happens:** The TC002 and F821 fixes are coupled — both modify the same TYPE_CHECKING block.
**How to avoid:** Apply TC002 unsafe fix first (via `--unsafe-fixes`), then immediately apply the F821 manual fix to add `Starlette` to the same block.
**Warning signs:** `ruff check .` still shows F821 after `--unsafe-fixes` run.

### Pitfall 5: B007 Loop Variable — Wrong Line
**What goes wrong:** Changing `for i, m in enumerate(fts_memories)` instead of `for i, m in enumerate(sem_memories)`.
**Why it happens:** Both loops look similar; line 174 uses `i` but line 180 does not.
**How to avoid:** B007 is flagged at line 180 (the `sem_memories` loop). Line 174 (`fts_memories` loop) uses `i` for ranking score and MUST NOT be changed.
**Warning signs:** `scores[mid] = i * 0.5` changes from using index to using 0 (if _ mistakenly applied to line 174).

## Code Examples

Verified patterns from codebase inspection:

### SIM105 Fix Pattern (db.py line 215)
```python
# Source: db.py — migrate_schema, schema version 1 idempotent migration
# BEFORE (SIM105 warning):
try:
    db.execute("ALTER TABLE memories ADD COLUMN capture_id TEXT DEFAULT NULL")
except sqlite3.OperationalError:
    pass  # Column already exists — skip silently.

# AFTER (contextlib.suppress):
import contextlib  # add to module-level imports

with contextlib.suppress(sqlite3.OperationalError):
    db.execute("ALTER TABLE memories ADD COLUMN capture_id TEXT DEFAULT NULL")
```

### B007 Fix Pattern (tools.py line 180)
```python
# BEFORE (B007 warning):
for i, m in enumerate(sem_memories):
    mid = m["id"]
    # i is never used in this loop body

# AFTER:
for _, m in enumerate(sem_memories):
    mid = m["id"]
```

### F821 + TC002 Fix Pattern (api.py)
```python
# BEFORE (F821: Starlette undefined, TC002: Request should be in TYPE_CHECKING):
from typing import Any

def _build_api_app() -> "Starlette":
    from starlette.applications import Starlette
    from starlette.requests import Request
    ...

# AFTER (manual F821 fix + TC002 unsafe fix applied together):
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from starlette.applications import Starlette
    from starlette.requests import Request

def _build_api_app() -> "Starlette":
    from starlette.applications import Starlette  # still needed at runtime
    # from starlette.requests import Request  — removed by TC002 fix (safe with from __future__)
    ...
```

### pid.py Exception Narrowing Pattern
```python
# BEFORE (QUAL-02 — bare except Exception at safe call site):
import urllib.request
try:
    req = urllib.request.Request(url + "/api/stats", method="GET")
    with urllib.request.urlopen(req, timeout=2) as resp:
        return resp.status == 200
except Exception:
    return False

# AFTER (narrowed to specific urllib/socket exception types):
import urllib.error
import urllib.request
try:
    req = urllib.request.Request(url + "/api/stats", method="GET")
    with urllib.request.urlopen(req, timeout=2) as resp:
        return resp.status == 200
except (urllib.error.URLError, OSError):
    return False
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `Optional[X]` type annotations | `X \| None` (PEP 604) | Python 3.10+ | UP045: ruff auto-fixes all 9 instances in models.py |
| `timezone.utc` | `datetime.UTC` (alias) | Python 3.11+ | UP017: ruff auto-fixes in db.py |
| `try`-`except`-`pass` | `contextlib.suppress()` | Long-standing pattern | SIM105: cleaner, more explicit intent |

**Note on warning count discrepancy:** The Phase 4 success criteria (written during roadmap planning) states "26 auto-fix + 4 manual = 30 total warnings." The actual count (ruff 0.14.14) is 85 total / 61 in active code. The discrepancy is because the roadmap was written against an earlier ruff run that may have used different rule selection or a subset of files. The goal remains zero warnings — the counts in this research supersede the roadmap numbers.

## Open Questions

1. **Should TC002 unsafe fix for api.py be applied at all?**
   - What we know: The fix removes the local `from starlette.requests import Request` from inside `_build_api_app()` and adds it to a module-level `TYPE_CHECKING` block. With `from __future__ import annotations`, function-level annotations ARE strings at runtime, so this is safe.
   - What's unclear: Whether ruff's unsafe fix correctly handles the nested function context (annotations inside closures). However, since all annotation evaluation is deferred with the future import, this should be fine.
   - Recommendation: Apply the TC002 unsafe fix and run the full test suite immediately. If any test fails, revert and add `# noqa: TC002` instead.

2. **What exact ruff version is authoritative?**
   - What we know: `ruff 0.14.14` is installed at `~/.local/bin/ruff`. The project venv may have a different version.
   - What's unclear: Whether the CI pipeline (Phase 5) will use the same version.
   - Recommendation: Pin ruff version in `pyproject.toml` dev dependencies once Phase 5 CI is set up. For Phase 4, use whatever is in the venv.

## Sources

### Primary (HIGH confidence)
- Direct codebase inspection — ruff run on live project with `--output-format=json --statistics`
- `pyproject.toml` — ruff configuration, rule selection (E, F, W, I, N, UP, B, SIM, TCH)
- `.planning/STATE.md` — Blockers/Concerns section for ONNX exception preservation
- `remind_me_mcp/embeddings.py`, `pid.py`, `updater.py` — all exception handler locations verified by inspection

### Secondary (MEDIUM confidence)
- ruff docs (https://docs.astral.sh/ruff/rules/) — rule descriptions and fix applicability
- Python docs — urllib.error exception hierarchy verified with `urllib.error.URLError.__bases__`

### Tertiary (LOW confidence)
- None — all findings based on direct inspection

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — ruff already installed and configured, version confirmed, all warnings enumerated
- Architecture: HIGH — all exception handlers inspected, fix strategies verified against live codebase
- Pitfalls: HIGH — based on STATE.md documented blockers and direct code inspection

**Research date:** 2026-02-24
**Valid until:** 2026-04-24 (stable tooling; no fast-moving dependencies)
