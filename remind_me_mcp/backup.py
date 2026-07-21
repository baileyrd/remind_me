"""
remind_me_mcp.backup — On-demand and pre-migration SQLite backups (issue #17).

Uses ``sqlite3.Connection.backup()`` (the WAL-safe online backup API) rather
than a raw file copy, which could read a torn/partially-checkpointed page
while the WAL is mid-write. Backups live under ``config.BACKUP_DIR`` and are
pruned to ``config.BACKUP_RETENTION_COUNT`` most-recent files after each new
backup is created.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from remind_me_mcp.config import BACKUP_DIR, BACKUP_RETENTION_COUNT

log = logging.getLogger("remind_me_mcp.backup")


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
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / f"{label}-{_timestamp()}.db"
    dest_conn = sqlite3.connect(str(dest))
    try:
        db.backup(dest_conn)
    finally:
        dest_conn.close()
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


def _prune_old_backups(keep: int = BACKUP_RETENTION_COUNT) -> int:
    """Delete the oldest backup files beyond ``keep``, returning the count removed."""
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
