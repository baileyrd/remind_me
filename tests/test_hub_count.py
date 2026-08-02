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
    assert not re.search(r"GROUP\s+BY", _count_body(), re.IGNORECASE)


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
    body = _count_body()
    assert "deleted_at IS NOT NULL" in body
    assert "'live'" in body or '"live"' in body


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
