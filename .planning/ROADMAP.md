# Roadmap: Remind Me MCP

## Milestones

- **v1.0 Full Refactor** — Phases 1-3 (shipped 2026-02-24)
- **v1.1 Address 1.0 Tech Debt** — Phases 4-8 (in progress)

## Phases

<details>
<summary>v1.0 Full Refactor (Phases 1-3) — SHIPPED 2026-02-24</summary>

- [x] Phase 1: Package Structure (3/3 plans) — completed 2026-02-24
- [x] Phase 2: Test Infrastructure (4/4 plans) — completed 2026-02-24
- [x] Phase 3: Quality and Bug Fixes (5/5 plans) — completed 2026-02-24

</details>

### v1.1 Address 1.0 Tech Debt (In Progress)

**Milestone Goal:** Clean up all known tech debt from v1.0 — code quality, CI/CD, security, embedding correctness, and performance.

- [x] **Phase 4: Code Quality and Cleanup** - Resolve all ruff warnings, narrow exception handlers, and remove the dead monolith file (completed 2026-02-24)
- [x] **Phase 5: CI/CD Pipeline** - Establish GitHub Actions with lint, test, and coverage gates that validate all subsequent phases (completed 2026-02-24)
- [x] **Phase 6: Security Hardening** - Lock down CORS, restrict import paths, and add optional API key authentication (completed 2026-02-24)
- [ ] **Phase 7: API Embedding Parity** - Fix the correctness gap where REST API memories are invisible to semantic search
- [ ] **Phase 8: Performance Improvements** - Batch reindex and concurrent file import for large-scale operation

## Phase Details

### Phase 4: Code Quality and Cleanup
**Goal**: The codebase is clean, lint-free, and contains no dead code — establishing a stable baseline for all subsequent phases
**Depends on**: Phase 3 (v1.0 complete)
**Requirements**: QUAL-01, QUAL-02, QUAL-03
**Success Criteria** (what must be TRUE):
  1. Running `ruff check .` produces zero warnings or errors (all 26 auto-fix and 4 manual warnings resolved)
  2. Exception handlers in embeddings.py, pid.py, and updater.py catch specific types rather than bare `Exception` at safe call sites, with broad clauses preserved only at intentional graceful-degradation boundaries
  3. The file `remind_me_mcp_original.py` no longer exists in the repository
  4. All 190 existing tests continue to pass after cleanup changes
**Plans:** 2/2 plans complete
Plans:
- [x] 04-01-PLAN.md — Delete monolith and resolve all ruff warnings (QUAL-03, QUAL-01) — completed 2026-02-24
- [ ] 04-02-PLAN.md — Narrow exception handlers and final validation (QUAL-02)

### Phase 5: CI/CD Pipeline
**Goal**: Every push and pull request is automatically validated against lint, tests, and coverage — making regressions visible immediately
**Depends on**: Phase 4
**Requirements**: CICD-01, CICD-02
**Success Criteria** (what must be TRUE):
  1. A GitHub Actions workflow runs on every push and pull request, executing ruff lint and pytest across Python 3.11 and 3.12 matrix
  2. The CI job fails if test coverage falls below 80%, blocking merges on coverage regression
  3. A passing green CI badge is visible on the repository after a clean push
**Plans:** 2/2 plans complete
Plans:
- [x] 05-01-PLAN.md — Create CI workflow (lint + test matrix + coverage gate) and add README badge (CICD-01, CICD-02)
- [x] 05-02-PLAN.md — Correct CICD-02 requirement status tracking (gap closure) (CICD-01, CICD-02) — completed 2026-02-24

### Phase 6: Security Hardening
**Goal**: The dashboard API is hardened against cross-origin misuse, filesystem traversal, and unauthorized access when exposed outside localhost
**Depends on**: Phase 5
**Requirements**: SEC-01, SEC-02, SEC-03
**Success Criteria** (what must be TRUE):
  1. The dashboard API accepts requests only from `http://localhost` and `http://127.0.0.1` origins — fetch calls from any other origin are rejected with a CORS error
  2. Import API calls with file paths outside the user's home directory (or configured `REMIND_ME_IMPORT_ROOTS`) are rejected with an error, not executed
  3. When `REMIND_ME_API_KEY` is set, all `/api/*` routes require a matching `Authorization: Bearer <token>` header and return 401 for missing or incorrect tokens; when unset, all routes remain open (backward-compatible)
  4. Existing deployments with no new env vars set continue to function identically after upgrading
**Plans:** 2/2 plans complete
Plans:
- [ ] 06-01-PLAN.md — Add security config constants, restrict CORS to localhost, add import path guard (SEC-01, SEC-02)
- [ ] 06-02-PLAN.md — Add optional Bearer token auth middleware for /api/* routes (SEC-03)

### Phase 7: API Embedding Parity
**Goal**: Memories created or updated through the REST API are immediately embedded and retrievable via semantic search — matching MCP tool behavior
**Depends on**: Phase 6
**Requirements**: EMBD-01, EMBD-02
**Success Criteria** (what must be TRUE):
  1. A memory added via `POST /api/memories` appears in semantic search results within the same request/response cycle (a vector row exists in the embeddings table)
  2. A memory updated via `PUT /api/memories/{id}` with new content produces an updated embedding — the old semantic representation is replaced
  3. REST API memories and MCP tool memories are indistinguishable in semantic search results
**Plans**: TBD

### Phase 8: Performance Improvements
**Goal**: Reindexing large memory databases and importing large file directories complete significantly faster via batch and concurrent processing
**Depends on**: Phase 7
**Requirements**: PERF-01, PERF-02
**Success Criteria** (what must be TRUE):
  1. The reindex tool processes embeddings in batches of 32 using the `embedder.embed(list)` batch API rather than one-at-a-time calls (measurable via reduced call count in tests)
  2. Directory import processes files concurrently with a bounded semaphore, completing faster than sequential processing on directories with 10+ files
  3. All 190+ tests continue to pass after performance refactors, confirming no behavioral regression
**Plans**: TBD

## Progress

**Execution Order:**
Phases execute in numeric order: 4 → 5 → 6 → 7 → 8

| Phase | Milestone | Plans Complete | Status | Completed |
|-------|-----------|----------------|--------|-----------|
| 1. Package Structure | v1.0 | 3/3 | Complete | 2026-02-24 |
| 2. Test Infrastructure | v1.0 | 4/4 | Complete | 2026-02-24 |
| 3. Quality and Bug Fixes | v1.0 | 5/5 | Complete | 2026-02-24 |
| 4. Code Quality and Cleanup | 2/2 | Complete    | 2026-02-24 | - |
| 5. CI/CD Pipeline | v1.1 | 2/2 | Complete | 2026-02-24 |
| 6. Security Hardening | 2/2 | Complete   | 2026-02-24 | - |
| 7. API Embedding Parity | v1.1 | 0/? | Not started | - |
| 8. Performance Improvements | v1.1 | 0/? | Not started | - |
