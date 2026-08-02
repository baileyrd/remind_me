"""
remind_me_mcp.tools.saved_searches — save/list/run/delete named searches,
with optional background watch-polling for new matches (issue #194).

Thin wrappers around :mod:`remind_me_mcp.saved_searches`'s plain, FastMCP-free
core -- exactly like ``tools/reminders.py`` wraps
``reminders.list_reminders``/``digest.build_digest_data`` -- plus response
formatting. See that module's docstring for the storage-shape decision
(a dedicated ``saved_search_seen_memories`` table, not a single ``sync_flags``
watermark) and the first-poll-seeds-without-notifying behavior.

No separate "toggle watch" tool: ``remind_me_save_search`` upserts by name,
so re-saving under the same name with a different ``watch`` value is how a
saved search's watch state is changed -- the same "same name is the same
logical thing, re-save to change it" shape ``remind_me_wiki_write`` already
uses for pages, and simpler than a second dedicated tool for a single field.
"""

from __future__ import annotations

import json

from remind_me_mcp import saved_searches as core
from remind_me_mcp import tools as _pkg
from remind_me_mcp.models import (
    SaveSearchInput,  # noqa: TC001  # FastMCP resolves this annotation at runtime for the tool schema
)
from remind_me_mcp.server import mcp


@mcp.tool(
    name="remind_me_save_search",
    annotations={
        "title": "Save a Search",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_save_search(params: SaveSearchInput) -> str:
    """Save a named, replayable remind_me_search query, optionally watched for new matches.

    Saving again under a name that already exists updates that saved search
    in place (query/filters/watch all overwritten) rather than creating a
    duplicate — the same name is treated as the same logical saved search.

    Set `watch=true` to have the background scheduler poll this search every
    `REMIND_ME_SAVED_SEARCH_POLL_INTERVAL` seconds (default 300) and notify
    (see Notifications) on memories that newly start matching. The very
    first poll after turning watch on seeds its "already seen" state from
    whatever currently matches WITHOUT notifying — only a match that shows
    up on a LATER poll is treated as new.

    Args:
        params (SaveSearchInput): Name, query, filters, and watch flag.

    Returns:
        str: Confirmation, noting whether the search is now watched.
    """
    db = _pkg._get_db()
    saved = core.save_search(
        db,
        name=params.name,
        query=params.query,
        category=params.category,
        tags=params.tags,
        include_sensitive=params.include_sensitive,
        watch=params.watch,
    )
    if saved["watch"]:
        note = (
            " — watched: polled in the background for new matches "
            "(first poll seeds silently, see remind_me_save_search's docs)."
        )
    else:
        note = ""
    return f"✓ Saved search '{saved['name']}' stored.{note}"


@mcp.tool(
    name="remind_me_list_saved_searches",
    annotations={
        "title": "List Saved Searches",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_list_saved_searches() -> str:
    """List every saved search with its query, filters, and watch status.

    Returns:
        str: JSON array of saved searches, or a plain message if none exist.
    """
    db = _pkg._get_db()
    searches = core.list_saved_searches(db)
    if not searches:
        return "_No saved searches._"
    return json.dumps(searches, indent=2, default=str)


@mcp.tool(
    name="remind_me_run_saved_search",
    annotations={
        "title": "Run a Saved Search",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_run_saved_search(name: str) -> str:
    """Re-run a saved search's stored query/filters, exactly like calling remind_me_search with those params.

    Args:
        name (str): The saved search's name, from remind_me_save_search or
            remind_me_list_saved_searches.

    Returns:
        str: The same output remind_me_search would produce for the saved
            query/filters, or an error message if no saved search has this
            name.
    """
    db = _pkg._get_db()
    saved = core.get_saved_search(db, name)
    if saved is None:
        return f"Saved search '{name}' not found."
    return await core.execute_saved_search(saved)


@mcp.tool(
    name="remind_me_delete_saved_search",
    annotations={
        "title": "Delete a Saved Search",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_delete_saved_search(name: str) -> str:
    """Delete a saved search by name (also stops any background watch-polling for it).

    Args:
        name (str): The saved search's name.

    Returns:
        str: Confirmation or error message.
    """
    db = _pkg._get_db()
    deleted = core.delete_saved_search(db, name)
    if not deleted:
        return f"Saved search '{name}' not found."
    return f"✓ Saved search '{name}' deleted."


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "remind_me_save_search",
    "remind_me_list_saved_searches",
    "remind_me_run_saved_search",
    "remind_me_delete_saved_search",
]
