"""
remind_me_mcp.tools.normalize — ingest-time LLM normalization (FT-09, Phase 5b).

Raw imported content (chat/document imports) is often verbatim and noisy.
``remind_me_normalize_batch`` surfaces un-normalized imports for the calling
agent to distill into a {question, summary, resolution?, refs?} shape — the
LLM work happens client-side, exactly like ``remind_me_decompose`` already
does for atomic-fact extraction (no in-server LLM dependency, consistent
with the project's zero-ops design). ``remind_me_normalize_apply`` then
writes each distillation as a new memory, non-destructively linked back to
the raw row via a ``normalized_from`` metadata pointer — the same
dialog/summary linking idiom ``remind_me_auto_capture`` uses. The normalized
memory inherits its source's ``doc_id``/``chunk_index`` (so neighbor-aware
retrieval still finds it) and accepts its own ``entities`` list (FT-04),
since the raw import is never entity-linked automatically.

Patchable shared state and cross-module helpers are looked up through the
``remind_me_mcp.tools`` package namespace (``_pkg.<name>``) at call time so
monkeypatching ``remind_me_mcp.tools.<name>`` keeps working (HY-02).
"""

from __future__ import annotations

import asyncio
import json

from remind_me_mcp import tools as _pkg
from remind_me_mcp.config import CLIENT, NODE_ID
from remind_me_mcp.db import _make_id, _now_iso
from remind_me_mcp.models import (  # noqa: TC001  # FastMCP resolves these annotations at runtime for tool schemas
    NormalizeApplyInput,
    NormalizeBatchInput,
)
from remind_me_mcp.server import mcp

NORMALIZED_CATEGORY = "normalized"
"""``memories.category`` assigned to memories created by remind_me_normalize_apply."""

# Raw imports eligible for normalization: not superseded, from the file
# import pipeline (document_import/chat_import — FT-02), and not already
# normalized (no existing memory points back at it via normalized_from).
_UNNORMALIZED_WHERE = """
    m.superseded_by IS NULL
    AND m.deleted_at IS NULL
    AND m.source IN ('document_import', 'chat_import')
    AND NOT EXISTS (
        SELECT 1 FROM memories n WHERE json_extract(n.metadata, '$.normalized_from') = m.id
    )
"""


@mcp.tool(
    name="remind_me_normalize_batch",
    annotations={
        "title": "Get Un-normalized Imports",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_normalize_batch(params: NormalizeBatchInput) -> str:
    """Fetch a batch of raw imported memories that have not yet been normalized.

    Returns document/chat import memories (FT-02) with no linked
    normalization yet. Claude can review each raw chunk and call
    remind_me_normalize_apply with a distilled {question, summary,
    resolution?, refs?} to make noisy raw imports individually searchable
    in a cleaner form (Phase 5b) — the raw memory is kept, not replaced.

    Args:
        params: Batch size (default 20, max 100).

    Returns:
        JSON string with a memories array and total_unnormalized count.
    """
    db = _pkg._get_db()

    total_row = db.execute(
        f"SELECT COUNT(*) as cnt FROM memories m WHERE {_UNNORMALIZED_WHERE}"
    ).fetchone()

    rows = db.execute(
        f"""SELECT id, substr(content, 1, 1000) as content_snippet,
                   category, source, tags, metadata
            FROM memories m
            WHERE {_UNNORMALIZED_WHERE}
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
        metadata = row["metadata"]
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (json.JSONDecodeError, TypeError):
                metadata = {}
        memories.append({
            "id": row["id"],
            "content_snippet": row["content_snippet"],
            "category": row["category"],
            "source": row["source"],
            "tags": tags,
            "filename": metadata.get("filename") if isinstance(metadata, dict) else None,
        })

    result = {
        "memories": memories,
        "total_unnormalized": total_row["cnt"],
    }
    return json.dumps(result, indent=2)


@mcp.tool(
    name="remind_me_normalize_apply",
    annotations={
        "title": "Apply Normalizations",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def remind_me_normalize_apply(params: NormalizeApplyInput) -> str:
    """Write distilled normalizations as new memories linked back to their raw import.

    Each normalization creates a NEW memory (category 'normalized') whose
    content is the distilled {question, summary, resolution?} — the raw
    memory it was distilled from is left untouched and stays searchable in
    its own right. The link is metadata-only (normalized_from), the same
    non-destructive idiom remind_me_auto_capture uses for dialog/summary
    pairs, so remind_me_normalize_batch skips an already-normalized raw row
    on the next call (once ANY normalization points back at it). The new
    memory inherits the raw row's doc_id/chunk_index (so include_neighbors
    still finds it) and links any entities passed in the entry (so it's
    reachable via remind_me_entity/remind_me_entity_traverse).

    Args:
        params: A batch of {memory_id, question, summary, resolution?, refs?, entities?}.

    Returns:
        JSON string with per-entry results and any errors.
    """
    db = _pkg._get_db()
    now = _now_iso()

    results: list[dict] = []
    errors: list[dict] = []

    for entry in params.normalizations:
        raw_row = db.execute(
            "SELECT tags, doc_id, chunk_index FROM memories WHERE id = ?", (entry.memory_id,)
        ).fetchone()
        if raw_row is None:
            errors.append({"memory_id": entry.memory_id, "error": "memory not found"})
            continue

        raw_tags = raw_row["tags"]
        if isinstance(raw_tags, str):
            try:
                tags = json.loads(raw_tags)
            except (json.JSONDecodeError, TypeError):
                tags = []
        else:
            tags = raw_tags if raw_tags else []

        content_parts = [f"**Q:** {entry.question}", "", entry.summary]
        if entry.resolution:
            content_parts += ["", f"**Resolution:** {entry.resolution}"]
        content = "\n".join(content_parts)

        normalized_id = _make_id(content)
        metadata: dict = {
            "normalized_from": entry.memory_id,
            "question": entry.question,
            "refs": entry.refs,
        }
        if entry.resolution:
            metadata["resolution"] = entry.resolution

        db.execute(
            """INSERT OR IGNORE INTO memories
               (id, content, category, tags, source, metadata, created_at, updated_at,
                node_id, client, doc_id, chunk_index)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                normalized_id,
                content,
                NORMALIZED_CATEGORY,
                json.dumps(tags),
                "normalization",
                json.dumps(metadata),
                now,
                now,
                NODE_ID,
                CLIENT,
                raw_row["doc_id"],
                raw_row["chunk_index"],
            ),
        )

        # FT-04: upsert mentioned entities and record the mention links, same
        # as memory_add -- normalize_apply's raw source is never entity-linked
        # automatically, so without this the normalized memory would be
        # invisible to remind_me_entity/remind_me_entity_traverse.
        for ent in entry.entities:
            eid = _pkg._upsert_entity(
                db, ent.name, ent.kind, ent.aliases, node_id=NODE_ID, now=now
            )
            _pkg._link_memory_entity(db, normalized_id, eid, now)

        results.append({"memory_id": entry.memory_id, "normalized_id": normalized_id})

        _pkg._spawn_task(asyncio.to_thread(_pkg._embed_and_store, normalized_id, content))

    db.commit()

    return json.dumps({
        "normalized": len(results),
        "results": results,
        "errors": errors,
    })
