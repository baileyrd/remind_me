"""
remind_me_mcp.backup — On-demand and pre-migration SQLite backups (issue #17).

Uses ``sqlite3.Connection.backup()`` (the WAL-safe online backup API) rather
than a raw file copy, which could read a torn/partially-checkpointed page
while the WAL is mid-write. Backups live under ``BACKUP_DIR`` and are pruned
to ``BACKUP_RETENTION_COUNT`` most-recent files after each new backup is
created.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from remind_me_mcp.config import BACKUP_DIR, BACKUP_RETENTION_COUNT, DB_PATH

log = logging.getLogger("remind_me_mcp.backup")


class RestoreError(Exception):
    """Raised when a backup file fails validation and cannot be restored."""


def _timestamp() -> str:
    # Microsecond precision avoids filename collisions between backups taken
    # within the same second (e.g. two manual calls in quick succession).
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def create_backup(db: sqlite3.Connection, *, label: str = "manual") -> Path:
    """Create a WAL-safe online backup of the database.

    Args:
        db: The live database connection to back up. ``Connection.backup()``
            reads through SQLite's own backup API, so it's safe to call while
            other threads hold the connection and while the WAL is active.
        label: A short tag prefixed to the backup filename (e.g. "manual" or
            "pre-migration-v12") to make the backup's origin identifiable.

    Returns:
        The path to the newly created backup file.

    Raises:
        sqlite3.Error: if the backup itself fails (disk full, I/O error,
            locked source). A failed attempt never leaves a partial/corrupt
            file at the final destination (issue #149): the backup is
            written to a sibling ``.tmp`` path and only ``os.replace``d into
            place once ``db.backup()`` completes successfully, so
            ``list_backups``/``_prune_old_backups`` can never mistake a
            half-written file for a real, restorable backup.
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / f"{label}-{_timestamp()}.db"
    tmp_dest = dest.with_suffix(".db.tmp")
    dest_conn = sqlite3.connect(str(tmp_dest))
    try:
        db.backup(dest_conn)
    except Exception:
        dest_conn.close()
        Path(tmp_dest).unlink(missing_ok=True)
        raise
    else:
        dest_conn.close()
    os.replace(tmp_dest, dest)
    _prune_old_backups()
    return dest


def list_backups() -> list[dict[str, Any]]:
    """List existing backup files, newest first.

    Returns:
        A list of dicts with ``filename``, ``path``, ``size_bytes``, and
        ``created_at`` (ISO-8601 UTC, derived from the file's mtime).
    """
    if not BACKUP_DIR.exists():
        return []
    entries = []
    for p in sorted(BACKUP_DIR.glob("*.db")):
        stat = p.stat()
        entries.append(
            {
                "filename": p.name,
                "path": str(p),
                "size_bytes": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
            }
        )
    entries.sort(key=lambda e: str(e["created_at"]), reverse=True)
    return entries


def _prune_old_backups(keep: int | None = None) -> int:
    """Delete the oldest backup files beyond ``keep``, returning the count removed.

    ``keep`` defaults to ``BACKUP_RETENTION_COUNT`` read at call time inside
    the function body, not bound as the parameter's default value (issue
    #150): a default value is evaluated exactly once, when the function is
    *defined* (at module import), so a caller relying on the default would
    silently keep using whatever ``BACKUP_RETENTION_COUNT`` was at import
    time even after a runtime/config reload changed it.
    """
    if keep is None:
        keep = BACKUP_RETENTION_COUNT
    backups = list_backups()
    stale = backups[keep:]
    removed = 0
    for entry in stale:
        try:
            Path(entry["path"]).unlink()
            removed += 1
        except OSError as e:
            log.warning("Failed to prune old backup %s: %s", entry["path"], e)
    return removed


def _validate_backup_file(source: Path) -> None:
    """Sanity-check *source* is a genuine, uncorrupted SQLite database.

    Deliberately conservative: this is the only thing standing between a
    truncated/corrupt file and clobbering the live database, so a failure
    here must refuse rather than guess.

    Raises:
        RestoreError: if *source* doesn't exist, isn't a valid SQLite
            database, or fails ``PRAGMA integrity_check``.
    """
    if not source.exists():
        raise RestoreError(f"backup file not found: {source}")
    try:
        conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    except sqlite3.OperationalError as e:
        raise RestoreError(f"could not open {source} as a SQLite database: {e}") from e
    try:
        (result,) = conn.execute("PRAGMA integrity_check").fetchone()
        if result != "ok":
            raise RestoreError(f"integrity check failed for {source}: {result}")
        (table_count,) = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='memories'"
        ).fetchone()
        if table_count == 0:
            raise RestoreError(f"{source} has no 'memories' table -- not a remind-me database")
    except sqlite3.DatabaseError as e:
        raise RestoreError(f"{source} is not a valid SQLite database: {e}") from e
    finally:
        conn.close()


def restore_backup(source: Path, *, dest: Path | None = None) -> Path | None:
    """Restore a backup file over the live database (issue #152).

    The backup/pre-migration-snapshot system existed specifically "so a
    failed or buggy migration can be rolled back by restoring it" (see
    ``db._maybe_snapshot_before_migration``), but nothing ever implemented
    the restore half -- this closes that gap.

    Must be called with the server stopped: this operates directly on
    files, not through a live connection, since the whole point is
    recovering from a database that may not be safely openable by the
    running server in the first place.

    Steps, in order:
    1. Validate *source* (``PRAGMA integrity_check`` + has a ``memories``
       table) -- refuses rather than guesses if this fails.
    2. If *dest* already exists, snapshot it first (to
       ``BACKUP_DIR/pre-restore-<timestamp>.db``) so a bad restore is
       itself recoverable -- a raw file copy, not ``create_backup``'s live
       ``Connection.backup()`` API, since *dest* may be exactly the
       corrupt/unopenable file this restore is trying to replace.
    3. Remove any stale ``-wal``/``-shm`` sidecar files next to *dest* --
       they belong to the file being replaced and would otherwise be
       (incorrectly) applied on top of the restored content.
    4. ``os.replace`` *source* into place at *dest*.

    Args:
        source: Path to the backup file to restore.
        dest: Path to restore over. Defaults to ``config.DB_PATH``.

    Returns:
        Path to the pre-restore snapshot of *dest*, or None if *dest*
        didn't exist (nothing to snapshot).

    Raises:
        RestoreError: if *source* fails validation.
    """
    import shutil

    source = Path(source)
    dest = Path(dest) if dest is not None else DB_PATH
    _validate_backup_file(source)
    dest.parent.mkdir(parents=True, exist_ok=True)

    pre_restore_snapshot: Path | None = None
    if dest.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        pre_restore_snapshot = BACKUP_DIR / f"pre-restore-{_timestamp()}.db"
        shutil.copy2(dest, pre_restore_snapshot)
        log.info("Pre-restore snapshot of %s created: %s", dest, pre_restore_snapshot)

    for suffix in ("-wal", "-shm"):
        sidecar = dest.with_name(dest.name + suffix)
        sidecar.unlink(missing_ok=True)

    # Copy (not move/rename) source into a temp file next to dest, then
    # os.replace that temp copy into place -- restoring must never consume
    # or delete the backup file itself, which os.replace(source, dest)
    # directly would (rename semantics remove the source).
    tmp_dest = dest.with_suffix(".db.restoring")
    shutil.copy2(source, tmp_dest)
    os.replace(tmp_dest, dest)
    log.info("Restored %s over %s", source, dest)
    return pre_restore_snapshot
