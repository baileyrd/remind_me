"""
remind_me_mcp.tools.capture — auto_capture / get_capture / decompose /
extract_batch / annotate tool handlers.

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
    AnnotateInput,
    AutoCaptureInput,
    DecomposeBatchInput,
    DecomposeInput,
    EntityInput,
    ExtractBatchInput,
)
from remind_me_mcp.server import mcp
from remind_me_mcp.tools._shared import log
from remind_me_mcp.vitality import DECAY_RATES


def _apply_entity_mentions(
    db, memory_id: str, entities: list[EntityInput], now: str
) -> int:
    """Upsert entities and link them to *memory_id* (FT-04). Does not commit.

    Args:
        db: An open SQLite connection.
        memory_id: The memory the entities are mentioned by.
        entities: Validated entity inputs ({name, kind?, aliases?}).
        now: Timestamp for created/updated rows.

    Returns:
        The number of NEW mention links created (existing links are ignored).
    """
    linked = 0
    for ent in entities:
        eid = _pkg._upsert_entity(
            db, ent.name, ent.kind, ent.aliases, node_id=NODE_ID, now=now
        )
        if _pkg._link_memory_entity(db, memory_id, eid, now):
            linked += 1
    return linked


def _maybe_link_entity_relation(
    db, subject: str | None, predicate: str | None, obj: str | None, now: str
) -> bool:
    """Best-effort link a typed entity-to-entity relation from an SPO triple (Phase 3).

    A memory's SPO triple is free text -- writing it doesn't imply the
    subject/object name known entities. This only records a relation edge
    when BOTH resolve to entities that already exist (typically because the
    same call's ``entities`` list -- via :func:`_apply_entity_mentions`,
    called just before this -- or an earlier annotation already upserted
    them). Facts whose subject/object don't name a known entity keep working
    exactly as before this feature existed: a memory-level triple with no
    graph edge.

    Args:
        db: An open SQLite connection.
        subject: The fact's SPO subject text, if any.
        predicate: The fact's SPO predicate text (the relation label), if any.
        obj: The fact's SPO object text, if any.
        now: Timestamp for the relation row. Does NOT commit.

    Returns:
        True if a relation edge was written (or already existed) because both
        sides resolved; False if the triple is incomplete or either side
        doesn't name a known entity.
    """
    if not subject or not predicate or not obj:
        return False
    subj_entity = _pkg._resolve_entity(db, subject)
    obj_entity = _pkg._resolve_entity(db, obj)
    if subj_entity is None or obj_entity is None:
        return False
    _pkg._upsert_entity_relation(
        db, subj_entity["id"], predicate, obj_entity["id"], node_id=NODE_ID, now=now
    )
    return True


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
    receive additional tags and a memory_type classification. A fact whose
    SPO triple contradicts an existing one (same subject+predicate, different
    object — e.g. "I live in Seattle" vs. "I moved to Boston") supersedes it
    (gap #5).

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
    entities_linked = 0
    relations_linked = 0
    superseded_ids: list[str] = []

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
                status, accessed_at, access_count,
                subject, predicate, object
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                fact.subject,
                fact.predicate,
                fact.object,
            ),
        )

        fact_ids.append(fact_id)

        # FT-04: upsert mentioned entities and link them to this fact.
        entities_linked += _apply_entity_mentions(db, fact_id, fact.entities, now)

        # Typed entity-to-entity relation, when the SPO subject/object both
        # name known entities (Phase 3).
        if _maybe_link_entity_relation(db, fact.subject, fact.predicate, fact.object, now):
            relations_linked += 1

        # Contradiction-based supersession (gap #5): a decomposed fact whose
        # SPO triple conflicts with an existing one (same subject+predicate,
        # different object) supersedes it.
        superseded_ids.extend(
            _pkg._supersede_contradicting_facts(
                db, fact_id, fact.subject, fact.predicate, fact.object, now
            )
        )

        # Fire-and-forget embed (reference held in _background_tasks, PF-04)
        _pkg._spawn_task(asyncio.to_thread(_pkg._embed_and_store, fact_id, fact.content))

    db.commit()

    result = {
        "created": len(fact_ids),
        "fact_ids": fact_ids,
        "capture_id": params.capture_id,
        "parent_tags_inherited": parent_tags,
        "relations_linked": relations_linked,
        "entities_linked": entities_linked,
        "superseded_ids": superseded_ids,
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


# ---------------------------------------------------------------------------
# Entity & relation extraction tools (FT-04)
# ---------------------------------------------------------------------------

# Memories eligible for entity/SPO annotation: not superseded, not raw
# verbatim dialogs (annotate the summary/facts instead), and not yet
# annotated — no SPO triple AND no entity mentions. A category='fact' row
# that already has SPO is excluded by the subject/predicate/object check.
_UNANNOTATED_WHERE = """
    m.superseded_by IS NULL
    AND m.deleted_at IS NULL
    AND m.category != 'dialog'
    AND m.subject IS NULL AND m.predicate IS NULL AND m.object IS NULL
    AND NOT EXISTS (
        SELECT 1 FROM memory_entities me WHERE me.memory_id = m.id
    )
"""


@mcp.tool(
    name="remind_me_extract_batch",
    annotations={
        "title": "Get Memories Needing Entity Extraction",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_extract_batch(params: ExtractBatchInput) -> str:
    """Fetch a batch of memories that have no structured triple and no entity mentions yet.

    Returns memories (facts, documents, summaries — anything except raw
    dialogs and superseded rows) whose subject/predicate/object columns are
    empty and that mention no entities. Claude can review each and call
    remind_me_annotate with extracted {subject, predicate, object} triples
    and {name, kind, aliases} entities to build the knowledge-graph layer.

    Args:
        params: Batch size (default 20, max 100).

    Returns:
        JSON string with a memories array and total_unannotated count.
    """
    db = _pkg._get_db()

    total_row = db.execute(
        f"SELECT COUNT(*) as cnt FROM memories m WHERE {_UNANNOTATED_WHERE}"
    ).fetchone()

    rows = db.execute(
        f"""SELECT id, substr(content, 1, 500) as content_snippet,
                   category, memory_type, tags
            FROM memories m
            WHERE {_UNANNOTATED_WHERE}
            ORDER BY m.created_at DESC
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
            "memory_type": row["memory_type"],
            "tags": tags,
        })

    result = {
        "memories": memories,
        "total_unannotated": total_row["cnt"],
    }
    return json.dumps(result, indent=2)


@mcp.tool(
    name="remind_me_annotate",
    annotations={
        "title": "Annotate Memories with Triples & Entities",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_annotate(params: AnnotateInput) -> str:
    """Apply structured annotations (subject/predicate/object triples and entity mentions) to existing memories.

    For each annotation, the provided SPO fields are written onto the memory
    (omitted fields are left unchanged), mentioned entities are upserted into
    the entity table (deterministic ids — the same name always maps to the
    same entity, aliases union-merge), and mention links are recorded.
    updated_at is bumped so the changes sync to peers. If the memory's
    (possibly just-annotated) triple contradicts another fact — same
    subject+predicate, different object — that other fact is superseded
    (gap #5).

    Args:
        params: A batch of {memory_id, subject?, predicate?, object?, entities?}.

    Returns:
        JSON string with per-memory results and any errors.
    """
    db = _pkg._get_db()
    now = _now_iso()

    results: list[dict] = []
    errors: list[dict] = []

    for ann in params.annotations:
        row = db.execute(
            "SELECT id FROM memories WHERE id = ?", (ann.memory_id,)
        ).fetchone()
        if row is None:
            errors.append({"memory_id": ann.memory_id, "error": "memory not found"})
            continue

        sets: list[str] = []
        bindings: list = []
        for col, val in (
            ("subject", ann.subject),
            ("predicate", ann.predicate),
            ("object", ann.object),
        ):
            if val is not None:
                sets.append(f"{col} = ?")
                bindings.append(val)
        sets.append("updated_at = ?")
        bindings.append(now)
        db.execute(
            f"UPDATE memories SET {', '.join(sets)} WHERE id = ?",
            (*bindings, ann.memory_id),
        )

        linked = _apply_entity_mentions(db, ann.memory_id, ann.entities, now)

        # Typed entity-to-entity relation (Phase 3). subject/predicate/object
        # are each optionally omitted (left unchanged) on this call, so
        # re-read the memory's full current triple rather than relying only
        # on this annotation's possibly-partial fields.
        current = db.execute(
            "SELECT subject, predicate, object FROM memories WHERE id = ?",
            (ann.memory_id,),
        ).fetchone()
        relation_linked = _maybe_link_entity_relation(
            db, current["subject"], current["predicate"], current["object"], now
        )

        # Contradiction-based supersession (gap #5), same reasoning as
        # above: re-read the full current triple rather than the possibly-
        # partial annotation fields.
        superseded = _pkg._supersede_contradicting_facts(
            db, ann.memory_id, current["subject"], current["predicate"], current["object"], now
        )

        results.append({
            "memory_id": ann.memory_id,
            "entities_linked": linked,
            "relation_linked": relation_linked,
            "superseded_ids": superseded,
        })

    db.commit()

    return json.dumps({
        "annotated": len(results),
        "results": results,
        "errors": errors,
    })
