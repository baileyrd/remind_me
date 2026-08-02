"""
Tests for time-based reminders (issue #179).

Covers the v22->v23 schema migration (remind_at column, reminder_deliveries
table, partial index), remind_me_set_reminder/remind_me_list_reminders (the
tools/reminders.py handlers), and remind_me_mcp.scheduler's due-reminder
poll logic. Follows test_tombstones.py's shape: db_conn fixture for an
isolated in-memory database, memory_factory for fixture rows, and the
scheduler's poll_once() is exercised directly rather than through a real
sleep-based thread loop.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from remind_me_mcp.db import _ensure_schema, _now_iso
from remind_me_mcp.models import ListRemindersInput, ReminderWindow, SetReminderInput
from remind_me_mcp.tools import remind_me_list_reminders, remind_me_set_reminder

FUTURE = lambda days=1: (datetime.now(UTC) + timedelta(days=days)).isoformat()  # noqa: E731
PAST = lambda days=1: (datetime.now(UTC) - timedelta(days=days)).isoformat()  # noqa: E731

# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


def test_v22_to_v23_adds_remind_at_column(db_conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in db_conn.execute("PRAGMA table_info(memories)").fetchall()}
    assert "remind_at" in cols


def test_v22_to_v23_adds_reminder_deliveries_table(db_conn: sqlite3.Connection) -> None:
    tables = {
        r["name"]
        for r in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert "reminder_deliveries" in tables
    cols = {r["name"] for r in db_conn.execute("PRAGMA table_info(reminder_deliveries)").fetchall()}
    assert cols == {"id", "memory_id", "remind_at", "delivered_at"}


def test_v22_to_v23_adds_partial_index(db_conn: sqlite3.Connection) -> None:
    row = db_conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'idx_memories_remind_at'"
    ).fetchone()
    assert row is not None
    assert "remind_at IS NOT NULL" in row["sql"]
    assert "deleted_at IS NULL" in row["sql"]


def test_v22_to_v23_is_idempotent() -> None:
    """Running the migration on an already-migrated DB (or twice) doesn't error."""
    from remind_me_mcp.db import _migrate_schema

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    _ensure_schema(db)
    _migrate_schema(db)  # second run — must be a safe no-op
    cols = {r["name"] for r in db.execute("PRAGMA table_info(memories)").fetchall()}
    assert "remind_at" in cols
    db.close()


def test_remind_at_rides_the_update_outbox_trigger(db_conn: sqlite3.Connection) -> None:
    """A remind_at UPDATE produces a normal outbox row carrying it in the
    payload -- it's a real content field, not access-tracking metadata."""
    import json

    db_conn.execute(
        "INSERT OR REPLACE INTO sync_flags (key, value) VALUES ('sync_enabled', '1')"
    )
    now = _now_iso()
    db_conn.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
           VALUES ('m1', 'content', 'general', '[]', 'manual', '{}', ?, ?)""",
        (now, now),
    )
    db_conn.commit()

    later = _now_iso()
    remind_at = FUTURE()
    db_conn.execute(
        "UPDATE memories SET remind_at = ?, updated_at = ? WHERE id = 'm1'",
        (remind_at, later),
    )
    db_conn.commit()

    row = db_conn.execute(
        "SELECT operation, payload FROM sync_outbox WHERE memory_id = 'm1' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row["operation"] == "update"
    payload = json.loads(row["payload"])
    assert payload["remind_at"] == remind_at


# ---------------------------------------------------------------------------
# SetReminderInput validation
# ---------------------------------------------------------------------------


def test_set_reminder_input_rejects_past_timestamp() -> None:
    with pytest.raises(ValidationError):
        SetReminderInput(memory_id="m1", remind_at=PAST())


def test_set_reminder_input_rejects_unparseable_timestamp() -> None:
    with pytest.raises(ValidationError):
        SetReminderInput(memory_id="m1", remind_at="not-a-timestamp")


def test_set_reminder_input_accepts_future_timestamp() -> None:
    future = FUTURE()
    params = SetReminderInput(memory_id="m1", remind_at=future)
    assert params.remind_at is not None


def test_set_reminder_input_defaults_to_none() -> None:
    params = SetReminderInput(memory_id="m1")
    assert params.remind_at is None


# ---------------------------------------------------------------------------
# remind_me_set_reminder
# ---------------------------------------------------------------------------


async def test_set_reminder_sets_remind_at(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    mem = memory_factory(content="call mom back")
    future = FUTURE()

    result = await remind_me_set_reminder(SetReminderInput(memory_id=mem["id"], remind_at=future))

    assert "set" in result.lower()
    row = db_conn.execute("SELECT remind_at FROM memories WHERE id = ?", (mem["id"],)).fetchone()
    assert row["remind_at"] == future


async def test_set_reminder_bumps_updated_at(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    mem = memory_factory(content="renew passport")
    before = db_conn.execute(
        "SELECT updated_at FROM memories WHERE id = ?", (mem["id"],)
    ).fetchone()["updated_at"]

    await remind_me_set_reminder(SetReminderInput(memory_id=mem["id"], remind_at=FUTURE()))

    after = db_conn.execute(
        "SELECT updated_at FROM memories WHERE id = ?", (mem["id"],)
    ).fetchone()["updated_at"]
    assert after > before


async def test_set_reminder_clears_existing_reminder(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    mem = memory_factory(content="pay rent", remind_at=FUTURE())

    result = await remind_me_set_reminder(SetReminderInput(memory_id=mem["id"], remind_at=None))

    assert "cleared" in result.lower()
    row = db_conn.execute("SELECT remind_at FROM memories WHERE id = ?", (mem["id"],)).fetchone()
    assert row["remind_at"] is None


async def test_set_reminder_unknown_memory_id_reports_not_found(
    db_conn: sqlite3.Connection,
) -> None:
    result = await remind_me_set_reminder(
        SetReminderInput(memory_id="does-not-exist", remind_at=FUTURE())
    )
    assert "not found" in result.lower()


async def test_set_reminder_excludes_soft_deleted_memory(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    mem = memory_factory(content="deleted memory", deleted_at=_now_iso())

    result = await remind_me_set_reminder(SetReminderInput(memory_id=mem["id"], remind_at=FUTURE()))

    assert "not found" in result.lower()


# ---------------------------------------------------------------------------
# remind_me_list_reminders
# ---------------------------------------------------------------------------


async def test_list_reminders_upcoming(db_conn: sqlite3.Connection, memory_factory) -> None:
    upcoming = memory_factory(content="upcoming reminder", remind_at=FUTURE())
    overdue = memory_factory(content="overdue reminder", remind_at=PAST())
    memory_factory(content="no reminder at all")

    result = await remind_me_list_reminders(ListRemindersInput(when=ReminderWindow.UPCOMING))

    assert upcoming["id"] in result
    assert overdue["id"] not in result


async def test_list_reminders_overdue_excludes_delivered(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    past = PAST()
    delivered = memory_factory(content="already delivered", remind_at=past)
    missed = memory_factory(content="missed while offline", remind_at=past)
    db_conn.execute(
        "INSERT INTO reminder_deliveries (memory_id, remind_at, delivered_at) VALUES (?, ?, ?)",
        (delivered["id"], delivered["remind_at"], _now_iso()),
    )
    db_conn.commit()

    result = await remind_me_list_reminders(ListRemindersInput(when=ReminderWindow.OVERDUE))

    assert missed["id"] in result
    assert delivered["id"] not in result


async def test_list_reminders_overdue_excludes_future(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    memory_factory(content="future reminder", remind_at=FUTURE())

    result = await remind_me_list_reminders(ListRemindersInput(when=ReminderWindow.OVERDUE))

    assert "future reminder" not in result


async def test_list_reminders_all_is_union(db_conn: sqlite3.Connection, memory_factory) -> None:
    past = PAST()
    upcoming = memory_factory(content="union upcoming", remind_at=FUTURE())
    missed = memory_factory(content="union overdue", remind_at=past)
    delivered = memory_factory(content="union delivered", remind_at=past)
    db_conn.execute(
        "INSERT INTO reminder_deliveries (memory_id, remind_at, delivered_at) VALUES (?, ?, ?)",
        (delivered["id"], delivered["remind_at"], _now_iso()),
    )
    db_conn.commit()

    result = await remind_me_list_reminders(ListRemindersInput(when=ReminderWindow.ALL))

    assert upcoming["id"] in result
    assert missed["id"] in result
    assert delivered["id"] not in result


async def test_list_reminders_ignores_memories_without_a_reminder(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    memory_factory(content="plain memory, no reminder")

    result = await remind_me_list_reminders(ListRemindersInput(when=ReminderWindow.ALL))

    assert result == "_No memories found._"


async def test_list_reminders_json_format(db_conn: sqlite3.Connection, memory_factory) -> None:
    import json

    from remind_me_mcp.models import ResponseFormat

    memory_factory(content="json reminder", remind_at=FUTURE())

    result = await remind_me_list_reminders(
        ListRemindersInput(when=ReminderWindow.UPCOMING, response_format=ResponseFormat.JSON)
    )
    parsed = json.loads(result)
    assert parsed["count"] == 1


# ---------------------------------------------------------------------------
# scheduler.poll_once
# ---------------------------------------------------------------------------


@pytest.fixture()
def scheduler_db(db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    """Point remind_me_mcp.scheduler at the shared in-memory test database."""
    import remind_me_mcp.scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "_get_db", lambda: db_conn)
    return db_conn


def test_poll_once_delivers_due_reminder_and_records_delivery(
    scheduler_db: sqlite3.Connection, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import remind_me_mcp.scheduler as scheduler_mod

    delivered_ids: list[str] = []
    monkeypatch.setattr(scheduler_mod, "_delivery_hook", lambda m: delivered_ids.append(m["id"]))

    mem = memory_factory(content="due now", remind_at=PAST(days=0.001))

    count = scheduler_mod.poll_once()

    assert count == 1
    assert delivered_ids == [mem["id"]]
    row = scheduler_db.execute(
        "SELECT * FROM reminder_deliveries WHERE memory_id = ?", (mem["id"],)
    ).fetchone()
    assert row is not None
    assert row["remind_at"] == mem["remind_at"]


def test_poll_once_does_not_refire_an_already_delivered_reminder(
    scheduler_db: sqlite3.Connection, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import remind_me_mcp.scheduler as scheduler_mod

    delivered_ids: list[str] = []
    monkeypatch.setattr(scheduler_mod, "_delivery_hook", lambda m: delivered_ids.append(m["id"]))

    memory_factory(content="fires once", remind_at=PAST(days=0.001))

    first = scheduler_mod.poll_once()
    second = scheduler_mod.poll_once()

    assert first == 1
    assert second == 0
    assert len(delivered_ids) == 1


def test_poll_once_ignores_future_reminders(
    scheduler_db: sqlite3.Connection, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import remind_me_mcp.scheduler as scheduler_mod

    delivered_ids: list[str] = []
    monkeypatch.setattr(scheduler_mod, "_delivery_hook", lambda m: delivered_ids.append(m["id"]))

    memory_factory(content="not due yet", remind_at=FUTURE())

    count = scheduler_mod.poll_once()

    assert count == 0
    assert delivered_ids == []


def test_poll_once_ignores_soft_deleted_memory(
    scheduler_db: sqlite3.Connection, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import remind_me_mcp.scheduler as scheduler_mod

    delivered_ids: list[str] = []
    monkeypatch.setattr(scheduler_mod, "_delivery_hook", lambda m: delivered_ids.append(m["id"]))

    memory_factory(
        content="deleted but overdue", remind_at=PAST(days=0.001), deleted_at=_now_iso()
    )

    count = scheduler_mod.poll_once()

    assert count == 0
    assert delivered_ids == []


def test_poll_once_delivers_multiple_due_reminders_independently(
    scheduler_db: sqlite3.Connection, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import remind_me_mcp.scheduler as scheduler_mod

    delivered_ids: list[str] = []
    monkeypatch.setattr(scheduler_mod, "_delivery_hook", lambda m: delivered_ids.append(m["id"]))

    mem_a = memory_factory(content="first due", remind_at=PAST(days=0.002))
    mem_b = memory_factory(content="second due", remind_at=PAST(days=0.001))

    count = scheduler_mod.poll_once()

    assert count == 2
    assert set(delivered_ids) == {mem_a["id"], mem_b["id"]}


def test_default_delivery_hook_logs_and_truncates_long_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The default (log-only) delivery hook -- issue #180's seam -- logs at
    INFO and truncates a long content preview rather than dumping it whole."""
    import remind_me_mcp.scheduler as scheduler_mod

    long_content = "x" * 500
    with caplog.at_level("INFO", logger="remind_me_mcp.scheduler"):
        scheduler_mod._log_delivery({"id": "m1", "remind_at": PAST(), "content": long_content})

    assert any("Reminder due" in r.message for r in caplog.records)
    assert not any(long_content in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# start_scheduler / stop_scheduler thread lifecycle
# ---------------------------------------------------------------------------


def test_scheduler_thread_start_stop_joins_promptly(monkeypatch: pytest.MonkeyPatch) -> None:
    """start_scheduler() spawns the loop thread; stop_scheduler() joins it
    promptly, mirroring watcher.py's FolderWatcher.start/stop test."""
    import time

    import remind_me_mcp.scheduler as scheduler_mod

    def _unreachable_get_db() -> sqlite3.Connection:
        raise AssertionError("poll should not run again before stop_scheduler")

    monkeypatch.setattr(scheduler_mod, "REMINDER_POLL_INTERVAL", 3600)  # never wakes on its own
    monkeypatch.setattr(scheduler_mod, "_get_db", _unreachable_get_db)

    thread = scheduler_mod.start_scheduler()
    try:
        assert thread.is_alive()
        # Idempotent start returns the same running thread.
        assert scheduler_mod.start_scheduler() is thread
    finally:
        started = time.monotonic()
        scheduler_mod.stop_scheduler(timeout=10.0)
        elapsed = time.monotonic() - started
        assert not thread.is_alive()
        assert elapsed < 5.0
