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

``remind_me_list_reminders`` is a thin wrapper around
``remind_me_mcp.reminders.list_reminders`` plus response formatting -- the
window SQL itself was factored out to that module so
``remind_me_mcp.digest`` (issue #188) can reuse the identical
upcoming/overdue definition rather than re-deriving it.

``remind_me_reminders_ics_url`` (issue #190) is the odd one out in this
module: it doesn't touch the memory store at all, it just hands back the
subscribable calendar-feed URL that ``GET /api/reminders/{token}.ics`` (see
``remind_me_mcp.api``) serves, so Claude/the user can retrieve it without
needing filesystem or env access to read REMIND_ME_ICS_TOKEN directly.

``remind_me_digest`` (issue #188) is pure synthesis over data every other
status tool already reports independently -- recent additions, vault
vitality, reminders, and sync health -- assembled by
``remind_me_mcp.digest.build_digest_data``/``render_digest_markdown`` so this
handler stays a thin wrapper, exactly like the other three tools here.
"""

from __future__ import annotations

import json

from remind_me_mcp import digest
from remind_me_mcp import tools as _pkg
from remind_me_mcp.config import resolve_ics_token
from remind_me_mcp.formatting import _fmt_memories
from remind_me_mcp.models import (  # noqa: TC001  # FastMCP resolves these annotations at runtime for tool schemas
    DigestInput,
    ListRemindersInput,
    ResponseFormat,
    SetReminderInput,
)
from remind_me_mcp.reminders import list_reminders
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
    memories = list_reminders(db, params.when, params.limit)
    return _fmt_memories(memories, params.response_format)


@mcp.tool(
    name="remind_me_reminders_ics_url",
    annotations={
        "title": "Get the Reminders Calendar Feed URL",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_reminders_ics_url() -> str:
    """Return the subscribable ICS calendar feed URL for reminders (issue #190).

    Paste the returned URL into Google/Apple/Outlook calendar's "subscribe
    by URL" feature to see every upcoming/overdue-and-undelivered reminder
    as a calendar event, refreshed on whatever poll interval the calendar
    provider itself uses (not configurable from here).

    The URL embeds a per-install secret token (REMIND_ME_ICS_TOKEN) instead
    of requiring an Authorization header, because a calendar subscription is
    polled unauthenticated by the provider's own servers on a schedule you
    don't control -- there's no way for it to send a custom header. WARNING:
    whoever holds this URL can read every reminder's content -- treat it
    exactly like a password. Rotate it by deleting
    ``~/.remind-me/ics_token`` (or wherever REMIND_ME_MCP_DIR points).

    Returns:
        str: The full feed URL when the dashboard HTTP server is running.
        Otherwise a placeholder explaining how to start it -- stdio-only
        mode has no HTTP surface to serve the feed from, so there is no
        real URL to return yet.
    """
    status = _pkg.get_server_status()
    token = resolve_ics_token()
    feed_path = f"/api/reminders/{token}.ics"
    if status["ui_server"] != "running":
        return (
            "No HTTP surface is currently active to serve the reminders "
            "calendar feed (this MCP connection is stdio-only). Start the "
            "dashboard server (`remind-me-mcp --serve-ui`) and call this "
            f"tool again for the full URL. Feed path once running: {feed_path}"
        )
    base = str(status["ui_url"]).rstrip("/")
    return f"{base}{feed_path}"


@mcp.tool(
    name="remind_me_digest",
    annotations={
        "title": "Vault Digest",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def remind_me_digest(params: DigestInput) -> str:
    """Summarize recent additions, vault vitality, reminders, and sync health (issue #188).

    Pure synthesis over data every other status tool already reports
    independently, compressed into one read: memories added in the last
    ``since_days`` days (default 7), the same vitality buckets
    ``remind_me_vitality_report`` computes, the same upcoming/overdue
    reminders ``remind_me_list_reminders`` returns, and the same sync
    outbox/drain state ``remind_me_sync_status`` reports -- plus, when sync
    is enabled, a fresh ``remind_me_sync_reconcile``-equivalent verdict
    against the hub. Never disagrees with those tools because it calls the
    exact same underlying functions rather than re-deriving the numbers.

    Works standalone with no configuration required. Optional scheduled
    delivery through a configured notification channel is a separate opt-in
    (``REMIND_ME_DIGEST_INTERVAL`` -- see the README); this tool call itself
    never depends on that being set up.

    Args:
        params: How many days back counts as "recent" and the response format.

    Returns:
        str: A markdown digest, or JSON with the same underlying data
        (``response_format=json``) for programmatic consumption.
    """
    db = _pkg._get_db()
    data = digest.build_digest_data(db, params.since_days)

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(data, indent=2, default=str)

    reconcile = None
    try:
        from remind_me_mcp.sync import reconcile_with_hub

        reconcile = await reconcile_with_hub()
    except Exception:  # noqa: BLE001 — the optional hub check must never break the digest
        reconcile = None

    return digest.render_digest_markdown(data, reconcile)


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "remind_me_set_reminder",
    "remind_me_list_reminders",
    "remind_me_reminders_ics_url",
    "remind_me_digest",
]
