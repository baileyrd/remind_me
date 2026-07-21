"""
remind_me_mcp.tools.crud — add / get / list / update / delete tool handlers.

Patchable shared state and cross-module helpers are looked up through the
``remind_me_mcp.tools`` package namespace (``_pkg.<name>``) at call time so
monkeypatching ``remind_me_mcp.tools.<name>`` keeps working (HY-02).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
from typing import Any

from remind_me_mcp import ann_index
from remind_me_mcp import tools as _pkg
from remind_me_mcp.config import CLIENT, NODE_ID, SYNC_ENABLED
from remind_me_mcp.db import _delete_chunks, _make_id, _now_iso, _row_to_dict
from remind_me_mcp.formatting import _fmt_memories, _fmt_memory_md
from remind_me_mcp.models import (  # noqa: TC001  # FastMCP resolves these annotations at runtime for tool schemas
    MemoryAddInput,
    MemoryDeleteInput,
    MemoryListInput,
    MemoryUpdateInput,
)
from remind_me_mcp.server import mcp
from remind_me_mcp.tools._shared import _maybe_update_notice, log


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
    try:
        db.execute(
            """INSERT INTO memories (id, content, category, tags, source, metadata,
                                     created_at, updated_at, node_id, client,
                                     subject, predicate, object)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
        msg += f" Superseded {len(superseded)} contradicted fact(s): {', '.join(superseded)}."
    return _maybe_update_notice(msg)


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
    """List memories with optional filtering by category, tags, or source. Results are paginated.

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
    """Retrieve a single memory by its ID.

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
    """Update an existing memory's content, category, tags, or metadata.

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
    return f"✓ Memory `{params.memory_id}` updated."


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
    # Remove chunk vectors first — FTS and tags are cleaned by triggers, but
    # vec_chunks/memories_vec are not, and (for a hard delete) SQLite reuses
    # freed rowids (DI-01).
    removed_vec_rowids: list[int] = []
    with contextlib.suppress(sqlite3.OperationalError):
        removed_vec_rowids = _delete_chunks(db, row[0])
    # FT-04: entity mention links have no FK (sync can deliver them out of
    # order), so clean them up explicitly — mirroring the DI-01 chunk cleanup.
    # Entities themselves stay; other memories may still mention them.
    db.execute(
        "DELETE FROM memory_entities WHERE memory_id = ?", (params.memory_id,)
    )
    if SYNC_ENABLED:
        now = _now_iso()
        db.execute(
            "UPDATE memories SET deleted_at = ?, updated_at = ? WHERE id = ?",
            (now, now, params.memory_id),
        )
    else:
        db.execute("DELETE FROM memories WHERE id = ?", (params.memory_id,))
    db.commit()
    # ANN mutations only after the commit succeeds — see db._delete_chunks.
    for vec_rowid in removed_vec_rowids:
        ann_index.remove_vector(db, vec_rowid)
    return f"✓ Memory `{params.memory_id}` deleted."
