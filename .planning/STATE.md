# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-02-24)

**Core value:** Persistent, searchable memory across all Claude interfaces — modular, tested, maintainable
**Current focus:** v1.1 Phase 4 — Code Quality and Cleanup

## Current Position

Phase: 4 of 8 (Code Quality and Cleanup)
Plan: 2 of 2 in current phase — Phase 4 COMPLETE
Status: In progress
Last activity: 2026-02-24 — Plan 04-02 complete (narrow exception handlers, document preserved broad handlers, Phase 4 done)

Progress: [###░░░░░░░] 30% (v1.1 — 1.5/5 phases... all 2 plans of phase 4 done)

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

### Pending Todos

None.

### Blockers/Concerns

- Phase 4 (RESOLVED 04-01): Side-effect import preservation — noqa: F401 comments survived ruff I001 auto-fix correctly
- Phase 4 (RESOLVED 04-02): ONNX exception boundaries in embeddings.py (lines 82, 145, 164) and updater.py (line 370) documented with "Broad catch intentional:" comments; pid.py narrowed to except OSError
- Phase 5: Measure actual coverage before setting `--cov-fail-under` threshold — set at (measured - 2%) to allow headroom for new code in Phases 6-8
- Phase 6: Include both `localhost` and `127.0.0.1` in CORS allow_origins — they are distinct browser origins

## Session Continuity

Last session: 2026-02-24
Stopped at: Completed 04-02-PLAN.md (narrow exception handlers, document preserved broad handlers, Phase 4 complete)
Resume file: None
