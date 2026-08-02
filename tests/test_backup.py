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
    from pathlib import Path

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


def test_create_backup_failure_leaves_no_corrupt_file_behind() -> None:
    """Regression guard for issue #149.

    A failed db.backup() used to leave a partial/empty .db file at the final
    destination -- sqlite3.connect(dest) creates the file immediately, and a
    later exception mid-backup left it there, where list_backups/prune
    treated it as a real, restorable backup. The fix writes to a sibling
    .tmp path and only os.replace()s it into place on success.

    sqlite3.Connection is a C type that can't be monkeypatched directly, so
    this uses a duck-typed stand-in for the source connection -- create_backup
    only ever calls .backup(dest_conn) on whatever it's given.
    """
    import sqlite3 as sqlite3_module

    class _FailingSource:
        def backup(self, target):
            raise sqlite3_module.OperationalError("disk full")

    try:
        backup_mod.create_backup(_FailingSource(), label="manual")
        raise AssertionError("expected create_backup to propagate the failure")
    except sqlite3_module.OperationalError:
        pass

    # Neither the final .db nor a leftover .tmp file should exist.
    assert backup_mod.list_backups() == []
    leftovers = list(backup_mod.BACKUP_DIR.glob("*")) if backup_mod.BACKUP_DIR.exists() else []
    assert leftovers == []


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


def test_prune_old_backups_default_reads_config_at_call_time(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard for issue #150.

    ``keep: int = BACKUP_RETENTION_COUNT`` as a default parameter value is
    evaluated once, when the function is defined (module import) -- a
    runtime change to BACKUP_RETENTION_COUNT would never take effect for a
    caller relying on the default. The fix reads it inside the function
    body instead, so a monkeypatch applied *after* import is honoured.
    """
    for i in range(5):
        p = backup_mod.BACKUP_DIR / f"manual-{i}.db"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()

    monkeypatch.setattr(backup_mod, "BACKUP_RETENTION_COUNT", 2)

    removed = backup_mod._prune_old_backups()  # no explicit keep= -- must use the patched value

    assert removed == 3
    assert len(backup_mod.list_backups()) == 2


# ---------------------------------------------------------------------------
# restore_backup (issue #152)
# ---------------------------------------------------------------------------


def _make_valid_backup(db_conn: sqlite3.Connection, content: str) -> Path:
    """Create a real, valid backup file via create_backup, seeded with one
    memory holding *content* -- a real remind-me database restore_backup
    can validate and restore from."""
    db_conn.execute(
        "INSERT INTO memories (id, content, category, tags, source, metadata, "
        "created_at, updated_at) VALUES (?, ?, 'note', '[]', 'test', '{}', "
        "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
        (f"id-{content}", content),
    )
    db_conn.commit()
    return backup_mod.create_backup(db_conn, label="manual")


def test_restore_backup_validates_before_touching_anything(tmp_path) -> None:
    """A corrupt/garbage source file must be rejected before any file at
    dest is touched -- validation is the whole point of a restore path.

    Uses an isolated dest= under tmp_path rather than the shared, session-
    scoped DB_PATH default: other test files (test_api.py, test_async.py)
    open real file-backed WAL-mode connections at that shared path, and
    writing raw -wal/-shm sidecar bytes there can collide with Windows'
    OS-level memory-mapping for those still-live connections.
    """
    garbage = tmp_path / "not-a-database.db"
    garbage.write_bytes(b"this is not a sqlite file at all")

    dest = tmp_path / "dest" / "memory.db"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"original db content")

    try:
        backup_mod.restore_backup(garbage, dest=dest)
        raise AssertionError("expected RestoreError")
    except backup_mod.RestoreError:
        pass

    assert dest.read_bytes() == b"original db content"


def test_restore_backup_rejects_a_missing_source(tmp_path) -> None:
    try:
        backup_mod.restore_backup(tmp_path / "does-not-exist.db")
        raise AssertionError("expected RestoreError")
    except backup_mod.RestoreError:
        pass


def test_restore_backup_rejects_a_db_with_no_memories_table(tmp_path) -> None:
    """A structurally-valid SQLite file that isn't a remind-me database
    (no memories table) must still be refused, not silently 'restored'."""
    import sqlite3 as sqlite3_module

    unrelated = tmp_path / "unrelated.db"
    conn = sqlite3_module.connect(str(unrelated))
    conn.execute("CREATE TABLE something_else (id INTEGER)")
    conn.commit()
    conn.close()

    try:
        backup_mod.restore_backup(unrelated)
        raise AssertionError("expected RestoreError")
    except backup_mod.RestoreError as e:
        assert "memories" in str(e)


def test_restore_backup_snapshots_the_existing_dest_first(
    db_conn: sqlite3.Connection, tmp_path
) -> None:
    """Restoring over an existing dest snapshots the pre-restore content
    first, so a bad restore is itself recoverable."""
    good_backup = _make_valid_backup(db_conn, "the backup content")

    dest = tmp_path / "dest" / "memory.db"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"pre-restore original content")

    snapshot = backup_mod.restore_backup(good_backup, dest=dest)

    assert snapshot is not None
    assert snapshot.name.startswith("pre-restore-")
    assert snapshot.read_bytes() == b"pre-restore original content"
    # dest now holds the restored backup's content, not the original.
    assert dest.read_bytes() != b"pre-restore original content"


def test_restore_backup_returns_none_when_dest_did_not_exist(
    db_conn: sqlite3.Connection, tmp_path
) -> None:
    """No pre-existing dest means nothing to snapshot -- must not raise or
    fabricate a snapshot path."""
    good_backup = _make_valid_backup(db_conn, "fresh restore")
    dest = tmp_path / "dest" / "memory.db"
    assert not dest.exists()

    snapshot = backup_mod.restore_backup(good_backup, dest=dest)

    assert snapshot is None
    assert dest.exists()


def test_restore_backup_does_not_consume_the_source_backup_file(
    db_conn: sqlite3.Connection, tmp_path
) -> None:
    """Restoring must copy, not move, the backup -- the backup file must
    remain available for a future restore afterward."""
    good_backup = _make_valid_backup(db_conn, "keep me around")

    backup_mod.restore_backup(good_backup, dest=tmp_path / "dest" / "memory.db")

    assert good_backup.exists()


def test_restore_backup_removes_stale_wal_shm_sidecars(
    db_conn: sqlite3.Connection, tmp_path
) -> None:
    """Leftover -wal/-shm files from the *old* dest must not survive a
    restore -- they'd otherwise be incorrectly replayed on top of the
    newly-restored content."""
    good_backup = _make_valid_backup(db_conn, "sidecar cleanup")

    dest = tmp_path / "dest" / "memory.db"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"original")
    wal = dest.with_name(dest.name + "-wal")
    shm = dest.with_name(dest.name + "-shm")
    wal.write_bytes(b"stale wal")
    shm.write_bytes(b"stale shm")

    backup_mod.restore_backup(good_backup, dest=dest)

    assert not wal.exists()
    assert not shm.exists()


def test_restore_backup_can_target_a_custom_dest(
    db_conn: sqlite3.Connection, tmp_path
) -> None:
    """The dest parameter overrides the DB_PATH default, for tooling that
    wants to restore to a scratch location rather than the live DB."""
    good_backup = _make_valid_backup(db_conn, "custom dest")
    custom_dest = tmp_path / "scratch" / "restored.db"
    # DB_PATH is session-scoped -- record its state rather than assuming
    # it's pristine, since other tests in this file may have touched it.
    db_path_existed_before = backup_mod.DB_PATH.exists()
    db_path_bytes_before = backup_mod.DB_PATH.read_bytes() if db_path_existed_before else None

    snapshot = backup_mod.restore_backup(good_backup, dest=custom_dest)

    assert snapshot is None  # custom_dest didn't exist yet
    assert custom_dest.exists()
    # The real default must be completely untouched by a dest= override.
    assert backup_mod.DB_PATH.exists() == db_path_existed_before
    if db_path_existed_before:
        assert backup_mod.DB_PATH.read_bytes() == db_path_bytes_before
