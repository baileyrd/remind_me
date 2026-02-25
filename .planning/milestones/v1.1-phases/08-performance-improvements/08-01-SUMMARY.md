---
phase: 08-performance-improvements
plan: 01
subsystem: database
tags: [embeddings, onnx, performance, batching, sqlite-vec]

# Dependency graph
requires:
  - phase: 07-api-embedding-parity
    provides: embedder.embed(list[str]) batch API already established in _Embedder class
provides:
  - Batched embedding loop in remind_me_reindex using embedder.embed(texts)
  - EMBED_BATCH_SIZE = 32 module-level constant in tools.py
  - Test proving 40 memories = 2 embed() calls (32 + 8), not 40
affects: [09-future-phases, embeddings, reindex]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Batch ONNX embed calls: collect texts into list[str], call embedder.embed(texts) once per EMBED_BATCH_SIZE chunk"
    - "Spy pattern for counting batch calls: monkeypatch wraps original method, appends args to call_log"

key-files:
  created: []
  modified:
    - remind_me_mcp/tools.py
    - tests/test_tools.py

key-decisions:
  - "EMBED_BATCH_SIZE = 32 chosen as module-level constant to match ONNX batch overhead vs memory tradeoff"
  - "Batch try/except wraps embed() call; per-row DB inserts are inside the same block so a DB error on one row does not lose the batch embedding work"
  - "mem_id renamed to _mem_id in inner loop (B007) since error logging uses ids[0]; zip(..., strict=True) added (B905)"
  - "Used db_conn (no sqlite-vec) for batch count test: embed() calls happen even when DB inserts fail; call_log accurately captures batch count"

patterns-established:
  - "Deviation Rule 3: ruff B007/B905 warnings caught immediately after Task 1 commit, fixed inline before Task 2"

requirements-completed: [PERF-01]

# Metrics
duration: 2min
completed: 2026-02-24
---

# Phase 8 Plan 1: Batch Reindex Embedding Loop Summary

**Replaced per-item embed_one() loop in remind_me_reindex with batched embedder.embed(texts) calls using EMBED_BATCH_SIZE=32, reducing ONNX call overhead ~32x for large memory databases**

## Performance

- **Duration:** 2 min
- **Started:** 2026-02-25T00:00:54Z
- **Completed:** 2026-02-25T00:02:55Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments

- Added `EMBED_BATCH_SIZE = 32` module-level constant to `remind_me_mcp/tools.py`
- Replaced 64-call loop with 2-call batched loop in `remind_me_reindex` (for 64 items)
- Added `test_reindex_batches_embed_calls`: 40 memories produce exactly 2 `embed()` calls (batch 32 + batch 8), satisfying PERF-01
- Full test suite green: 214 tests pass (1 new), zero ruff warnings

## Task Commits

Each task was committed atomically:

1. **Task 1: Batch the reindex embedding loop** - `6d68419` (feat)
2. **Task 2: Add batch call count verification test** - `ec8acd9` (test)

**Plan metadata:** (docs commit follows)

## Files Created/Modified

- `/home/baileyrd/projects/remind_me/remind_me_mcp/tools.py` - EMBED_BATCH_SIZE constant; batched embedding loop replacing per-item embed_one() calls
- `/home/baileyrd/projects/remind_me/tests/test_tools.py` - test_reindex_batches_embed_calls verifying 2 embed() calls for 40 memories

## Decisions Made

- `EMBED_BATCH_SIZE = 32` at module level — visible to both the production loop and the test's import
- Batch-level `try/except` wraps the `embed()` call; per-row DB inserts are inside the same block so a DB OperationalError on one row doesn't lose the batch's embedding work (though it exits the inner loop via exception)
- Used `db_conn` (no sqlite-vec extension) for `test_reindex_batches_embed_calls`: DB inserts fail gracefully, but `embed()` calls are captured in `call_log` before any DB error, proving batch count correctly
- `zip(ids, rowids, strict=True)` used since both lists are always equal length (both derived from same `batch` slice)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Fixed ruff B007 and B905 lint errors in Task 1 implementation**
- **Found during:** Task 2 verification (ruff check after Task 1 commit)
- **Issue:** `mem_id` loop variable unused inside inner loop (B007); `zip()` missing `strict=` parameter (B905)
- **Fix:** Renamed `mem_id` to `_mem_id`; added `strict=True` to `zip(ids, rowids)`
- **Files modified:** `remind_me_mcp/tools.py`
- **Verification:** `ruff check .` returns "All checks passed!"; all 214 tests still pass
- **Committed in:** `ec8acd9` (Task 2 commit — included in same commit as the test)

---

**Total deviations:** 1 auto-fixed (Rule 3 - blocking lint errors)
**Impact on plan:** Necessary for zero-warning requirement in plan verification criteria. No scope creep.

## Issues Encountered

None — plan executed cleanly with one minor ruff fix.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- PERF-01 satisfied: reindex processes embeddings in batches of 32
- Phase 8 complete — all v1.1 phases done
- Coverage has grown to 214 tests; CI coverage gate at 74% should now be safely exceeded; recommend raising `--cov-fail-under` to 80 to fully satisfy CICD-02

## Self-Check: PASSED

- FOUND: `remind_me_mcp/tools.py`
- FOUND: `tests/test_tools.py`
- FOUND: `.planning/phases/08-performance-improvements/08-01-SUMMARY.md`
- FOUND commit: `6d68419` (feat: batch reindex embedding loop)
- FOUND commit: `ec8acd9` (test: add batch call count verification)

---
*Phase: 08-performance-improvements*
*Completed: 2026-02-24*
