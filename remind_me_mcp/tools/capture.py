"""
remind_me_mcp.tools.capture — auto_capture / get_capture / decompose tool handlers.

Patchable shared state and cross-module helpers are looked up through the
``remind_me_mcp.tools`` package namespace (``_pkg.<name>``) at call time so
monkeypatching ``remind_me_mcp.tools.<name>`` keeps working (HY-02).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

from remind_me_mcp import tools as _pkg
from remind_me_mcp.config import CLIENT, NODE_ID
from remind_me_mcp.db import _make_id, _now_iso, _row_to_dict
from remind_me_mcp.formatting import _fmt_memory_md
from remind_me_mcp.models import (  # noqa: TC001  # FastMCP resolves these annotations at runtime for tool schemas
    AutoCaptureInput,
    DecomposeBatchInput,
    DecomposeInput,
)
from remind_me_mcp.server import mcp
from remind_me_mcp.tools._shared import log
from remind_me_mcp.vitality import DECAY_RATES


@mcp.tool(
    name="remind_me_auto_capture",
    annotations={
        "title": "Auto-Capture Conversation",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def remind_me_auto_capture(params: AutoCaptureInput) -> str:
    """Capture an entire conversation as two linked memories: the full verbatim dialog and a concise summary.

    Use this at the end of every conversation to persist both the raw exchange
    and a distilled summary of key information. The summary is linked to the
    dialog via metadata so they can be retrieved together.

    The full dialog is stored with category 'dialog' and the summary uses the
    category specified in params (default: 'conversation').

    Args:
        params (AutoCaptureInput): The conversation text, summary, tags, and metadata.

    Returns:
        str: Confirmation with both memory IDs.
    """
    db = _pkg._get_db()
    now = _now_iso()

    # Generate a shared capture_id to link dialog + summary
    capture_id = _make_id(params.conversation[:200] + params.summary[:200])

    title = params.title or params.summary[:80].split("\n")[0]

    # -- Store the full dialog --
    dialog_id = _make_id(params.conversation)
    dialog_meta = {
        **params.metadata,
        "capture_id": capture_id,
        "linked_summary": "",  # placeholder, filled after summary is created
        "title": title,
        "type": "dialog",
    }

    # -- Store the summary --
    summary_id = _make_id(params.summary)
    summary_meta = {
        **params.metadata,
        "capture_id": capture_id,
        "linked_dialog": dialog_id,
        "title": title,
        "type": "summary",
    }

    try:
        db.execute(
            """INSERT INTO memories (id, content, category, tags, source, metadata, capture_id, created_at, updated_at, node_id, client)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                dialog_id,
                params.conversation,
                "dialog",
                json.dumps(params.tags),
                "auto_capture",
                json.dumps(dialog_meta),
                capture_id,
                now,
                now,
                NODE_ID,
                CLIENT,
            ),
        )
        db.execute(
            """INSERT INTO memories (id, content, category, tags, source, metadata, capture_id, created_at, updated_at, node_id, client)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                summary_id,
                params.summary,
                params.category,
                json.dumps(params.tags),
                "auto_capture",
                json.dumps(summary_meta),
                capture_id,
                now,
                now,
                NODE_ID,
                CLIENT,
            ),
        )

        # -- Back-link the dialog to the summary --
        dialog_meta["linked_summary"] = summary_id
        db.execute(
            "UPDATE memories SET metadata = ? WHERE id = ?",
            (json.dumps(dialog_meta), dialog_id),
        )

        db.commit()
    except sqlite3.OperationalError as e:
        log.error("Failed to capture conversation: %s", e)
        return f"Error: Could not capture conversation — database error: {e}"

    # Embed both for semantic search (summary is more searchable, dialog has full context)
    await asyncio.to_thread(_pkg._embed_and_store, summary_id, params.summary)
    await asyncio.to_thread(_pkg._embed_and_store, dialog_id, params.conversation)

    tag_str = ", ".join(params.tags) if params.tags else "none"
    return (
        f"✓ Conversation captured!\n\n"
        f"**Title:** {title}\n"
        f"**Dialog:** `{dialog_id}` (category: dialog, {len(params.conversation)} chars)\n"
        f"**Summary:** `{summary_id}` (category: {params.category})\n"
        f"**Tags:** {tag_str}\n"
        f"**Capture ID:** `{capture_id}` (links both memories)\n\n"
        f"The full dialog and summary are linked — search for either and "
        f"use `remind_me_get_capture` with capture_id `{capture_id}` to retrieve both."
        f"\n\n---\n"
        f"**decomposition_pending**: This capture can be decomposed into atomic facts. "
        f"Call `remind_me_decompose` with capture_id `{capture_id}` and an array of "
        f"extracted facts (decisions, preferences, learnings, action items, etc.) to "
        f"make each fact individually searchable."
    )


@mcp.tool(
    name="remind_me_get_capture",
    annotations={
        "title": "Get Linked Dialog + Summary",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_get_capture(capture_id: str) -> str:
    """Retrieve a linked dialog and summary pair by their shared capture_id.

    When a conversation is auto-captured, both the full dialog and its summary
    share a capture_id in their metadata. This tool retrieves both.

    Args:
        capture_id (str): The capture_id that links a dialog and summary.

    Returns:
        str: Both memories formatted together, or an error if not found.
    """
    db = _pkg._get_db()
    # Use the indexed capture_id column for direct lookup (BUGF-02 fix)
    rows = db.execute(
        "SELECT * FROM memories WHERE capture_id = ? ORDER BY category",
        (capture_id,),
    ).fetchall()

    if not rows:
        return f"No capture found with id `{capture_id}`."

    memories = [_row_to_dict(r) for r in rows]
    dialog = next((m for m in memories if m.get("metadata", {}).get("type") == "dialog"), None)
    summary = next((m for m in memories if m.get("metadata", {}).get("type") == "summary"), None)

    title = (summary or dialog or {}).get("metadata", {}).get("title", "Untitled")
    parts = [f"## Capture: {title}", f"**Capture ID:** `{capture_id}`\n"]

    if summary:
        tags = ", ".join(summary.get("tags", [])) or "none"
        parts.append(f"### Summary (`{summary['id']}`)")
        parts.append(f"**Category:** {summary['category']}  |  **Tags:** {tags}")
        parts.append(f"**Captured:** {summary['created_at']}\n")
        parts.append(summary["content"])
        parts.append("")

    if dialog:
        char_count = len(dialog["content"])
        parts.append(f"### Full Dialog (`{dialog['id']}` — {char_count:,} chars)")
        parts.append("**Category:** dialog\n")
        # Show first 3000 chars with truncation notice
        if char_count > 3000:
            parts.append(dialog["content"][:3000])
            parts.append(f"\n\n… _({char_count - 3000:,} more characters — use `remind_me_get` with id `{dialog['id']}` for full text)_")
        else:
            parts.append(dialog["content"])

    if not dialog and not summary:
        parts.append("_Found memories with this capture_id but couldn't identify dialog/summary types._\n")
        for m in memories:
            parts.append(_fmt_memory_md(m))

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Decomposition tools (Phase 12 Plan 01)
# ---------------------------------------------------------------------------


@mcp.tool(
    name="remind_me_decompose",
    annotations={
        "title": "Decompose Capture into Atomic Facts",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def remind_me_decompose(params: DecomposeInput) -> str:
    """Break a captured conversation into individually searchable atomic facts.

    Each fact is stored as a separate memory linked to the parent capture via
    source_capture_id. Facts inherit the parent's tags and can optionally
    receive additional tags and a memory_type classification.

    Args:
        params: The capture_id to decompose and the list of extracted facts.

    Returns:
        JSON string with created count, fact IDs, capture_id, and inherited tags.
    """
    db = _pkg._get_db()
    now = _now_iso()

    # Look up parent capture by capture_id
    parent_row = db.execute(
        "SELECT tags FROM memories WHERE capture_id = ? LIMIT 1",
        (params.capture_id,),
    ).fetchone()

    if parent_row is None:
        return f"Error: No memory found with capture_id '{params.capture_id}'"

    # Parse parent tags
    raw_tags = parent_row["tags"]
    if isinstance(raw_tags, str):
        try:
            parent_tags = json.loads(raw_tags)
        except (json.JSONDecodeError, TypeError):
            parent_tags = []
    else:
        parent_tags = raw_tags if raw_tags else []

    fact_ids: list[str] = []

    for fact in params.facts:
        fact_id = _make_id(fact.content)

        # Merge tags: parent_tags + extra_tags, deduplicated, order preserved
        merged_tags = list(dict.fromkeys(parent_tags + fact.extra_tags))

        # Determine memory_type and decay_rate
        memory_type = fact.memory_type or "unclassified"
        decay_rate = DECAY_RATES.get(memory_type, 0.10)

        db.execute(
            """INSERT INTO memories (
                id, content, category, tags, source, metadata,
                capture_id, source_capture_id,
                created_at, updated_at, node_id, client,
                memory_type, decay_rate, vitality, base_weight,
                status, accessed_at, access_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fact_id,
                fact.content,
                "fact",
                json.dumps(merged_tags),
                "decomposition",
                json.dumps({"source_capture_id": params.capture_id}),
                None,  # capture_id — these are not captures themselves
                params.capture_id,  # source_capture_id
                now,
                now,
                NODE_ID,
                CLIENT,
                memory_type,
                decay_rate,
                1.0,  # vitality
                1.0,  # base_weight
                "active",
                now,  # accessed_at
                0,  # access_count
            ),
        )

        fact_ids.append(fact_id)

        # Fire-and-forget embed (reference held in _background_tasks, PF-04)
        _pkg._spawn_task(asyncio.to_thread(_pkg._embed_and_store, fact_id, fact.content))

    db.commit()

    result = {
        "created": len(fact_ids),
        "fact_ids": fact_ids,
        "capture_id": params.capture_id,
        "parent_tags_inherited": parent_tags,
    }
    return json.dumps(result)


@mcp.tool(
    name="remind_me_decompose_batch",
    annotations={
        "title": "Get Undecomposed Memories",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_decompose_batch(params: DecomposeBatchInput) -> str:
    """Fetch a batch of captures that have not yet been decomposed into atomic facts.

    Returns memories that have a capture_id (are captures), are not themselves
    decomposed children, and have no children with matching source_capture_id.
    Claude can then review each and call remind_me_decompose with extracted facts.

    Args:
        params: Batch size (default 20, max 100).

    Returns:
        JSON string with memories array and total_undecomposed count.
    """
    db = _pkg._get_db()

    # Count total undecomposed captures
    total_row = db.execute(
        """SELECT COUNT(*) as cnt FROM memories m
           WHERE m.capture_id IS NOT NULL
             AND m.source_capture_id IS NULL
             AND NOT EXISTS (
                 SELECT 1 FROM memories c WHERE c.source_capture_id = m.capture_id
             )""",
    ).fetchone()
    total_undecomposed = total_row["cnt"]

    # Fetch batch
    rows = db.execute(
        """SELECT id, substr(content, 1, 500) as content_snippet, category, tags, capture_id
           FROM memories m
           WHERE m.capture_id IS NOT NULL
             AND m.source_capture_id IS NULL
             AND NOT EXISTS (
                 SELECT 1 FROM memories c WHERE c.source_capture_id = m.capture_id
             )
           LIMIT ?""",
        (params.batch_size,),
    ).fetchall()

    memories = []
    for row in rows:
        tags = row["tags"]
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except (json.JSONDecodeError, TypeError):
                tags = []
        memories.append({
            "id": row["id"],
            "content_snippet": row["content_snippet"],
            "category": row["category"],
            "tags": tags,
            "capture_id": row["capture_id"],
        })

    result = {
        "memories": memories,
        "total_undecomposed": total_undecomposed,
    }
    return json.dumps(result, indent=2)
