# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-02-24)

**Core value:** Persistent, searchable memory across all Claude interfaces — modular, tested, maintainable
**Current focus:** v1.1 Phase 5 — CI/CD Pipeline

## Current Position

Phase: 5 of 8 (CI/CD Pipeline)
Plan: 1 of 1 in current phase — Phase 5 Plan 1 COMPLETE
Status: In progress
Last activity: 2026-02-24 — Plan 05-01 complete (GitHub Actions CI workflow with ruff lint, pytest coverage gate, Python 3.11/3.12 matrix, CI badge in README)

Progress: [####░░░░░░] 40% (v1.1 — 2/5 phases... all 1 plan of phase 5 done)

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
| 05-ci-cd-pipeline | 1/1 | 2min | 2min |

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

### Pending Todos

None.

### Blockers/Concerns

- Phase 4 (RESOLVED 04-01): Side-effect import preservation — noqa: F401 comments survived ruff I001 auto-fix correctly
- Phase 4 (RESOLVED 04-02): ONNX exception boundaries in embeddings.py (lines 82, 145, 164) and updater.py (line 370) documented with "Broad catch intentional:" comments; pid.py narrowed to except OSError
- Phase 5 (RESOLVED 05-01): Coverage gate set at 74% (measured 76% minus 2% headroom) — not 80% target; pytest-asyncio added explicitly for asyncio_mode=auto
- Phase 5 (OPEN): CICD-02 requires 80% coverage gate but current gate is 74% (measured coverage 76%). Will resolve when Phases 6-8 add tests to reach 80%, at which point --cov-fail-under in ci.yml should be raised to 80
- Phase 6: Include both `localhost` and `127.0.0.1` in CORS allow_origins — they are distinct browser origins

## Session Continuity

Last session: 2026-02-24
Stopped at: Completed 05-01-PLAN.md (GitHub Actions CI workflow with ruff lint, pytest coverage gate, Python 3.11/3.12 matrix)
Resume file: None
