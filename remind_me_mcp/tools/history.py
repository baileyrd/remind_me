"""
remind_me_mcp.tools.history — list and revert a memory's edit history (issue #187).

Follows the crud.py/_pkg.<name> patchable-lookup convention (see that
module's docstring): db access and the shared raw-update helper
(``_apply_memory_field_update``) are looked up through the
``remind_me_mcp.tools`` package namespace at call time, so monkeypatching
``remind_me_mcp.tools.<name>`` in tests keeps working.

``remind_me_revert`` is deliberately implemented by calling the same
``_apply_memory_field_update`` choke point ``remind_me_update`` uses, rather
than a raw overwrite of the ``memories`` row — that is what makes a revert
ride the normal sync outbox trigger (propagating like any other edit) and
what makes it automatically create a fresh ``memory_revisions`` snapshot of
the state just before the revert, so a revert is itself undoable without any
special-casing here (see ``tools/crud.py``'s ``_apply_memory_field_update``
docstring for the mechanism).
"""

from __future__ import annotations

import asyncio

from remind_me_mcp import tools as _pkg
from remind_me_mcp.formatting import _fmt_revisions
from remind_me_mcp.models import (  # noqa: TC001  # FastMCP resolves these annotations at runtime for tool schemas
    RevertInput,
    RevisionHistoryInput,
)
from remind_me_mcp.server import mcp


@mcp.tool(
    name="remind_me_history",
    annotations={
        "title": "List a Memory's Edit History",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_history(params: RevisionHistoryInput) -> str:
    """List a memory's prior content revisions, newest first.

    Each revision captures the memory's content/category/tags/metadata as
    they were immediately *before* an edit that changed one of them —
    plain access (search hits) and reminder-only changes never appear here,
    only genuine content/category/tag/metadata edits (including reverts,
    which create their own revision of the pre-revert state).

    Args:
        params (RevisionHistoryInput): The memory ID and how many revisions
            to return.

    Returns:
        str: The revision list in the requested format, or an error message
        if the memory doesn't exist.
    """
    db = _pkg._get_db()
    row = db.execute(
        "SELECT id FROM memories WHERE id = ? AND deleted_at IS NULL", (params.memory_id,)
    ).fetchone()
    if row is None:
        return f"Memory `{params.memory_id}` not found."

    rows = db.execute(
        """SELECT id, content, category, tags, metadata, edited_at, revision_reason
             FROM memory_revisions
            WHERE memory_id = ?
            ORDER BY edited_at DESC, id DESC
            LIMIT ?""",
        (params.memory_id, params.limit),
    ).fetchall()
    revisions = [_pkg._row_to_dict(r) for r in rows]
    return _fmt_revisions(params.memory_id, revisions, params.response_format)


@mcp.tool(
    name="remind_me_revert",
    annotations={
        "title": "Revert a Memory to a Prior Revision",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def remind_me_revert(params: RevertInput) -> str:
    """Restore a memory's content/category/tags/metadata to a prior revision.

    The revision id must have come from ``remind_me_history`` for this exact
    memory — reverting to a nonexistent id, or one that belongs to a
    different memory, fails with a clear error rather than silently doing
    nothing. Reverting is itself an edit: it rides the normal update path
    (bumping ``updated_at``, entering the sync outbox like any other change,
    re-embedding if content changed) and creates a new revision snapshotting
    the state just before the revert, so the revert can itself be undone.

    Args:
        params (RevertInput): The memory ID, the revision ID to restore, and
            an optional free-text reason recorded on the new revision this
            revert creates.

    Returns:
        str: Confirmation or a clear error message.
    """
    db = _pkg._get_db()
    mem_row = db.execute(
        "SELECT content FROM memories WHERE id = ? AND deleted_at IS NULL",
        (params.memory_id,),
    ).fetchone()
    if mem_row is None:
        return f"Memory `{params.memory_id}` not found."

    rev_row = db.execute(
        "SELECT content, category, tags, metadata, edited_at FROM memory_revisions "
        "WHERE id = ? AND memory_id = ?",
        (params.revision_id, params.memory_id),
    ).fetchone()
    if rev_row is None:
        return (
            f"Revision `{params.revision_id}` not found for memory "
            f"`{params.memory_id}` — check remind_me_history for valid revision ids."
        )

    content_changed = mem_row["content"] != rev_row["content"]
    reason = params.reason or f"revert to revision {params.revision_id}"
    _pkg._apply_memory_field_update(
        db,
        params.memory_id,
        ["content = ?", "category = ?", "tags = ?", "metadata = ?"],
        [rev_row["content"], rev_row["category"], rev_row["tags"], rev_row["metadata"]],
        revision_reason=reason,
    )
    if content_changed:
        await asyncio.to_thread(_pkg._embed_and_store, params.memory_id, rev_row["content"])
    return (
        f"✓ Memory `{params.memory_id}` reverted to revision `{params.revision_id}` "
        f"(captured {rev_row['edited_at']})."
    )


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "remind_me_history",
    "remind_me_revert",
]
