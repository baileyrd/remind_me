"""
remind_me_mcp.server — FastMCP server instance and application lifespan.

Defines the global `mcp` FastMCP instance and the async lifespan context
manager that opens the database at startup and closes it on shutdown.

IMPORTANT: This module must NOT import from tools.py. Instead, tools.py
imports `mcp` from this module and registers handlers onto it, avoiding
circular imports.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import FastMCP

from remind_me_mcp.config import DB_PATH, SYNC_ENABLED
from remind_me_mcp.db import _close_db, _get_db
from remind_me_mcp.telemetry import maybe_span

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mcp.types import ContentBlock

log = logging.getLogger("remind_me_mcp.server")


class _TracedFastMCP(FastMCP):
    """FastMCP with an OTEL span wrapped around every tool call (Phase 7a).

    ``call_tool`` is the single choke point every MCP tool invocation passes
    through, but wrapping it AFTER construction (``mcp.call_tool = ...``)
    doesn't work: FastMCP's own ``__init__`` registers ``self.call_tool`` as
    the protocol-level handler while it runs, capturing whichever method the
    instance's actual class resolves at that moment. Subclassing overrides
    the method before that registration happens (Python's normal MRO
    lookup), so this is the only reliable place to intercept every call
    without touching each of the ~40 individually-decorated tool functions.
    ``maybe_span`` is a no-op unless REMIND_ME_OTEL_ENABLED is set, so this
    has no effect (and negligible overhead) by default.
    """

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> Sequence[ContentBlock] | dict[str, Any]:
        with maybe_span(f"tool.{name}"):
            return await super().call_tool(name, arguments)


# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def app_lifespan(app: FastMCP):
    """Open the database at startup and close it on shutdown.

    Passed as the lifespan argument to the FastMCP constructor. On startup,
    opens the SQLite connection (triggering schema creation/migration),
    logs the database path, and kicks off a background update check.
    On shutdown (after yield), closes the connection.

    Args:
        app: The FastMCP application instance (unused, provided by the framework).

    Yields:
        Dict with key 'db' containing the open sqlite3.Connection.
    """
    db = _get_db()
    log.info("Remind Me MCP started — db at %s", DB_PATH)

    from remind_me_mcp.updater import start_background_check
    start_background_check()

    # Imported unconditionally (not just under SYNC_ENABLED) so shutdown can
    # always call them — both are no-ops when never started.
    from remind_me_mcp.peer_server import stop_peer_server
    from remind_me_mcp.sync import stop_sync_thread

    if SYNC_ENABLED:
        from remind_me_mcp.peer_server import start_peer_server
        from remind_me_mcp.sync import start_sync_thread
        start_peer_server()
        start_sync_thread()
        log.info("Sync started")

    # FT-03: folder watcher — start_watcher() is a no-op unless
    # REMIND_ME_WATCH_DIRS is configured with at least one valid directory.
    from remind_me_mcp.watcher import start_watcher, stop_watcher
    start_watcher()

    # FT-09/Phase 5a: push/webhook ingestion — start_webhook_server() is a
    # no-op unless REMIND_ME_WEBHOOK_SECRET is configured.
    from remind_me_mcp.webhook_server import start_webhook_server, stop_webhook_server
    start_webhook_server()

    # FT-08: LLM Wiki — seed the maintainer schema and reconcile the file-backed
    # index into the DB at startup so external edits (hand edits, git pull) are
    # picked up. Best-effort: a wiki problem must never block server startup.
    try:
        from remind_me_mcp import wiki
        wiki.ensure_schema_file()
        stats = wiki.reconcile()
        log.info("Wiki ready at %s — %d page(s) indexed", wiki.wiki_dir(), stats["pages"])
    except Exception:  # noqa: BLE001 — never let the wiki layer break startup
        log.warning("Wiki startup reconcile failed", exc_info=True)

    try:
        yield {"db": db}
    finally:
        # FT-03/FT-09/SY-*/SE-07: stop the watcher, webhook, peer server, and
        # sync threads *before* closing the database connections so an
        # in-flight scan, request, or sync cycle never writes to a closed
        # handle.
        stop_watcher()
        stop_webhook_server()
        stop_peer_server()
        stop_sync_thread()
        # SE-07: always close every tracked connection, even when the body
        # raised — otherwise file descriptors leak and the WAL is never
        # checkpointed.
        _close_db()

# ---------------------------------------------------------------------------
# MCP server instance
# ---------------------------------------------------------------------------

mcp = _TracedFastMCP("remind_me_mcp", lifespan=app_lifespan)

# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "mcp",
    "app_lifespan",
]
