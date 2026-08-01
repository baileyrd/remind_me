"""
Tests for maintenance backlog counts, the throttled nudge, and capture health.

Two behaviours are load-bearing here and easy to regress silently:

* **The nudge must reach a response Claude reads.** Its whole purpose is that
  ``pending_wiki_compile`` previously lived only on status tools nobody calls.
* **It must not corrupt a JSON response.** The nudge is prose; a JSON search
  result with markdown stapled to the end no longer parses, and nothing in the
  tool layer would catch that.

The nudge throttle is module-level state; ``conftest.reset_maintenance_throttle``
resets it before every test, so the throttling assertions below are testing the
throttle rather than whatever an earlier test happened to leave behind.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from remind_me_mcp import maintenance
from remind_me_mcp.db import _make_id, _now_iso
from remind_me_mcp.models import MemorySearchInput, ResponseFormat
from remind_me_mcp.tools import memory_search, remind_me_server_status


def _add_unclassified(db: sqlite3.Connection, n: int, content: str = "budget note") -> None:
    """Insert *n* memories that land in the unclassified maintenance queue."""
    now = _now_iso()
    for i in range(n):
        db.execute(
            "INSERT INTO memories (id, content, category, tags, source, metadata, "
            "memory_type, created_at, updated_at) "
            "VALUES (?, ?, 'note', '[]', 'test', '{}', 'unclassified', ?, ?)",
            (_make_id(f"{content}-{i}"), f"{content} {i}", now, now),
        )
    db.commit()


def _add_capture(db: sqlite3.Connection, capture_id: str = "cap-1") -> None:
    """Insert a dialog/summary pair sharing one capture_id, as auto_capture does."""
    now = _now_iso()
    for kind, category in (("dialog", "dialog"), ("summary", "conversation")):
        db.execute(
            "INSERT INTO memories (id, content, category, tags, source, metadata, "
            "capture_id, created_at, updated_at) "
            "VALUES (?, ?, ?, '[]', 'auto_capture', '{}', ?, ?, ?)",
            (_make_id(f"{capture_id}-{kind}"), f"{kind} body", category, capture_id, now, now),
        )
    db.commit()


# ---------------------------------------------------------------------------
# Pending counts
# ---------------------------------------------------------------------------


def test_pending_counts_reports_every_queue(db_conn: sqlite3.Connection) -> None:
    counts = maintenance.pending_counts(db_conn)
    assert set(counts) == {
        "undecomposed_captures",
        "unannotated_memories",
        "unnormalized_imports",
        "unclassified_memories",
        "pending_wiki_compile",
    }
    assert all(isinstance(v, int) for v in counts.values())


def test_pending_counts_tracks_the_unclassified_queue(db_conn: sqlite3.Connection) -> None:
    assert maintenance.pending_counts(db_conn)["unclassified_memories"] == 0
    _add_unclassified(db_conn, 3)
    assert maintenance.pending_counts(db_conn)["unclassified_memories"] == 3


def test_pending_counts_survives_a_broken_database() -> None:
    """A status helper must never be the reason a search fails."""
    broken = sqlite3.connect(":memory:")  # no schema at all
    broken.row_factory = sqlite3.Row
    counts = maintenance.pending_counts(broken)
    assert all(v == 0 for v in counts.values())
    broken.close()


def test_unnormalized_and_unannotated_queries_use_an_index_seek(
    db_conn: sqlite3.Connection,
) -> None:
    """Regression guard for the correlated-subquery hang (issue #120).

    ``UNNORMALIZED_WHERE`` used to spell its "not already normalized" check as
    a correlated ``NOT EXISTS`` over ``json_extract(metadata, ...)``. With no
    usable index, SQLite resolved it as a per-row table SCAN instead of a
    SEARCH — invisible on the handful of rows any of these tests seed, but an
    O(n*m) hang on a real ~60K-row store that pegged a CPU core for minutes.

    ``EXPLAIN QUERY PLAN`` catches the plan *shape* regardless of row count,
    so this fails immediately if either WHERE clause regresses back to a
    non-indexed correlated scan, without needing to seed thousands of rows.
    A ``SCAN`` is only acceptable when it runs *once* to materialize a
    non-correlated ``LIST SUBQUERY`` (the ``NOT IN`` rewrite's one-time set
    build) rather than once per outer row under a ``CORRELATED`` subquery.
    """
    for where in (maintenance.UNNORMALIZED_WHERE, maintenance.UNANNOTATED_WHERE):
        plan = db_conn.execute(
            f"EXPLAIN QUERY PLAN SELECT COUNT(*) FROM memories m WHERE {where}"
        ).fetchall()
        by_id = {row[0]: row for row in plan}

        def _is_under_correlated_subquery(node_id: int) -> bool:
            node = by_id.get(node_id)
            while node is not None:
                if "CORRELATED" in node[3].upper():
                    return True
                node = by_id.get(node[1])
            return False

        for row in plan:
            detail = row[3].upper()
            if detail.startswith("SCAN") and _is_under_correlated_subquery(row[1]):
                pytest.fail(
                    f"{where.strip()[:40]!r} resolved to a per-row table scan "
                    f"under a correlated subquery — this is the O(n*m) shape "
                    f"that caused issue #120: {detail}"
                )


def test_batch_tool_and_nudge_share_one_queue_definition() -> None:
    """The tools import their WHERE clauses from maintenance, not copies of them.

    Two divergent copies would let the nudge advertise a backlog the batch tool
    does not actually return — the exact inconsistency that makes a nudge
    untrustworthy.
    """
    from remind_me_mcp.tools import capture as capture_mod
    from remind_me_mcp.tools import normalize as normalize_mod

    assert capture_mod._UNANNOTATED_WHERE is maintenance.UNANNOTATED_WHERE
    assert normalize_mod._UNNORMALIZED_WHERE is maintenance.UNNORMALIZED_WHERE


# ---------------------------------------------------------------------------
# Nudge: thresholds and throttling
# ---------------------------------------------------------------------------


def test_no_nudge_below_the_threshold(db_conn: sqlite3.Connection) -> None:
    """A handful of pending items is normal use, not something to report."""
    _add_unclassified(db_conn, maintenance.NUDGE_THRESHOLD - 1)
    assert maintenance.maybe_maintenance_notice(db_conn) is None


def test_nudge_at_the_threshold_names_the_queue_and_its_prompt(
    db_conn: sqlite3.Connection,
) -> None:
    _add_unclassified(db_conn, maintenance.NUDGE_THRESHOLD)
    notice = maintenance.maybe_maintenance_notice(db_conn)
    assert notice is not None
    assert str(maintenance.NUDGE_THRESHOLD) in notice
    assert "classify_memories" in notice


def test_nudge_is_throttled_after_the_first_check(db_conn: sqlite3.Connection) -> None:
    _add_unclassified(db_conn, maintenance.NUDGE_THRESHOLD)
    assert maintenance.maybe_maintenance_notice(db_conn) is not None
    assert maintenance.maybe_maintenance_notice(db_conn) is None
    assert maintenance.maybe_maintenance_notice(db_conn) is None


def test_throttle_claims_its_slot_even_when_nothing_is_pending(
    db_conn: sqlite3.Connection,
) -> None:
    """An empty vault must not re-run the COUNTs on every single search.

    The timer is claimed before the queries run, so a second call inside the
    window short-circuits regardless of what the first call found.
    """
    assert maintenance.maybe_maintenance_notice(db_conn) is None
    _add_unclassified(db_conn, maintenance.NUDGE_THRESHOLD * 2)
    assert maintenance.maybe_maintenance_notice(db_conn) is None


def test_nudges_can_be_disabled(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(maintenance, "NUDGES_ENABLED", False)
    _add_unclassified(db_conn, maintenance.NUDGE_THRESHOLD * 5)
    assert maintenance.maybe_maintenance_notice(db_conn) is None


# ---------------------------------------------------------------------------
# Nudge: delivery into real tool responses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_markdown_search_carries_the_nudge(db_conn: sqlite3.Connection) -> None:
    """The whole point: the backlog reaches a response Claude actually reads."""
    _add_unclassified(db_conn, maintenance.NUDGE_THRESHOLD, content="quarterly budget")
    result = await memory_search(
        MemorySearchInput(query="budget", response_format=ResponseFormat.MARKDOWN)
    )
    assert "Maintenance pending" in result


@pytest.mark.asyncio
async def test_json_search_stays_parseable(db_conn: sqlite3.Connection) -> None:
    """A nudge appended to a JSON envelope would make it unparseable.

    This is why the notice is wired onto markdown return paths only, matching
    how the pre-existing update notice is wired.
    """
    _add_unclassified(db_conn, maintenance.NUDGE_THRESHOLD, content="quarterly budget")
    result = await memory_search(
        MemorySearchInput(query="budget", response_format=ResponseFormat.JSON)
    )
    parsed = json.loads(result)  # would raise if a nudge were appended
    assert "Maintenance pending" not in result
    assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# Capture health (#4)
# ---------------------------------------------------------------------------


def test_capture_health_on_a_vault_that_never_captured(db_conn: sqlite3.Connection) -> None:
    health = maintenance.capture_health(db_conn)
    assert health["ever_captured"] is False
    assert health["captures"] == 0
    assert health["last_capture_at"] is None


def test_capture_health_counts_a_capture_once_not_twice(
    db_conn: sqlite3.Connection,
) -> None:
    """auto_capture writes a dialog *and* a summary sharing one capture_id."""
    _add_capture(db_conn, "cap-a")
    health = maintenance.capture_health(db_conn)
    assert health["ever_captured"] is True
    assert health["captures"] == 1
    assert health["last_capture_at"]


def test_capture_health_counts_distinct_captures(db_conn: sqlite3.Connection) -> None:
    _add_capture(db_conn, "cap-a")
    _add_capture(db_conn, "cap-b")
    assert maintenance.capture_health(db_conn)["captures"] == 2


@pytest.mark.asyncio
async def test_status_distinguishes_never_configured_from_working(
    db_conn: sqlite3.Connection,
) -> None:
    """"Never configured" and "configured but quiet" used to look identical."""
    before = await remind_me_server_status()
    assert "none recorded" in before
    assert "opt-in" in before

    _add_capture(db_conn, "cap-a")
    after = await remind_me_server_status()
    assert "none recorded" not in after
    assert "1 capture(s)" in after


@pytest.mark.asyncio
async def test_status_reports_maintenance_backlogs(db_conn: sqlite3.Connection) -> None:
    empty = await remind_me_server_status()
    assert "every queue is drained" in empty

    _add_unclassified(db_conn, 4)
    populated = await remind_me_server_status()
    assert "unclassified memories" in populated


# ---------------------------------------------------------------------------
# Slow-call watchdog surfaced in status (#128)
# ---------------------------------------------------------------------------


async def test_status_reports_watchdog_armed(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from remind_me_mcp import watchdog

    monkeypatch.setattr(
        watchdog,
        "status",
        lambda: {"enabled": True, "threshold_seconds": 30.0, "calls_in_flight": 1},
    )
    result = await remind_me_server_status()
    assert "Slow-call watchdog:** ✓ armed at 30s" in result


async def test_status_reports_watchdog_disabled(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from remind_me_mcp import watchdog

    monkeypatch.setattr(
        watchdog,
        "status",
        lambda: {"enabled": False, "threshold_seconds": 0.0, "calls_in_flight": 0},
    )
    result = await remind_me_server_status()
    assert "Slow-call watchdog:** ✗ disabled" in result
    assert "REMIND_ME_SLOW_CALL_SECONDS" in result


# ---------------------------------------------------------------------------
# Feedback hint (#6)
# ---------------------------------------------------------------------------


def test_feedback_hint_steers_toward_the_query_contextual_form() -> None:
    """The global form penalises a memory for every future query, not just this one.

    Nothing in a session ever asks for feedback, so if the hint is going to
    appear at all it should point at the mode actually worth having.
    """
    hint = maintenance.maybe_feedback_hint()
    assert hint is not None
    assert "remind_me_feedback" in hint
    assert "query" in hint


def test_feedback_hint_is_throttled() -> None:
    assert maintenance.maybe_feedback_hint() is not None
    assert maintenance.maybe_feedback_hint() is None


def test_feedback_hint_respects_the_disable_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(maintenance, "NUDGES_ENABLED", False)
    assert maintenance.maybe_feedback_hint() is None


def test_throttles_are_independent(db_conn: sqlite3.Connection) -> None:
    """One advisory claiming its slot must not silence the other."""
    _add_unclassified(db_conn, maintenance.NUDGE_THRESHOLD)
    assert maintenance.maybe_maintenance_notice(db_conn) is not None
    # The maintenance timer is now claimed; feedback runs on its own timer.
    assert maintenance.maybe_feedback_hint() is not None


@pytest.mark.asyncio
async def test_search_shows_at_most_one_advisory(db_conn: sqlite3.Connection) -> None:
    """Stacking both would train the reader to skip the tail of every search."""
    _add_unclassified(db_conn, maintenance.NUDGE_THRESHOLD, content="quarterly budget")
    result = await memory_search(
        MemorySearchInput(query="budget", response_format=ResponseFormat.MARKDOWN)
    )
    assert "Maintenance pending" in result
    assert "remind_me_feedback" not in result


@pytest.mark.asyncio
async def test_search_falls_back_to_the_feedback_hint(db_conn: sqlite3.Connection) -> None:
    """With no backlog to report, the standing affordance gets the slot."""
    db_conn.execute(
        "INSERT INTO memories (id, content, category, tags, source, metadata, "
        "memory_type, created_at, updated_at) "
        "VALUES (?, 'quarterly budget planning', 'note', '[]', 'test', '{}', 'fact', ?, ?)",
        (_make_id("solo"), _now_iso(), _now_iso()),
    )
    db_conn.commit()
    result = await memory_search(
        MemorySearchInput(query="budget", response_format=ResponseFormat.MARKDOWN)
    )
    assert "Maintenance pending" not in result
    assert "remind_me_feedback" in result


@pytest.mark.asyncio
async def test_json_search_carries_no_advisory_either(db_conn: sqlite3.Connection) -> None:
    """The JSON envelope must stay parseable no matter which advisory is due."""
    db_conn.execute(
        "INSERT INTO memories (id, content, category, tags, source, metadata, "
        "memory_type, created_at, updated_at) "
        "VALUES (?, 'quarterly budget planning', 'note', '[]', 'test', '{}', 'fact', ?, ?)",
        (_make_id("solo-json"), _now_iso(), _now_iso()),
    )
    db_conn.commit()
    result = await memory_search(
        MemorySearchInput(query="budget", response_format=ResponseFormat.JSON)
    )
    json.loads(result)
    assert "remind_me_feedback" not in result


# ---------------------------------------------------------------------------
# Tool-description disambiguation (#5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overlapping_finder_tools_point_at_each_other() -> None:
    """search/list/get/entity all read as "find things"; the descriptions must
    say which is which, since that confusion sits entirely inside the tools a
    conversational session keeps — hiding admin tools would not touch it.
    """
    from remind_me_mcp.server import mcp

    by_name = {t.name: (t.description or "") for t in await mcp.list_tools()}

    # Each overlapping tool must name the neighbour to prefer instead.
    assert "remind_me_search" in by_name["remind_me_list"]
    assert "remind_me_search" in by_name["remind_me_get"]
    assert "remind_me_search" in by_name["remind_me_entity"]
    # ...and search must mark itself as the default entry point.
    assert "remind_me_list" in by_name["remind_me_search"]


@pytest.mark.asyncio
async def test_list_is_marked_as_browsing_not_finding() -> None:
    """The most dangerous of the cluster: it does no relevance ranking at all,
    but its old description read like a perfectly good way to find things.
    """
    from remind_me_mcp.server import mcp

    desc = next(
        t.description or "" for t in await mcp.list_tools() if t.name == "remind_me_list"
    ).lower()
    assert "no relevance ranking" in desc


def test_feedback_query_field_describes_its_real_effect() -> None:
    """It previously read "for future audit/reporting", which is not what it does.

    Passing `query` switches the whole mechanism to query-contextual; a caller
    who believed the old description would reasonably omit it, which is a plain
    reason the query-contextual path never got exercised.
    """
    from remind_me_mcp.models import FeedbackInput

    desc = FeedbackInput.model_fields["query"].description or ""
    assert "audit" not in desc.lower()
    assert "global" in desc.lower()
