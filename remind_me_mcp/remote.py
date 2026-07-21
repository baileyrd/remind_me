"""
remind_me_mcp.remote — remote MCP connector over Streamable HTTP (FT-05/FT-07).

Exposes the FastMCP server as a remote connector that claude.ai (Settings →
Connectors → Add custom connector) can attach to through any HTTPS tunnel
(e.g. Tailscale Funnel). Two auth modes share one app:

  - **OAuth 2.1 (FT-07, when REMIND_ME_REMOTE_ISSUER is set).** The MCP
    SDK's auth framework serves RFC 8414 AS metadata, RFC 9728 protected-
    resource metadata, RFC 7591 dynamic client registration, the PKCE (S256)
    authorization-code flow with refresh, and RFC 7009 revocation — backed
    by :class:`remind_me_mcp.oauth.SingleUserOAuthProvider` (consent = the
    owner pastes the connector token on ``/consent``). ``/mcp`` then accepts
    any bearer the provider verifies: an issued OAuth access token or the
    legacy connector token. claude.ai discovers OAuth via the well-known
    metadata (or the 401's WWW-Authenticate hint) and prefers it. The issuer
    must be configured explicitly — it is never derived from the Host
    header, which is attacker-influenced while DNS-rebinding protection is
    disabled.
  - **Secret-path fallback (FT-05, issuer unset).** The MCP endpoint is
    reachable at ``/mcp/<token>`` where ``<token>`` is a high-entropy secret
    generated and persisted exactly like the dashboard API key (SE-01; see
    config.resolve_connector_token) — the URL itself is the credential.
    Header-capable clients (Claude Code, scripts) may instead hit ``/mcp``
    with ``Authorization: Bearer <token>``. The secret-path URL keeps
    working in OAuth mode too (it is rewritten into a bearer request).

Everything else — wrong token, bare ``/mcp/...`` probes, unrelated paths —
is rejected (404/401) before reaching the MCP app. Token comparisons use
``hmac.compare_digest`` (SE-05 convention).

The app delegates its lifespan to the MCP HTTP sub-app (SE-03 pattern), so
the DB / sync / watcher / embedder lifecycle is identical to stdio mode.

TLS is the tunnel's job, not this app's (SEC-09). This app always speaks
plain HTTP -- it never terminates TLS itself, by design, to stay a thin
zero-ops layer that any HTTPS tunnel can front. That means every credential
described above (the secret-path/bearer token, and OAuth access/refresh
tokens) is only as protected as whatever sits in front of the bind address.
The default bind (127.0.0.1, see config.REMOTE_MCP_HOST) exists precisely so
those credentials never cross a wire in cleartext to anything but the tunnel
process on the same host; widening the bind without an actual tunnel (or
your own TLS termination) in front of it exposes them in plaintext to
whatever can reach that address. __main__._run_remote logs a startup
warning when the bind isn't loopback, but can't enforce this -- there is no
reliable signal the app itself could use to tell "arrived through the
tunnel" from "arrived directly" (trusting a header for that would repeat
the exact attacker-influenced-header mistake DNS-rebinding protection above
is designed to avoid).

Starlette imports are kept lazy (inside build_remote_app) so importing this
module in MCP stdio mode never loads the web framework.
"""

from __future__ import annotations

import hmac
import logging
from typing import TYPE_CHECKING, Any

from remind_me_mcp.api import _header, _send_json

if TYPE_CHECKING:
    from starlette.applications import Starlette

    from remind_me_mcp.api import _ASGIApp, _Receive, _Scope, _Send

log = logging.getLogger("remind_me_mcp.remote")


def redact_token(token: str) -> str:
    """Return a log-safe preview of the connector token (first 4 chars)."""
    return f"{token[:4]}…" if len(token) > 4 else "…"


class SecretPathMiddleware:
    """Pure-ASGI secret-path gate for the remote MCP connector (FT-05/FT-07).

    Admits a request when either:
      - its path is ``{mcp_path}/<token>`` (optionally with a trailing
        slash) — the path is rewritten to ``{mcp_path}`` and forwarded (in
        OAuth mode the matched token is also injected as an
        ``Authorization: Bearer`` header so the SDK bearer middleware
        authenticates it), or
      - its path is exactly ``{mcp_path}``. In legacy mode it must carry
        ``Authorization: Bearer <token>``; in OAuth mode it is forwarded
        as-is — the SDK's RequireAuthMiddleware decides (401 with a
        WWW-Authenticate hint pointing at the resource metadata, which is
        how clients discover the authorization server).

    ``allow_paths`` (the unauthenticated ``/health`` probe, SE-04) and
    ``allow_prefixes`` (the ``/.well-known/`` metadata documents in OAuth
    mode) pass through untouched. Every other HTTP request is answered 404
    (path-based probes never learn whether the endpoint exists) or 401.
    Non-HTTP scopes (lifespan, websocket) pass through so the app lifespan
    still runs.
    """

    def __init__(
        self,
        app: _ASGIApp,
        token: str,
        mcp_path: str = "/mcp",
        allow_paths: tuple[str, ...] = ("/health",),
        allow_prefixes: tuple[str, ...] = (),
        oauth_mode: bool = False,
    ) -> None:
        self.app = app
        self.token = token
        self.mcp_path = mcp_path
        self.allow_paths = allow_paths
        self.allow_prefixes = allow_prefixes
        self.oauth_mode = oauth_mode

    async def __call__(self, scope: _Scope, receive: _Receive, send: _Send) -> None:
        """Gate HTTP requests on the secret path, bearer token, or OAuth routes."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        if path in self.allow_paths or any(path.startswith(p) for p in self.allow_prefixes):
            await self.app(scope, receive, send)
            return

        prefix = self.mcp_path + "/"
        if path.startswith(prefix):
            segment, _, rest = path[len(prefix):].partition("/")
            if segment and hmac.compare_digest(
                segment.encode("utf-8"), self.token.encode("utf-8")
            ):
                # Strip the token segment so the MCP route (served at
                # mcp_path exactly) matches; /mcp/<token>/ also maps to /mcp.
                new_path = self.mcp_path + (f"/{rest}" if rest else "")
                rewritten: dict[str, Any] = dict(scope)
                rewritten["path"] = new_path
                rewritten["raw_path"] = new_path.encode("utf-8")
                if self.oauth_mode:
                    # Re-express the secret path as a bearer credential so
                    # the SDK auth stack (BearerAuthBackend → provider)
                    # authenticates it like any other token.
                    headers = [
                        (k, v)
                        for k, v in scope.get("headers", [])
                        if k.lower() != b"authorization"
                    ]
                    headers.append((b"authorization", f"Bearer {self.token}".encode()))
                    rewritten["headers"] = headers
                await self.app(rewritten, receive, send)
                return
            await _send_json(send, 404, {"error": "Not found"})
            return

        if path == self.mcp_path:
            if self.oauth_mode:
                # OAuth mode: the SDK bearer middleware + RequireAuthMiddleware
                # own /mcp auth (accepting OAuth access tokens AND the legacy
                # connector token via the provider's verifier).
                await self.app(scope, receive, send)
                return
            auth = _header(scope, b"authorization")
            expected = f"Bearer {self.token}"
            if hmac.compare_digest(auth.encode("utf-8"), expected.encode("utf-8")):
                await self.app(scope, receive, send)
                return
            await _send_json(send, 401, {"error": "Unauthorized"})
            return

        await _send_json(send, 404, {"error": "Not found"})


def build_remote_app(token: str, issuer: str | None = None) -> Starlette:
    """Build the remote-connector Starlette app (FT-05/FT-07).

    Lifts the MCP Streamable HTTP routes into a new app gated by
    :class:`SecretPathMiddleware`, plus an unauthenticated ``/health`` route
    (SE-04 parity — lets the tunnel / pid checks probe liveness). The app's
    lifespan delegates to the MCP sub-app's lifespan (SE-03), which runs both
    the StreamableHTTP session manager and the application lifespan
    (DB open/close, sync, watcher).

    When *issuer* (the public HTTPS origin, REMIND_ME_REMOTE_ISSUER) is set,
    the single-user OAuth 2.1 authorization server (FT-07) is mounted
    alongside: the SDK's auth routes (/.well-known metadata, /authorize,
    /token, /register, /revoke), the owner-consent page (/consent), and the
    SDK bearer middleware in front of ``/mcp`` — which then accepts issued
    OAuth access tokens and the legacy connector token alike. Without an
    issuer the app is the plain FT-05 secret-path connector and a warning
    explains how to turn OAuth on.

    The SDK's DNS-rebinding protection is disabled for this app: its Host
    allowlist defaults to localhost only, but behind a tunnel the public
    hostname is not knowable in advance — the credential is the secret
    path / bearer token, and OAuth metadata uses the explicit issuer, never
    the Host header.

    Args:
        token: The connector token (from config.resolve_connector_token()).
            In OAuth mode it doubles as the owner credential on /consent.
        issuer: Public base URL for OAuth metadata (origin only, e.g.
            ``https://machine.tailnet.ts.net``), or None for legacy mode.

    Returns:
        A configured Starlette application serving MCP at /mcp.

    Raises:
        ValueError: If *issuer* is not an https origin (path/query present,
            or http on a non-localhost host).
    """
    from contextlib import asynccontextmanager

    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from remind_me_mcp.server import mcp

    # Must be set before the first streamable_http_app() call — the session
    # manager is created lazily once and caches its security settings.
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )
    # The MCP app serves its endpoint at settings.streamable_http_path
    # (default "/mcp"); its Route objects are lifted directly into this app
    # (same SE-03 layout as combined mode — no nested Mount).
    mcp_http_app = mcp.streamable_http_app()
    mcp_path = str(mcp.settings.streamable_http_path)

    async def health(request: Any) -> JSONResponse:
        """Unauthenticated liveness probe (SE-04) — reveals no data."""
        return JSONResponse({"status": "ok"})

    @asynccontextmanager
    async def _remote_lifespan(app: Starlette):
        """Run the MCP app's lifespan for the lifetime of the remote app."""
        async with mcp_http_app.router.lifespan_context(mcp_http_app):
            yield

    if not issuer:
        log.warning(
            "Remote connector OAuth is INACTIVE — set REMIND_ME_REMOTE_ISSUER "
            "to the public HTTPS origin (e.g. https://machine.tailnet.ts.net) "
            "to serve the single-user OAuth authorization server (FT-07). "
            "Falling back to the FT-05 secret-path/bearer mode."
        )
        return Starlette(
            routes=[
                Route("/health", health),
                *mcp_http_app.routes,
            ],
            middleware=[
                Middleware(SecretPathMiddleware, token=token, mcp_path=mcp_path)
            ],
            lifespan=_remote_lifespan,
        )

    # ----- OAuth mode (FT-07) -----
    from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
    from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
    from mcp.server.auth.provider import ProviderTokenVerifier
    from mcp.server.auth.routes import (
        build_resource_metadata_url,
        create_auth_routes,
        create_protected_resource_routes,
    )
    from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
    from mcp.server.fastmcp.server import StreamableHTTPASGIApp
    from pydantic import AnyHttpUrl
    from starlette.middleware.authentication import AuthenticationMiddleware

    from remind_me_mcp import config as cfg
    from remind_me_mcp.oauth import CONSENT_PATH, OAuthStateStore, SingleUserOAuthProvider

    issuer_url = AnyHttpUrl(issuer)  # raises ValueError on a malformed URL
    if issuer_url.path not in (None, "/") or issuer_url.query or issuer_url.fragment:
        raise ValueError(
            f"REMIND_ME_REMOTE_ISSUER must be an origin only (no path/query): {issuer!r}"
        )
    resource_url = AnyHttpUrl(str(issuer_url).rstrip("/") + mcp_path)

    store = OAuthStateStore(cfg.MEMORY_DIR / "oauth.json")
    provider = SingleUserOAuthProvider(owner_token=token, store=store)

    # SDK-provided endpoints: RFC 8414 metadata + /authorize + /token,
    # RFC 7591 /register, RFC 7009 /revoke — also validates the issuer
    # (https required outside localhost).
    auth_routes = create_auth_routes(
        provider,
        issuer_url=issuer_url,
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    )
    # RFC 9728 protected-resource metadata at the path-aware well-known URL,
    # plus an alias at the bare one for clients that probe without the
    # resource path.
    pr_routes = create_protected_resource_routes(
        resource_url=resource_url,
        authorization_servers=[issuer_url],
        resource_name="remind_me",
    )
    pr_alias = Route(
        "/.well-known/oauth-protected-resource",
        endpoint=pr_routes[0].endpoint,
        methods=["GET", "OPTIONS"],
    )

    # /mcp behind the SDK's RequireAuthMiddleware: the 401 carries a
    # WWW-Authenticate header pointing at the resource metadata, which is how
    # claude.ai discovers the authorization server.
    protected_mcp = Route(
        mcp_path,
        endpoint=RequireAuthMiddleware(
            StreamableHTTPASGIApp(mcp.session_manager),
            required_scopes=[],
            resource_metadata_url=build_resource_metadata_url(resource_url),
        ),
    )

    log.info(
        "Remote connector OAuth is ACTIVE (FT-07) — issuer %s, state file %s. "
        "claude.ai can connect to %s with per-client revocable tokens; the "
        "legacy secret-path URL keeps working.",
        issuer_url,
        store.path,
        resource_url,
    )
    return Starlette(
        routes=[
            Route("/health", health),
            *auth_routes,
            *pr_routes,
            pr_alias,
            Route(CONSENT_PATH, provider.handle_consent_page, methods=["GET"]),
            Route(CONSENT_PATH, provider.handle_consent_submit, methods=["POST"]),
            protected_mcp,
        ],
        middleware=[
            Middleware(
                SecretPathMiddleware,
                token=token,
                mcp_path=mcp_path,
                allow_paths=("/health", "/authorize", "/token", "/register", "/revoke", CONSENT_PATH),
                allow_prefixes=("/.well-known/",),
                oauth_mode=True,
            ),
            Middleware(
                AuthenticationMiddleware,
                backend=BearerAuthBackend(ProviderTokenVerifier(provider)),
            ),
            Middleware(AuthContextMiddleware),
        ],
        lifespan=_remote_lifespan,
    )


def get_remote_status() -> dict[str, Any]:
    """Report the remote-MCP connector configuration (FT-05/FT-07).

    Reads config attributes at call time so tests can monkeypatch them.
    The token itself is never included — only whether one exists and where
    it is stored.

    Returns:
        Dict with keys: enabled, host, port, token_file, token_configured,
        oauth_enabled, issuer, oauth_state_file, oauth_clients.
    """
    from remind_me_mcp import config as cfg
    from remind_me_mcp.oauth import OAuthStateStore

    token_file = cfg.MEMORY_DIR / "connector_token"
    oauth_state_file = cfg.MEMORY_DIR / "oauth.json"
    return {
        "enabled": cfg.REMOTE_MCP,
        "host": cfg.REMOTE_MCP_HOST,
        "port": cfg.REMOTE_MCP_PORT,
        "token_file": str(token_file),
        "token_configured": bool(cfg.REMOTE_MCP_TOKEN) or token_file.is_file(),
        "oauth_enabled": bool(cfg.REMOTE_MCP_ISSUER),
        "issuer": cfg.REMOTE_MCP_ISSUER,
        "oauth_state_file": str(oauth_state_file),
        "oauth_clients": len(OAuthStateStore(oauth_state_file).list_clients()),
    }


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "SecretPathMiddleware",
    "build_remote_app",
    "get_remote_status",
    "redact_token",
]
