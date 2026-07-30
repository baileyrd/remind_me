"""
Tests for remind_me_undo_import — rolling back a bulk import.

The operation is destructive and, on a sync-enabled node, propagates to every
other node, so the cases that matter most here are the safety properties:
dry-run changes nothing, derived rows do not survive the parent memory, and
the import-tracking rows are cleared so the same content can be imported
again.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    import sqlite3

import remind_me_mcp.tools.admin as admin_mod
from remind_me_mcp.db import _now_iso
from remind_me_mcp.models import UndoImportInput, UndoImportKind
from remind_me_mcp.tools.admin import remind_me_undo_import


def _add_memory(
    db: sqlite3.Connection,
    mem_id: str,
    *,
    category: str = "mempalace_import",
    doc_id: str | None = None,
) -> int:
    now = _now_iso()
    db.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata,
                                 created_at, updated_at, doc_id)
           VALUES (?, ?, ?, '[]', 'manual', '{}', ?, ?, ?)""",
        (mem_id, f"content for {mem_id}", category, now, now, doc_id),
    )
    db.commit()
    return int(
        db.execute("SELECT rowid FROM memories WHERE id = ?", (mem_id,)).fetchone()[0]
    )


def _track_mempalace(db: sqlite3.Connection, drawer_id: str, mem_id: str) -> None:
    db.execute(
        "INSERT INTO mempalace_imports (drawer_id, memory_id, imported_at) VALUES (?, ?, ?)",
        (drawer_id, mem_id, _now_iso()),
    )
    db.commit()


def _track_chat(db: sqlite3.Connection, import_id: str, filename: str) -> None:
    db.execute(
        "INSERT INTO chat_imports (import_id, filename, hash, imported_at) VALUES (?, ?, ?, ?)",
        (import_id, filename, f"hash-{import_id}", _now_iso()),
    )
    db.commit()


async def _undo(**kwargs: Any) -> dict[str, Any]:
    return json.loads(await remind_me_undo_import(UndoImportInput(**kwargs)))


@pytest.fixture()
def hard_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sync disabled — deletes are outright, so counts are easy to assert."""
    monkeypatch.setattr(admin_mod, "SYNC_ENABLED", False)


@pytest.fixture()
def soft_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sync enabled — deletes tombstone so the removal propagates."""
    monkeypatch.setattr(admin_mod, "SYNC_ENABLED", True)


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


async def test_dry_run_is_the_default_and_changes_nothing(
    db_conn: sqlite3.Connection, hard_delete: None
) -> None:
    """Omitting dry_run must not delete — bulk removal has to be opt-in."""
    for i in range(3):
        _add_memory(db_conn, f"m{i}")
        _track_mempalace(db_conn, f"drawer{i}", f"m{i}")

    result = await _undo(import_kind=UndoImportKind.MEMPALACE)

    assert result["dry_run"] is True
    assert result["matched"] == 3
    assert result["removed"] == 0
    assert result["remaining"] == 3
    assert db_conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 3
    assert db_conn.execute("SELECT COUNT(*) FROM mempalace_imports").fetchone()[0] == 3


async def test_dry_run_warns_that_tombstones_delay_space_reclamation(
    db_conn: sqlite3.Connection, soft_delete: None
) -> None:
    """The soft-delete hint must not imply disk is freed immediately."""
    _add_memory(db_conn, "m1")
    _track_mempalace(db_conn, "drawer1", "m1")

    result = await _undo(import_kind=UndoImportKind.MEMPALACE)

    assert "tombstone" in result["mode"]
    assert "compaction" in result["hint"]


# ---------------------------------------------------------------------------
# Actual removal
# ---------------------------------------------------------------------------


async def test_removes_memories_and_their_tracking_rows(
    db_conn: sqlite3.Connection, hard_delete: None
) -> None:
    """Tracking rows must go too, or the content can never be re-imported.

    Import paths skip anything already recorded, so orphaned tracking rows
    make a re-import a silent no-op.
    """
    for i in range(3):
        _add_memory(db_conn, f"m{i}")
        _track_mempalace(db_conn, f"drawer{i}", f"m{i}")

    result = await _undo(import_kind=UndoImportKind.MEMPALACE, dry_run=False)

    assert result["removed"] == 3
    assert result["remaining"] == 0
    assert result["tracking_rows_removed"] == 3
    assert db_conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert db_conn.execute("SELECT COUNT(*) FROM mempalace_imports").fetchone()[0] == 0


async def test_soft_delete_tombstones_rather_than_removing(
    db_conn: sqlite3.Connection, soft_delete: None
) -> None:
    """With sync on, rows survive as tombstones so the delete propagates."""
    _add_memory(db_conn, "m1")
    _track_mempalace(db_conn, "drawer1", "m1")

    result = await _undo(import_kind=UndoImportKind.MEMPALACE, dry_run=False)

    assert result["removed"] == 1
    row = db_conn.execute("SELECT deleted_at FROM memories WHERE id = 'm1'").fetchone()
    assert row is not None, "soft delete must keep the row as a tombstone"
    assert row[0] is not None


async def test_leaves_memories_from_other_imports_alone(
    db_conn: sqlite3.Connection, hard_delete: None
) -> None:
    """Only tracked rows are touched — untracked memories are never collateral."""
    _add_memory(db_conn, "tracked")
    _track_mempalace(db_conn, "drawer1", "tracked")
    _add_memory(db_conn, "untracked", category="fact")

    await _undo(import_kind=UndoImportKind.MEMPALACE, dry_run=False)

    remaining = [r[0] for r in db_conn.execute("SELECT id FROM memories").fetchall()]
    assert remaining == ["untracked"]


async def test_cleans_up_entity_links_and_feedback(
    db_conn: sqlite3.Connection, hard_delete: None
) -> None:
    """Derived rows must not outlive their memory.

    This is why the tool goes through db._purge_memory rather than issuing its
    own DELETE — a bulk SQL delete would orphan every one of these.
    """
    _add_memory(db_conn, "m1")
    _track_mempalace(db_conn, "drawer1", "m1")
    db_conn.execute(
        "INSERT INTO entities (id, name, created_at, updated_at) VALUES ('e1', 'Thing', ?, ?)",
        (_now_iso(), _now_iso()),
    )
    db_conn.execute(
        "INSERT INTO memory_entities (memory_id, entity_id, created_at) VALUES ('m1', 'e1', ?)",
        (_now_iso(),),
    )
    db_conn.execute(
        """INSERT INTO memory_feedback
               (id, memory_id, query, query_tokens, signal, magnitude, created_at)
           VALUES ('f1', 'm1', 'q', '["q"]', 'helpful', 1.0, ?)""",
        (_now_iso(),),
    )
    db_conn.commit()

    await _undo(import_kind=UndoImportKind.MEMPALACE, dry_run=False)

    assert db_conn.execute("SELECT COUNT(*) FROM memory_entities").fetchone()[0] == 0
    assert db_conn.execute("SELECT COUNT(*) FROM memory_feedback").fetchone()[0] == 0
    # The entity itself stays — other memories may still mention it.
    assert db_conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Batching / resumability
# ---------------------------------------------------------------------------


async def test_limit_bounds_one_call_and_work_resumes(
    db_conn: sqlite3.Connection, hard_delete: None
) -> None:
    """A large undo must not need to fit in one call.

    47000 records cannot be removed inside an MCP call timeout, so the tool
    reports what is left and callers loop.
    """
    for i in range(10):
        _add_memory(db_conn, f"m{i}")
        _track_mempalace(db_conn, f"drawer{i}", f"m{i}")

    first = await _undo(import_kind=UndoImportKind.MEMPALACE, dry_run=False, limit=4)
    assert first["removed"] == 4
    assert first["remaining"] == 6
    assert "resumable" in first["hint"]

    second = await _undo(import_kind=UndoImportKind.MEMPALACE, dry_run=False, limit=100)
    assert second["removed"] == 6
    assert second["remaining"] == 0
    assert db_conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0


async def test_repeated_call_after_completion_is_a_no_op(
    db_conn: sqlite3.Connection, hard_delete: None
) -> None:
    """Idempotent once drained — an extra call must not error or over-report."""
    _add_memory(db_conn, "m1")
    _track_mempalace(db_conn, "drawer1", "m1")
    await _undo(import_kind=UndoImportKind.MEMPALACE, dry_run=False)

    again = await _undo(import_kind=UndoImportKind.MEMPALACE, dry_run=False)

    assert again["matched"] == 0
    assert again["removed"] == 0
    assert again["remaining"] == 0


# ---------------------------------------------------------------------------
# Scoping
# ---------------------------------------------------------------------------


async def test_import_id_scopes_mempalace_by_drawer_prefix(
    db_conn: sqlite3.Connection, hard_delete: None
) -> None:
    """A wing can be targeted without naming every drawer in it."""
    _add_memory(db_conn, "keep")
    _track_mempalace(db_conn, "drawer_other_general_1", "keep")
    _add_memory(db_conn, "drop")
    _track_mempalace(db_conn, "drawer_rusty_term_general_1", "drop")

    result = await _undo(
        import_kind=UndoImportKind.MEMPALACE,
        import_id="drawer_rusty_term",
        dry_run=False,
    )

    assert result["removed"] == 1
    remaining = [r[0] for r in db_conn.execute("SELECT id FROM memories").fetchall()]
    assert remaining == ["keep"]


async def test_chat_undo_matches_on_doc_id(
    db_conn: sqlite3.Connection, hard_delete: None
) -> None:
    """chat_imports has no memory_id — the link is memories.doc_id = import_id."""
    _track_chat(db_conn, "imp-1", "chat.json")
    _add_memory(db_conn, "c1", category="chat_import", doc_id="imp-1")
    _add_memory(db_conn, "c2", category="chat_import", doc_id="imp-1")
    _add_memory(db_conn, "other", category="fact")

    result = await _undo(
        import_kind=UndoImportKind.CHAT, import_id="imp-1", dry_run=False
    )

    assert result["removed"] == 2
    assert result["tracking_rows_removed"] == 1
    remaining = [r[0] for r in db_conn.execute("SELECT id FROM memories").fetchall()]
    assert remaining == ["other"]


async def test_chat_tracking_row_survives_while_any_chunk_remains(
    db_conn: sqlite3.Connection, hard_delete: None
) -> None:
    """A partially-drained chat import must keep its tracking row.

    Dropping it early would let a re-import duplicate the chunks that are
    still present.
    """
    _track_chat(db_conn, "imp-1", "chat.json")
    for i in range(4):
        _add_memory(db_conn, f"c{i}", category="chat_import", doc_id="imp-1")

    first = await _undo(
        import_kind=UndoImportKind.CHAT, import_id="imp-1", dry_run=False, limit=2
    )

    assert first["removed"] == 2
    assert first["tracking_rows_removed"] == 0, "import is not fully drained yet"
    assert db_conn.execute("SELECT COUNT(*) FROM chat_imports").fetchone()[0] == 1

    second = await _undo(
        import_kind=UndoImportKind.CHAT, import_id="imp-1", dry_run=False
    )
    assert second["tracking_rows_removed"] == 1
    assert db_conn.execute("SELECT COUNT(*) FROM chat_imports").fetchone()[0] == 0


async def test_unknown_scope_matches_nothing_rather_than_everything(
    db_conn: sqlite3.Connection, hard_delete: None
) -> None:
    """A typo'd scope must be inert, never a full-store wipe."""
    _add_memory(db_conn, "m1")
    _track_mempalace(db_conn, "drawer1", "m1")

    result = await _undo(
        import_kind=UndoImportKind.MEMPALACE, import_id="nope", dry_run=False
    )

    assert result["matched"] == 0
    assert result["removed"] == 0
    assert db_conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1


async def test_already_tombstoned_rows_are_not_rematched(
    db_conn: sqlite3.Connection, soft_delete: None
) -> None:
    """Tombstoned memories are excluded, so a re-run reports 0 rather than
    tombstoning them a second time and re-enqueueing sync work."""
    _add_memory(db_conn, "m1")
    _track_mempalace(db_conn, "drawer1", "m1")
    await _undo(import_kind=UndoImportKind.MEMPALACE, dry_run=False)

    again = await _undo(import_kind=UndoImportKind.MEMPALACE)

    assert again["matched"] == 0
