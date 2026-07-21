"""
remind_me_mcp.__main__ — CLI argument parsing and mode dispatch.

Supports multiple execution modes:
  - MCP stdio mode (default): runs the FastMCP server over stdin/stdout
  - UI server mode (--serve-ui): starts the Starlette dashboard HTTP server
  - Remote connector mode (--serve-remote): MCP over Streamable HTTP behind a
    secret URL path, for claude.ai custom connectors via a tunnel (FT-05)
  - Status mode (--status): checks if the dashboard is running and exits
  - Version mode (--version): prints the installed version and exits
  - Check-update mode (--check-update): checks for updates and exits
  - Update mode (--update): pulls latest changes and reinstalls

Usage:
  python -m remind_me_mcp [--serve-ui] [--ui-port PORT] [--ui-host HOST]
                           [--serve-remote] [--remote-port PORT] [--remote-host HOST]
                           [--status] [--version] [--check-update] [--update]
"""

from __future__ import annotations

import argparse
import atexit
import logging
import signal
import sys
from typing import TYPE_CHECKING

import remind_me_mcp.tools  # noqa: F401 — ensure tools are registered before mcp.run()
from remind_me_mcp.api import _build_api_app
from remind_me_mcp.config import (
    MCP_HTTP_HOST,
    MCP_HTTP_PORT,
    REMOTE_MCP,
    REMOTE_MCP_HOST,
    REMOTE_MCP_PORT,
    SERVE_MCP,
    SERVE_UI,
    UI_PORT,
)
from remind_me_mcp.pid import (
    _check_ui_server_health,
    _read_pid_file,
    _remove_pid_file,
    _write_pid_file,
    get_server_status,
)
from remind_me_mcp.server import mcp

if TYPE_CHECKING:
    from starlette.applications import Starlette

log = logging.getLogger("remind_me_mcp.__main__")

__all__ = ["main"]


def _build_combined_app() -> tuple[Starlette, str]:
    """Build the combined Starlette app: dashboard API at / and MCP HTTP at /mcp.

    SE-03 fixes:
      - The combined app delegates its lifespan to the MCP app's lifespan
        (which starts the StreamableHTTP session manager). Starlette does not
        propagate lifespans to mounted/lifted sub-app routes, so without this
        every /mcp request fails with an uninitialised task group and the app
        lifespan (DB, sync, peer server) never runs.
      - MCP_HTTP_SECRET auth is applied as ASGI middleware on the combined app
        (the shared BearerAuthMiddleware from api.py, SE-05, gating only
        /mcp paths) instead of re-instantiating Starlette around the MCP
        routes, which discarded the lifespan again.

    Auth is always on (config.resolve_mcp_http_secret() auto-generates and
    persists a secret when REMIND_ME_MCP_HTTP_SECRET is unset, mirroring the
    remote connector's resolve_connector_token — there is no way to run
    combined mode with an open /mcp, since widening --ui-host to serve the
    dashboard remotely would otherwise silently expose the full MCP tool-call
    surface unauthenticated on the same host:port).

    Returns:
        (app, secret) — the secret is returned so the caller can log it.
    """
    from contextlib import asynccontextmanager

    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.routing import Mount

    from remind_me_mcp import config as cfg
    from remind_me_mcp.api import BearerAuthMiddleware

    secret = cfg.resolve_mcp_http_secret()

    dashboard_app = _build_api_app()
    # The MCP app serves its endpoint at settings.streamable_http_path
    # (default "/mcp"). Its Route objects are lifted directly into the
    # combined app — nesting it under Mount("/mcp") would serve /mcp/mcp.
    mcp_http_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def _combined_lifespan(app):
        """Run the MCP app's lifespan for the lifetime of the combined app."""
        async with mcp_http_app.router.lifespan_context(mcp_http_app):
            yield

    # Gate the MCP endpoint with bearer auth (shared middleware, SE-05);
    # dashboard paths keep their own auth from _build_api_app.
    middleware = [Middleware(BearerAuthMiddleware, secret=secret, protect_prefix="/mcp")]

    app = Starlette(
        routes=[
            *mcp_http_app.routes,
            Mount("/", app=dashboard_app),
        ],
        middleware=middleware,
        lifespan=_combined_lifespan,
    )
    return app, secret


def _run_combined(args) -> None:
    """Run dashboard API and MCP HTTP transport on the same Uvicorn instance."""
    import uvicorn

    from remind_me_mcp import config as cfg
    from remind_me_mcp.remote import redact_token

    combined, secret = _build_combined_app()

    log.info(
        "Combined server starting — dashboard: http://%s:%d  MCP HTTP: "
        "http://%s:%d/mcp (bearer secret required: %s, full secret at %s)",
        args.ui_host,
        args.ui_port,
        args.ui_host,
        args.ui_port,
        redact_token(secret),
        cfg.MCP_HTTP_SECRET_FILE,
    )
    uvicorn.run(combined, host=args.ui_host, port=args.ui_port)


def _run_remote(args) -> None:
    """Run the remote MCP connector (FT-05/FT-07) on a dedicated Uvicorn instance.

    Serves the MCP server over Streamable HTTP at /mcp. With
    REMIND_ME_REMOTE_ISSUER set, a single-user OAuth 2.1 authorization
    server (FT-07) is mounted alongside — claude.ai adds the connector with
    just the /mcp URL and approves it on the consent page using the owner
    token. Without an issuer, the FT-05 secret-path mode applies:
    /mcp/<connector token> (URL-as-credential) or /mcp with
    'Authorization: Bearer <token>'. The token is generated and persisted on
    first use (config.resolve_connector_token); the startup log shows it
    redacted — the full URL path is logged once at generation time and
    stored in the token file.
    """
    import uvicorn

    from remind_me_mcp import config as cfg
    from remind_me_mcp.remote import build_remote_app, redact_token

    token = cfg.resolve_connector_token()
    issuer = cfg.REMOTE_MCP_ISSUER
    app = build_remote_app(token, issuer=issuer)

    if issuer:
        log.info(
            "Remote MCP connector starting with OAuth — bind: http://%s:%d, "
            "public issuer: %s. Add %s/mcp as a claude.ai custom connector; "
            "approve clients on the consent page with the owner token at %s. "
            "Revoke clients via the remind_me_revoke_clients tool. The legacy "
            "secret-path URL (/mcp/%s) keeps working as a fallback.",
            args.remote_host,
            args.remote_port,
            issuer,
            issuer.rstrip("/"),
            cfg.MEMORY_DIR / "connector_token",
            redact_token(token),
        )
    else:
        log.info(
            "Remote MCP connector starting — endpoint: http://%s:%d/mcp/%s "
            "(token redacted; full token at %s). Header-capable clients may "
            "instead use http://%s:%d/mcp with 'Authorization: Bearer <token>'. "
            "Expose via an HTTPS tunnel (e.g. `tailscale funnel %d`) and add the "
            "public /mcp/<token> URL as a claude.ai custom connector. Set "
            "REMIND_ME_REMOTE_ISSUER=https://<public-host> to enable OAuth (FT-07).",
            args.remote_host,
            args.remote_port,
            redact_token(token),
            cfg.MEMORY_DIR / "connector_token",
            args.remote_host,
            args.remote_port,
            args.remote_port,
        )
    uvicorn.run(app, host=args.remote_host, port=args.remote_port)


def main() -> None:
    """Parse CLI arguments and dispatch to the appropriate execution mode."""
    # Root logging setup belongs in the entrypoint, not at package import time
    # (HY-06): importing remind_me_mcp must never reconfigure a host
    # application's logging. stderr only — stdout is the MCP stdio transport.
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="%(levelname)s | %(message)s"
    )

    parser = argparse.ArgumentParser(description="Remind Me MCP Server")
    parser.add_argument(
        "--serve-ui",
        action="store_true",
        default=SERVE_UI,
        help="Start the HTTP dashboard UI server",
    )
    parser.add_argument(
        "--ui-port",
        type=int,
        default=UI_PORT,
        help="Port for the dashboard UI (default: 5199)",
    )
    parser.add_argument(
        "--ui-host",
        type=str,
        default="127.0.0.1",
        help="Host to bind the UI server (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--serve-mcp",
        action="store_true",
        default=SERVE_MCP,
        help="Run MCP server over Streamable HTTP transport (port 8767 by default)",
    )
    parser.add_argument("--mcp-port", type=int, default=MCP_HTTP_PORT, help="MCP HTTP port")
    parser.add_argument("--mcp-host", default=MCP_HTTP_HOST, help="MCP HTTP host")
    parser.add_argument(
        "--serve-remote",
        action="store_true",
        default=REMOTE_MCP,
        help=(
            "Run the remote MCP connector (FT-05): Streamable HTTP behind a "
            "secret URL path for claude.ai custom connectors (port 8768 by default)"
        ),
    )
    parser.add_argument(
        "--remote-port", type=int, default=REMOTE_MCP_PORT, help="Remote MCP connector port"
    )
    parser.add_argument(
        "--remote-host", default=REMOTE_MCP_HOST, help="Remote MCP connector host"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Check if the UI server is running and exit",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print the installed version and exit",
    )
    parser.add_argument(
        "--check-update",
        action="store_true",
        help="Check for available updates and exit",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Pull latest changes from origin and reinstall",
    )
    args = parser.parse_args()

    # -- Version mode --
    if args.version:
        from remind_me_mcp import __version__

        print(f"remind-me-mcp {__version__}")
        sys.exit(0)

    # -- Check-update mode --
    if args.check_update:
        from remind_me_mcp.updater import check_for_update

        status = check_for_update()
        if status.error:
            print(f"Error: {status.error}")
            sys.exit(1)
        print(f"Installed: {status.installed_version} (commit {status.local_commit})")
        print(f"Remote:    commit {status.remote_commit}")
        if status.update_available:
            print(f"\nUpdate available — {status.commits_behind} commit(s) behind")
            if status.commit_messages:
                print("\nRecent changes:")
                for msg in status.commit_messages[:10]:
                    print(f"  {msg}")
            print("\nRun 'remind-me-mcp --update' to apply.")
        else:
            print("\nUp to date.")
        sys.exit(0)

    # -- Update mode --
    if args.update:
        from remind_me_mcp.updater import check_for_update, perform_update

        print("Checking for updates...")
        status = check_for_update()
        if status.error:
            print(f"Error: {status.error}")
            sys.exit(1)
        if not status.update_available:
            print(f"Already up to date at {status.installed_version} (commit {status.local_commit}).")
            sys.exit(0)

        print(f"Update available: {status.commits_behind} commit(s) behind")
        print("Pulling and reinstalling...")
        result = perform_update()
        if not result.success:
            print(f"Update failed: {result.error}")
            sys.exit(1)
        print(f"Updated: {result.previous_version} -> {result.new_version}")
        print(f"Commits: {result.previous_commit} -> {result.new_commit}")
        if result.restart_required:
            print("\nRestart the MCP server for changes to take effect.")
        sys.exit(0)

    # -- Status check mode --
    if args.status:
        server_status = get_server_status()
        if server_status["ui_server"] == "running":
            print(f"\u2713 Dashboard running at {server_status['ui_url']} (PID {server_status['ui_pid']})")
        else:
            print("\u2717 Dashboard not running")
        print(f"  Database: {server_status['db_path']} ({'exists' if server_status['db_exists'] else 'missing'})")
        sys.exit(0)

    # -- Remote MCP connector mode (FT-05) --
    # Standalone by design: the remote app owns the global FastMCP session
    # manager (created once per process), so it cannot share a process with
    # the local MCP HTTP / combined modes.
    if args.serve_remote:
        if args.serve_ui or args.serve_mcp:
            log.warning(
                "--serve-remote is standalone; ignoring --serve-ui/--serve-mcp "
                "(run those in a separate process)."
            )
        _run_remote(args)
        return

    # -- MCP HTTP + UI combined mode --
    if args.serve_mcp and args.serve_ui:
        _run_combined(args)
        return

    # -- MCP HTTP standalone mode --
    if args.serve_mcp:
        log.info("Starting MCP HTTP transport on %s:%d", args.mcp_host, args.mcp_port)
        # SE-03: FastMCP.run() accepts no host/port kwargs (TypeError on the
        # installed SDK); the bind address comes from mcp.settings instead.
        mcp.settings.host = args.mcp_host
        mcp.settings.port = args.mcp_port
        mcp.run(transport="streamable-http")
        return

    # -- UI server mode --
    if args.serve_ui:
        import uvicorn

        # Check if already running
        existing = _read_pid_file()
        if existing and _check_ui_server_health(existing.get("url", "")):
            log.warning(
                "Dashboard is already running at %s (PID %d). "
                "Stop it first or use a different port with --ui-port.",
                existing["url"],
                existing["pid"],
            )
            sys.exit(1)

        # Write PID file and register cleanup
        _write_pid_file(args.ui_host, args.ui_port)
        atexit.register(_remove_pid_file)

        def _signal_handler(signum, frame):
            """Handle SIGTERM and SIGINT by cleaning up PID file and exiting."""
            _remove_pid_file()
            sys.exit(0)

        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)

        app = _build_api_app()
        log.info("Starting Remind Me dashboard at http://%s:%d", args.ui_host, args.ui_port)
        uvicorn.run(app, host=args.ui_host, port=args.ui_port, log_level="info")

    # -- MCP stdio mode --
    else:
        # Check if UI server is running and log it
        existing = _read_pid_file()
        if existing and _check_ui_server_health(existing.get("url", "")):
            log.info("Dashboard UI is running at %s", existing["url"])
        mcp.run()


if __name__ == "__main__":
    main()
