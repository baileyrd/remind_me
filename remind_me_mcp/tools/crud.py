"""
remind_me_mcp.tools.crud — add / get / list / update / delete tool handlers.

Patchable shared state and cross-module helpers are looked up through the
``remind_me_mcp.tools`` package namespace (``_pkg.<name>``) at call time so
monkeypatching ``remind_me_mcp.tools.<name>`` keeps working (HY-02).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any

from remind_me_mcp import ann_index
from remind_me_mcp import tools as _pkg
from remind_me_mcp.config import CLIENT, NODE_ID, SYNC_ENABLED
from remind_me_mcp.db import _make_id, _now_iso, _row_to_dict
from remind_me_mcp.events import emit_event
from remind_me_mcp.formatting import _fmt_memories, _fmt_memory_md
from remind_me_mcp.models import (  # noqa: TC001  # FastMCP resolves these annotations at runtime for tool schemas
    MemoryAddInput,
    MemoryDeleteInput,
    MemoryListInput,
    MemoryUpdateInput,
)
from remind_me_mcp.server import mcp
from remind_me_mcp.tools._shared import _maybe_maintenance_notice, _maybe_update_notice, log
from remind_me_mcp.vitality import seed_base_weight


@mcp.tool(
    name="remind_me_add",
    annotations={
        "title": "Add a Memory",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def memory_add(params: MemoryAddInput) -> str:
    """Store a new memory. Use this to save facts, preferences, decisions, observations, or any information that should persist across conversations.

    Args:
        params (MemoryAddInput): Memory content and metadata.

    Returns:
        str: Confirmation with the new memory's ID.
    """
    db = _pkg._get_db()
    mem_id = _make_id(params.content)
    now = _now_iso()
    # Importance prior at write time (issue #56): memory_type isn't known
    # yet here (set later by remind_me_reclassify), so this seeds from
    # source alone -- falls back to the flat 1.0 default for "manual" or
    # any unrecognized source. A fresh memory's vitality equals its
    # base_weight exactly (access_count=0, days_since_last_access=0 in
    # compute_vitality's formula), so both columns must carry the same
    # seeded value or vitality would start inconsistent with base_weight.
    base_weight = seed_base_weight(source=params.source)
    try:
        db.execute(
            """INSERT INTO memories (id, content, category, tags, source, metadata,
                                     created_at, updated_at, node_id, client,
                                     subject, predicate, object, base_weight, vitality)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                mem_id,
                params.content,
                params.category,
                json.dumps(params.tags),
                params.source,
                json.dumps(params.metadata),
                now,
                now,
                NODE_ID,
                CLIENT,
                params.subject,
                params.predicate,
                params.object,
                base_weight,
                base_weight,
            ),
        )
        # FT-04: upsert mentioned entities and record the mention links.
        for ent in params.entities:
            eid = _pkg._upsert_entity(
                db, ent.name, ent.kind, ent.aliases, node_id=NODE_ID, now=now
            )
            _pkg._link_memory_entity(db, mem_id, eid, now)
        # Contradiction-based supersession (gap #5): an SPO triple that
        # conflicts with an existing fact (same subject+predicate, different
        # object) supersedes it, same mechanism as similarity-merge.
        superseded = _pkg._supersede_contradicting_facts(
            db, mem_id, params.subject, params.predicate, params.object, now
        )
        db.commit()
    except sqlite3.IntegrityError as e:
        log.error("Failed to add memory: %s", e)
        return "Error: Could not add memory — a memory with this content may already exist."
    except sqlite3.OperationalError as e:
        log.error("Database error adding memory: %s", e)
        return f"Error: Database operation failed — {e}"
    # Issue #198: unthrottled automation event stream, separate from the
    # human-alert notifications channel — see events.py's module docstring.
    emit_event("created", mem_id, params.category)
    await asyncio.to_thread(_pkg._embed_and_store, mem_id, params.content)
    msg = f"✓ Memory stored with id `{mem_id}` in category '{params.category}'."
    if superseded:
        previews = _pkg._supersession_preview(db, superseded)
        detail = "; ".join(f'`{p["id"]}` was: "{p["content"]}"' for p in previews)
        msg += (
            f" Superseded {len(superseded)} contradicted fact(s) sharing subject/predicate "
            f"({params.subject!r}, {params.predicate!r}) with a different object — {detail}. "
            "If that's not a real contradiction (e.g. a reused generic subject/predicate "
            "across unrelated facts), call remind_me_update on the superseded id with "
            "clear_superseded=true to un-hide it, then re-add this fact with a distinct "
            "predicate."
        )
    return _maybe_maintenance_notice(_maybe_update_notice(msg))


@mcp.tool(
    name="remind_me_list",
    annotations={
        "title": "List Memories",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def memory_list(params: MemoryListInput) -> str:
    """Browse memories by category, tag, or source — NOT a way to find things by topic.

    This applies filters and paginates; it does no relevance ranking whatsoever,
    so results are effectively arbitrary rows that happen to match the filter.
    Use it to enumerate a known slice ("show me everything tagged `work`",
    "how many memories are in `preference`").

    Use `remind_me_search` instead whenever the question is about *content* —
    what the user thinks, decided, prefers, or ran into. Searching for a topic
    with this tool returns rows that matched a filter, not rows that answer the
    question.

    Args:
        params (MemoryListInput): Filters and pagination.

    Returns:
        str: Memories in the requested format with pagination info.
    """
    db = _pkg._get_db()
    conditions: list[str] = ["m.deleted_at IS NULL"]
    bindings: list[Any] = []

    if params.category:
        conditions.append("m.category = ?")
        bindings.append(params.category)
    if params.source:
        conditions.append("m.source = ?")
        bindings.append(params.source)
    # Tag filtering via SQL JOIN on memory_tags (DATA-02 fix: correct pagination)
    if params.tags:
        for i, tag in enumerate(params.tags):
            alias = f"mt{i}"
            conditions.append(
                f"EXISTS (SELECT 1 FROM memory_tags {alias}"
                f" WHERE {alias}.memory_id = m.id AND {alias}.tag = ?)"
            )
            bindings.append(tag)

    where = f"WHERE {' AND '.join(conditions)}"
    total = db.execute(f"SELECT COUNT(*) as cnt FROM memories m {where}", bindings).fetchone()["cnt"]
    rows = db.execute(
        f"SELECT m.* FROM memories m {where} ORDER BY m.created_at DESC LIMIT ? OFFSET ?",
        bindings + [params.limit, params.offset],
    ).fetchall()
    memories = [_row_to_dict(r) for r in rows]

    return _maybe_update_notice(_fmt_memories(memories, params.response_format, total=total))


@mcp.tool(
    name="remind_me_get",
    annotations={
        "title": "Get a Memory by ID",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def memory_get(memory_id: str) -> str:
    """Fetch one memory whose exact ID you already have.

    Only useful when a previous result handed you the id. If you are trying to
    *find* a memory, use `remind_me_search` — ids are not guessable.

    Args:
        memory_id (str): The memory ID.

    Returns:
        str: The memory in markdown format, or an error message.
    """
    db = _pkg._get_db()
    row = db.execute(
        "SELECT * FROM memories WHERE id = ? AND deleted_at IS NULL", (memory_id,)
    ).fetchone()
    if not row:
        return f"Memory `{memory_id}` not found."
    return _fmt_memory_md(_row_to_dict(row))


# Columns a pre-edit memory_revisions snapshot (issue #187) tracks — exactly
# the fields MemoryUpdateInput can change, not every column _apply_memory_
# field_update might be asked to touch. See _apply_memory_field_update's
# docstring for why gating on these specific columns is what keeps a
# remind_me_set_reminder call from producing a spurious revision.
_REVISION_TRACKED_COLUMNS = frozenset({"content", "category", "tags", "metadata"})


def _extract_tracked_field_changes(
    sets: list[str], bindings: list[Any]
) -> dict[str, Any]:
    """Map each tracked column touched by ``sets`` to its incoming new value.

    Every ``sets`` fragment in this codebase has one of two shapes: a bound
    placeholder (``"content = ?"``, consuming the next value off ``bindings``
    in order) or a bare literal (``"remind_at = NULL"`` / ``"superseded_by =
    NULL"``, no binding consumed). This walks both shapes to keep the
    bindings cursor aligned, but only returns columns in
    ``_REVISION_TRACKED_COLUMNS`` — a fragment for an untracked column (e.g.
    ``remind_at``) is parsed just enough to advance the cursor correctly and
    then discarded.

    Args:
        sets: The same ``sets`` list passed to _apply_memory_field_update.
        bindings: The same ``bindings`` list, positionally aligned with the
            ``?`` placeholders in ``sets``.

    Returns:
        ``{column: new_value}`` for each tracked column present in ``sets``.
    """
    changes: dict[str, Any] = {}
    idx = 0
    for fragment in sets:
        col, _, rhs = fragment.partition("=")
        col = col.strip()
        rhs = rhs.strip()
        if rhs == "?":
            value = bindings[idx]
            idx += 1
        elif rhs.upper() == "NULL":
            value = None
        else:
            # No other literal shape appears in this codebase today — skip
            # rather than mis-parse an unrecognized fragment.
            continue
        if col in _REVISION_TRACKED_COLUMNS:
            changes[col] = value
    return changes


def _apply_memory_field_update(
    db: sqlite3.Connection,
    memory_id: str,
    sets: list[str],
    bindings: list[Any],
    *,
    revision_reason: str | None = None,
) -> None:
    """Apply a raw column UPDATE to one memory row, always bumping updated_at.

    The single place every write path that mutates a real content column on
    ``memories`` funnels through — ``remind_me_update`` below,
    ``remind_me_revert`` (``tools/history.py``, issue #187), and
    ``remind_me_set_reminder`` (``tools/reminders.py``, issue #179) via the
    ``_pkg.<name>`` patchable-lookup convention (see this module's
    docstring) — so ``updated_at`` is bumped consistently. That is what the
    sync outbox trigger's LWW comparison keys off, and what distinguishes a
    genuine content change from the v22 access-tracking exception (which
    deliberately does NOT bump ``updated_at``). Callers must already have
    verified the memory exists and is not soft-deleted, and must not pass an
    empty ``sets``.

    Edit history (issue #187): before applying the update, this snapshots
    the row's *current* ``content``/``category``/``tags``/``metadata`` into
    ``memory_revisions`` — but only when ``sets`` actually touches one of
    those tracked columns AND the incoming value genuinely differs from what
    is already stored (mirroring the v22 migration's "only sync on genuine
    content change" discipline — a same-value update creates no revision).
    This is a deliberate judgment call: rather than duplicate the snapshot
    logic in ``remind_me_update`` and ``remind_me_revert`` separately, it
    lives once at this shared choke point. A useful side effect falls out of
    that choice for free — ``remind_me_set_reminder`` calls this same
    function, but ``sets`` for it only ever contains ``"remind_at = ..."``,
    which is not a tracked column, so a reminder set/clear never produces a
    revision. The snapshot INSERT and the ``memories`` UPDATE share the one
    ``db.commit()`` below (no commit in between), so a crash between the two
    cannot leave one without the other — same transactional discipline as
    ``_purge_memory``'s soft-delete path.

    Args:
        db: An open SQLite connection. This function commits.
        memory_id: The memory to update.
        sets: SQL ``"column = ?"`` (or a literal fragment, e.g.
            ``"remind_at = NULL"``) pieces, not including ``updated_at``.
        bindings: Positional bind values matching the ``?`` placeholders in
            ``sets``, in the same order.
        revision_reason: Optional free-text note stored on the captured
            revision (e.g. "revert to revision 3"). Never required — plain
            ``remind_me_update`` calls leave it ``NULL``.
    """
    changes = _extract_tracked_field_changes(sets, bindings)
    if changes:
        old_row = db.execute(
            "SELECT content, category, tags, metadata FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        if old_row is not None and any(
            old_row[col] != new_value for col, new_value in changes.items()
        ):
            db.execute(
                """INSERT INTO memory_revisions
                       (memory_id, content, category, tags, metadata, edited_at, revision_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    memory_id,
                    old_row["content"],
                    old_row["category"],
                    old_row["tags"],
                    old_row["metadata"],
                    _now_iso(),
                    revision_reason,
                ),
            )

    all_sets = [*sets, "updated_at = ?"]
    all_bindings = [*bindings, _now_iso(), memory_id]
    db.execute(f"UPDATE memories SET {', '.join(all_sets)} WHERE id = ?", all_bindings)
    db.commit()


@mcp.tool(
    name="remind_me_update",
    annotations={
        "title": "Update a Memory",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def memory_update(params: MemoryUpdateInput) -> str:
    """Update an existing memory's content, category, tags, metadata, or clear a false-positive supersession.

    Args:
        params (MemoryUpdateInput): The memory ID and fields to update.

    Returns:
        str: Confirmation or error message.
    """
    db = _pkg._get_db()
    row = db.execute(
        "SELECT * FROM memories WHERE id = ? AND deleted_at IS NULL", (params.memory_id,)
    ).fetchone()
    if not row:
        return f"Memory `{params.memory_id}` not found."

    sets: list[str] = []
    bindings: list[Any] = []
    if params.content is not None:
        sets.append("content = ?")
        bindings.append(params.content)
    if params.category is not None:
        sets.append("category = ?")
        bindings.append(params.category)
    if params.tags is not None:
        sets.append("tags = ?")
        bindings.append(json.dumps(params.tags))
    if params.metadata is not None:
        sets.append("metadata = ?")
        bindings.append(json.dumps(params.metadata))
    if params.clear_superseded:
        sets.append("superseded_by = NULL")

    if not sets:
        return "Nothing to update — no fields provided."

    _apply_memory_field_update(db, params.memory_id, sets, bindings)
    # Issue #198: fired here in remind_me_update itself, deliberately NOT
    # inside the shared _apply_memory_field_update choke point above — that
    # function is also called by remind_me_set_reminder (tools/reminders.py,
    # issue #179) with sets=["remind_at = ..."] only, and by remind_me_revert
    # (tools/history.py, issue #187). Neither of those is a "memory update"
    # in the sense this issue means: a reminder being set/cleared touches no
    # content field at all, and a revert is itself framed (and documented,
    # see history.py) as its own distinct operation. Keeping the emit call
    # here, specific to this tool, is what keeps both of those from firing a
    # spurious "updated" automation event. category reports the memory's
    # post-update category — the new value when this call changed it,
    # otherwise its unchanged existing value from the row fetched above.
    emit_event(
        "updated",
        params.memory_id,
        params.category if params.category is not None else row["category"],
    )
    # Re-embed if content changed
    if params.content is not None:
        await asyncio.to_thread(_pkg._embed_and_store, params.memory_id, params.content)
    msg = f"✓ Memory `{params.memory_id}` updated."
    if params.clear_superseded:
        msg += " Cleared superseded_by — visible to search/entity lookups again."
    return msg


@mcp.tool(
    name="remind_me_delete",
    annotations={
        "title": "Delete a Memory",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def memory_delete(params: MemoryDeleteInput) -> str:
    """Delete a memory by ID.

    When sync is configured (any of hub or peer sync — config.SYNC_ENABLED),
    this is a soft delete: the row is tombstoned (``deleted_at`` set) rather
    than removed, so the deletion propagates to other devices via the normal
    sync path (gap #11) — a hard DELETE produces no outbox row at all (the
    sync triggers only fire on INSERT/UPDATE), so it would otherwise silently
    resurrect on the next pull elsewhere. The tombstone is excluded from
    every normal read (search/list/get) and is eventually hard-deleted by a
    background compaction pass once it's safely old
    (config.TOMBSTONE_RETENTION_DAYS). On a node with sync disabled, there's
    nothing to propagate to, so this is a plain, immediate delete exactly as
    before.

    Args:
        params (MemoryDeleteInput): The memory ID to delete.

    Returns:
        str: Confirmation or error message.
    """
    db = _pkg._get_db()
    row = db.execute(
        "SELECT rowid, category FROM memories WHERE id = ? AND deleted_at IS NULL",
        (params.memory_id,),
    ).fetchone()
    if row is None:
        return f"Memory `{params.memory_id}` not found."
    # Derived-row cleanup and the tombstone/hard-delete split both live in
    # db._purge_memory — see its docstring for why they must not be inlined
    # per call site.
    removed_vec_rowids = _pkg._purge_memory(
        db, params.memory_id, row["rowid"], soft=SYNC_ENABLED, now=_now_iso()
    )
    db.commit()
    # ANN mutations only after the commit succeeds — see db._delete_chunks.
    for vec_rowid in removed_vec_rowids:
        ann_index.remove_vector(db, vec_rowid)
    # Issue #198: fired only after the delete/tombstone commit above
    # succeeds — an event stream consumer must never see "deleted" for a
    # delete that didn't actually happen.
    emit_event("deleted", params.memory_id, row["category"])
    return f"✓ Memory `{params.memory_id}` deleted."
