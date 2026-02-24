---
phase: 06-security-hardening
plan: 01
subsystem: api
tags: [starlette, cors, security, pathlib, middleware]

# Dependency graph
requires:
  - phase: 05-ci-cd-pipeline
    provides: CI validates every security change automatically

provides:
  - CORS restriction to localhost/127.0.0.1 origins via allow_origin_regex
  - Import path traversal guard (SEC-02) using IMPORT_ROOTS allow-list
  - API_KEY and IMPORT_ROOTS security constants in config.py
  - 11 new security tests (7 CORS + 4 import path guard) in test_api.py

affects: [06-02, api, security]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "CORS restriction: CORSMiddleware(allow_origin_regex=...) with fullmatch prevents subdomain bypass"
    - "Path guard order: allowed-roots check BEFORE p.exists() prevents information disclosure"
    - "IMPORT_ROOTS: colon-separated env var parsed at startup, default=Path.home()"
    - "Test fixture patching: IMPORT_ROOTS patched in both config and api modules to include /tmp"

key-files:
  created: []
  modified:
    - remind_me_mcp/config.py
    - remind_me_mcp/api.py
    - tests/test_api.py

key-decisions:
  - "allow_origin_regex=r'http://(localhost|127\\.0\\.0\\.1)(:\\d+)?' covers both hosts and any port using regex fullmatch — localhost.evil.com does not match"
  - "Path guard fires before p.exists() to prevent information disclosure about forbidden filesystem locations"
  - "IMPORT_ROOTS defaults to [Path.home()] when env var unset; empty string env var treated as unset via 'if _import_roots_env' (empty string is falsy)"
  - "Test fixture patches IMPORT_ROOTS to include /tmp so pytest tmp_path fixtures work with SEC-02 guard active"

patterns-established:
  - "Security constants added to config.py Security section (between UI and Logging sections)"
  - "Middleware order: CORS before AUTH (CORS must intercept OPTIONS preflight before auth can reject it)"

requirements-completed: [SEC-01, SEC-02]

# Metrics
duration: 4min
completed: 2026-02-24
---

# Phase 6 Plan 01: Security Hardening — CORS and Import Path Guard Summary

**CORS restricted to localhost/127.0.0.1 origins via regex middleware, import path traversal blocked via IMPORT_ROOTS allow-list, with 11 new security tests covering all allowed/rejected origin and path combinations.**

## Performance

- **Duration:** ~4 min
- **Started:** 2026-02-24T21:02:42Z
- **Completed:** 2026-02-24T21:06:41Z
- **Tasks:** 2
- **Files modified:** 3

## Accomplishments

- Added `API_KEY` and `IMPORT_ROOTS` security constants to `config.py`, exported in `__all__`
- Replaced `allow_origins=["*"]` with `allow_origin_regex` targeting `http://localhost` and `http://127.0.0.1` with optional port in `api.py`
- Added SEC-02 path guard in `api_import()` before `p.exists()` check, preventing filesystem traversal and information disclosure
- Added 7 SEC-01 CORS tests and 4 SEC-02 path guard tests, all passing alongside all 26 pre-existing tests (37 total)

## Task Commits

Each task was committed atomically:

1. **Task 1: Add security config constants and restrict CORS to localhost** - `9cf98b6` (feat)
2. **Task 2: Add CORS and import path guard tests** - `1eeb1ad` (feat)

**Plan metadata:** (docs commit follows)

## Files Created/Modified

- `remind_me_mcp/config.py` - Added Security section with `API_KEY` and `IMPORT_ROOTS` constants, updated `__all__`
- `remind_me_mcp/api.py` - Updated config import, replaced CORS wildcard with regex, added SEC-02 path guard in `api_import()`, updated docstring
- `tests/test_api.py` - Moved `Path` to runtime import, patched `IMPORT_ROOTS` in client fixture, added 11 security tests, fixed `test_api_import_nonexistent_file`

## Decisions Made

- `allow_origin_regex` uses `re.fullmatch()` internally (Starlette 0.52.1 verified): `localhost.evil.com` does not match, preventing subdomain bypass
- CORS regex includes `(:\d+)?` to match any port — dashboard port is configurable (`REMIND_ME_MCP_UI_PORT`), restricting to a single port would break custom configurations
- `API_KEY` not imported into `api.py` in this plan — deferred to Plan 02 (Bearer auth middleware) as specified
- Path guard error message: "Path not in allowed import roots: {p}" contains "not in allowed" (lowercase match used in tests)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed test_api_import_nonexistent_file assertion broken by SEC-02 path guard**
- **Found during:** Task 2 (CORS and import path guard tests)
- **Issue:** The existing test used `/nonexistent/path/file.json` which is outside IMPORT_ROOTS, so SEC-02 now returns "Path not in allowed import roots" instead of "not found". The test assertion `"not found" in data["error"].lower()` failed.
- **Fix:** Updated the test to use `/tmp/nonexistent_remind_me_test_file.json` — a path inside the patched IMPORT_ROOTS (`/tmp`) that does not exist, so the guard passes and the existence check fires correctly.
- **Files modified:** `tests/test_api.py`
- **Verification:** 37 tests pass after fix
- **Committed in:** `1eeb1ad` (Task 2 commit)

**2. [Rule 1 - Bug] Removed empty TYPE_CHECKING block left after moving Path to runtime import**
- **Found during:** Task 2, lint check
- **Issue:** After moving `from pathlib import Path` out of the `TYPE_CHECKING` block (needed at runtime for traversal test), the `if TYPE_CHECKING: pass` block remained, causing ruff TC005 warning.
- **Fix:** Removed the empty `TYPE_CHECKING` block entirely.
- **Files modified:** `tests/test_api.py`
- **Verification:** `ruff check` passes with zero warnings
- **Committed in:** `1eeb1ad` (Task 2 commit)

---

**Total deviations:** 2 auto-fixed (both Rule 1 - Bug)
**Impact on plan:** Both fixes necessary for test correctness and lint compliance. No scope creep.

## Issues Encountered

None — all plan steps executed as specified.

## Self-Check: PASSED

- `remind_me_mcp/config.py` — FOUND
- `remind_me_mcp/api.py` — FOUND
- `tests/test_api.py` — FOUND
- Commit `9cf98b6` — FOUND
- Commit `1eeb1ad` — FOUND

## User Setup Required

None — no external service configuration required. `REMIND_ME_API_KEY` and `REMIND_ME_IMPORT_ROOTS` env vars are optional; defaults preserve backward compatibility.

## Next Phase Readiness

- SEC-01 and SEC-02 complete; api.py ready for Plan 02 (SEC-03 Bearer auth middleware)
- Plan 02 will add `BearerAuthMiddleware` class and import `API_KEY` from config
- CORS middleware already in correct position (before auth) for Plan 02 middleware wiring

---
*Phase: 06-security-hardening*
*Completed: 2026-02-24*
