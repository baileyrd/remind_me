---
phase: 07-api-embedding-parity
verified: 2026-02-24T23:30:00Z
status: passed
score: 4/4 must-haves verified
re_verification: false
---

# Phase 7: API Embedding Parity Verification Report

**Phase Goal:** Memories created or updated through the REST API are immediately embedded and retrievable via semantic search — matching MCP tool behavior
**Verified:** 2026-02-24T23:30:00Z
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | A memory created via POST /api/memories has a corresponding row in memories_vec within the same request/response cycle | VERIFIED | `api_add` calls `await asyncio.to_thread(_embed_and_store, db, mem_id, content)` after `db.commit()` (api.py:266); `test_api_add_creates_embedding` and `test_api_add_embedding_rowid_matches_memory` both PASS |
| 2 | A memory updated via PUT /api/memories/{id} with new content has its memories_vec row replaced with a fresh embedding | VERIFIED | `api_update` calls `await asyncio.to_thread(_embed_and_store, db, memory_id, body["content"])` gated on `"content" in body and body["content"] is not None` (api.py:305-306); `test_api_update_content_regenerates_embedding` PASSES |
| 3 | A tag-only update via PUT /api/memories/{id} does NOT alter the memories_vec row | VERIFIED | Gate condition `if "content" in body and body["content"] is not None` (api.py:305) blocks embedding call when only tags are provided; `test_api_update_no_content_preserves_embedding` PASSES |
| 4 | REST API memories and MCP tool memories are equally retrievable via _semantic_search | VERIFIED | `_semantic_search` fixed to use `AND mv.k = ?` constraint (db.py:375) for sqlite-vec 0.1.6 JOIN compatibility; `test_rest_and_mcp_memories_equally_findable_by_semantic_search` PASSES |

**Score:** 4/4 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `tests/conftest.py` | `db_conn_with_vec` fixture that loads sqlite-vec into :memory: test DB | VERIFIED | Fixture exists (lines 165-198); contains `sqlite_vec.load(db)` (line 180); patches all 4 module `_get_db` references; loads vec before `_ensure_schema` |
| `tests/test_api.py` | 5 embedding parity integration tests | VERIFIED | All 5 functions present (lines 623-790): `test_api_add_creates_embedding`, `test_api_add_embedding_rowid_matches_memory`, `test_api_update_content_regenerates_embedding`, `test_api_update_no_content_preserves_embedding`, `test_rest_and_mcp_memories_equally_findable_by_semantic_search`; all 5 PASS |
| `remind_me_mcp/api.py` | Embedding calls in api_add and api_update handlers | VERIFIED | `_embed_and_store` imported (line 22); `import asyncio` present (line 14); two `await asyncio.to_thread(_embed_and_store, ...)` calls at lines 266 and 306 |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `remind_me_mcp/api.py` | `remind_me_mcp.db._embed_and_store` | module-level import + `await asyncio.to_thread` call | WIRED | Import confirmed line 22; calls at api.py:266 and api.py:306 match pattern `await asyncio\.to_thread\(_embed_and_store` |
| `remind_me_mcp/api.py` | `memories_vec` virtual table | `_embed_and_store` inserts embedding row after `db.commit()` | WIRED | `_embed_and_store` in db.py (lines 331-335) deletes existing vec row then inserts fresh one; called after `db.commit()` at api.py:265-266 and api.py:304-306 |
| `tests/conftest.py` | sqlite_vec extension | `db_conn_with_vec` fixture loads extension into :memory: connection | WIRED | `sqlite_vec.load(db)` at conftest.py:180 within `db_conn_with_vec` fixture; extension loaded before `_ensure_schema` so `memories_vec` virtual table is created |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|----------|
| EMBD-01 | 07-01-PLAN.md | REST API POST /api/memories generates semantic embeddings on create (matching MCP tool behavior) | SATISFIED | `api_add` calls `_embed_and_store` after `db.commit()`; `test_api_add_creates_embedding` and `test_api_add_embedding_rowid_matches_memory` PASS; REQUIREMENTS.md marks EMBD-01 Complete |
| EMBD-02 | 07-01-PLAN.md | REST API PUT /api/memories/{id} regenerates semantic embeddings on content update (matching MCP tool behavior) | SATISFIED | `api_update` calls `_embed_and_store` gated on content change; `test_api_update_content_regenerates_embedding` and `test_api_update_no_content_preserves_embedding` PASS; REQUIREMENTS.md marks EMBD-02 Complete |

No orphaned requirements — REQUIREMENTS.md Traceability table maps EMBD-01 and EMBD-02 to Phase 7 with status Complete, matching the plan's `requirements` field exactly.

### Anti-Patterns Found

None. No TODO, FIXME, XXX, HACK, or PLACEHOLDER markers in any of the 4 modified files (`remind_me_mcp/api.py`, `remind_me_mcp/db.py`, `tests/conftest.py`, `tests/test_api.py`).

### Test and Coverage Results

- Embedding parity tests: **5/5 PASS** (`pytest tests/test_api.py -k "embedding or semantic_search"`)
- Full test suite: **213/213 PASS** (zero regressions)
- Coverage: **77.03%** (above 74% gate; CICD-02 partial threshold met)
- Ruff lint: **0 warnings** on all 4 modified files

### Human Verification Required

None. All observable truths are verifiable programmatically via the integration tests. The embedding behavior (vector row creation, rowid integrity, conditional re-embedding, and cross-mode semantic search parity) is fully exercised by the 5 test functions using a real in-memory SQLite database with sqlite-vec loaded.

### Notable Implementation Detail: _semantic_search Fix

The SUMMARY documents an unplanned fix to `_semantic_search` in `db.py`. The original query used `LIMIT ?` as the knn constraint; sqlite-vec 0.1.6 requires `AND mv.k = ?` at the virtual table scan level because LIMIT cannot push through a JOIN. This was correctly fixed in commit `b7488be` and is verified by the passing parity test. The fix benefits all code paths that call `_semantic_search`, not just the REST API path.

---

_Verified: 2026-02-24T23:30:00Z_
_Verifier: Claude (gsd-verifier)_
