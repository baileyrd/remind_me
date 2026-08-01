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

    sets.append("updated_at = ?")
    bindings.append(_now_iso())
    bindings.append(params.memory_id)

    db.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id = ?", bindings)
    db.commit()
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
        "SELECT rowid FROM memories WHERE id = ? AND deleted_at IS NULL",
        (params.memory_id,),
    ).fetchone()
    if row is None:
        return f"Memory `{params.memory_id}` not found."
    # Derived-row cleanup and the tombstone/hard-delete split both live in
    # db._purge_memory — see its docstring for why they must not be inlined
    # per call site.
    removed_vec_rowids = _pkg._purge_memory(
        db, params.memory_id, row[0], soft=SYNC_ENABLED, now=_now_iso()
    )
    db.commit()
    # ANN mutations only after the commit succeeds — see db._delete_chunks.
    for vec_rowid in removed_vec_rowids:
        ann_index.remove_vector(db, vec_rowid)
    return f"✓ Memory `{params.memory_id}` deleted."
