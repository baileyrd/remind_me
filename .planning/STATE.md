# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-02-24)

**Core value:** Persistent, searchable memory across all Claude interfaces — modular, tested, maintainable
**Current focus:** v1.1 Phase 7 — API Embedding Parity

## Current Position

Phase: 7 of 8 (API Embedding Parity)
Plan: 1 of 1 in current phase — Phase 7 Plan 1 COMPLETE
Status: In progress
Last activity: 2026-02-24 — Plan 07-01 complete (EMBD-01/EMBD-02: REST API embedding parity, 5 integration tests)

Progress: [#######░░░] 70% (v1.1 — 4/5 phases)

## Performance Metrics

**Velocity (v1.0):**
- Total plans completed: 12
- Average duration: 3.7min
- Total execution time: ~0.6 hours

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| 01-package-structure | 3/3 | 13min | 4min |
| 02-test-infrastructure | 4/4 | 10min | 2.5min |
| 03-quality-and-bug-fixes | 5/5 | 21min | 4.2min |
| 04-code-quality-and-cleanup | 2/2 | 3min | 1.5min |

**v1.1 metrics:**

| Phase | Plans | Duration | Avg/Plan |
|-------|-------|----------|----------|
| 05-ci-cd-pipeline | 2/2 | 5min | 2.5min |
| 06-security-hardening | 2/2 | 6min | 3min |
| 07-api-embedding-parity | 1/1 | 2min | 2min |

*v1.1 metrics will accumulate as phases complete*

## Accumulated Context

### Decisions

Full decision log in PROJECT.md Key Decisions table.

Recent decisions affecting v1.1:
- Phase ordering: lint before CI (30 ruff warnings guarantee red pipeline otherwise)
- CI before security (CI validates every subsequent security change automatically)
- Security before embedding parity (both touch api.py — sequential keeps diffs reviewable)
- Performance last (highest concurrency risk, lowest correctness priority)
- 04-01: Applied ruff --fix (safe) then ruff --fix --unsafe-fixes (unsafe) in two passes to isolate regressions
- 04-01: TYPE_CHECKING block in api.py includes both Starlette (F821 manual) and Request (TC002 unsafe); runtime import of Starlette preserved inside _build_api_app()
- 04-01: contextlib.suppress used for SIM105 in db.py (idiomatic over noqa suppression)
- 04-01: Only sem_memories loop variable changed to _ (B007 line 180); fts_memories loop at line 174 uses i for ranking
- 04-02: Used except OSError (builtin) not except urllib.error.URLError — simpler, no import needed, URLError is OSError subclass
- 04-02: Four broad handlers preserved at ONNX and background-task boundaries; all carry "Broad catch intentional:" comment for grep auditing
- [Phase 05-ci-cd-pipeline]: Coverage gate at 74% (measured 76% minus 2% headroom) — not 80% CICD-02 target; will increase as tests are added in Phases 6-8
- [Phase 05-ci-cd-pipeline]: pytest-asyncio installed explicitly in CI — required for asyncio_mode=auto even though not a declared project dependency
- [Phase 05-ci-cd-pipeline]: CICD-02 status corrected from Complete to Partial — gate mechanism works at 74% but requirement specifies 80%; will be fully satisfied when coverage reaches 80% in Phases 6-8
- [Phase 06-security-hardening, plan 01]: allow_origin_regex uses re.fullmatch() in Starlette 0.52.1 — localhost.evil.com does not match the pattern; both localhost and 127.0.0.1 covered with any port
- [Phase 06-security-hardening, plan 01]: Path guard fires before p.exists() to prevent information disclosure about forbidden paths; IMPORT_ROOTS defaults to [Path.home()]; empty env var treated as unset
- [Phase 06-security-hardening, plan 01]: test fixture patches IMPORT_ROOTS to include /tmp so pytest tmp_path fixtures pass SEC-02 guard; test_api_import_nonexistent_file updated to use /tmp path inside allowed roots
- [Phase 06-security-hardening, plan 02]: BearerAuthMiddleware defined inside _build_api_app() to preserve lazy Starlette import pattern (MCP stdio mode compatibility)
- [Phase 06-security-hardening, plan 02]: CORS middleware must be outermost (first in list) so OPTIONS preflight succeeds before auth sees the request
- [Phase 06-security-hardening, plan 02]: hmac.compare_digest() used for timing-safe token comparison — stdlib, no extra deps
- [Phase 06-security-hardening, plan 02]: client_with_auth test fixture patches API_KEY in both remind_me_mcp.config AND remind_me_mcp.api — from-import creates separate binding that must be updated independently
- [Phase 07-api-embedding-parity, plan 01]: sqlite-vec 0.1.6 requires 'AND mv.k = ?' constraint instead of 'LIMIT ?' in knn JOIN queries — LIMIT does not push through the JOIN planner; fixed in _semantic_search
- [Phase 07-api-embedding-parity, plan 01]: Gate api_update re-embed on 'content' in body and body['content'] is not None — tag-only updates must not call _embed_and_store (mirrors tools.py lines 359-360)
- [Phase 07-api-embedding-parity, plan 01]: _embed_and_store called via asyncio.to_thread in async route handlers — consistent with tools.py memory_add/memory_update pattern

### Pending Todos

None.

### Blockers/Concerns

- Phase 4 (RESOLVED 04-01): Side-effect import preservation — noqa: F401 comments survived ruff I001 auto-fix correctly
- Phase 4 (RESOLVED 04-02): ONNX exception boundaries in embeddings.py (lines 82, 145, 164) and updater.py (line 370) documented with "Broad catch intentional:" comments; pid.py narrowed to except OSError
- Phase 5 (RESOLVED 05-01): Coverage gate set at 74% (measured 76% minus 2% headroom) — not 80% target; pytest-asyncio added explicitly for asyncio_mode=auto
- Phase 5 (OPEN): CICD-02 requires 80% coverage gate but current gate is 74% (measured coverage 76%). Will resolve when Phases 6-8 add tests to reach 80%, at which point --cov-fail-under in ci.yml should be raised to 80
- Phase 6 (RESOLVED 06-01): Include both `localhost` and `127.0.0.1` in CORS — handled via regex; both covered with any port

## Session Continuity

Last session: 2026-02-24
Stopped at: Completed 07-01-PLAN.md (EMBD-01/EMBD-02 REST API embedding parity + _semantic_search knn fix, Phase 7 COMPLETE)
Resume file: None
