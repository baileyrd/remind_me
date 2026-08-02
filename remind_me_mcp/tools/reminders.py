"""
remind_me_mcp.tools.reminders — set/clear and list time-based reminders (issue #179).

Follows the crud.py/_pkg.<name> patchable-lookup convention (see that
module's docstring): db access and the shared raw-update helper
(``_apply_memory_field_update``) are looked up through the
``remind_me_mcp.tools`` package namespace at call time, so monkeypatching
``remind_me_mcp.tools.<name>`` in tests keeps working.

Delivery itself (actually notifying the user) is out of scope here — see
``remind_me_mcp.scheduler`` for the background poll loop that fires a
reminder once it's due; that's issue #180's seam, not this module's.
"""

from __future__ import annotations

from remind_me_mcp import tools as _pkg
from remind_me_mcp.db import _now_iso, _row_to_dict
from remind_me_mcp.formatting import _fmt_memories
from remind_me_mcp.models import (  # noqa: TC001  # FastMCP resolves these annotations at runtime for tool schemas
    ListRemindersInput,
    ReminderWindow,
    SetReminderInput,
)
from remind_me_mcp.server import mcp


@mcp.tool(
    name="remind_me_set_reminder",
    annotations={
        "title": "Set or Clear a Memory Reminder",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_set_reminder(params: SetReminderInput) -> str:
    """Set a future reminder on an existing memory, or clear one already set.

    Args:
        params (SetReminderInput): The memory ID and the reminder timestamp
            (omit or pass null to clear an existing reminder instead).

    Returns:
        str: Confirmation or error message.
    """
    db = _pkg._get_db()
    row = db.execute(
        "SELECT id FROM memories WHERE id = ? AND deleted_at IS NULL",
        (params.memory_id,),
    ).fetchone()
    if row is None:
        return f"Memory `{params.memory_id}` not found."

    if params.remind_at is None:
        _pkg._apply_memory_field_update(db, params.memory_id, ["remind_at = NULL"], [])
        return f"✓ Reminder cleared on memory `{params.memory_id}`."

    _pkg._apply_memory_field_update(
        db, params.memory_id, ["remind_at = ?"], [params.remind_at]
    )
    return f"✓ Reminder set on memory `{params.memory_id}` for {params.remind_at}."


@mcp.tool(
    name="remind_me_list_reminders",
    annotations={
        "title": "List Memory Reminders",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_list_reminders(params: ListRemindersInput) -> str:
    """List memories that have a reminder set, filtered to a time window.

    'upcoming' is a set reminder still in the future. 'overdue' is a
    reminder whose time has passed but the background scheduler has not
    (yet) delivered it — typically because it came due while the server was
    offline; once delivered it drops out of both views. 'all' is the union
    of the two.

    Args:
        params (ListRemindersInput): Which window to list, and pagination.

    Returns:
        str: Matching memories in the requested format.
    """
    db = _pkg._get_db()
    now = _now_iso()
    not_delivered = (
        "NOT EXISTS (SELECT 1 FROM reminder_deliveries rd "
        "WHERE rd.memory_id = m.id AND rd.remind_at = m.remind_at)"
    )
    upcoming = "m.remind_at > ?"
    overdue = f"(m.remind_at <= ? AND {not_delivered})"

    if params.when == ReminderWindow.UPCOMING:
        window_sql = upcoming
        bindings: list[str] = [now]
    elif params.when == ReminderWindow.OVERDUE:
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
        [*bindings, params.limit],
    ).fetchall()
    memories = [_row_to_dict(r) for r in rows]

    return _fmt_memories(memories, params.response_format)


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "remind_me_set_reminder",
    "remind_me_list_reminders",
]
