"""
Checks for the hub's ``GET /stats`` route (SY-13).

These are STATIC checks against ``hub/main.py``'s source, not integration
tests. The hub ships as its own container with its own dependencies —
``fastapi`` and ``psycopg`` are deliberately not in this package's
requirements, so neither CI leg can import ``hub.main`` at all. Runtime
coverage lives in ``hub/e2e_test.py``, which runs against a real deployment.

What these lock down is the property most expensive to get wrong and least
visible in review: that ``/stats`` is auth-gated while ``/health`` is not.
Record counts and category names leak information about content, so a
``/stats`` that shipped unauthenticated would be a quiet data leak on any
hub reachable through a tunnel.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

HUB_MAIN = Path(__file__).resolve().parent.parent / "hub" / "main.py"


def _source() -> str:
    return HUB_MAIN.read_text(encoding="utf-8")


def _route_decorators() -> dict[str, list[str]]:
    """Map each route path to the source of its decorator call.

    Parsed via AST rather than regex so a reformatted or multi-line decorator
    can't silently drop a route from the check.
    """
    tree = ast.parse(_source())
    routes: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            func = dec.func
            if not (isinstance(func, ast.Attribute) and func.attr in {"get", "post"}):
                continue
            if not dec.args or not isinstance(dec.args[0], ast.Constant):
                continue
            path = str(dec.args[0].value)
            routes.setdefault(path, []).append(ast.unparse(dec))
    return routes


def test_stats_route_exists() -> None:
    """The route SY-13 adds is present."""
    assert "/stats" in _route_decorators()


def test_stats_route_is_auth_gated() -> None:
    """/stats must require the bearer secret — counts leak content information."""
    decorators = _route_decorators()["/stats"]
    assert any("_require_auth" in d for d in decorators), (
        "GET /stats must be declared with dependencies=[Depends(_require_auth)]; "
        "record counts and category names must not be publicly readable"
    )


def test_health_route_stays_unauthenticated() -> None:
    """/health must NOT become auth-gated — deploy healthchecks depend on it.

    Guards the inverse mistake of the one above: adding auth to /health would
    break Railway's healthcheckPath and compose's service_healthy gate.
    """
    decorators = _route_decorators()["/health"]
    assert not any("_require_auth" in d for d in decorators)


def test_health_route_has_no_aggregate_queries() -> None:
    """Counts belong in /stats, not /health.

    /health is documented as responding even when Postgres is unreachable and
    is polled by healthchecks, so it must stay cheap.
    """
    src = _source()
    start = src.index('@app.get("/health")')
    end = src.index('@app.get("/stats"')
    assert "COUNT(*)" not in src[start:end]


def test_stats_reports_every_synced_record_type() -> None:
    """A partial /stats would silently under-report drift.

    Reconciliation compares a node against the hub; a record type absent here
    is one that can drift undetected, so all four synced tables must appear.
    """
    src = _source()
    start = src.index('@app.get("/stats"')
    end = src.index("Push — record-type dispatch")
    body = src[start:end]
    for table in ("memories", "entities", "memory_entities", "entity_relations"):
        assert re.search(rf"\bFROM {table}\b", body), f"/stats does not count {table}"


def test_stats_groups_by_origin_node_and_category() -> None:
    """The per-node breakdown is the whole point — origin_node is hub-only.

    ``origin_node`` never crosses the sync wire, so the hub is the only place
    "which node pushed what" is observable at all.
    """
    src = _source()
    start = src.index('@app.get("/stats"')
    end = src.index("Push — record-type dispatch")
    body = src[start:end]
    assert "origin_node" in body
    assert "category" in body


def test_stats_distinguishes_tombstones() -> None:
    """Tombstoned rows are counted separately so totals can be reconciled.

    A node's user-visible count filters ``deleted_at IS NULL``, so without
    this split a hub total would look inflated against every node.
    """
    src = _source()
    start = src.index('@app.get("/stats"')
    end = src.index("Push — record-type dispatch")
    assert "deleted_at IS NOT NULL" in src[start:end]
