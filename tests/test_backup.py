"""
Unit tests for remind_me_mcp.backup — on-demand and pre-migration SQLite
backups (issue #17).

BACKUP_DIR is monkeypatched to a temp directory by the session-scoped
tmp_memory_dir fixture in conftest.py, so these tests never touch the real
~/.remind-me/backups/ directory.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

from remind_me_mcp import backup as backup_mod

if TYPE_CHECKING:
    import sqlite3

    import pytest

# ---------------------------------------------------------------------------
# create_backup
# ---------------------------------------------------------------------------


def test_create_backup_writes_file(db_conn: sqlite3.Connection) -> None:
    """create_backup writes a .db file under BACKUP_DIR named with its label."""
    path = backup_mod.create_backup(db_conn, label="manual")

    assert path.exists()
    assert path.parent == backup_mod.BACKUP_DIR
    assert path.name.startswith("manual-")
    assert path.suffix == ".db"


def test_create_backup_default_label_is_manual(db_conn: sqlite3.Connection) -> None:
    """create_backup defaults to the 'manual' label when none is given."""
    path = backup_mod.create_backup(db_conn)

    assert path.name.startswith("manual-")


def test_create_backup_is_a_valid_restorable_copy(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """The backup file is a fully independent, queryable SQLite database."""
    import sqlite3 as sqlite3_module

    memory_factory(content="Something worth backing up")

    path = backup_mod.create_backup(db_conn, label="manual")

    restored = sqlite3_module.connect(str(path))
    try:
        row = restored.execute(
            "SELECT content FROM memories WHERE content = ?",
            ("Something worth backing up",),
        ).fetchone()
    finally:
        restored.close()
    assert row is not None
    assert row[0] == "Something worth backing up"


def test_create_backup_prunes_automatically(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """create_backup invokes pruning after writing the new backup."""
    calls = []
    monkeypatch.setattr(
        backup_mod, "_prune_old_backups", lambda *a, **kw: calls.append((a, kw)) or 0
    )

    backup_mod.create_backup(db_conn, label="manual")

    assert len(calls) == 1


# ---------------------------------------------------------------------------
# list_backups
# ---------------------------------------------------------------------------


def test_list_backups_empty_when_dir_missing() -> None:
    """list_backups returns [] rather than raising when BACKUP_DIR doesn't exist."""
    assert backup_mod.list_backups() == []


def test_list_backups_reports_created_files(db_conn: sqlite3.Connection) -> None:
    """list_backups surfaces filename, path, size, and an ISO-8601 created_at."""
    backup_mod.create_backup(db_conn, label="manual")

    backups = backup_mod.list_backups()

    assert len(backups) == 1
    entry = backups[0]
    assert entry["filename"].startswith("manual-")
    assert entry["path"]
    assert entry["size_bytes"] > 0
    assert "T" in entry["created_at"]


def test_list_backups_sorted_newest_first(db_conn: sqlite3.Connection) -> None:
    """Backups are ordered newest-first by created_at (mtime)."""
    backup_mod.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    older = backup_mod.BACKUP_DIR / "manual-older.db"
    newer = backup_mod.BACKUP_DIR / "manual-newer.db"
    older.touch()
    newer.touch()
    old_time = time.time() - 1000
    os.utime(older, (old_time, old_time))

    backups = backup_mod.list_backups()

    assert [b["filename"] for b in backups] == [newer.name, older.name]


# ---------------------------------------------------------------------------
# _prune_old_backups
# ---------------------------------------------------------------------------


def test_prune_old_backups_keeps_only_most_recent(db_conn: sqlite3.Connection) -> None:
    """Pruning deletes the oldest files beyond the keep count."""
    backup_mod.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    files = []
    for i in range(5):
        p = backup_mod.BACKUP_DIR / f"manual-{i}.db"
        p.touch()
        os.utime(p, (now - (5 - i) * 10, now - (5 - i) * 10))
        files.append(p)

    removed = backup_mod._prune_old_backups(keep=2)

    assert removed == 3
    remaining = {p.name for p in backup_mod.BACKUP_DIR.glob("*.db")}
    assert remaining == {files[-1].name, files[-2].name}


def test_prune_old_backups_noop_when_under_limit(db_conn: sqlite3.Connection) -> None:
    """Pruning is a no-op when there are fewer backups than the keep count."""
    backup_mod.create_backup(db_conn, label="manual")

    removed = backup_mod._prune_old_backups(keep=10)

    assert removed == 0
    assert len(backup_mod.list_backups()) == 1
