---
phase: 01-package-structure
plan: "01"
subsystem: database
tags: [python, pydantic, sqlite, onnx, numpy, embeddings, modular-package]

# Dependency graph
requires: []
provides:
  - remind_me_mcp/config.py — all module-level constants and environment configuration
  - remind_me_mcp/models.py — ResponseFormat enum and all 9 Pydantic input models
  - remind_me_mcp/formatting.py — _fmt_memory_md and _fmt_memories helpers
  - remind_me_mcp/embeddings.py — _Embedder class and _get_embedder factory
  - remind_me_mcp/db.py — database connection, schema, and 7 helper functions
affects:
  - 01-package-structure (plans 02-03 will import these foundation modules)
  - 02-test-coverage (tests will import and exercise these modules)
  - 03-bug-fixes (bug fixes will modify these modules)

# Tech tracking
tech-stack:
  added: [pydantic-v2, sqlite3, onnxruntime, numpy, huggingface-hub, tokenizers, sqlite-vec]
  patterns:
    - "Leaf module pattern: config has no internal deps; embeddings imports config; db imports config+embeddings"
    - "__all__ defined on every module for explicit public surface"
    - "Module-level logger via logging.getLogger('remind_me_mcp.<module>')"
    - "Lazy-loading heavy dependencies inside methods (onnxruntime, huggingface_hub)"
    - "from __future__ import annotations on every module for deferred type evaluation"

key-files:
  created:
    - remind_me_mcp/__init__.py
    - remind_me_mcp/config.py
    - remind_me_mcp/models.py
    - remind_me_mcp/formatting.py
    - remind_me_mcp/embeddings.py
    - remind_me_mcp/db.py
  modified: []

key-decisions:
  - "Pure extraction only — no logic changes in this plan; all signatures and docstrings preserved verbatim"
  - "SERVE_UI and UI_PORT extracted to config.py (not HTTP layer) because they are environment configuration"
  - "uv venv created for project — no venv existed prior; uv pip install -e '.[semantic]' used"

patterns-established:
  - "Import order: from __future__ → stdlib → third-party → internal (from remind_me_mcp.<module> import ...)"
  - "Private module globals prefixed with _ (e.g., _embedder singleton)"
  - "Docstrings required on all public and private functions"

requirements-completed: [ARCH-01, ARCH-04, ARCH-05]

# Metrics
duration: 3min
completed: 2026-02-24
---

# Phase 1 Plan 1: Foundation Module Extraction Summary

**Five foundation modules extracted from the 2,500-line monolith: config constants, Pydantic models, formatting helpers, ONNX embedding engine, and SQLite database layer — all circular-import-free**

## Performance

- **Duration:** 3 min
- **Started:** 2026-02-24T03:20:31Z
- **Completed:** 2026-02-24T03:23:58Z
- **Tasks:** 2
- **Files modified:** 6

## Accomplishments

- Created `remind_me_mcp/` Python package with `__init__.py` and 5 foundation modules
- Extracted all environment-based configuration constants into `config.py` with `__all__` and logger
- Extracted `ResponseFormat` enum and all 9 Pydantic input models into `models.py`
- Extracted `_fmt_memory_md` and `_fmt_memories` formatting helpers into `formatting.py`
- Extracted `_Embedder` class and `_get_embedder` factory into `embeddings.py` with lazy loading
- Extracted all 7 DB helpers into `db.py` with correct import chain (no circular imports)

## Task Commits

Each task was committed atomically:

1. **Task 1: Create config.py, models.py, and formatting.py** - `3ffa97d` (feat)
2. **Task 2: Create db.py and embeddings.py** - `3eb1ff8` (feat)

**Plan metadata:** _(docs commit below)_

## Files Created/Modified

- `remind_me_mcp/__init__.py` — Package marker with module docstring
- `remind_me_mcp/config.py` — MEMORY_DIR, DB_PATH, IMPORT_LOG, PID_FILE, EMBEDDING_MODEL, EMBEDDING_DIM, MODEL_DIR, SERVE_UI, UI_PORT
- `remind_me_mcp/models.py` — ResponseFormat, MemoryAddInput, MemorySearchInput, MemoryListInput, MemoryUpdateInput, MemoryDeleteInput, ChatImportInput, MemoryStatsInput, BulkImportDirInput, AutoCaptureInput
- `remind_me_mcp/formatting.py` — _fmt_memory_md, _fmt_memories
- `remind_me_mcp/embeddings.py` — _Embedder class, _get_embedder factory, _embedder singleton
- `remind_me_mcp/db.py` — _get_db, _ensure_schema, _embed_and_store, _semantic_search, _now_iso, _make_id, _row_to_dict

## Decisions Made

- Pure extraction with no logic changes — all function signatures, docstrings, and behavior preserved verbatim from the monolith.
- `SERVE_UI` and `UI_PORT` extracted to `config.py` (not the HTTP API layer) because they are environment configuration, consistent with all other env-derived constants.
- Created project venv using `uv venv` + `uv pip install -e '.[semantic]'` since no venv existed in the project directory.

## Deviations from Plan

None — plan executed exactly as written.

## Issues Encountered

- No `pyproject.toml` virtual environment existed in the project directory. Created `.venv` with `uv venv` and installed all dependencies (including semantic extras) before running verification commands. This was a one-time setup step, not a code deviation.

## User Setup Required

None — no external service configuration required.

## Next Phase Readiness

- All 5 foundation modules verified to import cleanly with no circular dependency errors
- Each module has `__all__` and its own logger
- Plans 01-02 and 01-03 can now import from these modules directly
- `.venv` is set up in the project directory for subsequent plan executions

## Self-Check: PASSED

All 6 files found on disk. Both task commits (3ffa97d, 3eb1ff8) confirmed in git log.

---
*Phase: 01-package-structure*
*Completed: 2026-02-24*
