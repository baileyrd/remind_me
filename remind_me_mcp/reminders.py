"""
remind_me_mcp.reminders — core reminder-window query logic (issues #179/#188).

Factored out of ``tools/reminders.py`` so :mod:`remind_me_mcp.digest` (issue
#188) can reuse the exact same upcoming/overdue window definition without
duplicating the SQL -- mirroring how ``tools/lifecycle.py``'s
``remind_me_vitality_report`` calls into ``vitality.build_vitality_report``
rather than embedding its own query. ``tools/reminders.py``'s
``remind_me_list_reminders`` tool is now a thin wrapper around
:func:`list_reminders` plus response formatting, exactly as
``remind_me_vitality_report`` wraps ``build_vitality_report``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from remind_me_mcp.db import _now_iso, _row_to_dict
from remind_me_mcp.models import ReminderWindow

if TYPE_CHECKING:
    import sqlite3

__all__ = ["list_reminders"]


def list_reminders(
    db: sqlite3.Connection, when: ReminderWindow, limit: int
) -> list[dict[str, Any]]:
    """Return memories with a set reminder, filtered to a time window.

    'upcoming' is a set reminder still in the future. 'overdue' is a
    reminder whose time has passed but has not yet been recorded in
    ``reminder_deliveries`` -- typically because it came due while the
    background scheduler (:mod:`remind_me_mcp.scheduler`) was offline; once
    delivered it drops out of both views. 'all' is the union of the two.

    Args:
        db: An open SQLite connection.
        when: Which window to list.
        limit: Maximum number of memories to return.

    Returns:
        Matching memory dicts (as from :func:`remind_me_mcp.db._row_to_dict`),
        ordered by ``remind_at`` ascending (soonest first).
    """
    now = _now_iso()
    not_delivered = (
        "NOT EXISTS (SELECT 1 FROM reminder_deliveries rd "
        "WHERE rd.memory_id = m.id AND rd.remind_at = m.remind_at)"
    )
    upcoming = "m.remind_at > ?"
    overdue = f"(m.remind_at <= ? AND {not_delivered})"

    if when == ReminderWindow.UPCOMING:
        window_sql = upcoming
        bindings: list[str] = [now]
    elif when == ReminderWindow.OVERDUE:
        window_sql = overdue
        bindings = [now]
    else:
        window_sql = f"({upcoming} OR {overdue})"
        bindings = [now, now]

    rows = db.execute(
        f"""SELECT m.* FROM memories m
             WHERE m.remind_at IS NOT NULL
               AND m.deleted_at IS NULL
               AND {window_sql}
             ORDER BY m.remind_at ASC
             LIMIT ?""",
        [*bindings, limit],
    ).fetchall()

    return [_row_to_dict(r) for r in rows]
