"""
Checks for the hub's version reporting (``HUB_VERSION``).

Like ``test_hub_stats.py``, these are STATIC checks against ``hub/main.py``'s
source rather than integration tests: the hub ships as its own container with
its own dependencies (``fastapi``, ``psycopg``), neither of which is in this
package's requirements, so no CI leg can import ``hub.main``. Runtime coverage
lives in ``hub/e2e_test.py``, which runs against a real deployment.

What's locked down here is what a static reading can actually establish, and
what would be quietly wrong otherwise: that the constant exists and is a
plausible version, that every route an operator or client would look at
reports it (a version visible on only some of them is worse than none — it
invites "the hub didn't answer" when the truth is "you asked the wrong
route"), and that the public one stays a bare identifier.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

HUB_MAIN = Path(__file__).resolve().parent.parent / "hub" / "main.py"

# Routes that must report the hub's version. /health is the unauthenticated
# deploy check; /stats and /count are what a client reads.
_VERSION_ROUTES = ("/health", "/stats", "/count")


def _source() -> str:
    return HUB_MAIN.read_text(encoding="utf-8")


def _hub_version() -> str:
    """Read HUB_VERSION out of the source without importing hub.main.

    Importing is not an option (no fastapi/psycopg in this environment), and
    a regex over the whole file would happily match the string inside a
    docstring, so the assignment is located via AST.
    """
    for node in ast.walk(ast.parse(_source())):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "HUB_VERSION" for t in node.targets
        ):
            assert isinstance(node.value, ast.Constant), "HUB_VERSION must be a literal"
            return str(node.value.value)
    raise AssertionError("hub/main.py does not define HUB_VERSION")


def _route_decorators(path: str) -> list[str]:
    """Source of every decorator on the handler for ``GET path``."""
    tree = ast.parse(_source())
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr == "get"
                and dec.args
                and isinstance(dec.args[0], ast.Constant)
                and dec.args[0].value == path
            ):
                return [ast.unparse(d) for d in node.decorator_list]
    raise AssertionError(f"hub/main.py has no GET {path} route")


def _route_body(path: str) -> str:
    """Executable source of the handler decorated with ``@app.get(path)``.

    The docstring is dropped first: these routes document their own security
    tradeoffs in prose, so a substring check against the full source would
    match the explanation of a leak rather than the leak.
    """
    tree = ast.parse(_source())
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr == "get"
                and dec.args
                and isinstance(dec.args[0], ast.Constant)
                and dec.args[0].value == path
            ):
                body = node.body
                if ast.get_docstring(node) is not None:
                    body = body[1:]
                return "\n".join(ast.unparse(stmt) for stmt in body)
    raise AssertionError(f"hub/main.py has no GET {path} route")


def test_hub_version_is_a_literal_semver_string() -> None:
    """The container has no pyproject or git checkout to derive a version from.

    The Containerfile copies ``main.py`` alone, so anything computed at import
    time would resolve to nothing inside the image. It must be a literal, and
    semver so the documented MAJOR/MINOR/PATCH bump rule has meaning.
    """
    assert re.fullmatch(r"\d+\.\d+\.\d+", _hub_version()), (
        f"HUB_VERSION must be a literal MAJOR.MINOR.PATCH string, got {_hub_version()!r}"
    )


def test_every_version_route_reports_it() -> None:
    """A version on only some routes sends operators to the wrong one."""
    for path in _VERSION_ROUTES:
        assert "HUB_VERSION" in _route_body(path), (
            f"GET {path} must include HUB_VERSION in its response body"
        )


def test_health_reports_version_without_auth() -> None:
    """Verifying a rollover must not require the sync secret.

    /health is the route documented as answering even when Postgres is down;
    gating the build identifier behind the bearer token would push operators
    toward putting SYNC_SECRET in their shell history to check a deploy.
    """
    assert "HUB_VERSION" in _route_body("/health")
    assert not any("_require_auth" in d for d in _route_decorators("/health"))


def test_public_version_carries_no_build_detail() -> None:
    """The unauthenticated version stays a bare string.

    Reporting it at all is a fingerprinting tradeoff taken deliberately for
    the deploy-verification win; attaching a commit, dependency set, or
    platform to it would widen that from "which release" to "which known
    CVEs" without adding anything an operator needs from /health.
    """
    body = _route_body("/health")
    for leak in ("sys.version", "platform", "commit", "psycopg.__version__"):
        assert leak not in body, f"/health must not report {leak}"
