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

from mcp.server.fastmcp import FastMCP

from remind_me_mcp.config import DB_PATH, SYNC_ENABLED
from remind_me_mcp.db import _close_db, _get_db

log = logging.getLogger("remind_me_mcp.server")


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

    try:
        yield {"db": db}
    finally:
        # FT-03/SE-07: stop the watcher thread *before* closing the database
        # connections so an in-flight scan never writes to a closed handle.
        stop_watcher()
        # SE-07: always close every tracked connection, even when the body
        # raised — otherwise file descriptors leak and the WAL is never
        # checkpointed. NOTE: sync/peer threads are daemon threads with no
        # stop mechanism yet (see SY-* workstream); once one exists it should
        # be signalled here *before* closing the connections.
        _close_db()

# ---------------------------------------------------------------------------
# MCP server instance
# ---------------------------------------------------------------------------

mcp = FastMCP("remind_me_mcp", lifespan=app_lifespan)

# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "mcp",
    "app_lifespan",
]
