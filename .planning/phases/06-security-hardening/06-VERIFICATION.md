---
phase: 06-security-hardening
verified: 2026-02-24T22:00:00Z
status: passed
score: 12/12 must-haves verified
re_verification: false
---

# Phase 6: Security Hardening Verification Report

**Phase Goal:** The dashboard API is hardened against cross-origin misuse, filesystem traversal, and unauthorized access when exposed outside localhost
**Verified:** 2026-02-24T22:00:00Z
**Status:** PASSED
**Re-verification:** No — initial verification

---

## Goal Achievement

### Observable Truths

From ROADMAP.md Success Criteria and plan must_haves (06-01 and 06-02 combined):

| #  | Truth | Status | Evidence |
|----|-------|--------|----------|
| 1  | Fetch calls from http://localhost receive Access-Control-Allow-Origin header | VERIFIED | `test_cors_allows_localhost` PASSES; `allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?"` in api.py line 380 |
| 2  | Fetch calls from http://127.0.0.1 receive Access-Control-Allow-Origin header | VERIFIED | `test_cors_allows_127_0_0_1` and `test_cors_allows_127_0_0_1_with_port` PASS |
| 3  | Fetch calls from any non-localhost origin do NOT receive Access-Control-Allow-Origin header | VERIFIED | `test_cors_no_acao_for_external_simple_request` and `test_cors_rejects_localhost_subdomain` PASS |
| 4  | Preflight OPTIONS from a non-localhost origin returns 400 | VERIFIED | `test_cors_denies_external_origin_preflight` PASSES |
| 5  | Import API rejects file paths outside the user home directory (or configured IMPORT_ROOTS) | VERIFIED | `test_import_rejects_path_outside_home` PASSES; guard at api.py lines 329-331 |
| 6  | Import API rejects traversal attempts like /home/user/../../etc/passwd | VERIFIED | `test_import_rejects_traversal_attempt` PASSES; `Path.expanduser().resolve()` normalises before check |
| 7  | Import API with paths inside allowed roots continues to work normally | VERIFIED | `test_import_allows_path_inside_home` and `test_import_custom_roots` PASS |
| 8  | When REMIND_ME_API_KEY is set, /api/* routes return 401 without a valid Bearer token | VERIFIED | `test_api_requires_auth_when_key_set` and `test_api_rejects_wrong_token` PASS |
| 9  | When REMIND_ME_API_KEY is set, /api/* routes return 200 with correct Bearer token | VERIFIED | `test_api_accepts_valid_token` PASSES |
| 10 | When REMIND_ME_API_KEY is unset, all /api/* routes remain open (backward-compatible) | VERIFIED | `test_api_open_when_no_key_configured` PASSES; `BearerAuthMiddleware.dispatch` returns early when `api_key is None` |
| 11 | The dashboard route (/) is accessible without auth regardless of API key configuration | VERIFIED | `test_dashboard_accessible_without_auth` PASSES; middleware only intercepts `/api/*` paths |
| 12 | CORS preflight OPTIONS requests are not blocked by the auth middleware | VERIFIED | `test_auth_does_not_block_cors_preflight` PASSES; CORS middleware is outermost, intercepts OPTIONS before auth |

**Score:** 12/12 truths verified

---

## Required Artifacts

### Plan 06-01 Artifacts

| Artifact | Provides | Status | Details |
|----------|----------|--------|---------|
| `remind_me_mcp/config.py` | API_KEY and IMPORT_ROOTS security constants | VERIFIED | Lines 48-57: both constants defined, exported in `__all__` (lines 80-81). IMPORT_ROOTS defaults to `[Path.home()]`, API_KEY defaults to None |
| `remind_me_mcp/api.py` | CORS regex restriction and import path guard | VERIFIED | Line 380: `allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?"`. Lines 329-331: SEC-02 path guard before `p.exists()` check |
| `tests/test_api.py` | CORS and path guard test coverage | VERIFIED | 7 CORS tests (lines 457-499) and 4 path guard tests (lines 507-551) present and passing |

### Plan 06-02 Artifacts

| Artifact | Provides | Status | Details |
|----------|----------|--------|---------|
| `remind_me_mcp/api.py` | BearerAuthMiddleware class and middleware wiring | VERIFIED | Lines 96-117: class defined inside `_build_api_app()`. Line 384: `Middleware(BearerAuthMiddleware, api_key=API_KEY)`. Uses `hmac.compare_digest` at line 115 |
| `tests/test_api.py` | Bearer auth test coverage with client_with_auth fixture | VERIFIED | Fixture at lines 51-66; 7 SEC-03 tests at lines 559-615, all passing |

---

## Key Link Verification

### Plan 06-01 Key Links

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `remind_me_mcp/api.py` | `remind_me_mcp/config.py` | `import IMPORT_ROOTS` | WIRED | Line 20: `from remind_me_mcp.config import API_KEY, DB_PATH, IMPORT_ROOTS` — includes IMPORT_ROOTS |
| `remind_me_mcp/api.py` | `starlette.middleware.cors.CORSMiddleware` | `allow_origin_regex parameter` | WIRED | Line 380: `allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?"` — wildcard `allow_origins=["*"]` completely absent from file |

### Plan 06-02 Key Links

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `remind_me_mcp/api.py` | `remind_me_mcp/config.py` | `import API_KEY` | WIRED | Line 20: `from remind_me_mcp.config import API_KEY, DB_PATH, IMPORT_ROOTS` — API_KEY imported |
| `remind_me_mcp/api.py (BearerAuthMiddleware)` | `remind_me_mcp/api.py (_build_api_app middleware list)` | `Middleware(BearerAuthMiddleware, api_key=API_KEY)` | WIRED | Line 384: `Middleware(BearerAuthMiddleware, api_key=API_KEY)` present in middleware list |
| `remind_me_mcp/api.py (CORS middleware)` | `remind_me_mcp/api.py (BearerAuthMiddleware)` | middleware list ordering — CORS first (outermost), auth second | WIRED | Lines 378-385: CORS middleware defined at index 0, BearerAuthMiddleware at index 1 — correct ordering confirmed |

---

## Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|-------------|-------------|--------|----------|
| SEC-01 | 06-01 | Dashboard API restricts CORS to localhost origins only | SATISFIED | `allow_origin_regex` targeting localhost and 127.0.0.1 wired in api.py; 7 CORS tests pass |
| SEC-02 | 06-01 | Import API restricts file paths to within user's home directory (configurable via REMIND_ME_IMPORT_ROOTS env var) | SATISFIED | Path guard in `api_import()` before `p.exists()`, uses `IMPORT_ROOTS` allow-list; 4 path guard tests pass including custom root test |
| SEC-03 | 06-02 | Optional API auth via REMIND_ME_API_KEY env var — Bearer token on all /api/* routes when set, no-op when unset | SATISFIED | `BearerAuthMiddleware` with `hmac.compare_digest` wired in middleware list; 7 auth tests pass covering all cases including backward compatibility |

All 3 requirements mapped to Phase 6 in REQUIREMENTS.md are SATISFIED. No orphaned requirements.

---

## Anti-Patterns Found

No anti-patterns detected across modified files:

- No TODO/FIXME/HACK/PLACEHOLDER comments in `remind_me_mcp/api.py`, `remind_me_mcp/config.py`, or `tests/test_api.py`
- No stub implementations (`return null`, `return {}`, empty handlers)
- No console.log-only handlers
- `ruff check` reports zero warnings on all three modified files

---

## Human Verification Required

None — all security behaviors are unit-tested and verifiable programmatically. The `TestClient` exercises real Starlette middleware stacks, so CORS header presence/absence and 401/200 responses are confirmed by the test suite.

---

## Verification Evidence Summary

**Test results:** 44 tests in `tests/test_api.py` — 44 passed, 0 failed (0.52s)

**Security-specific test breakdown:**
- SEC-01 CORS: 7 tests — `test_cors_allows_localhost`, `test_cors_allows_localhost_with_port`, `test_cors_allows_127_0_0_1`, `test_cors_allows_127_0_0_1_with_port`, `test_cors_denies_external_origin_preflight`, `test_cors_no_acao_for_external_simple_request`, `test_cors_rejects_localhost_subdomain`
- SEC-02 path guard: 4 tests — `test_import_rejects_path_outside_home`, `test_import_rejects_traversal_attempt`, `test_import_allows_path_inside_home`, `test_import_custom_roots`
- SEC-03 auth: 7 tests — `test_api_requires_auth_when_key_set`, `test_api_rejects_wrong_token`, `test_api_accepts_valid_token`, `test_dashboard_accessible_without_auth`, `test_api_open_when_no_key_configured`, `test_auth_does_not_block_cors_preflight`, `test_auth_protects_all_api_routes`

**Lint:** `ruff check remind_me_mcp/config.py remind_me_mcp/api.py tests/test_api.py` — All checks passed

**Commits verified:**
- `9cf98b6` — feat(06-01): add security config constants and restrict CORS to localhost
- `1eeb1ad` — feat(06-01): add CORS and import path guard tests
- `20f31b5` — feat(06-02): add BearerAuthMiddleware and wire into app
- `0960c5b` — feat(06-02): add Bearer auth tests with client_with_auth fixture

**Critical implementation details confirmed:**
- Wildcard `allow_origins=["*"]` — completely absent from api.py (zero matches)
- `allow_origin_regex` — present at line 380 with correct regex
- Path guard (line 329-331) — fires BEFORE `p.exists()` check (line 333), preventing information disclosure
- `hmac.compare_digest()` — used at line 115 for constant-time token comparison
- Middleware order — CORS (index 0) before BearerAuthMiddleware (index 1), correct for OPTIONS preflight handling
- `IMPORT_ROOTS` and `API_KEY` — both in `__all__` in config.py
- Backward compatibility — `api_key is None` short-circuit at line 109 confirmed

---

_Verified: 2026-02-24T22:00:00Z_
_Verifier: Claude (gsd-verifier)_
