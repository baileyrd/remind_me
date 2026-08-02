"""Remind Me MCP Server — persistent, searchable memory across Claude interfaces.

Exposes the FastMCP server instance for use as an entry point and imports the
tools module to trigger @mcp.tool() registration before mcp.run() is called.
"""

# Import tools module to trigger @mcp.tool() registration side effects.
# Without this import, the mcp instance has no tools registered.
import remind_me_mcp.tools  # noqa: F401
from remind_me_mcp.server import mcp  # noqa: F401 — re-export for entry point

# Resolved in version.py, not here, so the HTTP surfaces that report it can
# import it without cycling back through this module (see version.py).
# Re-exported so `from remind_me_mcp import __version__` keeps working.
from remind_me_mcp.version import __version__  # noqa: F401

__all__ = ["mcp", "__version__"]
