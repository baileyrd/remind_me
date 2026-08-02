"""
Tests for remind_me_mcp.version — the single source of the package version.

The version itself is trivial; what's worth locking down is the import shape
around it. ``__init__`` imports ``tools`` and ``server``, which reach
``api``/``sync``/``peer_server``, so any of those doing a *module-level*
``from remind_me_mcp import __version__`` reads the name off a
half-initialized package and raises ImportError at import time — the server
fails to start, not a test. That is exactly why ``updater.py`` does its
import inside functions, and why ``version.py`` exists as a leaf module.
"""

from __future__ import annotations

import ast
from pathlib import Path

import remind_me_mcp
from remind_me_mcp.version import __version__

PACKAGE = Path(remind_me_mcp.__file__).resolve().parent


def test_package_reexports_the_same_object() -> None:
    """`from remind_me_mcp import __version__` keeps working after the move."""
    assert remind_me_mcp.__version__ is __version__


def test_version_is_a_non_empty_string() -> None:
    """Every surface reports this verbatim; an empty string reads as a bug."""
    assert isinstance(__version__, str)
    assert __version__.strip()


def test_uninstalled_checkout_falls_back_instead_of_raising() -> None:
    """A source checkout that was never `pip install -e .`'d has no metadata.

    That path is invisible in normal test runs (the package is installed), and
    it sits at import time of a module every HTTP surface imports — so if the
    fallback ever regressed, the symptom would be a server that won't start
    for exactly the users running from a clone.
    """
    import importlib
    from importlib.metadata import PackageNotFoundError
    from unittest.mock import patch

    import remind_me_mcp.version as version_mod

    try:
        with patch(
            "importlib.metadata.version", side_effect=PackageNotFoundError("remind-me-mcp")
        ):
            reloaded = importlib.reload(version_mod)
            assert reloaded.__version__ == "0.0.0-dev"
    finally:
        # Restore the real value for anything importing it after this test.
        importlib.reload(version_mod)

    assert version_mod.__version__ == __version__


def test_no_module_imports_the_version_from_the_package_root() -> None:
    """Module-level ``from remind_me_mcp import __version__`` is the cycle trap.

    It resolves an attribute on the partially-initialized package rather than
    a submodule, so it only fails once the module is pulled in through
    ``__init__``'s own import chain — i.e. at server startup, on the machine
    that just upgraded, rather than here. Function-scoped imports of it
    (``updater.py``) are fine and deliberately not flagged: by call time the
    package is fully initialized.
    """
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Top-level statements only — a nested (function-scoped) import is safe.
        for node in tree.body:
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "remind_me_mcp"
                and any(a.name == "__version__" for a in node.names)
            ):
                offenders.append(f"{path.relative_to(PACKAGE)}:{node.lineno}")

    assert not offenders, (
        "import __version__ from remind_me_mcp.version, not the package root "
        f"(cycles through __init__): {', '.join(offenders)}"
    )
