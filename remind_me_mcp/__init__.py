"""Remind Me MCP Server — persistent, searchable memory across Claude interfaces.

Exposes the FastMCP server instance for use as an entry point and imports the
tools module to trigger @mcp.tool() registration before mcp.run() is called.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

# Import tools module to trigger @mcp.tool() registration side effects.
# Without this import, the mcp instance has no tools registered.
import remind_me_mcp.tools  # noqa: F401
from remind_me_mcp.server import mcp  # noqa: F401 — re-export for entry point

try:
    __version__ = _pkg_version("remind-me-mcp")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"

__all__ = ["mcp", "__version__"]
