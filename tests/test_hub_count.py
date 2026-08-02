"""
Checks for the hub's ``GET /count`` route.

STATIC checks against ``hub/main.py``'s source, for the reason spelled out in
``test_hub_stats.py``: the hub ships as its own container with its own
dependencies (``fastapi``, ``psycopg``), so no CI leg here can import
``hub.main``. Runtime coverage lives in ``hub/e2e_test.py``.

Two properties are worth locking statically. First, auth: /count returns
record totals, and totals leak how much an operator has stored and how fast
it grows — shipping it unauthenticated would be the same quiet data leak
``test_hub_stats.py`` guards /stats against. Second, that it stays *cheap*:
its whole reason to exist next to /stats is being pollable, which a GROUP BY
silently undoes.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

HUB_MAIN = Path(__file__).resolve().parent.parent / "hub" / "main.py"


def _source() -> str:
    return HUB_MAIN.read_text(encoding="utf-8")


def _count_route() -> ast.FunctionDef:
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
                and dec.args[0].value == "/count"
            ):
                return node
    raise AssertionError("hub/main.py has no GET /count route")


def _count_body() -> str:
    """Executable source of the /count handler, docstring excluded.

    The route documents in prose the very things these checks look for (that
    it does no GROUP BY, that the table filter is an allowlist), so matching
    against the docstring would pass on the explanation instead of the code.
    """
    node = _count_route()
    body = node.body[1:] if ast.get_docstring(node) is not None else node.body
    return "\n".join(ast.unparse(stmt) for stmt in body)


def _count_sql() -> str:
    """Every statement that actually produces /count's numbers.

    The route delegates its queries to ``_count_tables`` (shared with
    /metrics), so assertions about the SQL must look there as well as at the
    handler — checking only the handler would leave them passing while
    verifying nothing.
    """
    body = _count_body()
    for node in ast.walk(ast.parse(_source())):
        if isinstance(node, ast.FunctionDef) and node.name == "_count_tables":
            stmts = node.body[1:] if ast.get_docstring(node) else node.body
            return body + "\n" + "\n".join(ast.unparse(s) for s in stmts)
    raise AssertionError("hub/main.py has no _count_tables helper")


def _countable_tables() -> tuple[str, ...]:
    """The ``_COUNTABLE`` allowlist, read without importing hub.main."""
    for node in ast.walk(ast.parse(_source())):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_COUNTABLE" for t in node.targets
        ):
            assert isinstance(node.value, ast.Tuple), "_COUNTABLE must be a literal tuple"
            return tuple(str(e.value) for e in node.value.elts if isinstance(e, ast.Constant))
    raise AssertionError("hub/main.py does not define _COUNTABLE")


def test_count_route_exists() -> None:
    assert _count_route().name == "count"


def test_count_route_is_auth_gated() -> None:
    """Totals leak content information, exactly as /stats' counts do.

    An unauthenticated /count would let anyone who can reach the tunnel graph
    the operator's memory growth over time.
    """
    decorators = [ast.unparse(d) for d in _count_route().decorator_list]
    assert any("_require_auth" in d for d in decorators), (
        "GET /count must be declared with dependencies=[Depends(_require_auth)]; "
        "record totals must not be publicly readable"
    )


def test_count_does_not_group() -> None:
    """The cheapness is the feature — /stats already exists for breakdowns.

    /count is meant to be polled by a dashboard tile or a cron drift alarm; a
    GROUP BY over the whole memories table would make it as expensive as the
    endpoint it exists to avoid, without anything visible changing.
    """
    assert not re.search(r"GROUP\s+BY", _count_sql(), re.IGNORECASE)


def test_count_covers_every_synced_record_type() -> None:
    """A record type absent here is one that can drift unnoticed.

    Same reasoning as /stats': all four synced tables must be countable, or
    the endpoint answers "is it going up?" for only part of the store.
    """
    countable = _countable_tables()
    for table in ("memories", "entities", "memory_entities", "entity_relations"):
        assert table in countable, f"/count does not cover {table}"


def test_count_splits_live_from_tombstones() -> None:
    """A bare total silently disagrees with every node.

    A node's user-visible count filters ``deleted_at IS NULL`` while the hub
    retains tombstones, so a single number would look like permanent drift.
    """
    sql = _count_sql()
    assert "deleted_at IS NOT NULL" in sql
    assert "'live'" in sql or '"live"' in sql


def test_metrics_and_count_share_one_query_helper() -> None:
    """Two copies of the counting SQL is how a metric drifts from its endpoint.

    /metrics and /count report the same records; if they ever disagree, the
    dashboard graph and the reconcile check are both suspect and there is no
    way to tell which is right.
    """
    src = _source()
    assert "_count_tables" in src, "counting must live in a shared helper"
    assert "_count_tables(" in _count_body(), "/count must call the shared helper"

    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "metrics":
            assert "_count_tables(" in ast.unparse(node), (
                "/metrics must call the shared helper, not its own COUNT queries"
            )
            return
    raise AssertionError("hub/main.py has no /metrics route")


def test_metrics_route_is_auth_gated() -> None:
    """Diverges from the dashboard's unauthenticated /metrics, deliberately.

    The payload is the same aggregate /count and /stats are gated on, and
    anyone scraping the hub already holds SYNC_SECRET — so shipping it open
    would route around that gate rather than reconsider it.
    """
    tree = ast.parse(_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "metrics":
            decorators = [ast.unparse(d) for d in node.decorator_list]
            assert any("_require_auth" in d for d in decorators)
            return
    raise AssertionError("hub/main.py has no /metrics route")


def test_count_table_filter_is_an_allowlist() -> None:
    """The table name reaches the SQL as text, so it must never be user input.

    ``COUNT(*) FROM %s`` is not a parameterizable position in Postgres, so
    /count interpolates the name — safe only because the value is checked
    against a fixed tuple first. If that check is ever dropped, ``?table=``
    becomes SQL injection.
    """
    src = _source()
    assert "_COUNTABLE" in src, "the allowlist tuple must exist"
    body = _count_body()
    assert "_COUNTABLE" in body, "/count must validate `table` against the allowlist"
    assert "400" in body, "an unknown table must be rejected as a 400, not interpolated"


def test_approx_mode_uses_the_planner_estimate_not_a_scan() -> None:
    """?approx=1 must not just be an exact count with a different label.

    The whole point is dropping the scan: Postgres can't answer an
    unqualified COUNT(*) without one, which is what makes a per-minute
    scrape of a large table a standing load.
    """
    src = _source()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == "_approx_count_tables":
            body = ast.unparse(node.body[1:] if ast.get_docstring(node) else node.body)
            assert "pg_class" in body and "reltuples" in body
            assert "COUNT(*)" not in body, "approximate mode must not scan"
            return
    raise AssertionError("hub/main.py has no _approx_count_tables helper")


def test_exact_is_the_default() -> None:
    """Reconciliation needs real numbers; a silently-estimated total is worse
    than a slow one."""
    tree = ast.parse(_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "count":
            approx_arg = [a for a in node.args.args if a.arg == "approx"]
            assert approx_arg, "/count must accept an approx parameter"
            default = node.args.defaults[-1]
            assert isinstance(default, ast.Constant) and default.value is False
            return
    raise AssertionError("hub/main.py has no /count route")


def test_response_always_declares_which_kind_of_count_it_gave() -> None:
    """Present unconditionally, not only when true.

    Otherwise a caller has to infer exactness from a missing key, and the
    other signal — no live/tombstone split — is far too quiet to hang
    correctness on.
    """
    assert "'approximate': approx" in _count_body()
