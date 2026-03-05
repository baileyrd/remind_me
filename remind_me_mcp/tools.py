"""
remind_me_mcp.tools — All 19 MCP tool handlers and 2 resource handlers.

All handlers are registered on the `mcp` instance imported from server.py.
This module imports mcp from server (not the other way around) to avoid
circular imports.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from typing import Any

from remind_me_mcp.config import CLIENT, NODE_ID
from remind_me_mcp.db import (
    _embed_and_store,
    _get_db,
    _make_id,
    _now_iso,
    _row_to_dict,
    _semantic_search,
)
from remind_me_mcp.formatting import _fmt_memories, _fmt_memory_md
from remind_me_mcp.importer import import_chat_file, import_directory
from remind_me_mcp.models import (
    AutoCaptureInput,
    BulkImportDirInput,
    ChatImportInput,
    DecomposeBatchInput,
    DecomposeInput,
    MemoryAddInput,
    MemoryDeleteInput,
    MemoryListInput,
    MemorySearchInput,
    MemoryStatsInput,
    MemoryUpdateInput,
    ReclassifyBatchInput,
    ReclassifyInput,
    ResponseFormat,
    VitalityReportInput,
)
from remind_me_mcp.pid import get_server_status
from remind_me_mcp.retrieval import apply_token_budget, rank_rrf
from remind_me_mcp.server import mcp
from remind_me_mcp.updater import pop_update_notice
from remind_me_mcp.vitality import DECAY_RATES, record_access

log = logging.getLogger("remind_me_mcp.tools")

EMBED_BATCH_SIZE = 32


# ---------------------------------------------------------------------------
# Filter helper (applied before RRF ranking)
# ---------------------------------------------------------------------------


def _apply_filters(
    memories: list[dict],
    category: str | None,
    tags: list[str] | None,
) -> list[dict]:
    """Filter memories by category and/or tags before ranking.

    Args:
        memories: List of memory dicts to filter.
        category: If set, only keep memories with this category.
        tags: If set, only keep memories that have ALL of these tags.

    Returns:
        Filtered list of memory dicts.
    """
    result = memories
    if category:
        result = [m for m in result if m["category"] == category]
    if tags:
        tag_set = set(tags)
        result = [m for m in result if tag_set.issubset(set(m.get("tags", [])))]
    return result


# ---------------------------------------------------------------------------
# Update notice helper
# ---------------------------------------------------------------------------


def _maybe_update_notice(response: str) -> str:
    """Append a one-shot update notice to the response if available.

    The notice fires once (on the first tool call after startup) then clears.

    Args:
        response: The original tool response string.

    Returns:
        The response, possibly with an appended update notice.
    """
    notice = pop_update_notice()
    if notice:
        return response + "\n\n---\n" + notice
    return response


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


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
    db = _get_db()
    mem_id = _make_id(params.content)
    now = _now_iso()
    try:
        db.execute(
            """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at, node_id, client)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            ),
        )
        db.commit()
    except sqlite3.IntegrityError as e:
        log.error("Failed to add memory: %s", e)
        return "Error: Could not add memory — a memory with this content may already exist."
    except sqlite3.OperationalError as e:
        log.error("Database error adding memory: %s", e)
        return f"Error: Database operation failed — {e}"
    await asyncio.to_thread(_embed_and_store, mem_id, params.content)
    return _maybe_update_notice(f"✓ Memory stored with id `{mem_id}` in category '{params.category}'.")


@mcp.tool(
    name="remind_me_search",
    annotations={
        "title": "Search Memories",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def memory_search(params: MemorySearchInput) -> str:
    """Hybrid search across all stored memories. Combines FTS5 keyword matching with semantic vector similarity.

    If semantic search is available (embedding model loaded), results from both are merged
    and deduplicated, with keyword matches boosted. Falls back to FTS5-only if embeddings
    are unavailable.

    Supports FTS5 query syntax for keyword search: AND, OR, NOT, "exact phrase", prefix*.

    Args:
        params (MemorySearchInput): Search query and optional filters.

    Returns:
        str: Matching memories in the requested format.
    """
    from remind_me_mcp.embeddings import _get_embedder

    db = _get_db()

    # --- FTS5 keyword search ---
    fts_memories: list[dict] = []
    try:
        rows = db.execute(
            """SELECT m.* FROM memories m
               JOIN memories_fts fts ON m.rowid = fts.rowid
               WHERE memories_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (params.query, params.limit),
        ).fetchall()
        fts_memories = [_row_to_dict(r) for r in rows]
    except sqlite3.OperationalError as e:
        # FTS query syntax error — fall through to semantic-only
        log.warning("FTS5 query syntax error for query %r: %s", params.query, e)

    # --- Semantic vector search ---
    sem_memories = await asyncio.to_thread(_semantic_search, params.query, limit=params.limit)

    # --- Tag search method on raw results before RRF ---
    fts_ids = {m["id"] for m in fts_memories}
    sem_ids = {m["id"] for m in sem_memories}
    for m in fts_memories:
        m["_search_method"] = "keyword"
    for m in sem_memories:
        m["_search_method"] = "semantic"

    # --- Apply category/tag filters BEFORE RRF ranking ---
    filtered_fts = _apply_filters(fts_memories, params.category, params.tags)
    filtered_sem = _apply_filters(sem_memories, params.category, params.tags)

    # --- Dormant exclusion BEFORE RRF ranking ---
    if not params.include_dormant:
        filtered_fts = [m for m in filtered_fts if m.get("status") != "dormant"]
        filtered_sem = [m for m in filtered_sem if m.get("status") != "dormant"]

    # --- Min vitality filter ---
    if params.min_vitality > 0:
        filtered_fts = [
            m for m in filtered_fts
            if (m.get("vitality") or 1.0) >= params.min_vitality
        ]
        filtered_sem = [
            m for m in filtered_sem
            if (m.get("vitality") or 1.0) >= params.min_vitality
        ]

    # --- RRF ranking ---
    ranked = rank_rrf(filtered_fts, filtered_sem)

    # Mark hybrid results (appeared in both FTS and semantic)
    for m in ranked:
        mid = m["id"]
        if mid in fts_ids and mid in sem_ids:
            m["_search_method"] = "hybrid"

    # --- Apply limit, then token budget ---
    ranked = ranked[:params.limit]

    if params.token_budget == 0:
        envelope = apply_token_budget(ranked, 0)
    else:
        envelope = apply_token_budget(ranked, params.token_budget)

    # --- Record access for returned results (fire-and-forget) ---
    returned_ids = [m["id"] for m in envelope["memories"]]
    if returned_ids:
        async def _record_accesses(ids: list[str]) -> None:
            for mid in ids:
                await asyncio.to_thread(record_access, mid)

        asyncio.create_task(_record_accesses(returned_ids))

    # --- Format response ---
    if params.response_format == ResponseFormat.JSON:
        return json.dumps(
            {
                "total_candidates": envelope["total_candidates"],
                "returned": envelope["returned"],
                "trimmed": envelope["trimmed"],
                "tokens_used": envelope["tokens_used"],
                "budget": envelope["budget"],
                "memories": envelope["memories"],
            },
            indent=2,
            default=str,
        )

    if not envelope["memories"]:
        return "_No memories found._"

    parts: list[str] = []
    sem_available = len(sem_memories) > 0 or _get_embedder() is not None
    method_label = "hybrid (keyword + semantic)" if sem_available else "keyword only"
    parts.append(f"**{envelope['returned']} results** via {method_label} search")
    if envelope["trimmed"] > 0:
        parts.append(
            f"_{envelope['returned']} of {envelope['total_candidates']} candidates "
            f"(trimmed {envelope['trimmed']}, ~{envelope['tokens_used']}/{envelope['budget']} tokens)_\n"
        )
    else:
        parts.append(f"_~{envelope['tokens_used']} tokens used (budget: {envelope['budget']})_\n")

    for m in envelope["memories"]:
        method = m.pop("_search_method", "")
        dist = m.pop("semantic_distance", None)
        badge = {"hybrid": "⚡", "semantic": "🔮", "keyword": "🔤"}.get(method, "")
        parts.append(_fmt_memory_md(m).rstrip())
        extras = [badge + method]
        if dist is not None:
            extras.append(f"distance: {dist:.3f}")
        parts.append(f"_{' · '.join(extras)}_\n")

    return _maybe_update_notice("\n---\n".join(parts))


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
    db = _get_db()
    conditions: list[str] = []
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

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
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
    db = _get_db()
    row = db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
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
    db = _get_db()
    row = db.execute("SELECT * FROM memories WHERE id = ?", (params.memory_id,)).fetchone()
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
        await asyncio.to_thread(_embed_and_store, params.memory_id, params.content)
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
    """Permanently delete a memory by ID.

    Args:
        params (MemoryDeleteInput): The memory ID to delete.

    Returns:
        str: Confirmation or error message.
    """
    db = _get_db()
    result = db.execute("DELETE FROM memories WHERE id = ?", (params.memory_id,))
    db.commit()
    if result.rowcount == 0:
        return f"Memory `{params.memory_id}` not found."
    return f"✓ Memory `{params.memory_id}` deleted."


@mcp.tool(
    name="remind_me_import_chat",
    annotations={
        "title": "Import Chat Export",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def memory_import_chat(params: ChatImportInput) -> str:
    """Import a chat export file (JSON, JSONL, or Markdown) into memory.

    Supports Claude's export format, OpenAI's export format, and generic {role, content} message arrays.
    Deduplicates by file hash — re-importing the same file is a no-op.

    Args:
        params (ChatImportInput): File path, extraction mode, and tagging options.

    Returns:
        str: Import statistics.
    """
    try:
        result = import_chat_file(
            file_path=params.file_path,
            category=params.category,
            tags=params.tags,
            extract_mode=params.extract_mode,
            max_length=params.max_length,
        )
    except FileNotFoundError:
        return json.dumps({"status": "error", "error": f"File not found: {params.file_path}"})
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        log.error("Import parse error for %s: %s", params.file_path, e)
        return json.dumps({"status": "error", "error": f"Failed to parse file: {e}"})
    return json.dumps(result, indent=2)


@mcp.tool(
    name="remind_me_import_directory",
    annotations={
        "title": "Bulk Import Chat Directory",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def memory_import_directory(params: BulkImportDirInput) -> str:
    """Bulk import all chat export files from a directory.

    Scans for .json, .jsonl, .md, .markdown, and .txt files. Skips
    already-imported files (hash-based deduplication). Delegates to the
    shared import_directory() function in importer.py (DRY).

    Args:
        params (BulkImportDirInput): Directory path and import options.

    Returns:
        str: JSON summary with keys: files_processed, imported, skipped,
        errors, total_memories_created, details.
    """
    summary = await import_directory(
        directory=params.directory,
        category=params.category,
        tags=params.tags,
        extract_mode=params.extract_mode,
        max_length=params.max_length,
        recursive=params.recursive,
    )
    return json.dumps(summary, indent=2)


@mcp.tool(
    name="remind_me_stats",
    annotations={
        "title": "Memory Statistics",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def memory_stats(params: MemoryStatsInput) -> str:
    """Get statistics about the memory store: total count, categories, sources, recent activity.

    Args:
        params (MemoryStatsInput): Response format preference.

    Returns:
        str: Statistics in the requested format.
    """
    from remind_me_mcp.config import DB_PATH

    db = _get_db()
    total = db.execute("SELECT COUNT(*) as cnt FROM memories").fetchone()["cnt"]
    categories = db.execute(
        "SELECT category, COUNT(*) as cnt FROM memories GROUP BY category ORDER BY cnt DESC"
    ).fetchall()
    sources = db.execute(
        "SELECT source, COUNT(*) as cnt FROM memories GROUP BY source ORDER BY cnt DESC"
    ).fetchall()
    imports = db.execute("SELECT COUNT(*) as cnt FROM chat_imports").fetchone()["cnt"]
    recent = db.execute(
        "SELECT id, category, substr(content, 1, 80) as preview, created_at FROM memories ORDER BY created_at DESC LIMIT 5"
    ).fetchall()

    try:
        db_size = round(DB_PATH.stat().st_size / 1_048_576, 2) if DB_PATH.exists() else 0
    except OSError as e:
        log.warning("Could not stat DB file: %s", e)
        db_size = 0

    data = {
        "total_memories": total,
        "total_imports": imports,
        "categories": {r["category"]: r["cnt"] for r in categories},
        "sources": {r["source"]: r["cnt"] for r in sources},
        "recent": [dict(r) for r in recent],
        "db_path": str(DB_PATH),
        "db_size_mb": db_size,
    }

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(data, indent=2, default=str)

    lines = [
        "## Memory Store Statistics",
        "",
        f"**Total memories:** {total}",
        f"**Total imports:** {imports}",
        f"**Database:** `{DB_PATH}` ({data['db_size_mb']} MB)",
        "",
        "### Categories",
    ]
    for cat, cnt in data["categories"].items():
        lines.append(f"- **{cat}**: {cnt}")
    lines.append("")
    lines.append("### Sources")
    for src, cnt in data["sources"].items():
        lines.append(f"- **{src}**: {cnt}")
    lines.append("")
    lines.append("### Recent Memories")
    for r in data["recent"]:
        lines.append(f"- `{r['id']}` [{r['category']}] {r['preview']}…")
    return _maybe_update_notice("\n".join(lines))


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
    db = _get_db()
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
    await asyncio.to_thread(_embed_and_store, summary_id, params.summary)
    await asyncio.to_thread(_embed_and_store, dialog_id, params.conversation[:2000])

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
    db = _get_db()
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


@mcp.tool(
    name="remind_me_reindex",
    annotations={
        "title": "Rebuild Vector Embeddings",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_reindex() -> str:
    """Rebuild vector embeddings for all memories that don't have them yet.

    Run this after installing the embedding dependencies, or after importing
    memories that were added before semantic search was enabled.
    Existing embeddings are preserved; only missing ones are generated.

    Returns:
        str: Summary of how many embeddings were created.
    """
    from remind_me_mcp.embeddings import _get_embedder

    embedder = _get_embedder()
    if embedder is None:
        return (
            "Embedding model not available. Install dependencies:\n"
            "```\npip install onnxruntime tokenizers huggingface-hub numpy sqlite-vec\n```\n"
            "The model (~80MB) downloads automatically on first use."
        )

    db = _get_db()
    # Find memories without embeddings
    all_rows = db.execute("SELECT id, rowid, content FROM memories").fetchall()
    existing_vecs = set()
    try:
        vec_rows = db.execute("SELECT rowid FROM memories_vec").fetchall()
        existing_vecs = {r[0] for r in vec_rows}
    except sqlite3.OperationalError as e:
        log.debug("memories_vec table not available: %s", e)

    missing = [(r["id"], r["rowid"], r["content"]) for r in all_rows if r["rowid"] not in existing_vecs]

    if not missing:
        return f"✓ All {len(all_rows)} memories already have embeddings."

    created = 0
    for batch_start in range(0, len(missing), EMBED_BATCH_SIZE):
        batch = missing[batch_start : batch_start + EMBED_BATCH_SIZE]
        ids = [item[0] for item in batch]
        rowids = [item[1] for item in batch]
        texts = [item[2][:2000] for item in batch]
        try:
            vecs = await asyncio.to_thread(embedder.embed, texts)
            for i, (_mem_id, rowid) in enumerate(zip(ids, rowids, strict=True)):
                vec_bytes = vecs[i].tobytes()
                db.execute(
                    "INSERT OR REPLACE INTO memories_vec(rowid, embedding) VALUES (?, ?)",
                    (rowid, vec_bytes),
                )
                created += 1
        except (sqlite3.OperationalError, ValueError, TypeError) as e:
            log.warning("Failed to embed batch starting at %s: %s", ids[0], e)

    db.commit()
    return (
        f"✓ Reindex complete.\n\n"
        f"**Total memories:** {len(all_rows)}\n"
        f"**Already embedded:** {len(existing_vecs)}\n"
        f"**Newly embedded:** {created}\n"
        f"**Failed:** {len(missing) - created}"
    )


@mcp.tool(
    name="remind_me_server_status",
    annotations={
        "title": "Server Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_server_status() -> str:
    """Check the status of Remind Me services: whether the UI dashboard server is running, the database path, and connection info.

    Use this to verify the system is operational or to get the dashboard URL.

    Returns:
        str: Status information about running instances.
    """
    from remind_me_mcp.config import EMBEDDING_MODEL
    from remind_me_mcp.embeddings import _get_embedder

    status = get_server_status()
    lines = ["## Remind Me Server Status\n"]

    if status["ui_server"] == "running":
        lines.append(f"**Dashboard UI:** ✓ Running at {status['ui_url']}")
        lines.append(f"**UI PID:** {status['ui_pid']}")
        lines.append(f"**Started:** {status['ui_started']}")
    else:
        lines.append("**Dashboard UI:** ✗ Not running")
        lines.append("_Start with: `python remind_me_mcp.py --serve-ui`_")

    lines.append(f"\n**Database:** `{status['db_path']}`")
    lines.append(f"**DB exists:** {'yes' if status['db_exists'] else 'no'}")
    lines.append("\n**MCP (stdio):** ✓ Active (this connection)")

    # Embedding status
    embedder = _get_embedder()
    if embedder is not None:
        db = _get_db()
        total_mems = db.execute("SELECT COUNT(*) as cnt FROM memories").fetchone()["cnt"]
        try:
            total_vecs = db.execute("SELECT COUNT(*) as cnt FROM memories_vec").fetchone()["cnt"]
        except sqlite3.OperationalError as e:
            log.debug("memories_vec table not available for status check: %s", e)
            total_vecs = 0
        lines.append(f"\n**Semantic search:** ✓ Enabled ({EMBEDDING_MODEL})")
        lines.append(f"**Embeddings:** {total_vecs}/{total_mems} memories indexed")
        if total_vecs < total_mems:
            lines.append(f"_Run `remind_me_reindex` to embed the remaining {total_mems - total_vecs} memories._")
    else:
        lines.append("\n**Semantic search:** ✗ Unavailable (install onnxruntime, tokenizers, huggingface-hub, numpy, sqlite-vec)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Vitality report tool (Phase 11 Plan 03)
# ---------------------------------------------------------------------------


@mcp.tool(
    name="remind_me_vitality_report",
    annotations={
        "title": "Vitality Report",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_vitality_report(params: VitalityReportInput) -> str:
    """Generate a vault health report with vitality metrics, dormant counts, and decay distribution.

    Provides an overview of the memory vault's health including active/dormant
    counts, average vitality, vitality distribution across buckets, and a
    breakdown by memory type.

    Args:
        params: Report options including response format.

    Returns:
        str: Vault health report in the requested format (JSON or markdown).
    """
    db = _get_db()

    # Core counts
    total = db.execute("SELECT COUNT(*) as cnt FROM memories").fetchone()["cnt"]
    active_count = db.execute(
        "SELECT COUNT(*) as cnt FROM memories WHERE status = 'active'"
    ).fetchone()["cnt"]
    dormant_count = db.execute(
        "SELECT COUNT(*) as cnt FROM memories WHERE status = 'dormant'"
    ).fetchone()["cnt"]

    # Average vitality
    avg_row = db.execute("SELECT AVG(vitality) as avg_v FROM memories").fetchone()
    avg_vitality = round(avg_row["avg_v"], 2) if avg_row["avg_v"] is not None else 0.0

    # Decay distribution by memory_type
    type_rows = db.execute(
        "SELECT memory_type, COUNT(*) as cnt FROM memories GROUP BY memory_type"
    ).fetchall()
    decay_distribution = {r["memory_type"]: r["cnt"] for r in type_rows}

    # Vitality buckets
    bucket_ranges = [
        ("0.00-0.05", 0.0, 0.05),
        ("0.05-0.25", 0.05, 0.25),
        ("0.25-0.50", 0.25, 0.50),
        ("0.50-0.75", 0.50, 0.75),
        ("0.75-1.00", 0.75, 1.01),  # 1.01 to include vitality=1.0
    ]
    vitality_buckets: dict[str, int] = {}
    for label, low, high in bucket_ranges:
        count = db.execute(
            "SELECT COUNT(*) as cnt FROM memories WHERE vitality >= ? AND vitality < ?",
            (low, high),
        ).fetchone()["cnt"]
        vitality_buckets[label] = count

    # Vault health score
    health_pct = round(active_count / total * 100) if total > 0 else 0
    vault_health_score = f"{health_pct}%"

    data = {
        "total_memories": total,
        "active_count": active_count,
        "dormant_count": dormant_count,
        "average_vitality": avg_vitality,
        "vault_health_score": vault_health_score,
        "decay_distribution": decay_distribution,
        "vitality_buckets": vitality_buckets,
    }

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(data, indent=2, default=str)

    # Markdown format
    lines = [
        "## Vault Vitality Report",
        "",
        f"**Total memories:** {total}",
        f"**Active:** {active_count}",
        f"**Dormant:** {dormant_count}",
        f"**Vault health:** {vault_health_score}",
        f"**Average vitality:** {avg_vitality:.2f}",
        "",
        "### Vitality Distribution",
        "",
    ]
    for label, count in vitality_buckets.items():
        bar = "#" * min(count, 40)
        lines.append(f"  {label}: {bar} ({count})")
    lines.append("")
    lines.append("### Memory Type Distribution")
    lines.append("")
    for mtype, count in sorted(decay_distribution.items()):
        lines.append(f"- **{mtype}**: {count}")

    return _maybe_update_notice("\n".join(lines))


# ---------------------------------------------------------------------------
# Update tools
# ---------------------------------------------------------------------------


@mcp.tool(
    name="remind_me_check_update",
    annotations={
        "title": "Check for Updates",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def remind_me_check_update() -> str:
    """Check if a newer version of remind-me-mcp is available on origin/main.

    Fetches from the remote repository and compares commits. This is a
    read-only operation — it does not modify any files.

    Returns:
        str: Markdown-formatted version status with commit comparison.
    """
    from remind_me_mcp.updater import check_for_update

    status = await asyncio.to_thread(check_for_update)

    if status.error:
        return f"**Update check failed:** {status.error}"

    lines = ["## remind-me-mcp Version Status\n"]
    lines.append(f"**Installed version:** `{status.installed_version}`")
    lines.append(f"**Local commit:** `{status.local_commit}`")
    lines.append(f"**Remote commit:** `{status.remote_commit}`")

    if status.update_available:
        lines.append(
            f"\n**Update available** — {status.commits_behind} "
            f"commit{'s' if status.commits_behind != 1 else ''} behind"
        )
        if status.commit_messages:
            lines.append("\n### Recent changes")
            for msg in status.commit_messages[:10]:
                lines.append(f"- `{msg}`")
        lines.append(
            "\nRun `remind_me_self_update` to pull and install the latest version."
        )
    else:
        lines.append("\n**Up to date.**")

    if status.repo_path:
        lines.append(f"\n_Repository: `{status.repo_path}`_")

    return "\n".join(lines)


@mcp.tool(
    name="remind_me_self_update",
    annotations={
        "title": "Self-Update",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def remind_me_self_update(force: bool = False) -> str:
    """Pull the latest changes from origin/main and reinstall the package.

    Performs ``git pull --ff-only`` followed by ``pip install -e .``.
    Refuses to run if the working tree has uncommitted changes, unless
    ``force=True`` is passed.

    After a successful update, the MCP server should be restarted for
    changes to take effect.

    Args:
        force: Skip dirty-tree check if True. Defaults to False.

    Returns:
        str: Markdown-formatted result with version change and restart instructions.
    """
    from remind_me_mcp.updater import perform_update

    result = await asyncio.to_thread(perform_update, force=force)

    if not result.success:
        return f"**Update failed:** {result.error}"

    lines = ["## Update Successful\n"]
    lines.append(f"**Previous:** `{result.previous_version}` (commit `{result.previous_commit}`)")
    lines.append(f"**Updated to:** `{result.new_version}` (commit `{result.new_commit}`)")

    if result.restart_required:
        lines.append(
            "\n**Restart required.** The MCP server must be restarted "
            "for the new version to take effect."
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Classification tools (Phase 11 Plan 02)
# ---------------------------------------------------------------------------


@mcp.tool(
    name="remind_me_reclassify",
    annotations={
        "title": "Reclassify Memories",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_reclassify(params: ReclassifyInput) -> str:
    """Apply memory type classifications to one or more memories.

    For each classification, sets the memory's memory_type and updates its
    decay_rate to match the per-category rate from the DECAY_RATES table.
    Classification is idempotent -- reclassifying a memory overwrites its
    previous type and decay rate.

    Args:
        params: List of {memory_id, memory_type} classification pairs.

    Returns:
        JSON string with updated count, not_found IDs, and total processed.
    """
    db = _get_db()
    now = _now_iso()
    updated = 0
    not_found: list[str] = []

    for classification in params.classifications:
        row = db.execute(
            "SELECT id FROM memories WHERE id = ?",
            (classification.memory_id,),
        ).fetchone()

        if row is None:
            not_found.append(classification.memory_id)
            continue

        decay_rate = DECAY_RATES.get(classification.memory_type, 0.10)
        db.execute(
            "UPDATE memories SET memory_type = ?, decay_rate = ?, updated_at = ? WHERE id = ?",
            (classification.memory_type, decay_rate, now, classification.memory_id),
        )
        updated += 1

    db.commit()

    result = {
        "updated": updated,
        "not_found": not_found,
        "total": len(params.classifications),
    }
    return json.dumps(result)


@mcp.tool(
    name="remind_me_reclassify_batch",
    annotations={
        "title": "Get Unclassified Memories",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_reclassify_batch(params: ReclassifyBatchInput) -> str:
    """Fetch a batch of unclassified memories for Claude to classify.

    Returns memories with memory_type='unclassified' so Claude can review
    their content and call remind_me_reclassify with appropriate types.
    Each memory includes its id, a content snippet (first 500 chars),
    category, and tags.

    Args:
        params: Batch size (default 20, max 100).

    Returns:
        JSON string with memories array and total_unclassified count.
    """
    db = _get_db()

    # Get total count of unclassified memories
    total_row = db.execute(
        "SELECT COUNT(*) as cnt FROM memories WHERE memory_type = 'unclassified'",
    ).fetchone()
    total_unclassified = total_row["cnt"]

    # Fetch batch
    rows = db.execute(
        "SELECT id, substr(content, 1, 500) as content_snippet, category, tags "
        "FROM memories WHERE memory_type = 'unclassified' LIMIT ?",
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
        })

    result = {
        "memories": memories,
        "total_unclassified": total_unclassified,
    }
    return json.dumps(result, indent=2)


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
    db = _get_db()
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

        # Fire-and-forget embed
        asyncio.create_task(
            asyncio.to_thread(_embed_and_store, fact_id, fact.content)
        )

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
    db = _get_db()

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
# Resource handlers
# ---------------------------------------------------------------------------


@mcp.resource("memory://stats")
async def resource_stats() -> str:
    """Quick stats for the memory store."""
    from remind_me_mcp.config import DB_PATH

    db = _get_db()
    total = db.execute("SELECT COUNT(*) as cnt FROM memories").fetchone()["cnt"]
    return json.dumps({"total_memories": total, "db_path": str(DB_PATH)})


@mcp.resource("memory://categories")
async def resource_categories() -> str:
    """List all memory categories with counts."""
    db = _get_db()
    rows = db.execute("SELECT category, COUNT(*) as cnt FROM memories GROUP BY category ORDER BY cnt DESC").fetchall()
    return json.dumps({r["category"]: r["cnt"] for r in rows}, indent=2)


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "memory_add",
    "memory_search",
    "memory_list",
    "memory_get",
    "memory_update",
    "memory_delete",
    "memory_import_chat",
    "memory_import_directory",
    "memory_stats",
    "remind_me_auto_capture",
    "remind_me_get_capture",
    "remind_me_reindex",
    "remind_me_server_status",
    "remind_me_check_update",
    "remind_me_self_update",
    "remind_me_reclassify",
    "remind_me_reclassify_batch",
    "remind_me_vitality_report",
    "remind_me_decompose",
    "remind_me_decompose_batch",
    "resource_stats",
    "resource_categories",
]
