"""
Tests for edit history / revisions (issue #187).

Covers the v23->v24 schema migration (memory_revisions table + index),
the revision-snapshot logic in tools/crud.py's _apply_memory_field_update
(the shared choke point remind_me_update/remind_me_revert/
remind_me_set_reminder all funnel through), the remind_me_history and
remind_me_revert tool handlers (tools/history.py), and db._compact_revisions
retention, including that it is actually invoked from the reminder
scheduler's periodic loop. Follows test_tombstones.py/test_reminders.py's
shape: db_conn fixture for an isolated in-memory database, memory_factory
for fixture rows.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest  # noqa: TC002  # used at runtime via pytest.MonkeyPatch annotations across this module

from remind_me_mcp.db import _ensure_schema
from remind_me_mcp.models import (
    MemoryUpdateInput,
    RevertInput,
    RevisionHistoryInput,
    SetReminderInput,
)
from remind_me_mcp.tools import (
    memory_update,
    remind_me_history,
    remind_me_revert,
    remind_me_set_reminder,
)

# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


def test_v23_to_v24_adds_memory_revisions_table(db_conn: sqlite3.Connection) -> None:
    tables = {
        r["name"]
        for r in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert "memory_revisions" in tables
    cols = {r["name"] for r in db_conn.execute("PRAGMA table_info(memory_revisions)").fetchall()}
    assert cols == {
        "id", "memory_id", "content", "category", "tags", "metadata",
        "edited_at", "revision_reason",
    }


def test_v23_to_v24_adds_index(db_conn: sqlite3.Connection) -> None:
    row = db_conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' "
        "AND name = 'idx_memory_revisions_memory_edited'"
    ).fetchone()
    assert row is not None
    assert "memory_id" in row["sql"]
    assert "edited_at" in row["sql"]


def test_v23_to_v24_is_idempotent() -> None:
    """Running the migration on an already-migrated DB (or twice) doesn't error."""
    from remind_me_mcp.db import _migrate_schema

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    _ensure_schema(db)
    _migrate_schema(db)  # second run — must be a safe no-op
    cols = {r["name"] for r in db.execute("PRAGMA table_info(memory_revisions)").fetchall()}
    assert "content" in cols
    db.close()


def test_memory_revisions_not_in_outbox_payload_columns() -> None:
    """memory_revisions is a local-only audit table, like reminder_deliveries
    -- it must never ride the sync outbox trigger mechanism."""
    from remind_me_mcp.db import _OUTBOX_PAYLOAD_COLUMNS

    assert "memory_revisions" not in _OUTBOX_PAYLOAD_COLUMNS
    # Only memories columns belong in this tuple at all — a stronger
    # assertion than "not present" would be brittle against unrelated column
    # additions, so this just confirms no revision-table leakage occurred.


def test_memory_revisions_has_no_outbox_trigger(db_conn: sqlite3.Connection) -> None:
    triggers = {
        r["name"]
        for r in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
    }
    assert not any("memory_revisions" in t for t in triggers)


# ---------------------------------------------------------------------------
# memory_update creates a revision snapshot (tools/crud.py choke point)
# ---------------------------------------------------------------------------


async def test_memory_update_creates_revision_snapshot(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    mem = memory_factory(content="original content", category="general")
    await memory_update(
        MemoryUpdateInput(memory_id=mem["id"], content="edited content")
    )

    rows = db_conn.execute(
        "SELECT * FROM memory_revisions WHERE memory_id = ?", (mem["id"],)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["content"] == "original content"
    assert rows[0]["category"] == "general"
    assert rows[0]["edited_at"] is not None


async def test_memory_update_noop_creates_no_revision(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """Passing the same content back through remind_me_update must not
    create a spurious revision -- mirrors migration v22's "only sync on
    genuine content change" discipline."""
    mem = memory_factory(content="unchanged content")
    await memory_update(
        MemoryUpdateInput(memory_id=mem["id"], content="unchanged content")
    )

    count = db_conn.execute(
        "SELECT COUNT(*) FROM memory_revisions WHERE memory_id = ?", (mem["id"],)
    ).fetchone()[0]
    assert count == 0


async def test_memory_update_category_only_change_creates_revision(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    mem = memory_factory(content="same content", category="general")
    await memory_update(MemoryUpdateInput(memory_id=mem["id"], category="work"))

    row = db_conn.execute(
        "SELECT content, category FROM memory_revisions WHERE memory_id = ?", (mem["id"],)
    ).fetchone()
    assert row is not None
    assert row["category"] == "general"  # pre-edit value captured


async def test_memory_update_clear_superseded_only_creates_no_revision(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """superseded_by is out of scope for revisions -- it's not one of the
    fields remind_me_update's tracked columns cover."""
    mem = memory_factory(content="a fact", superseded_by="other-id")
    await memory_update(MemoryUpdateInput(memory_id=mem["id"], clear_superseded=True))

    count = db_conn.execute(
        "SELECT COUNT(*) FROM memory_revisions WHERE memory_id = ?", (mem["id"],)
    ).fetchone()[0]
    assert count == 0


async def test_multiple_edits_create_multiple_revisions_newest_last_in_table(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    mem = memory_factory(content="v1")
    await memory_update(MemoryUpdateInput(memory_id=mem["id"], content="v2"))
    await memory_update(MemoryUpdateInput(memory_id=mem["id"], content="v3"))

    rows = db_conn.execute(
        "SELECT content FROM memory_revisions WHERE memory_id = ? ORDER BY id", (mem["id"],)
    ).fetchall()
    assert [r["content"] for r in rows] == ["v1", "v2"]

    current = db_conn.execute(
        "SELECT content FROM memories WHERE id = ?", (mem["id"],)
    ).fetchone()
    assert current["content"] == "v3"


# ---------------------------------------------------------------------------
# remind_me_set_reminder does NOT create a revision (judgment call)
# ---------------------------------------------------------------------------


async def test_set_reminder_creates_no_revision(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """remind_me_set_reminder funnels through the same shared
    _apply_memory_field_update choke point as remind_me_update, but only
    ever touches remind_at -- which is not a revision-tracked column -- so
    it must never produce a memory_revisions row."""
    mem = memory_factory(content="reminder target")
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    await remind_me_set_reminder(SetReminderInput(memory_id=mem["id"], remind_at=future))

    count = db_conn.execute(
        "SELECT COUNT(*) FROM memory_revisions WHERE memory_id = ?", (mem["id"],)
    ).fetchone()[0]
    assert count == 0


# ---------------------------------------------------------------------------
# remind_me_history
# ---------------------------------------------------------------------------


async def test_remind_me_history_returns_revisions_newest_first(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    mem = memory_factory(content="first")
    await memory_update(MemoryUpdateInput(memory_id=mem["id"], content="second"))
    await memory_update(MemoryUpdateInput(memory_id=mem["id"], content="third"))

    from remind_me_mcp.models import ResponseFormat

    result = await remind_me_history(
        RevisionHistoryInput(memory_id=mem["id"], response_format=ResponseFormat.JSON)
    )
    data = json.loads(result)
    assert data["count"] == 2
    contents = [r["content"] for r in data["revisions"]]
    # newest-first: the revision capturing "second" (the more recent pre-edit
    # state) comes before the one capturing "first".
    assert contents == ["second", "first"]


async def test_remind_me_history_respects_limit(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    from remind_me_mcp.models import ResponseFormat

    mem = memory_factory(content="v0")
    for i in range(1, 5):
        await memory_update(MemoryUpdateInput(memory_id=mem["id"], content=f"v{i}"))

    result = await remind_me_history(
        RevisionHistoryInput(
            memory_id=mem["id"], limit=2, response_format=ResponseFormat.JSON
        )
    )
    data = json.loads(result)
    assert data["count"] == 2


async def test_remind_me_history_unknown_memory_reports_not_found() -> None:
    result = await remind_me_history(RevisionHistoryInput(memory_id="nonexistent"))
    assert "not found" in result.lower()


async def test_remind_me_history_markdown_shows_content_preview(
    memory_factory,
) -> None:
    mem = memory_factory(content="original")
    await memory_update(MemoryUpdateInput(memory_id=mem["id"], content="changed"))

    result = await remind_me_history(RevisionHistoryInput(memory_id=mem["id"]))
    assert "original" in result
    assert "Revision" in result


# ---------------------------------------------------------------------------
# remind_me_revert
# ---------------------------------------------------------------------------


async def test_remind_me_revert_restores_prior_content(
    db_conn: sqlite3.Connection, memory_factory, mock_embedder
) -> None:
    mem = memory_factory(content="original content")
    await memory_update(MemoryUpdateInput(memory_id=mem["id"], content="bad edit"))

    rev_id = db_conn.execute(
        "SELECT id FROM memory_revisions WHERE memory_id = ?", (mem["id"],)
    ).fetchone()["id"]

    result = await remind_me_revert(
        RevertInput(memory_id=mem["id"], revision_id=rev_id)
    )
    assert "reverted" in result.lower()

    row = db_conn.execute(
        "SELECT content FROM memories WHERE id = ?", (mem["id"],)
    ).fetchone()
    assert row["content"] == "original content"


async def test_remind_me_revert_bumps_updated_at_and_enters_outbox(
    db_conn: sqlite3.Connection, memory_factory, mock_embedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A revert rides the normal update path -- same LWW/outbox behavior as
    any other edit, per the issue's explicit requirement."""
    import remind_me_mcp.tools.crud as crud_mod

    monkeypatch.setattr(crud_mod, "SYNC_ENABLED", True)
    db_conn.execute(
        "INSERT OR REPLACE INTO sync_flags (key, value) VALUES ('sync_enabled', '1')"
    )
    db_conn.commit()

    mem = memory_factory(content="original content")
    before_updated_at = mem["updated_at"]
    await memory_update(MemoryUpdateInput(memory_id=mem["id"], content="bad edit"))

    rev_id = db_conn.execute(
        "SELECT id FROM memory_revisions WHERE memory_id = ?", (mem["id"],)
    ).fetchone()["id"]

    await remind_me_revert(RevertInput(memory_id=mem["id"], revision_id=rev_id))

    row = db_conn.execute(
        "SELECT updated_at FROM memories WHERE id = ?", (mem["id"],)
    ).fetchone()
    assert row["updated_at"] > before_updated_at

    outbox_row = db_conn.execute(
        "SELECT operation, payload FROM sync_outbox WHERE memory_id = ? ORDER BY id DESC LIMIT 1",
        (mem["id"],),
    ).fetchone()
    assert outbox_row is not None
    assert outbox_row["operation"] == "update"
    payload = json.loads(outbox_row["payload"])
    assert payload["content"] == "original content"


async def test_remind_me_revert_creates_new_revision_of_pre_revert_state(
    db_conn: sqlite3.Connection, memory_factory, mock_embedder
) -> None:
    """A revert is itself an edit -- it must snapshot the state just before
    the revert, so the revert can itself be undone."""
    mem = memory_factory(content="original content")
    await memory_update(MemoryUpdateInput(memory_id=mem["id"], content="bad edit"))

    rev_id = db_conn.execute(
        "SELECT id FROM memory_revisions WHERE memory_id = ?", (mem["id"],)
    ).fetchone()["id"]

    await remind_me_revert(RevertInput(memory_id=mem["id"], revision_id=rev_id))

    rows = db_conn.execute(
        "SELECT content FROM memory_revisions WHERE memory_id = ? ORDER BY id", (mem["id"],)
    ).fetchall()
    # First revision: pre-"bad edit" state ("original content").
    # Second revision: pre-revert state ("bad edit"), created by the revert.
    assert [r["content"] for r in rows] == ["original content", "bad edit"]

    # And the revert itself is now undoable: reverting to the second
    # revision (the pre-revert "bad edit" state) restores that content.
    second_rev_id = db_conn.execute(
        "SELECT id FROM memory_revisions WHERE memory_id = ? AND content = 'bad edit'",
        (mem["id"],),
    ).fetchone()["id"]
    await remind_me_revert(RevertInput(memory_id=mem["id"], revision_id=second_rev_id))
    current = db_conn.execute(
        "SELECT content FROM memories WHERE id = ?", (mem["id"],)
    ).fetchone()
    assert current["content"] == "bad edit"


async def test_remind_me_revert_unknown_revision_id_fails_cleanly(
    memory_factory,
) -> None:
    mem = memory_factory(content="content")
    result = await remind_me_revert(RevertInput(memory_id=mem["id"], revision_id=999999))
    assert "not found" in result.lower()


async def test_remind_me_revert_wrong_memory_id_fails_cleanly(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """A revision_id that exists but belongs to a *different* memory must be
    rejected, not silently applied cross-memory."""
    mem_a = memory_factory(content="memory A original")
    mem_b = memory_factory(content="memory B original")
    await memory_update(MemoryUpdateInput(memory_id=mem_a["id"], content="memory A edited"))

    rev_id = db_conn.execute(
        "SELECT id FROM memory_revisions WHERE memory_id = ?", (mem_a["id"],)
    ).fetchone()["id"]

    result = await remind_me_revert(RevertInput(memory_id=mem_b["id"], revision_id=rev_id))
    assert "not found" in result.lower()

    # memory B is untouched.
    row = db_conn.execute("SELECT content FROM memories WHERE id = ?", (mem_b["id"],)).fetchone()
    assert row["content"] == "memory B original"


async def test_remind_me_revert_nonexistent_memory_fails_cleanly() -> None:
    result = await remind_me_revert(RevertInput(memory_id="nonexistent", revision_id=1))
    assert "not found" in result.lower()


async def test_remind_me_revert_records_reason(
    db_conn: sqlite3.Connection, memory_factory, mock_embedder
) -> None:
    mem = memory_factory(content="original")
    await memory_update(MemoryUpdateInput(memory_id=mem["id"], content="oops"))
    rev_id = db_conn.execute(
        "SELECT id FROM memory_revisions WHERE memory_id = ?", (mem["id"],)
    ).fetchone()["id"]

    await remind_me_revert(
        RevertInput(memory_id=mem["id"], revision_id=rev_id, reason="undoing a typo")
    )

    new_rev = db_conn.execute(
        "SELECT revision_reason FROM memory_revisions WHERE memory_id = ? AND content = 'oops'",
        (mem["id"],),
    ).fetchone()
    assert new_rev["revision_reason"] == "undoing a typo"


# ---------------------------------------------------------------------------
# _compact_revisions retention
# ---------------------------------------------------------------------------


def _insert_revision(
    db: sqlite3.Connection, memory_id: str, edited_at: str, content: str = "old"
) -> None:
    db.execute(
        """INSERT INTO memory_revisions (memory_id, content, category, tags, metadata, edited_at)
           VALUES (?, ?, 'general', '[]', '{}', ?)""",
        (memory_id, content, edited_at),
    )
    db.commit()


def test_compact_revisions_prunes_old_rows(
    db_conn: sqlite3.Connection, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import remind_me_mcp.db as db_mod

    monkeypatch.setattr(db_mod, "REVISION_RETENTION_DAYS", 30)

    mem = memory_factory(content="current")
    old_ts = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    _insert_revision(db_conn, mem["id"], old_ts, content="ancient revision")

    removed = db_mod._compact_revisions(db_conn)
    assert removed == 1
    count = db_conn.execute(
        "SELECT COUNT(*) FROM memory_revisions WHERE memory_id = ?", (mem["id"],)
    ).fetchone()[0]
    assert count == 0


def test_compact_revisions_keeps_recent_rows(
    db_conn: sqlite3.Connection, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import remind_me_mcp.db as db_mod

    monkeypatch.setattr(db_mod, "REVISION_RETENTION_DAYS", 90)

    mem = memory_factory(content="current")
    recent_ts = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    _insert_revision(db_conn, mem["id"], recent_ts, content="recent revision")

    removed = db_mod._compact_revisions(db_conn)
    assert removed == 0
    count = db_conn.execute(
        "SELECT COUNT(*) FROM memory_revisions WHERE memory_id = ?", (mem["id"],)
    ).fetchone()[0]
    assert count == 1


def test_compact_revisions_mixed_ages_only_prunes_old(
    db_conn: sqlite3.Connection, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import remind_me_mcp.db as db_mod

    monkeypatch.setattr(db_mod, "REVISION_RETENTION_DAYS", 30)

    mem = memory_factory(content="current")
    old_ts = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    recent_ts = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    _insert_revision(db_conn, mem["id"], old_ts, content="old one")
    _insert_revision(db_conn, mem["id"], recent_ts, content="recent one")

    removed = db_mod._compact_revisions(db_conn)
    assert removed == 1
    remaining = db_conn.execute(
        "SELECT content FROM memory_revisions WHERE memory_id = ?", (mem["id"],)
    ).fetchall()
    assert [r["content"] for r in remaining] == ["recent one"]


def test_compact_revisions_invoked_from_scheduler_loop(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integration-level check: the always-on reminder scheduler loop --
    not sync.py's SYNC_ENABLED-gated loop -- is what calls _compact_revisions,
    since revisions accumulate regardless of whether sync is configured.
    Follows test_digest.py's scheduler-loop-wiring test shape: a short poll
    interval and a real (bounded-wait) thread rather than a sleep guess."""
    import threading as _threading

    import remind_me_mcp.scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "_get_db", lambda: db_conn)
    called = _threading.Event()

    def _spy(db):
        called.set()
        return 0

    monkeypatch.setattr(scheduler_mod, "_compact_revisions", _spy)
    monkeypatch.setattr(scheduler_mod, "REMINDER_POLL_INTERVAL", 0.05)

    thread = scheduler_mod.start_scheduler()
    try:
        assert called.wait(timeout=3.0)
    finally:
        scheduler_mod.stop_scheduler(timeout=5.0)
        assert not thread.is_alive()
