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

from remind_me_mcp.config import DB_PATH
from remind_me_mcp.db import _close_db, _get_db

log = logging.getLogger("remind_me_mcp.server")

# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def app_lifespan(app):
    """Open the database at startup and close it on shutdown."""
    db = _get_db()
    log.info("Remind Me MCP started — db at %s", DB_PATH)
    yield {"db": db}
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
