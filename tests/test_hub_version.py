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


def _app_call() -> str:
    """Source of the ``FastAPI(...)`` construction."""
    for node in ast.walk(ast.parse(_source())):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "FastAPI"
        ):
            return ast.unparse(node)
    raise AssertionError("hub/main.py does not construct a FastAPI app")


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


def test_fastapi_metadata_uses_the_hub_version() -> None:
    """FastAPI's own ``version=`` must not be left at its "0.1.0" placeholder.

    Anything that reads the app's OpenAPI metadata would otherwise report a
    version contradicting HUB_VERSION — a stale version that looks
    authoritative, which is worse than none.
    """
    call = _app_call()
    assert "version=HUB_VERSION" in call, (
        "FastAPI(version=...) must be HUB_VERSION, not the framework default"
    )


def test_interactive_docs_routes_are_disabled() -> None:
    """/docs, /redoc and /openapi.json default to ON and UNAUTHENTICATED.

    Left at the default they publish every route — including
    POST /admin/compact_tombstones, which hard-deletes rows — with full
    schemas, to anyone who can reach the port. The hub is documented as
    commonly fronted by a tunnel reachable from the open internet, so this
    silently undoes the auth posture the rest of this file argues for.
    """
    call = _app_call()
    for kwarg in ("docs_url=None", "redoc_url=None", "openapi_url=None"):
        assert kwarg in call, (
            f"FastAPI(...) must pass {kwarg}; the framework default exposes an "
            "unauthenticated documentation surface listing every route"
        )


# ---------------------------------------------------------------------------
# The CI bump guard (.github/scripts/check_hub_version.py)
# ---------------------------------------------------------------------------


def _guard():
    """Import the CI script, which lives outside the package."""
    import importlib.util

    path = HUB_MAIN.parent.parent / ".github" / "scripts" / "check_hub_version.py"
    spec = importlib.util.spec_from_file_location("check_hub_version", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_OLD = 'HUB_VERSION = "1.0.0"\nx = 1\n'
_EDITED = 'HUB_VERSION = "1.0.0"\nx = 2\n'
_BUMPED = 'HUB_VERSION = "1.1.0"\nx = 2\n'


def test_guard_passes_when_the_hub_is_untouched() -> None:
    ok, _ = _guard().decide(_OLD, _OLD, skip=False)
    assert ok


def test_guard_passes_when_the_version_was_bumped() -> None:
    ok, message = _guard().decide(_OLD, _BUMPED, skip=False)
    assert ok
    assert "1.0.0 -> 1.1.0" in message


def test_guard_fails_on_an_edit_without_a_bump() -> None:
    """The whole point: a changed hub with an unchanged version is the bug."""
    ok, message = _guard().decide(_OLD, _EDITED, skip=False)
    assert not ok
    assert "still 1.0.0" in message


def test_guard_honours_the_opt_out() -> None:
    """Comment-only edits must not be forced to churn the version."""
    ok, message = _guard().decide(_OLD, _EDITED, skip=True)
    assert ok
    assert "opted out" in message


def test_guard_fails_if_the_constant_disappears() -> None:
    ok, message = _guard().decide(_OLD, "x = 2\n", skip=False)
    assert not ok
    assert "HUB_VERSION" in message


def test_guard_ignores_the_constant_name_in_prose() -> None:
    """`HUB_VERSION` appears throughout the file's own docstrings.

    Only the column-0 assignment counts, or a docstring mentioning the name
    would be read as the declaration.
    """
    guard = _guard()
    assert guard.extract_version('"""See HUB_VERSION = "9.9.9" above."""\n') is None
    assert guard.extract_version(_OLD) == "1.0.0"


def test_guard_reads_the_real_hub_version() -> None:
    """The regex must match the file as actually written, not a fixture of it."""
    assert _guard().extract_version(_source()) == _hub_version()


def test_version_header_is_applied_app_wide() -> None:
    """The header must be middleware, not per-route.

    Its entire value is covering the routes nobody remembered to annotate —
    /sync/push and /sync/pull, which is where nearly all real traffic goes,
    plus error responses that carry no JSON body at all. A per-route
    implementation would drift out of coverage the first time a route is
    added.
    """
    src = _source()
    tree = ast.parse(src)
    middlewares = [
        ast.unparse(dec)
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
        for dec in node.decorator_list
        if isinstance(dec, ast.Call)
        and isinstance(dec.func, ast.Attribute)
        and dec.func.attr == "middleware"
    ]
    assert middlewares, "X-Hub-Version must be stamped by @app.middleware, not per route"
    assert "X-Hub-Version" in src


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
