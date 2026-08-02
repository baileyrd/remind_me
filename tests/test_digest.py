"""
Tests for remind_me_mcp.digest — vault digest synthesis (issue #188).

Covers digest assembly (recent additions / vitality / reminders / sync
health sections, each delegating to the same underlying function its own
standalone tool already uses), the zero-data ("nothing to report") case, the
`remind_me_digest` MCP tool wrapper, and REMIND_ME_DIGEST_INTERVAL-gated
scheduled delivery (throttling, watermark persistence across a simulated
restart, and the scheduler-loop wiring). Follows test_reminders.py's shape:
db_conn/memory_factory fixtures for an isolated in-memory database, and a
local scheduler_db fixture (mirroring test_reminders.py's) for the
thread-wiring tests.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from remind_me_mcp import config, digest, notifications
from remind_me_mcp.db import _ensure_schema, _now_iso
from remind_me_mcp.models import DigestInput, ResponseFormat

FUTURE = lambda days=1: (datetime.now(UTC) + timedelta(days=days)).isoformat()  # noqa: E731
PAST = lambda days=1: (datetime.now(UTC) - timedelta(days=days)).isoformat()  # noqa: E731


# ---------------------------------------------------------------------------
# build_digest_data — recent additions
# ---------------------------------------------------------------------------


def test_recent_additions_counts_only_memories_inside_the_window(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    memory_factory(content="added today", created_at=_now_iso())
    memory_factory(content="added yesterday", created_at=PAST(days=1))
    memory_factory(content="added long ago", created_at=PAST(days=30))

    data = digest.build_digest_data(db_conn, since_days=7)

    assert data["recent_total"] == 2
    assert {m["content"] for m in data["recent_memories"]} == {
        "added today",
        "added yesterday",
    }


def test_recent_additions_since_days_is_configurable(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    memory_factory(content="ten days ago", created_at=PAST(days=10))

    narrow = digest.build_digest_data(db_conn, since_days=7)
    wide = digest.build_digest_data(db_conn, since_days=30)

    assert narrow["recent_total"] == 0
    assert wide["recent_total"] == 1


def test_recent_additions_excludes_soft_deleted_memories(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    memory_factory(content="deleted", created_at=_now_iso(), deleted_at=_now_iso())

    data = digest.build_digest_data(db_conn, since_days=7)

    assert data["recent_total"] == 0


# ---------------------------------------------------------------------------
# build_digest_data — vitality (delegates to vitality.build_vitality_report)
# ---------------------------------------------------------------------------


def test_digest_vitality_matches_build_vitality_report(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    from remind_me_mcp.vitality import build_vitality_report

    memory_factory(content="a memory")

    data = digest.build_digest_data(db_conn)

    assert data["vitality"] == build_vitality_report(db_conn)
    assert data["vitality"]["total_memories"] == 1


# ---------------------------------------------------------------------------
# build_digest_data — reminders (delegates to reminders.list_reminders)
# ---------------------------------------------------------------------------


def test_digest_reminders_upcoming_and_overdue(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    upcoming = memory_factory(content="upcoming reminder", remind_at=FUTURE())
    overdue = memory_factory(content="overdue reminder", remind_at=PAST())
    memory_factory(content="no reminder at all")

    data = digest.build_digest_data(db_conn)

    assert [m["id"] for m in data["reminders_upcoming"]] == [upcoming["id"]]
    assert [m["id"] for m in data["reminders_overdue"]] == [overdue["id"]]


def test_digest_reminders_exclude_delivered_overdue_reminders(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    past = PAST()
    delivered = memory_factory(content="already delivered", remind_at=past)
    db_conn.execute(
        "INSERT INTO reminder_deliveries (memory_id, remind_at, delivered_at) VALUES (?, ?, ?)",
        (delivered["id"], delivered["remind_at"], _now_iso()),
    )
    db_conn.commit()

    data = digest.build_digest_data(db_conn)

    assert data["reminders_overdue"] == []


# ---------------------------------------------------------------------------
# build_digest_data — sync health (delegates to sync.get_sync_status)
# ---------------------------------------------------------------------------


def test_digest_sync_section_disabled_by_default(db_conn: sqlite3.Connection) -> None:
    """No REMIND_ME_NODE_ID/HUB_URL/SYNC_SECRET configured in the test env, so
    sync.get_sync_status() takes its disabled short-circuit and never touches
    the database -- this must work with zero sync configuration."""
    data = digest.build_digest_data(db_conn)

    assert data["sync"]["enabled"] is False
    assert "hint" in data["sync"]


# ---------------------------------------------------------------------------
# render_digest_markdown — sections and zero-data ("nothing to report")
# ---------------------------------------------------------------------------


def test_render_includes_every_section_header(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    memory_factory(content="something new")

    markdown = digest.build_digest_markdown(db_conn)

    assert "# remind_me Digest" in markdown
    assert "## Recent Additions" in markdown
    assert "## Vault Vitality" in markdown
    assert "## Reminders" in markdown
    assert "## Sync Health" in markdown


def test_render_reflects_recent_and_reminder_counts(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    memory_factory(content="fresh memory", created_at=PAST(days=30))
    memory_factory(content="upcoming reminder", remind_at=FUTURE())
    memory_factory(content="overdue reminder", remind_at=PAST())

    markdown = digest.build_digest_markdown(db_conn)

    # Only the two reminder memories (created "now" by the factory default)
    # fall inside the default 7-day window -- the one backdated to 30 days
    # ago does not.
    assert "**2** new memories added." in markdown
    assert "**Upcoming:** 1  |  **Overdue:** 1" in markdown


def test_empty_vault_produces_a_sensible_digest_not_an_exception_or_blank_string(
    db_conn: sqlite3.Connection,
) -> None:
    """A brand-new vault with no memories, reminders, or sync config must
    still produce a coherent digest -- not raise, and not an empty string."""
    markdown = digest.build_digest_markdown(db_conn)

    assert markdown  # non-empty
    assert "_No new memories in this window._" in markdown
    assert "_The vault is empty" in markdown
    assert "_No reminders set._" in markdown
    assert "_Sync disabled" in markdown


def test_render_digest_json_data_round_trips(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    memory_factory(content="a memory")

    data = digest.build_digest_data(db_conn)

    # Every section must be JSON-serializable (the tool's response_format=json
    # path dumps this dict directly).
    json.dumps(data, default=str)


def test_render_appends_hub_reconcile_verdict_when_provided(
    db_conn: sqlite3.Connection,
) -> None:
    data = digest.build_digest_data(db_conn)
    # sync disabled in the test env, so build a fake "ok" reconcile result by
    # hand to exercise the optional line without a real hub.
    data["sync"] = {**data["sync"], "enabled": True, "node_id": "n1", "hub_url": "http://hub",
                    "outbox": {"pending": 0, "drain": {"verdict": "idle"}}, "remotes": []}

    markdown = digest.render_digest_markdown(data, reconcile={"status": "ok", "verdict": "in-sync"})

    assert "hub reconcile verdict: **in-sync**" in markdown


def test_render_omits_hub_reconcile_line_when_reconcile_is_none(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    markdown = digest.build_digest_markdown(db_conn, reconcile=None)

    assert "hub reconcile verdict" not in markdown


# ---------------------------------------------------------------------------
# remind_me_digest MCP tool
# ---------------------------------------------------------------------------


async def test_remind_me_digest_tool_markdown_default(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    from remind_me_mcp.tools import remind_me_digest

    memory_factory(content="fresh addition")

    result = await remind_me_digest(DigestInput())

    assert "# remind_me Digest" in result
    assert "fresh addition" in result


async def test_remind_me_digest_tool_json_format(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    from remind_me_mcp.tools import remind_me_digest

    memory_factory(content="fresh addition")

    result = await remind_me_digest(DigestInput(response_format=ResponseFormat.JSON))

    parsed = json.loads(result)
    assert "vitality" in parsed
    assert "sync" in parsed
    assert parsed["recent_total"] == 1


async def test_remind_me_digest_tool_works_on_an_empty_vault(
    db_conn: sqlite3.Connection,
) -> None:
    from remind_me_mcp.tools import remind_me_digest

    result = await remind_me_digest(DigestInput())

    assert result
    assert "_The vault is empty" in result


def test_digest_input_since_days_bounds() -> None:
    from pydantic import ValidationError

    assert DigestInput(since_days=1).since_days == 1
    with pytest.raises(ValidationError):
        DigestInput(since_days=0)
    with pytest.raises(ValidationError):
        DigestInput(since_days=366)


# ---------------------------------------------------------------------------
# Scheduled delivery: REMIND_ME_DIGEST_INTERVAL-gated throttling
# ---------------------------------------------------------------------------


def test_maybe_send_scheduled_digest_is_a_noop_when_disabled(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "DIGEST_INTERVAL_SECONDS", None)
    calls: list[tuple] = []
    monkeypatch.setattr(notifications, "notify", lambda *a: calls.append(a))

    def _must_not_be_called() -> sqlite3.Connection:
        raise AssertionError("disabled digest must never touch the database")

    monkeypatch.setattr(digest, "_get_db", _must_not_be_called)

    sent = digest.maybe_send_scheduled_digest()

    assert sent is False
    assert calls == []


def test_maybe_send_scheduled_digest_sends_once_then_throttles_within_the_interval(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "DIGEST_INTERVAL_SECONDS", 3600)
    calls: list[str] = []
    monkeypatch.setattr(notifications, "notify", lambda subject, body: calls.append(subject))

    first = digest.maybe_send_scheduled_digest(db_conn)
    second = digest.maybe_send_scheduled_digest(db_conn)

    assert first is True
    assert second is False
    assert len(calls) == 1
    assert calls[0] == "remind_me: digest"


def test_maybe_send_scheduled_digest_fires_again_once_the_interval_elapses(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "DIGEST_INTERVAL_SECONDS", 3600)
    calls: list[str] = []
    monkeypatch.setattr(notifications, "notify", lambda subject, body: calls.append(subject))

    digest.maybe_send_scheduled_digest(db_conn)
    # Backdate the watermark past the interval instead of sleeping an hour.
    digest._mark_digest_sent(db_conn, when=datetime.now(UTC) - timedelta(hours=2))

    second = digest.maybe_send_scheduled_digest(db_conn)

    assert second is True
    assert len(calls) == 2


def test_digest_watermark_persists_across_a_simulated_restart(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """digest.py keeps no in-memory record of the last send (unlike
    maintenance._due's in-process timer dict) -- the only state is the
    ``sync_flags`` row, so re-reading it via a brand-new connection (mirroring
    a freshly reopened db file after a process restart) must see the same
    throttle decision a live process would."""
    monkeypatch.setattr(config, "DIGEST_INTERVAL_SECONDS", 3600)
    monkeypatch.setattr(notifications, "notify", lambda *a: None)

    assert digest.maybe_send_scheduled_digest(db_conn) is True

    row = db_conn.execute(
        "SELECT value FROM sync_flags WHERE key = 'digest_last_sent_at'"
    ).fetchone()
    assert row is not None

    fresh = sqlite3.connect(":memory:")
    fresh.row_factory = sqlite3.Row
    _ensure_schema(fresh)
    fresh.execute(
        "INSERT INTO sync_flags (key, value) VALUES ('digest_last_sent_at', ?)",
        (row["value"],),
    )
    fresh.commit()

    # A fresh connection carrying only the persisted watermark still reports
    # "not due yet" -- the throttle decision survives independent of any
    # Python-level object from the sending process.
    assert digest.is_digest_due(fresh, 3600) is False
    fresh.close()


def test_is_digest_due_fires_on_first_ever_check(db_conn: sqlite3.Connection) -> None:
    """No watermark yet (a freshly-enabled interval) fires immediately rather
    than waiting a full interval for the first digest."""
    assert digest.is_digest_due(db_conn, 3600) is True


def test_is_digest_due_false_when_interval_is_none() -> None:
    assert digest.is_digest_due(sqlite3.connect(":memory:"), None) is False


# ---------------------------------------------------------------------------
# Scheduler-loop wiring (extends the existing reminder poll loop, issue #188)
# ---------------------------------------------------------------------------


@pytest.fixture()
def scheduler_db(db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    """Point remind_me_mcp.scheduler at the shared in-memory test database
    (mirrors test_reminders.py's fixture of the same name)."""
    import remind_me_mcp.scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "_get_db", lambda: db_conn)
    return db_conn


def test_scheduler_loop_checks_for_a_due_digest_each_tick(
    scheduler_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The digest check rides the existing reminder-poll thread (design
    decision: no second background thread) -- assert the loop actually calls
    it, using a short poll interval and a real (bounded-wait) thread rather
    than a sleep guess."""
    import remind_me_mcp.digest as digest_mod
    import remind_me_mcp.scheduler as scheduler_mod

    called = threading.Event()

    def _spy() -> bool:
        called.set()
        return False

    monkeypatch.setattr(digest_mod, "maybe_send_scheduled_digest", _spy)
    monkeypatch.setattr(scheduler_mod, "REMINDER_POLL_INTERVAL", 0.05)

    thread = scheduler_mod.start_scheduler()
    try:
        assert called.wait(timeout=3.0)
    finally:
        scheduler_mod.stop_scheduler(timeout=5.0)
        assert not thread.is_alive()


def test_scheduler_never_calls_notify_for_a_digest_when_interval_is_disabled(
    scheduler_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REMIND_ME_DIGEST_INTERVAL="" (default) must mean the scheduler never
    fires a digest notification, even while its ordinary reminder polling
    keeps running every tick."""
    import remind_me_mcp.scheduler as scheduler_mod

    monkeypatch.setattr(config, "DIGEST_INTERVAL_SECONDS", None)
    monkeypatch.setattr(scheduler_mod, "REMINDER_POLL_INTERVAL", 0.05)
    calls: list[tuple] = []
    monkeypatch.setattr(notifications, "notify", lambda *a: calls.append(a))

    thread = scheduler_mod.start_scheduler()
    try:
        time.sleep(0.3)  # several ticks at the 0.05s poll interval
    finally:
        scheduler_mod.stop_scheduler(timeout=5.0)
        assert not thread.is_alive()

    assert calls == []
