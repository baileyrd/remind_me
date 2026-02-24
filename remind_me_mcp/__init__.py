"""Remind Me MCP Server — persistent, searchable memory across Claude interfaces.

Exposes the FastMCP server instance for use as an entry point and imports the
tools module to trigger @mcp.tool() registration before mcp.run() is called.
"""

from remind_me_mcp.server import mcp  # noqa: F401 — re-export for entry point

# Import tools module to trigger @mcp.tool() registration side effects.
# Without this import, the mcp instance has no tools registered.
import remind_me_mcp.tools  # noqa: F401

__version__ = "0.1.0"

__all__ = ["mcp", "__version__"]
