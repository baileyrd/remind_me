"""
Checks for the hub bug-fix round covering issues #160-#165.

Like ``test_hub_stats.py``, these are STATIC checks against ``hub/main.py``'s
source, not integration tests — ``fastapi`` and ``psycopg`` are deliberately
not in this package's requirements, so neither CI leg can import ``hub.main``
at all. Runtime coverage lives in ``hub/e2e_test.py``, which runs against a
real deployment.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

HUB_MAIN = Path(__file__).resolve().parent.parent / "hub" / "main.py"


def _source() -> str:
    return HUB_MAIN.read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    """Return the exact source of a top-level def/route handler by name."""
    tree = ast.parse(_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(_source(), node) or ""
    raise AssertionError(f"no function named {name!r} found in hub/main.py")


def _route_decorators() -> dict[str, list[str]]:
    """Map each route path to the source of its decorator call (see test_hub_stats.py)."""
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


# ---------------------------------------------------------------------------
# issue #163 — non-ASCII Authorization header crashes with 500 instead of 401
# ---------------------------------------------------------------------------


def test_require_auth_compares_encoded_bytes_not_str() -> None:
    """hmac.compare_digest on two str raises TypeError for non-ASCII input;
    comparing .encode()d bytes sidesteps the crash entirely."""
    src = _function_source("_require_auth")
    assert "compare_digest" in src
    assert re.search(r"compare_digest\(\s*auth\.encode\(", src), (
        "expected hmac.compare_digest to be called on auth.encode(...), not the raw str"
    )


# ---------------------------------------------------------------------------
# issue #164 — /health leaks Postgres connection details
# ---------------------------------------------------------------------------


def test_health_does_not_expose_raw_exception_text() -> None:
    """The public db_status field must be a fixed string, not f'error: {e}' --
    OperationalError text typically embeds host/IP/port/db/username."""
    src = _function_source("health")
    assert "f\"error: {e}\"" not in src
    assert "db_status = \"unreachable\"" in src


def test_health_still_logs_the_real_exception() -> None:
    """The detail must not just vanish -- it should still be logged server-side."""
    src = _function_source("health")
    assert "log.warning" in src or "log.error" in src


# ---------------------------------------------------------------------------
# issue #162 — no Postgres connect/statement timeout
# ---------------------------------------------------------------------------


def test_connect_sets_a_connect_timeout() -> None:
    src = _function_source("_connect")
    assert "connect_timeout" in src


def test_connect_sets_a_statement_timeout() -> None:
    src = _function_source("_connect")
    assert "statement_timeout" in src


# ---------------------------------------------------------------------------
# issue #165a — unbounded push payload size
# ---------------------------------------------------------------------------


def test_sync_push_rejects_oversized_batches_with_413() -> None:
    src = _function_source("sync_push")
    assert "MAX_PUSH_BATCH" in src
    assert "413" in src


def test_max_push_batch_constant_exists() -> None:
    assert re.search(r"^MAX_PUSH_BATCH\s*=\s*\d+", _source(), re.MULTILINE)


# ---------------------------------------------------------------------------
# issue #165b — no tombstone retention/purge path
# ---------------------------------------------------------------------------


def test_tombstone_compaction_route_exists() -> None:
    assert "/admin/compact_tombstones" in _route_decorators()


def test_tombstone_compaction_route_is_auth_gated() -> None:
    decorators = _route_decorators()["/admin/compact_tombstones"]
    assert any("_require_auth" in d for d in decorators)


def test_tombstone_compaction_deletes_old_tombstones_only() -> None:
    src = _function_source("compact_tombstones")
    assert "deleted_at IS NOT NULL" in src
    assert "deleted_at <" in src


# ---------------------------------------------------------------------------
# issue #160 — pull cursor keyed on client-authored updated_at
# ---------------------------------------------------------------------------


def test_memories_table_has_hub_assigned_sequence_column() -> None:
    src = _source()
    assert "hub_seq" in src
    assert "CREATE SEQUENCE IF NOT EXISTS memories_hub_seq" in src


def test_legacy_hub_seq_backfill_present() -> None:
    """A pre-existing table's rows must get hub_seq backfilled during migration,
    not just newly-inserted ones -- otherwise every historical row sorts as
    NULL forever under the new cursor."""
    src = _function_source("_migrate")
    assert "hub_seq" in src
    assert "nextval('memories_hub_seq')" in src


def test_every_memory_upsert_bumps_hub_seq() -> None:
    """Both the INSERT and the ON CONFLICT DO UPDATE branch must assign a
    fresh hub_seq -- an update that reused the old value would defeat the
    whole point (the row wouldn't move to the end of the pull cursor)."""
    upsert_start = _source().index("_MEMORY_UPSERT = ")
    upsert_end = _source().index('_REQUIRED_MEMORY_KEYS')
    body = _source()[upsert_start:upsert_end]
    assert body.count("nextval('memories_hub_seq')") >= 2


def test_sync_pull_accepts_since_seq_and_orders_by_hub_seq() -> None:
    src = _function_source("sync_pull")
    assert "since_seq" in src
    assert "hub_seq" in src


def test_sync_pull_since_seq_is_additive_not_required() -> None:
    """Old clients that never send since_seq must keep working exactly as
    before -- this is an opt-in cursor upgrade, not a breaking wire change."""
    tree = ast.parse(_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "sync_pull":
            for arg, default in zip(
                reversed(node.args.args), reversed(node.args.defaults), strict=False
            ):
                if arg.arg == "since_seq":
                    assert isinstance(default, ast.Constant) and default.value is None
                    return
    raise AssertionError("since_seq parameter not found on sync_pull")


# ---------------------------------------------------------------------------
# issue #161 — full re-seed pull mode
# ---------------------------------------------------------------------------


def test_sync_pull_accepts_full_flag() -> None:
    src = _function_source("sync_pull")
    assert re.search(r"\bfull\s*:\s*bool\s*=\s*False\b", src)


def test_full_flag_bypasses_exclude_node_filter() -> None:
    src = _function_source("sync_pull")
    assert re.search(r"exclude_node\s+and\s+not\s+full", src), (
        "full=True must bypass the origin_node exclusion filter so a "
        "rebuilt node can re-seed its own historical records"
    )


def test_sync_pull_entities_also_supports_full() -> None:
    src = _function_source("sync_pull_entities")
    assert re.search(r"\bfull\s*:\s*bool\s*=\s*False\b", src)
    assert re.search(r"exclude_node\s+and\s+not\s+full", src)
