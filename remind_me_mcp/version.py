"""Single source of the installed package version.

Lives in its own module rather than directly in ``remind_me_mcp/__init__.py``
so the runtime surfaces that report the version — ``api.py``'s ``/health``,
``peer_server.py``'s ``/health``, ``sync.py``'s status/reconcile — can import
it at module scope. Importing the package root instead would be circular:
``__init__`` imports ``server`` and ``tools``, which import ``api``/``sync``,
so those modules would read ``remind_me_mcp.__version__`` off a
half-initialized module and raise ``ImportError`` (this is exactly why
``updater.py`` does its ``from remind_me_mcp import __version__`` lazily,
inside functions). Importing this leaf module is safe from anywhere.

``__init__`` re-exports the name, so ``from remind_me_mcp import __version__``
keeps working unchanged for existing callers.
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("remind-me-mcp")
except PackageNotFoundError:
    # A source checkout that was never `pip install -e .`'d has no dist
    # metadata. Reported verbatim rather than raising: a version string is
    # diagnostic output, never a reason for the server to fail to start.
    __version__ = "0.0.0-dev"

__all__ = ["__version__"]
