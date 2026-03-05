---
phase: 13-structured-memory-and-transparency
verified: 2026-03-05T20:00:00Z
status: passed
score: 6/6 must-haves verified
---

# Phase 13: Structured Memory and Transparency Verification Report

**Phase Goal:** Structured memory columns (subject/predicate/object triples, supersession tracking) and search transparency (debug ranking signals, envelope enrichment)
**Verified:** 2026-03-05T20:00:00Z
**Status:** passed
**Re-verification:** No -- initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | Memories can store subject, predicate, object triples as nullable columns | VERIFIED | `_migrate_v6_to_v7` in db.py adds four ALTER TABLE columns with `TEXT DEFAULT NULL`; `_SCHEMA_VERSION = 7` |
| 2 | Indexes on subject and memory_type enable fast structured lookups | VERIFIED | `idx_memories_subject` created in `_migrate_v6_to_v7`; `idx_memories_memory_type` created in `_migrate_v4_to_v5` |
| 3 | Search detects structured query patterns and routes to indexed lookup before semantic search | VERIFIED | `_detect_structured_query` parses `subject:` / `predicate:` patterns; `memory_search` calls it before RRF pipeline (line 290); `_structured_lookup` performs direct SQL; fallback strips prefixes via `_strip_structured_prefixes` |
| 4 | A superseded_by column tracks when a structured fact is replaced by a newer version | VERIFIED | Column added in `_migrate_v6_to_v7`; `_structured_lookup` has `WHERE superseded_by IS NULL` (line 118) |
| 5 | Search results include debug_signals block when verbose=True with ranking signals and days_old | VERIFIED | `build_debug_signals` in retrieval.py returns semantic_rank, keyword_rank, recency_rank, vitality_rank, days_old; tools.py attaches per-memory when `params.verbose` is True (line 438-440) |
| 6 | Response envelope includes tier_breakdown and dormant_excluded count | VERIFIED | `compute_tier_breakdown` in retrieval.py counts keyword/semantic/hybrid; tools.py always computes and adds both to JSON response (lines 454-455) and Markdown tier line (lines 499-502) |

**Score:** 6/6 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `remind_me_mcp/db.py` | Schema migration v6->v7 with 4 new columns, index, updated triggers | VERIFIED | `_migrate_v6_to_v7` function present; adds subject, predicate, object, superseded_by columns; creates `idx_memories_subject` index; outbox triggers updated with all new columns |
| `remind_me_mcp/tools.py` | Structured query detection and indexed lookup routing | VERIFIED | `_detect_structured_query`, `_structured_lookup`, `_strip_structured_prefixes` all present and wired into `memory_search` flow |
| `remind_me_mcp/models.py` | verbose field on MemorySearchInput | VERIFIED | `verbose: bool = Field(default=False, ...)` present on line 109-112 |
| `remind_me_mcp/retrieval.py` | build_debug_signals and compute_tier_breakdown | VERIFIED | Both functions implemented with proper logic; exported in `__all__` |
| `tests/test_db.py` | Tests for v6->v7 migration | VERIFIED | 8 migration tests present, all passing |
| `tests/test_tools.py` | Tests for structured query routing and verbose/envelope | VERIFIED | 9 structured search tests + 7 transparency tests, all passing |
| `tests/test_retrieval.py` | Tests for debug signal building and tier breakdown | VERIFIED | 8 tests (5 debug signals + 3 tier breakdown), all passing |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `tools.py` | `db.py` | SQL queries using subject/predicate with indexes | WIRED | `_structured_lookup` builds `WHERE subject = ? AND predicate = ?` with parameterized queries |
| `tools.py:memory_search` | `_structured_lookup` | Structured query detection routes to indexed lookup before RRF | WIRED | Line 290: `_detect_structured_query`; line 292: `_structured_lookup` called; line 347: fallback strips prefixes |
| `tools.py` | `retrieval.py` | import build_debug_signals, compute_tier_breakdown | WIRED | Line 49-50: explicit imports; line 440: `build_debug_signals` called; line 443: `compute_tier_breakdown` called |
| `tools.py:memory_search` | SearchEnvelope | Adds tier_breakdown and dormant_excluded to envelope | WIRED | Lines 454-455: both added to JSON dict; lines 499-502: tier breakdown in Markdown footer |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|----------|
| STRC-01 | 13-01 | memories table has subject, predicate, object columns (nullable) | SATISFIED | Four ALTER TABLE ADD COLUMN in `_migrate_v6_to_v7` |
| STRC-02 | 13-01 | Indexes on subject, memory_type for fast structured lookups | SATISFIED | `idx_memories_subject` in v7 migration; `idx_memories_memory_type` from v5 migration |
| STRC-03 | 13-01 | Search routes structured queries to indexed lookup before semantic search | SATISFIED | `_detect_structured_query` + `_structured_lookup` + fallback in `memory_search` |
| STRC-04 | 13-01 | superseded_by column tracks fact replacement | SATISFIED | Column added in v7 migration; excluded in structured lookup via WHERE clause |
| TRNS-01 | 13-02 | debug_signals block when verbose=True | SATISFIED | `build_debug_signals` returns all five ranking signals; wired in tools.py |
| TRNS-02 | 13-02 | tier_breakdown and dormant_excluded in envelope | SATISFIED | `compute_tier_breakdown` returns counts; both always included in JSON and Markdown |

No orphaned requirements found -- all 6 requirement IDs (STRC-01 through STRC-04, TRNS-01, TRNS-02) mapped in REQUIREMENTS.md to Phase 13 are covered by plans 13-01 and 13-02.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `remind_me_mcp/tools.py` | 852 | `# placeholder, filled after summary is created` | Info | Pre-existing from prior phase (auto_capture flow), not from phase 13 |

No blockers or warnings from phase 13 work.

### Human Verification Required

None required. All phase 13 deliverables are data/logic changes verifiable through automated tests. All 172 tests pass (0 failures).

### Test Results

```
172 passed in 0.64s
```

All tests pass including:
- 8 migration tests for v6->v7 schema
- 9 structured search tests (subject lookup, subject+predicate, fallback, filters, envelope, supersession)
- 5 build_debug_signals tests
- 3 compute_tier_breakdown tests
- 7 verbose/tier_breakdown/dormant_excluded integration tests

### Gaps Summary

No gaps found. All 6 observable truths verified. All 7 artifacts exist, are substantive, and are wired. All 4 key links confirmed. All 6 requirements satisfied. No blocker anti-patterns detected.

---

_Verified: 2026-03-05T20:00:00Z_
_Verifier: Claude (gsd-verifier)_
