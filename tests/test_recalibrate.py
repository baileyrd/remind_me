"""
Tests for remind_me_mcp.tools.recalibrate — importance recalibration
candidate surfacing (issue #200).

Covers the deterministic candidate heuristic (maintenance.py), the
remind_me_recalibrate_candidates tool's shape/bounding, and the maintenance
nudge's new "recalibration_candidates" queue -- following the exact
conventions test_maintenance.py/test_normalize.py already establish.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from remind_me_mcp import maintenance
from remind_me_mcp.db import _make_id, _now_iso
from remind_me_mcp.models import RecalibrateCandidatesInput
from remind_me_mcp.tools.recalibrate import remind_me_recalibrate_candidates

if TYPE_CHECKING:
    import sqlite3


def _days_ago(n: int) -> str:
    return (datetime.now(UTC) - timedelta(days=n)).isoformat()


# ---------------------------------------------------------------------------
# Candidate heuristic (maintenance.RECALIBRATION_CANDIDATE_WHERE)
# ---------------------------------------------------------------------------


def test_old_important_never_fed_back_memory_is_a_candidate(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    memory_factory(
        content="We decided to migrate to Postgres.",
        memory_type="decision",
        base_weight=1.3,
        accessed_at=_days_ago(maintenance.RECALIBRATION_STALE_DAYS + 10),
    )
    counts = maintenance.pending_counts(db_conn)
    assert counts["recalibration_candidates"] == 1


def test_recently_accessed_important_memory_is_not_a_candidate(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """Still being actively used -- presumably still classified correctly."""
    memory_factory(
        content="We decided to migrate to Postgres.",
        memory_type="decision",
        base_weight=1.3,
        accessed_at=_days_ago(1),
    )
    assert maintenance.pending_counts(db_conn)["recalibration_candidates"] == 0


def test_memory_with_prior_feedback_is_not_a_candidate(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """A remind_me_feedback signal is treated as a proxy for "already reviewed"."""
    mem = memory_factory(
        content="We decided to migrate to Postgres.",
        memory_type="decision",
        base_weight=1.3,
        accessed_at=_days_ago(maintenance.RECALIBRATION_STALE_DAYS + 10),
    )
    db_conn.execute(
        "INSERT INTO memory_feedback (id, memory_id, query, query_tokens, signal, "
        "magnitude, created_at) VALUES (?, ?, ?, ?, 'helpful', 0.15, ?)",
        (_make_id("fb-1"), mem["id"], "migration", "migration", _now_iso()),
    )
    db_conn.commit()
    assert maintenance.pending_counts(db_conn)["recalibration_candidates"] == 0


def test_low_importance_incidental_memory_is_not_a_candidate(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """Neither a high base_weight nor a durable memory_type -- not a candidate
    no matter how old, since nothing here signals it was ever "important"."""
    memory_factory(
        content="Grabbing coffee later.",
        memory_type="action_item",
        base_weight=1.0,
        accessed_at=_days_ago(maintenance.RECALIBRATION_STALE_DAYS + 100),
    )
    assert maintenance.pending_counts(db_conn)["recalibration_candidates"] == 0


def test_durable_memory_type_qualifies_even_at_default_base_weight(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """The issue's own example: a 'fact' whose base_weight was never bumped
    still qualifies by memory_type alone."""
    memory_factory(
        content="The VPN uses WireGuard.",
        memory_type="fact",
        base_weight=1.0,
        accessed_at=_days_ago(maintenance.RECALIBRATION_STALE_DAYS + 5),
    )
    assert maintenance.pending_counts(db_conn)["recalibration_candidates"] == 1


def test_high_base_weight_qualifies_even_without_a_durable_type(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    memory_factory(
        content="Some insight worth remembering.",
        memory_type="insight",
        base_weight=1.2,
        accessed_at=_days_ago(maintenance.RECALIBRATION_STALE_DAYS + 5),
    )
    assert maintenance.pending_counts(db_conn)["recalibration_candidates"] == 1


def test_superseded_memory_is_excluded(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    memory_factory(
        content="We decided to migrate to Postgres.",
        memory_type="decision",
        base_weight=1.3,
        accessed_at=_days_ago(maintenance.RECALIBRATION_STALE_DAYS + 10),
        superseded_by="some-other-id",
    )
    assert maintenance.pending_counts(db_conn)["recalibration_candidates"] == 0


def test_deleted_memory_is_excluded(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    memory_factory(
        content="We decided to migrate to Postgres.",
        memory_type="decision",
        base_weight=1.3,
        accessed_at=_days_ago(maintenance.RECALIBRATION_STALE_DAYS + 10),
        deleted_at=_now_iso(),
    )
    assert maintenance.pending_counts(db_conn)["recalibration_candidates"] == 0


def test_falls_back_to_created_at_when_never_accessed(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """accessed_at is NULL for a memory that's never been re-retrieved --
    staleness must fall back to created_at rather than treating a NULL as
    "just accessed" (which COALESCE handles)."""
    mem = memory_factory(
        content="We decided to migrate to Postgres.",
        memory_type="decision",
        base_weight=1.3,
    )
    old = _days_ago(maintenance.RECALIBRATION_STALE_DAYS + 10)
    db_conn.execute(
        "UPDATE memories SET created_at = ?, accessed_at = NULL WHERE id = ?",
        (old, mem["id"]),
    )
    db_conn.commit()
    assert maintenance.pending_counts(db_conn)["recalibration_candidates"] == 1


def test_batch_tool_and_nudge_share_one_queue_definition() -> None:
    """Same discipline test_maintenance.py already applies to the other
    queues: the tool must import the WHERE clause, not copy it, or the nudge
    count and the batch the tool returns could silently drift apart."""
    from remind_me_mcp.tools import recalibrate as recalibrate_mod

    assert recalibrate_mod._RECALIBRATION_CANDIDATE_WHERE is maintenance.RECALIBRATION_CANDIDATE_WHERE


# ---------------------------------------------------------------------------
# remind_me_recalibrate_candidates
# ---------------------------------------------------------------------------


async def test_recalibrate_candidates_returns_matching_memories(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    old = _days_ago(maintenance.RECALIBRATION_STALE_DAYS + 10)
    memory_factory(
        content="We decided to migrate to Postgres.",
        memory_type="decision",
        base_weight=1.3,
        accessed_at=old,
    )
    memory_factory(
        content="Grabbing coffee later.",
        memory_type="action_item",
        base_weight=1.0,
        accessed_at=old,
    )

    result = json.loads(await remind_me_recalibrate_candidates(RecalibrateCandidatesInput()))

    assert result["total_candidates"] == 1
    assert len(result["candidates"]) == 1
    candidate = result["candidates"][0]
    assert "We decided to migrate" in candidate["content_snippet"]
    assert candidate["memory_type"] == "decision"
    assert candidate["base_weight"] == 1.3
    assert candidate["accessed_at"] == old


async def test_recalibrate_candidates_respects_limit(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    old = _days_ago(maintenance.RECALIBRATION_STALE_DAYS + 10)
    for i in range(5):
        memory_factory(
            content=f"Old decision number {i}.",
            memory_type="decision",
            base_weight=1.3,
            accessed_at=old,
        )

    result = json.loads(
        await remind_me_recalibrate_candidates(RecalibrateCandidatesInput(limit=2))
    )
    assert result["total_candidates"] == 5
    assert len(result["candidates"]) == 2


async def test_recalibrate_candidates_empty_when_nothing_qualifies(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    memory_factory(content="Just a note.", memory_type="action_item", base_weight=1.0)
    result = json.loads(await remind_me_recalibrate_candidates(RecalibrateCandidatesInput()))
    assert result["total_candidates"] == 0
    assert result["candidates"] == []


async def test_recalibrate_candidates_default_limit_is_20() -> None:
    assert RecalibrateCandidatesInput().limit == 20


async def test_recalibrate_candidates_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError):
        RecalibrateCandidatesInput(limit=5, bogus_field="nope")


# ---------------------------------------------------------------------------
# Maintenance nudge integration
# ---------------------------------------------------------------------------


def _add_recalibration_candidates(db: sqlite3.Connection, memory_factory, n: int) -> None:
    old = _days_ago(maintenance.RECALIBRATION_STALE_DAYS + 10)
    for i in range(n):
        memory_factory(
            content=f"Old decision number {i}.",
            memory_type="decision",
            base_weight=1.3,
            accessed_at=old,
        )


def test_pending_counts_includes_the_recalibration_queue(db_conn: sqlite3.Connection) -> None:
    counts = maintenance.pending_counts(db_conn)
    assert "recalibration_candidates" in counts


def test_no_nudge_below_the_threshold(db_conn: sqlite3.Connection, memory_factory) -> None:
    _add_recalibration_candidates(db_conn, memory_factory, maintenance.NUDGE_THRESHOLD - 1)
    assert maintenance.maybe_maintenance_notice(db_conn) is None


def test_nudge_at_the_threshold_names_the_queue_and_its_prompt(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    _add_recalibration_candidates(db_conn, memory_factory, maintenance.NUDGE_THRESHOLD)
    notice = maintenance.maybe_maintenance_notice(db_conn)
    assert notice is not None
    assert "importance review" in notice
    assert "recalibrate_importance" in notice


@pytest.mark.asyncio
async def test_status_reports_the_recalibration_backlog(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    from remind_me_mcp.tools import remind_me_server_status

    _add_recalibration_candidates(db_conn, memory_factory, 4)
    result = await remind_me_server_status()
    assert "recalibration candidates" in result


# ---------------------------------------------------------------------------
# No new scheduler wiring (issue #200's architectural correction)
# ---------------------------------------------------------------------------


def test_recalibrate_module_does_not_import_scheduler() -> None:
    """This is a read-only, on-demand tool plus a deterministic count -- not
    a scheduler-hosted background job, per the issue's architectural
    correction (see tools/recalibrate.py's module docstring, which mentions
    "scheduler.py" only in prose explaining why this ISN'T one)."""
    import ast

    import remind_me_mcp.tools.recalibrate as recalibrate_mod

    assert "scheduler" not in recalibrate_mod.__dict__

    with open(recalibrate_mod.__file__, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=recalibrate_mod.__file__)

    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any("scheduler" in m for m in imported_modules)
