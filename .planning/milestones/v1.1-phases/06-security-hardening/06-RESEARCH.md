# Phase 6: Security Hardening - Research

**Researched:** 2026-02-24
**Domain:** Starlette middleware — CORS restriction, path traversal prevention, bearer token auth
**Confidence:** HIGH

## Summary

Phase 6 adds three independent security controls to the existing Starlette API in `remind_me_mcp/api.py`. All three requirements are implementable using Starlette's built-in middleware primitives — no new dependencies are needed. The project already depends on `starlette>=0.40.0`; the installed version is 0.52.1.

The current `_build_api_app()` function uses `allow_origins=["*"]` which permits all cross-origin requests. SEC-01 replaces this with `allow_origin_regex` targeting only `http://localhost` and `http://127.0.0.1` (including any port). SEC-02 adds a path validation check inside the existing `api_import()` handler, comparing the resolved path against `REMIND_ME_IMPORT_ROOTS` (defaulting to the user home directory). SEC-03 adds a `BearerAuthMiddleware` built on `BaseHTTPMiddleware` that gates all `/api/*` routes when `REMIND_ME_API_KEY` is set, and is a no-op when unset (preserving backward compatibility).

No new packages are required. All changes are confined to `api.py` (middleware wiring + import handler validation) and `config.py` (two new env-var constants). The dashboard route `/` is not an `/api/*` path and must remain open regardless of auth configuration, because the browser fetches it before knowing whether to add an Authorization header.

**Primary recommendation:** Use `CORSMiddleware(allow_origin_regex=...)` for SEC-01, inline path validation in `api_import()` for SEC-02, and a `BaseHTTPMiddleware` subclass for SEC-03. Add constants to `config.py`. Wire middleware in `_build_api_app()`.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| SEC-01 | Dashboard API restricts CORS to localhost origins only (both `127.0.0.1` and `localhost`) | `CORSMiddleware(allow_origin_regex=r'http://(localhost|127\.0\.0\.1)(:\d+)?')` — verified against Starlette 0.52.1; regex anchored with `fullmatch`. Browser enforces CORS for simple requests (no ACAO header → blocked); server returns 400 for disallowed preflight OPTIONS. |
| SEC-02 | Import API restricts file paths to within user's home directory (configurable via `REMIND_ME_IMPORT_ROOTS` env var) | Inline guard in `api_import()`: `Path(file_path).expanduser().resolve()`, then check `any(p == root or root in p.parents for root in import_roots)`. Path.resolve() eliminates traversal sequences. `REMIND_ME_IMPORT_ROOTS` is colon-separated; parsed at startup in config.py. |
| SEC-03 | Optional API auth via `REMIND_ME_API_KEY` env var — Bearer token on all `/api/*` routes when set, no-op when unset | `BaseHTTPMiddleware` subclass with `dispatch()` checking `Authorization: Bearer <token>` header. When `api_key is None` (env unset), middleware passes all requests through unchanged — exact backward compatibility. |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| starlette | 0.52.1 (installed); `>=0.40.0` (declared) | CORS middleware, base HTTP middleware | Already a project dependency; provides `CORSMiddleware` and `BaseHTTPMiddleware` |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| pathlib.Path | stdlib | Filesystem path resolution, traversal elimination | SEC-02 path validation — `resolve()` eliminates `..` sequences |
| os.environ | stdlib | Environment variable reading for API key and import roots | SEC-02 and SEC-03 configuration |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `allow_origin_regex` | Explicit `allow_origins` list with all ports | Regex is cleaner (one line vs 4 entries); `allow_origin_regex` uses `re.fullmatch()` so `localhost.evil.com` is not matched |
| `BaseHTTPMiddleware` | Pure ASGI middleware | `BaseHTTPMiddleware` is simpler to write and test; pure ASGI gives streaming control but is overkill for a simple auth check |
| Inline path guard in handler | Separate `PathValidationMiddleware` | Inline is correct here: only `api_import` needs path checking; a route-level middleware would require route inspection |

**Installation:** No new packages. All functionality is in existing dependencies.

## Architecture Patterns

### Recommended Project Structure

Changes are confined to two existing files:

```
remind_me_mcp/
├── config.py        # Add: API_KEY, IMPORT_ROOTS constants (read from env at import)
└── api.py           # Add: BearerAuthMiddleware class, path guard in api_import(),
                     #      update _build_api_app() middleware list + CORS regex
```

New test coverage goes in the existing test file:
```
tests/
└── test_api.py      # Add: SEC-01, SEC-02, SEC-03 test classes
```

### Pattern 1: CORS Restriction with allow_origin_regex

**What:** Replace `allow_origins=["*"]` with a regex anchored to `http://localhost` and `http://127.0.0.1` with optional port.

**When to use:** When the API must be accessible from the browser only on localhost, regardless of port.

**Verified behavior (Starlette 0.52.1):**
- `CORSMiddleware.is_allowed_origin()` calls `compiled_allow_origin_regex.fullmatch(origin)` — substring match is not possible; `localhost.evil.com` does not match.
- Simple (non-preflight) requests from disallowed origins receive a 200 response body but NO `Access-Control-Allow-Origin` header → browser rejects the response.
- Preflight (OPTIONS) requests from disallowed origins receive `400 Disallowed CORS origin`.
- Allowed origins receive the reflected origin in `Access-Control-Allow-Origin`.

```python
# Source: verified against starlette 0.52.1 source and live test
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware import Middleware

Middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_methods=["*"],
    allow_headers=["*"],
)
```

### Pattern 2: Bearer Token Auth Middleware (BaseHTTPMiddleware)

**What:** A `BaseHTTPMiddleware` subclass that gates all `/api/*` routes when `REMIND_ME_API_KEY` is set.

**When to use:** Optional auth — must be a no-op when the env var is unset.

**Verified behavior (Starlette 0.52.1):**
- Middleware list order `[CORS, AUTH]` means CORS receives the request first (wraps everything).
- `CORSMiddleware` intercepts OPTIONS preflight and short-circuits before the auth middleware dispatch runs — auth middleware does NOT need an explicit `OPTIONS` bypass.
- When `api_key is None`: `call_next(request)` is invoked immediately with no auth check.
- When `api_key` is set: missing or wrong `Authorization` header → 401 JSON response.

```python
# Source: verified with live Starlette 0.52.1 TestClient test
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Gate all /api/* routes behind Bearer token auth when REMIND_ME_API_KEY is set.

    When api_key is None (env var unset), all requests pass through unchanged
    preserving backward compatibility for existing deployments.

    Args:
        app: The ASGI app to wrap.
        api_key: The expected token value, or None to disable auth.
    """

    def __init__(self, app: ASGIApp, api_key: str | None = None) -> None:
        super().__init__(app)
        self.api_key = api_key

    async def dispatch(self, request: Request, call_next) -> Response:
        """Pass requests through when auth is disabled; enforce bearer token otherwise."""
        if self.api_key is None:
            return await call_next(request)
        if not request.url.path.startswith("/api/"):
            return await call_next(request)
        auth = request.headers.get("Authorization", "")
        if auth == f"Bearer {self.api_key}":
            return await call_next(request)
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
```

**Middleware wiring in `_build_api_app()`:**
```python
middleware = [
    Middleware(
        CORSMiddleware,
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_methods=["*"],
        allow_headers=["*"],
    ),
    Middleware(BearerAuthMiddleware, api_key=API_KEY),
]
```

### Pattern 3: Import Path Guard (SEC-02)

**What:** Inline path validation in `api_import()` before processing.

**When to use:** At the start of the handler, after resolving the path but before filesystem operations.

**Verified behavior:** `Path(...).expanduser().resolve()` canonicalizes the path (expands `~`, resolves `..`). Traversal attempts like `/home/user/../../etc/passwd` resolve to `/etc/passwd` and are denied.

```python
# Source: verified with pathlib on Python 3.11
from pathlib import Path
from remind_me_mcp.config import IMPORT_ROOTS  # list[Path]

async def api_import(request: Request) -> JSONResponse:
    # ... body parsing ...
    file_path = body.get("file_path", "").strip()
    if not file_path:
        return _json_err("'file_path' is required")

    p = Path(file_path).expanduser().resolve()

    # SEC-02: Reject paths outside allowed roots
    if not any(p == root or root in p.parents for root in IMPORT_ROOTS):
        return _json_err(f"Path not allowed: {p}")

    if not p.exists():
        return _json_err(f"Path not found: {p}")
    # ... rest of handler unchanged ...
```

**Important:** The allowed-roots check must come BEFORE the `p.exists()` check to prevent information disclosure (confirming whether a forbidden path exists).

### Pattern 4: Config Constants for New Env Vars

**What:** Add `API_KEY` and `IMPORT_ROOTS` to `config.py` following the existing pattern.

```python
# In config.py — following existing pattern (explicit, no magic)
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

API_KEY: str | None = os.environ.get("REMIND_ME_API_KEY") or None
"""Bearer token for /api/* routes. None = auth disabled (backward-compatible)."""

_import_roots_env: str | None = os.environ.get("REMIND_ME_IMPORT_ROOTS")
IMPORT_ROOTS: list[Path] = (
    [Path(r.strip()).expanduser().resolve() for r in _import_roots_env.split(":") if r.strip()]
    if _import_roots_env
    else [Path.home()]
)
"""Allowed filesystem roots for import operations. Default: user home directory."""
```

**Note:** `os.environ.get("REMIND_ME_API_KEY") or None` ensures an empty string `""` is treated as unset (same as missing env var). This prevents accidentally enabling auth with an empty key.

**Update `__all__` in config.py:**
```python
__all__ = [
    ...,
    "API_KEY",
    "IMPORT_ROOTS",
]
```

### Anti-Patterns to Avoid

- **`allow_origins=["*"]` (current state):** Permits any cross-origin request. Must be replaced with the regex. Do not add individual ports to the list as that creates maintenance burden.
- **Empty API key enabling auth:** `os.environ.get("REMIND_ME_API_KEY")` returns `""` not `None` for `REMIND_ME_API_KEY=` (empty assignment). Use `or None` to treat empty string as unset.
- **Checking path.exists() before allowed-roots guard:** Allows information disclosure about forbidden filesystem locations.
- **Putting auth before CORS in middleware list:** The dashboard's browser-side `fetch()` calls require a CORS preflight. If auth runs first and the preflight lacks an `Authorization` header, the browser's preflight will get a 401 and all API calls from the dashboard will fail.
- **Filtering path with `startswith("/home")` string comparison:** Use `root in p.parents` (Path comparison) not string prefix matching; string comparison is incorrect on edge cases (e.g., `/home_backup`).

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| CORS header management | Custom `Access-Control-*` header injection | `starlette.middleware.cors.CORSMiddleware` | Handles preflight, Vary headers, credentials, and all edge cases correctly |
| Request interception pipeline | Custom ASGI middleware from scratch | `starlette.middleware.base.BaseHTTPMiddleware` | Provides clean `dispatch(request, call_next)` pattern; handles streaming edge cases |
| Path canonicalization | Custom `..` stripping | `pathlib.Path.resolve()` | Built-in, handles symlinks, platform differences, all traversal patterns |

**Key insight:** All three security controls are one-liner configurations of existing, battle-tested Starlette primitives. The only custom code is the `BearerAuthMiddleware` class (10 lines) and the path guard (3 lines). No external auth libraries (e.g., `starlette-auth`, `authlib`) are needed for a simple static bearer token.

## Common Pitfalls

### Pitfall 1: localhost vs 127.0.0.1 as Distinct Browser Origins

**What goes wrong:** Configuring only `http://localhost` causes fetch failures when the user browses to `http://127.0.0.1:5199` (or vice versa). Both are common ways to reach a local server.

**Why it happens:** RFC 6454 defines origin as scheme + host + port. `localhost` and `127.0.0.1` are different hosts even though they resolve to the same address.

**How to avoid:** Use the regex `http://(localhost|127\.0\.0\.1)(:\d+)?` which covers both hosts and any port. The STATE.md already notes this: "Include both `localhost` and `127.0.0.1` in CORS allow_origins."

**Warning signs:** The dashboard loads but API calls fail with CORS errors when accessed via `127.0.0.1` instead of `localhost`.

### Pitfall 2: Port Number in Origin Header

**What goes wrong:** Allowing only `http://localhost` (without port) denies the dashboard's own fetch calls when served on port 5199 (`Origin: http://localhost:5199`).

**Why it happens:** `http://localhost` (port 80/443 implied) and `http://localhost:5199` are different origins. The browser sends the serving port in the Origin header for non-standard ports.

**How to avoid:** The regex `(:\d+)?` makes the port optional, matching both bare `http://localhost` and `http://localhost:5199`.

**Warning signs:** Dashboard page loads (HTML served directly, no CORS), but API fetch calls inside the dashboard return CORS errors.

### Pitfall 3: CORS Enforcement is Browser-Side for Simple Requests

**What goes wrong:** Testing CORS by making direct `curl` or `TestClient` requests without an Origin header and expecting 403. The server never rejects the request itself — the response simply lacks the ACAO header.

**Why it happens:** For simple (non-preflight) requests, browsers check the ACAO header in the response and block JavaScript from reading it. The server cannot prevent the request from arriving.

**How to avoid:** Test CORS by checking: (a) allowed origin → ACAO header present, (b) disallowed origin → ACAO header absent, (c) preflight OPTIONS from disallowed origin → 400 status. The test must send an `Origin` header to trigger CORS logic.

**Warning signs:** Tests pass because they don't send an Origin header, but the browser still blocks the dashboard.

### Pitfall 4: BearerAuthMiddleware Blocking CORS Preflight

**What goes wrong:** Auth middleware placed before CORS in the middleware list intercepts OPTIONS requests and returns 401 because they lack an Authorization header. All API calls from the browser then fail before they start.

**Why it happens:** Browsers send a `CORS preflight OPTIONS` request before any cross-origin request with custom headers (like `Authorization`). If the OPTIONS request itself requires auth, the browser cannot learn the CORS policy.

**How to avoid:** Always list CORS middleware before auth: `middleware=[Middleware(CORSMiddleware, ...), Middleware(BearerAuthMiddleware, ...)]`. Starlette applies middleware in list order (first = outermost), so CORS intercepts OPTIONS before auth sees it.

**Warning signs:** `OPTIONS` requests to `/api/*` return 401 instead of 200.

### Pitfall 5: Returning 403 vs 401

**What goes wrong:** Returning 403 Forbidden for missing/incorrect tokens instead of 401 Unauthorized.

**Why it happens:** Confusing "not authorized to do this thing" (403) with "not authenticated" (401).

**How to avoid:** Return 401 for missing or wrong Bearer token. 401 semantically means "you need to authenticate." The requirement explicitly states "return 401 for missing or incorrect tokens."

### Pitfall 6: IMPORT_ROOTS Empty String Handling

**What goes wrong:** `REMIND_ME_IMPORT_ROOTS=""` (set but empty) produces an empty roots list, then every import path is denied.

**Why it happens:** `"".split(":")` returns `[""]`, filtered by `if r.strip()` to an empty list. Then `any(...)` over an empty list is always `False`.

**How to avoid:** The parsing function must fall back to `[Path.home()]` when the env var is present but empty. The conditional `if _import_roots_env` handles this because an empty string is falsy in Python.

## Code Examples

Verified patterns from live Starlette 0.52.1 testing:

### Complete _build_api_app() Middleware Section (After Phase 6)

```python
# In remind_me_mcp/api.py
# Source: verified against Starlette 0.52.1

from remind_me_mcp.config import API_KEY, DB_PATH, IMPORT_ROOTS

def _build_api_app() -> Starlette:
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.middleware.cors import CORSMiddleware
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse, Response
    from starlette.routing import Route

    class BearerAuthMiddleware(BaseHTTPMiddleware):
        """Gate all /api/* routes behind Bearer token auth when REMIND_ME_API_KEY is set."""

        def __init__(self, app: ASGIApp, api_key: str | None = None) -> None:
            super().__init__(app)
            self.api_key = api_key

        async def dispatch(self, request: Request, call_next) -> Response:
            if self.api_key is None:
                return await call_next(request)
            if not request.url.path.startswith("/api/"):
                return await call_next(request)
            auth = request.headers.get("Authorization", "")
            if auth == f"Bearer {self.api_key}":
                return await call_next(request)
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

    # ... route handlers ...

    middleware = [
        Middleware(
            CORSMiddleware,
            allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
            allow_methods=["*"],
            allow_headers=["*"],
        ),
        Middleware(BearerAuthMiddleware, api_key=API_KEY),
    ]

    return Starlette(routes=routes, middleware=middleware)
```

### api_import() Path Guard (SEC-02)

```python
# In the api_import() handler — BEFORE the p.exists() check
async def api_import(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        return _json_err(f"Invalid JSON body: {e}")

    file_path = body.get("file_path", "").strip()
    if not file_path:
        return _json_err("'file_path' is required")

    p = Path(file_path).expanduser().resolve()

    # SEC-02: Reject paths outside configured import roots
    if not any(p == root or root in p.parents for root in IMPORT_ROOTS):
        return _json_err(f"Path not in allowed import roots: {p}")

    if not p.exists():
        return _json_err(f"Path not found: {p}")

    # ... rest of handler unchanged ...
```

### config.py Additions (SEC-02 and SEC-03)

```python
# In remind_me_mcp/config.py — Security section

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

API_KEY: str | None = os.environ.get("REMIND_ME_API_KEY") or None
"""Bearer token for /api/* routes. None when unset — auth disabled (backward-compatible)."""

_import_roots_env: str | None = os.environ.get("REMIND_ME_IMPORT_ROOTS")
IMPORT_ROOTS: list[Path] = (
    [Path(r.strip()).expanduser().resolve() for r in _import_roots_env.split(":") if r.strip()]
    if _import_roots_env
    else [Path.home()]
)
"""Allowed filesystem roots for import operations. Colon-separated paths.
Default: user home directory (~)."""
```

### Test Patterns for Security Requirements

```python
# SEC-01: CORS restriction tests
def test_cors_allows_localhost(client):
    r = client.get("/api/stats", headers={"Origin": "http://localhost"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost"

def test_cors_allows_localhost_with_port(client):
    r = client.get("/api/stats", headers={"Origin": "http://localhost:5199"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5199"

def test_cors_denies_external_origin_preflight(client):
    r = client.options("/api/stats", headers={
        "Origin": "http://evil.com",
        "Access-Control-Request-Method": "GET",
    })
    assert r.status_code == 400

def test_cors_no_acao_for_external_simple_request(client):
    r = client.get("/api/stats", headers={"Origin": "http://evil.com"})
    assert "access-control-allow-origin" not in r.headers

# SEC-02: Import path guard tests
def test_import_rejects_path_outside_home(client):
    r = client.post("/api/import", json={"file_path": "/etc/passwd"})
    assert r.status_code == 400
    assert "not in allowed" in r.json()["error"].lower()

def test_import_rejects_traversal_attempt(client):
    r = client.post("/api/import", json={"file_path": str(Path.home() / ".." / "etc" / "passwd")})
    assert r.status_code == 400

# SEC-03: Bearer auth tests
def test_api_requires_auth_when_key_set(client_with_auth):
    r = client_with_auth.get("/api/stats")
    assert r.status_code == 401

def test_api_accepts_valid_token(client_with_auth):
    r = client_with_auth.get("/api/stats", headers={"Authorization": "Bearer test-key"})
    assert r.status_code == 200

def test_dashboard_accessible_without_auth(client_with_auth):
    r = client_with_auth.get("/")
    assert r.status_code == 200  # / is not /api/*, no auth required

def test_api_open_when_no_key_configured(client):
    r = client.get("/api/stats")  # client fixture has no API_KEY
    assert r.status_code == 200
```

The `client_with_auth` fixture must monkeypatch `API_KEY` in `remind_me_mcp.api` and `remind_me_mcp.config`, rebuild the app, and return a `TestClient` with the test key active.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `allow_origins=["*"]` (current) | `allow_origin_regex=r"http://(localhost\|127\.0\.0\.1)(:\d+)?"` | Phase 6 | Rejects cross-origin fetch from non-localhost; dashboard still works |
| No path restriction on imports (current) | Guard against `IMPORT_ROOTS` before handler | Phase 6 | Prevents filesystem traversal exploitation |
| No auth (current, backward-compatible default) | Optional bearer token via `REMIND_ME_API_KEY` | Phase 6 | Users exposing dashboard beyond localhost can secure it |

**Deprecated/outdated:**
- `allow_origins=["*"]` in `_build_api_app()`: will be replaced in Phase 6. The docstring currently says "Includes CORS middleware allowing all origins" — this must be updated too.

## Open Questions

1. **Port range in CORS regex**
   - What we know: The regex `(:\d+)?` matches any port number, which is broader than strictly necessary.
   - What's unclear: Whether the requirement means only port 5199 (the dashboard port) or any port. The success criteria says "http://localhost and http://127.0.0.1 origins" without mentioning ports.
   - Recommendation: Use `(:\d+)?` (any port). The dashboard port is configurable (`REMIND_ME_MCP_UI_PORT`). Restricting to a specific port would break custom port configurations. The security value of CORS is rejecting non-localhost origins entirely; port restriction within localhost adds no meaningful security.

2. **`WWW-Authenticate` header on 401 responses**
   - What we know: RFC 7235 recommends including a `WWW-Authenticate` header on 401 responses indicating the auth scheme.
   - What's unclear: Whether to include `WWW-Authenticate: Bearer realm="remind-me-mcp"` on 401 responses.
   - Recommendation: Include it for correctness (`JSONResponse(..., headers={"WWW-Authenticate": 'Bearer realm="remind-me-mcp"'})`), but it is not required by the success criteria. Keep it simple — omit if it adds noise to the implementation.

3. **Secrets comparison timing attack**
   - What we know: Direct string comparison `auth == f"Bearer {self.api_key}"` is vulnerable to timing side-channel attacks in theory.
   - What's unclear: Whether `hmac.compare_digest()` is warranted for a personal localhost tool.
   - Recommendation: Use `hmac.compare_digest(auth, f"Bearer {self.api_key}")` (stdlib, no deps) for correctness. The REQUIREMENTS.md "Out of Scope" section notes this is a personal tool with no multi-tenant scenario, but the fix is trivial and free.

## Sources

### Primary (HIGH confidence)
- Starlette 0.52.1 source code (`starlette/middleware/cors.py`, `starlette/middleware/base.py`) — inspected directly via `inspect.getsource()` in the project virtualenv
- Live Starlette 0.52.1 `TestClient` verification — all code patterns executed and output verified above
- Python 3.11 stdlib `pathlib.Path` — path resolution and traversal prevention behavior

### Secondary (MEDIUM confidence)
- RFC 6454 (browser origin model) — informing localhost vs 127.0.0.1 as distinct origins
- RFC 7235 (HTTP auth framework) — informing 401 vs 403 status code choice

### Tertiary (LOW confidence)
- None

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — verified against installed Starlette 0.52.1 source and live tests
- Architecture: HIGH — all patterns executed and validated with TestClient before documenting
- Pitfalls: HIGH — identified from live testing (e.g., port-in-origin, CORS-before-auth ordering)

**Research date:** 2026-02-24
**Valid until:** 2026-09-24 (stable stdlib + mature Starlette middleware API; unlikely to change)
