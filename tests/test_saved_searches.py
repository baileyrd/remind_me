"""
Tests for remind_me_mcp.saved_searches / tools.saved_searches — saved and
watched searches (issue #194).

Covers the v26->v27 migration, CRUD round-tripping (including update-by-name
instead of duplication), that remind_me_run_saved_search reproduces exactly
what remind_me_search itself would return for equivalent params (including
the include_sensitive default), and the watch-polling mechanics: first-poll
seeding without notifying, a later genuinely-new match notifying exactly
once and being recorded, no re-notification on a repeat poll, watch=false
searches never being polled, and cleanup of seen-memory rows on delete.
"""

from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from remind_me_mcp import config, notifications, saved_searches
from remind_me_mcp.models import MemorySearchInput

# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migration_creates_saved_search_tables(db_conn: sqlite3.Connection) -> None:
    tables = {
        r["name"]
        for r in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert "saved_searches" in tables
    assert "saved_search_seen_memories" in tables

    cols = {r["name"] for r in db_conn.execute("PRAGMA table_info(saved_searches)").fetchall()}
    assert {"id", "name", "query", "filters", "watch", "created_at", "updated_at"} <= cols

    seen_cols = {
        r["name"] for r in db_conn.execute("PRAGMA table_info(saved_search_seen_memories)").fetchall()
    }
    assert {"saved_search_id", "memory_id", "first_seen_at"} <= seen_cols


def test_seen_memories_unique_index_prevents_duplicate_rows(db_conn: sqlite3.Connection) -> None:
    saved = saved_searches.save_search(db_conn, "idx-test", "whatever")
    db_conn.execute(
        "INSERT INTO saved_search_seen_memories (saved_search_id, memory_id, first_seen_at) "
        "VALUES (?, ?, ?)",
        (saved["id"], "m1", "2026-01-01T00:00:00+00:00"),
    )
    db_conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "INSERT INTO saved_search_seen_memories (saved_search_id, memory_id, first_seen_at) "
            "VALUES (?, ?, ?)",
            (saved["id"], "m1", "2026-01-02T00:00:00+00:00"),
        )


# ---------------------------------------------------------------------------
# CRUD round trip
# ---------------------------------------------------------------------------


def test_save_list_get_delete_round_trip(db_conn: sqlite3.Connection, memory_factory) -> None:
    memory_factory(content="roundtrip alpha content")

    saved = saved_searches.save_search(
        db_conn, "rt", "alpha", category=None, tags=None, include_sensitive=False, watch=False
    )
    assert saved["name"] == "rt"
    assert saved["query"] == "alpha"
    assert saved["watch"] is False
    assert saved["filters"] == {"category": None, "tags": None, "include_sensitive": False}

    listed = saved_searches.list_saved_searches(db_conn)
    assert [s["name"] for s in listed] == ["rt"]

    fetched = saved_searches.get_saved_search(db_conn, "rt")
    assert fetched is not None
    assert fetched["query"] == "alpha"

    assert saved_searches.get_saved_search(db_conn, "does-not-exist") is None

    deleted = saved_searches.delete_saved_search(db_conn, "rt")
    assert deleted is True
    assert saved_searches.get_saved_search(db_conn, "rt") is None
    # Deleting again reports nothing to delete rather than raising.
    assert saved_searches.delete_saved_search(db_conn, "rt") is False


def test_save_search_same_name_updates_not_duplicates(db_conn: sqlite3.Connection) -> None:
    first = saved_searches.save_search(db_conn, "dup", "first query")
    second = saved_searches.save_search(db_conn, "dup", "second query", watch=True, category="work")

    assert second["id"] == first["id"]
    all_rows = saved_searches.list_saved_searches(db_conn)
    assert len(all_rows) == 1
    assert all_rows[0]["query"] == "second query"
    assert all_rows[0]["watch"] is True
    assert all_rows[0]["filters"]["category"] == "work"


async def test_delete_cleans_up_seen_memory_rows(
    db_conn: sqlite3.Connection, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory_factory(content="cleanup gamma content")
    monkeypatch.setattr(notifications, "notify", lambda *a: None)

    saved = saved_searches.save_search(db_conn, "cleanup", "gamma", watch=True)
    await saved_searches.poll_saved_search(db_conn, saved)  # seeds one row

    seen_before = db_conn.execute(
        "SELECT COUNT(*) AS c FROM saved_search_seen_memories WHERE saved_search_id = ?",
        (saved["id"],),
    ).fetchone()["c"]
    assert seen_before == 1

    saved_searches.delete_saved_search(db_conn, "cleanup")

    seen_after = db_conn.execute(
        "SELECT COUNT(*) AS c FROM saved_search_seen_memories WHERE saved_search_id = ?",
        (saved["id"],),
    ).fetchone()["c"]
    assert seen_after == 0


# ---------------------------------------------------------------------------
# remind_me_run_saved_search parity with remind_me_search
# ---------------------------------------------------------------------------


async def test_run_saved_search_matches_direct_memory_search(
    db_conn: sqlite3.Connection, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from remind_me_mcp import maintenance
    from remind_me_mcp.tools.search import memory_search

    # Neutralize the throttled maintenance-nudge/feedback-hint advisories
    # (tools/_shared.py's _maybe_search_notices) -- both carry process-global
    # in-memory throttle state, so calling remind_me_search twice back to
    # back (once via the saved search, once directly) would otherwise only
    # attach the notice to whichever call ran first. That's an artifact of
    # the shared advisory throttle, not a real difference in search results.
    monkeypatch.setattr(maintenance, "maybe_maintenance_notice", lambda *a, **k: None)
    monkeypatch.setattr(maintenance, "maybe_feedback_hint", lambda *a, **k: None)

    memory_factory(content="parity check alpha content", category="notes", tags=["x"])
    memory_factory(content="parity check beta content", category="other")

    saved = saved_searches.save_search(
        db_conn, "parity", "parity check", category="notes", tags=["x"]
    )

    via_saved = await saved_searches.execute_saved_search(saved)
    via_direct = await memory_search(
        MemorySearchInput(query="parity check", category="notes", tags=["x"])
    )

    assert via_saved == via_direct
    assert "parity check alpha content" in via_saved
    assert "parity check beta content" not in via_saved


async def test_run_saved_search_respects_include_sensitive_default(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    memory_factory(content="topsecret sensitive alpha content", sensitive=True)

    saved_off = saved_searches.save_search(db_conn, "sens", "topsecret sensitive")
    excluded = await saved_searches.execute_saved_search(saved_off)
    assert "topsecret sensitive alpha content" not in excluded

    saved_on = saved_searches.save_search(
        db_conn, "sens", "topsecret sensitive", include_sensitive=True
    )
    included = await saved_searches.execute_saved_search(saved_on)
    assert "topsecret sensitive alpha content" in included


async def test_run_saved_search_tool_reports_missing_name(db_conn: sqlite3.Connection) -> None:
    from remind_me_mcp.tools.saved_searches import remind_me_run_saved_search

    result = await remind_me_run_saved_search("nope")
    assert "not found" in result.lower()


# ---------------------------------------------------------------------------
# Watch polling: first-poll seeding, later-poll notification, no re-notify
# ---------------------------------------------------------------------------


async def test_first_poll_seeds_without_notifying(
    db_conn: sqlite3.Connection, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory_factory(content="alpha project kickoff notes")
    calls: list[tuple] = []
    monkeypatch.setattr(notifications, "notify", lambda *a: calls.append(a))

    saved = saved_searches.save_search(db_conn, "alpha watch", "alpha", watch=True)
    new_count = await saved_searches.poll_saved_search(db_conn, saved)

    assert new_count == 0
    assert calls == []
    seen = db_conn.execute(
        "SELECT memory_id FROM saved_search_seen_memories WHERE saved_search_id = ?",
        (saved["id"],),
    ).fetchall()
    assert len(seen) == 1


async def test_later_poll_notifies_once_on_genuinely_new_match_then_stops(
    db_conn: sqlite3.Connection, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory_factory(content="alpha project kickoff notes")
    calls: list[tuple] = []
    monkeypatch.setattr(notifications, "notify", lambda *a: calls.append(a))

    saved = saved_searches.save_search(db_conn, "alpha watch", "alpha", watch=True)
    seeded = await saved_searches.poll_saved_search(db_conn, saved)
    assert seeded == 0
    assert calls == []

    new_mem = memory_factory(content="alpha roadmap update")
    new_count = await saved_searches.poll_saved_search(db_conn, saved)

    assert new_count == 1
    assert len(calls) == 1
    subject, body = calls[0]
    assert "alpha watch" in subject
    assert new_mem["id"] in body

    # A subsequent poll with no further changes must not re-notify for the
    # same match.
    calls.clear()
    again = await saved_searches.poll_saved_search(db_conn, saved)
    assert again == 0
    assert calls == []


def test_watch_false_searches_are_never_polled(
    db_conn: sqlite3.Connection, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory_factory(content="beta topic notes")
    calls: list[tuple] = []
    monkeypatch.setattr(notifications, "notify", lambda *a: calls.append(a))

    saved_off = saved_searches.save_search(db_conn, "beta off", "beta", watch=False)
    total = saved_searches.poll_watched_saved_searches(db_conn)

    assert total == 0
    assert calls == []
    seen = db_conn.execute(
        "SELECT 1 FROM saved_search_seen_memories WHERE saved_search_id = ?",
        (saved_off["id"],),
    ).fetchone()
    assert seen is None


def test_poll_watched_saved_searches_only_polls_watched_ones(
    db_conn: sqlite3.Connection, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory_factory(content="gamma delta notes")
    monkeypatch.setattr(notifications, "notify", lambda *a: None)

    saved_searches.save_search(db_conn, "gamma off", "gamma", watch=False)
    watched = saved_searches.save_search(db_conn, "gamma on", "gamma", watch=True)

    # First (seeding) pass over all watched searches.
    saved_searches.poll_watched_saved_searches(db_conn)

    seen_off = db_conn.execute(
        "SELECT COUNT(*) AS c FROM saved_search_seen_memories"
    ).fetchone()["c"]
    # Only the watched search's match gets seeded.
    assert seen_off == 1
    row = db_conn.execute(
        "SELECT saved_search_id FROM saved_search_seen_memories"
    ).fetchone()
    assert row["saved_search_id"] == watched["id"]


# ---------------------------------------------------------------------------
# Due-check throttling (mirrors digest.is_digest_due's shape)
# ---------------------------------------------------------------------------


def test_is_saved_search_poll_due_fires_on_first_ever_check(db_conn: sqlite3.Connection) -> None:
    assert saved_searches.is_saved_search_poll_due(db_conn, 300) is True


def test_maybe_poll_watched_searches_throttles_within_the_interval(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "SAVED_SEARCH_POLL_INTERVAL", 3600)
    calls: list[int] = []
    monkeypatch.setattr(
        saved_searches, "poll_watched_saved_searches", lambda db: calls.append(1) or 0
    )

    saved_searches.maybe_poll_watched_searches(db_conn)
    saved_searches.maybe_poll_watched_searches(db_conn)

    assert len(calls) == 1


def test_maybe_poll_watched_searches_fires_again_once_interval_elapses(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    monkeypatch.setattr(config, "SAVED_SEARCH_POLL_INTERVAL", 3600)
    calls: list[int] = []
    monkeypatch.setattr(
        saved_searches, "poll_watched_saved_searches", lambda db: calls.append(1) or 0
    )

    saved_searches.maybe_poll_watched_searches(db_conn)
    saved_searches._mark_polled(db_conn, when=datetime.now(UTC) - timedelta(hours=2))
    saved_searches.maybe_poll_watched_searches(db_conn)

    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Scheduler-loop wiring (extends the existing reminder poll loop, issue #194)
# ---------------------------------------------------------------------------


@pytest.fixture()
def scheduler_db(db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    import remind_me_mcp.scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "_get_db", lambda: db_conn)
    return db_conn


def test_scheduler_loop_checks_watched_searches_each_tick(
    scheduler_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import remind_me_mcp.saved_searches as saved_searches_mod
    import remind_me_mcp.scheduler as scheduler_mod

    called = threading.Event()

    def _spy(db=None) -> int:
        called.set()
        return 0

    monkeypatch.setattr(saved_searches_mod, "maybe_poll_watched_searches", _spy)
    monkeypatch.setattr(scheduler_mod, "REMINDER_POLL_INTERVAL", 0.05)

    thread = scheduler_mod.start_scheduler()
    try:
        assert called.wait(timeout=3.0)
    finally:
        scheduler_mod.stop_scheduler(timeout=5.0)
        assert not thread.is_alive()


# ---------------------------------------------------------------------------
# Tool wrappers (remind_me_save_search / list / delete)
# ---------------------------------------------------------------------------


async def test_remind_me_save_search_tool_upserts(db_conn: sqlite3.Connection) -> None:
    from remind_me_mcp.models import SaveSearchInput
    from remind_me_mcp.tools.saved_searches import remind_me_save_search

    result = await remind_me_save_search(
        SaveSearchInput(name="tool-test", query="hello world", watch=True)
    )
    assert "tool-test" in result
    assert "watched" in result.lower()

    assert len(saved_searches.list_saved_searches(db_conn)) == 1


async def test_remind_me_list_saved_searches_tool_json(
    db_conn: sqlite3.Connection,
) -> None:
    from remind_me_mcp.models import SaveSearchInput
    from remind_me_mcp.tools.saved_searches import (
        remind_me_list_saved_searches,
        remind_me_save_search,
    )

    empty = await remind_me_list_saved_searches()
    assert "No saved searches" in empty

    await remind_me_save_search(SaveSearchInput(name="listed", query="anything"))
    result = await remind_me_list_saved_searches()
    parsed = json.loads(result)
    assert parsed[0]["name"] == "listed"


async def test_remind_me_delete_saved_search_tool(db_conn: sqlite3.Connection) -> None:
    from remind_me_mcp.models import SaveSearchInput
    from remind_me_mcp.tools.saved_searches import (
        remind_me_delete_saved_search,
        remind_me_save_search,
    )

    await remind_me_save_search(SaveSearchInput(name="deleteme", query="anything"))
    ok = await remind_me_delete_saved_search("deleteme")
    assert "deleted" in ok.lower()

    missing = await remind_me_delete_saved_search("deleteme")
    assert "not found" in missing.lower()
