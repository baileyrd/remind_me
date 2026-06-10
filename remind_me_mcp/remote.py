"""
remind_me_mcp.remote — remote MCP connector over Streamable HTTP (FT-05).

Exposes the FastMCP server as a remote connector that claude.ai (Settings →
Connectors → Add custom connector) can attach to through any HTTPS tunnel
(e.g. Tailscale Funnel). claude.ai authenticates custom connectors via OAuth
or connects "unauthenticated" — its web UI cannot send custom headers — and a
full OAuth authorization server is out of scope for a personal store. The
pragmatic, widely-used pattern implemented here is a SECRET-PATH URL:

  - The MCP Streamable HTTP endpoint is reachable at ``/mcp/<token>`` where
    ``<token>`` is a high-entropy secret generated and persisted exactly like
    the dashboard API key (SE-01; see config.resolve_connector_token). The
    URL itself is the credential, so claude.ai can connect without headers.
  - Clients that CAN send headers (Claude Code, scripts) may instead hit
    ``/mcp`` directly with ``Authorization: Bearer <token>``.
  - Everything else — wrong token, bare ``/mcp/...`` probes, unrelated paths
    — is rejected (404/401) before reaching the MCP app. Token comparisons
    use ``hmac.compare_digest`` (SE-05 convention).

The app delegates its lifespan to the MCP HTTP sub-app (SE-03 pattern), so
the DB / sync / watcher / embedder lifecycle is identical to stdio mode.

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
    """Pure-ASGI secret-path gate for the remote MCP connector (FT-05).

    Admits a request when either:
      - its path is ``{mcp_path}/<token>`` (optionally with a trailing
        slash) — the path is rewritten to ``{mcp_path}`` and forwarded, or
      - its path is exactly ``{mcp_path}`` and it carries
        ``Authorization: Bearer <token>``.

    ``allow_paths`` (the unauthenticated ``/health`` probe, SE-04) pass
    through untouched. Every other HTTP request is answered 404 (path-based
    probes never learn whether the endpoint exists) or 401 (bare
    ``{mcp_path}`` without a valid bearer header). Non-HTTP scopes
    (lifespan, websocket) pass through so the app lifespan still runs.
    """

    def __init__(
        self,
        app: _ASGIApp,
        token: str,
        mcp_path: str = "/mcp",
        allow_paths: tuple[str, ...] = ("/health",),
    ) -> None:
        self.app = app
        self.token = token
        self.mcp_path = mcp_path
        self.allow_paths = allow_paths

    async def __call__(self, scope: _Scope, receive: _Receive, send: _Send) -> None:
        """Gate HTTP requests on the secret path or bearer token."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        if path in self.allow_paths:
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
                await self.app(rewritten, receive, send)
                return
            await _send_json(send, 404, {"error": "Not found"})
            return

        if path == self.mcp_path:
            auth = _header(scope, b"authorization")
            expected = f"Bearer {self.token}"
            if hmac.compare_digest(auth.encode("utf-8"), expected.encode("utf-8")):
                await self.app(scope, receive, send)
                return
            await _send_json(send, 401, {"error": "Unauthorized"})
            return

        await _send_json(send, 404, {"error": "Not found"})


def build_remote_app(token: str) -> Starlette:
    """Build the remote-connector Starlette app (FT-05).

    Lifts the MCP Streamable HTTP routes into a new app gated by
    :class:`SecretPathMiddleware`, plus an unauthenticated ``/health`` route
    (SE-04 parity — lets the tunnel / pid checks probe liveness). The app's
    lifespan delegates to the MCP sub-app's lifespan (SE-03), which runs both
    the StreamableHTTP session manager and the application lifespan
    (DB open/close, sync, watcher).

    The SDK's DNS-rebinding protection is disabled for this app: its Host
    allowlist defaults to localhost only, but behind a tunnel the public
    hostname is not knowable in advance — and the secret path, not the Host
    header, is the credential here.

    Args:
        token: The connector token (from config.resolve_connector_token()).

    Returns:
        A configured Starlette application serving MCP at /mcp/<token>.
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

    async def health(request: Any) -> JSONResponse:
        """Unauthenticated liveness probe (SE-04) — reveals no data."""
        return JSONResponse({"status": "ok"})

    @asynccontextmanager
    async def _remote_lifespan(app: Starlette):
        """Run the MCP app's lifespan for the lifetime of the remote app."""
        async with mcp_http_app.router.lifespan_context(mcp_http_app):
            yield

    return Starlette(
        routes=[
            Route("/health", health),
            *mcp_http_app.routes,
        ],
        middleware=[
            Middleware(
                SecretPathMiddleware,
                token=token,
                mcp_path=str(mcp.settings.streamable_http_path),
            )
        ],
        lifespan=_remote_lifespan,
    )


def get_remote_status() -> dict[str, Any]:
    """Report the remote-MCP connector configuration (FT-05).

    Reads config attributes at call time so tests can monkeypatch them.
    The token itself is never included — only whether one exists and where
    it is stored.

    Returns:
        Dict with keys: enabled, host, port, token_file, token_configured.
    """
    from remind_me_mcp import config as cfg

    token_file = cfg.MEMORY_DIR / "connector_token"
    return {
        "enabled": cfg.REMOTE_MCP,
        "host": cfg.REMOTE_MCP_HOST,
        "port": cfg.REMOTE_MCP_PORT,
        "token_file": str(token_file),
        "token_configured": bool(cfg.REMOTE_MCP_TOKEN) or token_file.is_file(),
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
