---
phase: 07-api-embedding-parity
plan: 01
subsystem: api
tags: [sqlite-vec, embeddings, semantic-search, rest-api, tdd, asyncio]

# Dependency graph
requires:
  - phase: 06-security-hardening
    provides: api.py with Bearer auth and CORS middleware in place

provides:
  - REST API create (POST /api/memories) now calls _embed_and_store after db.commit()
  - REST API update (PUT /api/memories/{id}) re-embeds when content changes
  - db_conn_with_vec fixture for embedding integration tests
  - 5 EMBD-01/EMBD-02 embedding parity integration tests
  - Fixed _semantic_search to use 'k = ?' constraint for sqlite-vec 0.1.6 JOIN compatibility

affects:
  - 07-api-embedding-parity (phase complete after this plan)
  - Any future tests that use _semantic_search with a JOIN pattern

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "await asyncio.to_thread(_embed_and_store, db, id, content) after db.commit() for embedding writes"
    - "Gate re-embed on 'content' in body and body['content'] is not None — tag-only updates skip embedding"
    - "db_conn_with_vec fixture: load sqlite-vec before _ensure_schema to create memories_vec virtual table"
    - "sqlite-vec 0.1.6 knn queries: use 'AND mv.k = ?' instead of 'LIMIT ?' when LIMIT cannot push through JOIN"

key-files:
  created:
    - tests/conftest.py (db_conn_with_vec fixture added)
  modified:
    - remind_me_mcp/api.py
    - remind_me_mcp/db.py
    - tests/test_api.py

key-decisions:
  - "sqlite-vec 0.1.6 requires 'AND mv.k = ?' constraint instead of 'LIMIT ?' in knn JOIN queries — LIMIT does not push through the JOIN planner"
  - "Gate api_update re-embed on 'content' in body — tag-only updates must not call _embed_and_store (mirrors tools.py lines 359-360)"
  - "_embed_and_store called via asyncio.to_thread in async route handlers — consistent with tools.py pattern"

patterns-established:
  - "REST API embedding pattern: call await asyncio.to_thread(_embed_and_store, db, id, content) after db.commit() in both create and conditional update handlers"
  - "sqlite-vec knn with JOIN: use 'WHERE mv.embedding MATCH ? AND mv.k = ?' not 'WHERE mv.embedding MATCH ? ... LIMIT ?'"

requirements-completed: [EMBD-01, EMBD-02]

# Metrics
duration: 2min
completed: 2026-02-24
---

# Phase 7 Plan 01: REST API Embedding Parity Summary

**REST API create and update handlers now call `_embed_and_store` via `asyncio.to_thread` closing the MCP/REST parity gap, with 5 new integration tests and a sqlite-vec JOIN query fix**

## Performance

- **Duration:** 2 min
- **Started:** 2026-02-24T23:20:19Z
- **Completed:** 2026-02-24T23:22:33Z
- **Tasks:** 2 (RED + GREEN TDD phases)
- **Files modified:** 4

## Accomplishments
- Added `db_conn_with_vec` fixture to `tests/conftest.py` that loads sqlite-vec into `:memory:` DB before schema creation
- Added 5 embedding parity integration tests to `tests/test_api.py` (EMBD-01, EMBD-02, parity)
- Added `import asyncio` and `_embed_and_store` import to `remind_me_mcp/api.py`
- Added `await asyncio.to_thread(_embed_and_store, db, mem_id, content)` to `api_add` after `db.commit()`
- Added conditional `_embed_and_store` call to `api_update` gated on content change
- Fixed `_semantic_search` in `db.py` to use `AND mv.k = ?` instead of `LIMIT ?` for sqlite-vec 0.1.6 JOIN compatibility
- Full test suite: 213 tests pass, 76.88% coverage (above 74% gate)

## Task Commits

Each task was committed atomically:

1. **Task 1: RED phase — add failing tests** - `4fe934e` (test)
2. **Task 2: GREEN phase — implement production code** - `b7488be` (feat)

*Note: TDD plan with RED + GREEN commits*

## Files Created/Modified
- `tests/conftest.py` - Added `db_conn_with_vec` fixture (sqlite-vec loaded into :memory: DB)
- `tests/test_api.py` - Added 5 embedding parity integration tests
- `remind_me_mcp/api.py` - Added `import asyncio`, `_embed_and_store` import, embedding calls in `api_add` and `api_update`
- `remind_me_mcp/db.py` - Fixed `_semantic_search` knn query to use `AND mv.k = ?` for sqlite-vec 0.1.6 JOIN compatibility

## Decisions Made
- **sqlite-vec 0.1.6 knn JOIN query**: `LIMIT ?` cannot push through a JOIN in sqlite-vec 0.1.6. Changed `_semantic_search` to use `AND mv.k = ?` constraint which is evaluated at the virtual table scan level. This makes the query work correctly in tests and production.
- **Gate update re-embed on `"content" in body`**: Tag-only updates (`{"tags": [...]}`) must not re-embed. Exactly mirrors `tools.py` lines 359-360 (`if params.content is not None`).
- **`asyncio.to_thread` pattern**: Consistent with `tools.py` — `_embed_and_store` is synchronous and SQLite I/O-bound; offloading to a thread prevents blocking the async event loop.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed `_semantic_search` sqlite-vec knn query constraint**
- **Found during:** Task 2 (GREEN phase — running 5th parity test)
- **Issue:** `_semantic_search` used `LIMIT ?` as the knn constraint in the JOIN query. sqlite-vec 0.1.6 requires `AND mv.k = ?` at the virtual table level — `LIMIT` cannot be pushed through the JOIN planner, causing `OperationalError: A LIMIT or 'k = ?' constraint is required on vec0 knn queries.`
- **Fix:** Changed `WHERE mv.embedding MATCH ? ORDER BY mv.distance LIMIT ?` to `WHERE mv.embedding MATCH ? AND mv.k = ? ORDER BY mv.distance` in `_semantic_search`
- **Files modified:** `remind_me_mcp/db.py`
- **Verification:** `test_rest_and_mcp_memories_equally_findable_by_semantic_search` now passes; full suite 213/213
- **Committed in:** `b7488be` (GREEN phase commit)

---

**Total deviations:** 1 auto-fixed (Rule 1 — bug)
**Impact on plan:** Essential fix — `_semantic_search` was broken for all users of the `:memory:` test DB and likely broken in production when the JOIN planner doesn't push LIMIT. No scope creep.

## Issues Encountered
- `test_rest_and_mcp_memories_equally_findable_by_semantic_search` was not matched by `-k "embedding"` filter (function name doesn't contain "embedding"). Used `-k "semantic_search"` to verify it. The plan's `-k "embedding"` filter only covers 4 of the 5 tests.

## Next Phase Readiness
- EMBD-01 and EMBD-02 requirements fully satisfied
- Phase 7 plan 01 complete — ready for phase 08 (performance) if applicable
- sqlite-vec knn fix benefits all semantic search operations, not just the test path

## Self-Check: PASSED

- tests/conftest.py: FOUND
- tests/test_api.py: FOUND
- remind_me_mcp/api.py: FOUND
- remind_me_mcp/db.py: FOUND
- 07-01-SUMMARY.md: FOUND
- commit 4fe934e (RED): FOUND
- commit b7488be (GREEN): FOUND

---
*Phase: 07-api-embedding-parity*
*Completed: 2026-02-24*
