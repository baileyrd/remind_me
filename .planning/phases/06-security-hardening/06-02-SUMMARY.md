---
phase: 06-security-hardening
plan: 02
subsystem: auth
tags: [bearer-token, middleware, starlette, hmac, api-security]

# Dependency graph
requires:
  - phase: 06-01
    provides: CORS restriction and import path guard already wired into api.py
provides:
  - BearerAuthMiddleware class inside _build_api_app() with hmac.compare_digest timing-safe comparison
  - Optional API key auth gating all /api/* routes when REMIND_ME_API_KEY env var is set
  - Full backward compatibility — auth is no-op when env var is unset
  - 7 SEC-03 test cases covering all auth scenarios
affects:
  - 07-embedding-parity
  - 08-performance

# Tech tracking
tech-stack:
  added: []
  patterns:
    - BearerAuthMiddleware defined inside _build_api_app() to avoid module-level Starlette import
    - CORS middleware first (outermost) in list — OPTIONS preflight intercepted before auth
    - hmac.compare_digest() for constant-time token comparison (prevents timing attacks)
    - API_KEY patched in both config and api modules in tests (from-import creates separate binding)

key-files:
  created: []
  modified:
    - remind_me_mcp/api.py
    - tests/test_api.py

key-decisions:
  - "BearerAuthMiddleware defined inside _build_api_app() alongside route handlers — keeps lazy Starlette import pattern intact"
  - "CORS middleware must be first (outermost) in middleware list so OPTIONS preflight succeeds without Authorization header before auth middleware sees it"
  - "hmac.compare_digest() used for token comparison — stdlib, no extra dependencies, prevents timing-based token oracle attacks"
  - "API_KEY must be patched in both remind_me_mcp.config and remind_me_mcp.api in tests — from-import creates a local binding that must be updated independently"

patterns-established:
  - "Bearer token auth: check api_key is not None first, then path prefix, then hmac.compare_digest — fail-safe ordering"
  - "Test fixture client_with_auth: patches both config and api module, rebuilds app after patching"

requirements-completed: [SEC-03]

# Metrics
duration: 2min
completed: 2026-02-24
---

# Phase 6 Plan 02: Bearer Token Authentication Summary

**Optional Bearer token auth middleware gating all /api/* routes via BearerAuthMiddleware with hmac.compare_digest, fully backward-compatible when REMIND_ME_API_KEY is unset**

## Performance

- **Duration:** 2 min
- **Started:** 2026-02-24T21:09:35Z
- **Completed:** 2026-02-24T21:11:48Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments

- BearerAuthMiddleware class added inside `_build_api_app()` using `BaseHTTPMiddleware`, preserving lazy Starlette import pattern
- Middleware list updated: CORSMiddleware first (outermost) so OPTIONS preflight passes before auth; BearerAuthMiddleware second
- `hmac.compare_digest()` used for constant-time Bearer token comparison — prevents timing oracle attacks
- `API_KEY` imported from `remind_me_mcp.config` in `api.py`; auth is no-op when `None` (backward compatible)
- 7 SEC-03 test cases covering all truth assertions from the plan's `must_haves`
- All 208 tests pass; ruff clean on all modified files

## Task Commits

Each task was committed atomically:

1. **Task 1: Add BearerAuthMiddleware and wire into app** - `20f31b5` (feat)
2. **Task 2: Add Bearer auth tests with client_with_auth fixture** - `0960c5b` (feat)

**Plan metadata:** (docs commit — see below)

## Files Created/Modified

- `remind_me_mcp/api.py` — Added `import hmac`, `API_KEY` import, `BearerAuthMiddleware` class inside `_build_api_app()`, updated middleware list
- `tests/test_api.py` — Added `client_with_auth` fixture, 7 SEC-03 auth test functions

## Decisions Made

- BearerAuthMiddleware defined inside `_build_api_app()` alongside route handlers to keep lazy Starlette import pattern intact — avoids importing web framework at module level in MCP stdio mode
- CORS middleware must be first (outermost) in the middleware list so browser OPTIONS preflight (no Authorization header) is handled by CORS before auth middleware rejects it
- `hmac.compare_digest()` used from stdlib — no extra dependencies, prevents timing-based token oracle attacks
- In `client_with_auth` test fixture, `API_KEY` is patched in both `remind_me_mcp.config` AND `remind_me_mcp.api` because `from remind_me_mcp.config import API_KEY` creates a separate local binding in `api.py` that must be updated independently

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None - verification command in plan used bare `python` which lacked numpy in the shell environment; resolved by using `uv run python` consistent with the project's workflow. This is expected behavior for the uv-managed project, not a deviation.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- SEC-03 Bearer token auth complete; any user who sets `REMIND_ME_API_KEY` env var will have their `/api/*` routes protected
- Phase 7 (embedding-parity) can proceed — both touch `api.py` but changes are additive, no conflicts expected
- CICD-02 coverage gate: test count grew from 201 to 208 (+7 auth tests), coverage will have improved; raise `--cov-fail-under` in `ci.yml` when measured coverage reaches 80%

## Self-Check: PASSED

- remind_me_mcp/api.py: FOUND
- tests/test_api.py: FOUND
- 06-02-SUMMARY.md: FOUND
- Commit 20f31b5 (Task 1): FOUND
- Commit 0960c5b (Task 2): FOUND
- BearerAuthMiddleware in api.py: FOUND (line 96)
- hmac.compare_digest in api.py: FOUND (line 115)
- test_api_requires_auth_when_key_set in test_api.py: FOUND (line 559)

---
*Phase: 06-security-hardening*
*Completed: 2026-02-24*
