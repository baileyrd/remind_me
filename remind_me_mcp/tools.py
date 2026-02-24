"""
remind_me_mcp.tools — All 13 MCP tool handlers and 2 resource handlers.

All handlers are registered on the `mcp` instance imported from server.py.
This module imports mcp from server (not the other way around) to avoid
circular imports.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from remind_me_mcp.db import (
    _embed_and_store,
    _get_db,
    _make_id,
    _now_iso,
    _row_to_dict,
    _semantic_search,
)
from remind_me_mcp.formatting import _fmt_memories, _fmt_memory_md
from remind_me_mcp.importer import import_chat_file
from remind_me_mcp.models import (
    AutoCaptureInput,
    BulkImportDirInput,
    ChatImportInput,
    MemoryAddInput,
    MemoryDeleteInput,
    MemoryListInput,
    MemorySearchInput,
    MemoryStatsInput,
    MemoryUpdateInput,
    ResponseFormat,
)
from remind_me_mcp.pid import get_server_status
from remind_me_mcp.server import mcp

log = logging.getLogger("remind_me_mcp.tools")

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
    db.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            mem_id,
            params.content,
            params.category,
            json.dumps(params.tags),
            params.source,
            json.dumps(params.metadata),
            now,
            now,
        ),
    )
    db.commit()
    _embed_and_store(db, mem_id, params.content)
    return f"✓ Memory stored with id `{mem_id}` in category '{params.category}'."


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
    except sqlite3.OperationalError:
        # FTS query syntax error — fall through to semantic-only
        pass

    # --- Semantic vector search ---
    sem_memories = _semantic_search(db, params.query, limit=params.limit)

    # --- Merge and deduplicate (hybrid ranking) ---
    seen: dict[str, dict] = {}
    scores: dict[str, float] = {}

    # FTS results get a boost (lower score = better)
    for i, m in enumerate(fts_memories):
        mid = m["id"]
        seen[mid] = m
        scores[mid] = i * 0.5  # FTS rank position, weighted

    # Semantic results scored by distance
    for i, m in enumerate(sem_memories):
        mid = m["id"]
        sem_score = m.get("semantic_distance", 2.0)
        if mid in seen:
            # Appeared in both — big boost
            scores[mid] = scores[mid] * 0.3 + sem_score * 0.3
            seen[mid]["_search_method"] = "hybrid"
        else:
            seen[mid] = m
            scores[mid] = sem_score + 0.5  # slight penalty for semantic-only
            seen[mid]["_search_method"] = "semantic"

    # Mark FTS-only results
    for mid in seen:
        if "_search_method" not in seen[mid]:
            seen[mid]["_search_method"] = "keyword"

    # Sort by combined score
    ranked = sorted(seen.values(), key=lambda m: scores[m["id"]])

    # Apply optional filters
    if params.category:
        ranked = [m for m in ranked if m["category"] == params.category]
    if params.tags:
        tag_set = set(params.tags)
        ranked = [m for m in ranked if tag_set.issubset(set(m.get("tags", [])))]

    ranked = ranked[:params.limit]

    # Add search method indicator to output
    if params.response_format == ResponseFormat.JSON:
        return json.dumps({"count": len(ranked), "memories": ranked}, indent=2, default=str)

    if not ranked:
        return "_No memories found._"

    parts = []
    sem_available = len(sem_memories) > 0 or _get_embedder() is not None
    method_label = "hybrid (keyword + semantic)" if sem_available else "keyword only"
    parts.append(f"**{len(ranked)} results** via {method_label} search\n")

    for m in ranked:
        method = m.pop("_search_method", "")
        dist = m.pop("semantic_distance", None)
        badge = {"hybrid": "⚡", "semantic": "🔮", "keyword": "🔤"}.get(method, "")
        parts.append(_fmt_memory_md(m).rstrip())
        extras = [badge + method]
        if dist is not None:
            extras.append(f"distance: {dist:.3f}")
        parts.append(f"_{' · '.join(extras)}_\n")

    return "\n---\n".join(parts)


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

    return _fmt_memories(memories, params.response_format, total=total)


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
        _embed_and_store(db, params.memory_id, params.content)
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
    result = import_chat_file(
        file_path=params.file_path,
        category=params.category,
        tags=params.tags,
        extract_mode=params.extract_mode,
        max_length=params.max_length,
    )
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

    Scans for .json, .jsonl, and .md files. Skips already-imported files.

    Args:
        params (BulkImportDirInput): Directory path and import options.

    Returns:
        str: Summary of import results.
    """
    root = Path(params.directory)
    extensions = {".json", ".jsonl", ".md", ".markdown", ".txt"}
    if params.recursive:
        files = [f for f in root.rglob("*") if f.suffix.lower() in extensions and f.is_file()]
    else:
        files = [f for f in root.iterdir() if f.suffix.lower() in extensions and f.is_file()]

    results = []
    for f in sorted(files):
        try:
            r = import_chat_file(
                file_path=str(f),
                category=params.category,
                tags=params.tags,
                extract_mode=params.extract_mode,
                max_length=params.max_length,
            )
            results.append(r)
        except Exception as e:
            results.append({"status": "error", "file": f.name, "error": str(e)})

    ok = [r for r in results if r.get("status") == "ok"]
    skipped = [r for r in results if r.get("status") == "skipped"]
    errors = [r for r in results if r.get("status") == "error"]
    total_memories = sum(r.get("memories_created", 0) for r in ok)

    summary = {
        "files_processed": len(results),
        "imported": len(ok),
        "skipped": len(skipped),
        "errors": len(errors),
        "total_memories_created": total_memories,
        "details": results,
    }
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

    data = {
        "total_memories": total,
        "total_imports": imports,
        "categories": {r["category"]: r["cnt"] for r in categories},
        "sources": {r["source"]: r["cnt"] for r in sources},
        "recent": [dict(r) for r in recent],
        "db_path": str(DB_PATH),
        "db_size_mb": round(DB_PATH.stat().st_size / 1_048_576, 2) if DB_PATH.exists() else 0,
    }

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(data, indent=2, default=str)

    lines = [
        f"## Memory Store Statistics",
        f"",
        f"**Total memories:** {total}",
        f"**Total imports:** {imports}",
        f"**Database:** `{DB_PATH}` ({data['db_size_mb']} MB)",
        f"",
        f"### Categories",
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
    return "\n".join(lines)


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
    db.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, capture_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
        ),
    )

    # -- Store the summary --
    summary_id = _make_id(params.summary)
    summary_meta = {
        **params.metadata,
        "capture_id": capture_id,
        "linked_dialog": dialog_id,
        "title": title,
        "type": "summary",
    }
    db.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, capture_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
        ),
    )

    # -- Back-link the dialog to the summary --
    dialog_meta["linked_summary"] = summary_id
    db.execute(
        "UPDATE memories SET metadata = ? WHERE id = ?",
        (json.dumps(dialog_meta), dialog_id),
    )

    db.commit()

    # Embed both for semantic search (summary is more searchable, dialog has full context)
    _embed_and_store(db, summary_id, params.summary)
    _embed_and_store(db, dialog_id, params.conversation[:2000])

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
        parts.append(f"**Category:** dialog\n")
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
    except Exception:
        pass

    missing = [(r["id"], r["rowid"], r["content"]) for r in all_rows if r["rowid"] not in existing_vecs]

    if not missing:
        return f"✓ All {len(all_rows)} memories already have embeddings."

    created = 0
    for mem_id, rowid, content in missing:
        try:
            vec_bytes = embedder.embed_one(content[:2000])
            db.execute("INSERT OR REPLACE INTO memories_vec(rowid, embedding) VALUES (?, ?)", (rowid, vec_bytes))
            created += 1
        except Exception as e:
            log.debug("Failed to embed %s: %s", mem_id, e)

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
    lines.append(f"\n**MCP (stdio):** ✓ Active (this connection)")

    # Embedding status
    embedder = _get_embedder()
    if embedder is not None:
        db = _get_db()
        total_mems = db.execute("SELECT COUNT(*) as cnt FROM memories").fetchone()["cnt"]
        try:
            total_vecs = db.execute("SELECT COUNT(*) as cnt FROM memories_vec").fetchone()["cnt"]
        except Exception:
            total_vecs = 0
        lines.append(f"\n**Semantic search:** ✓ Enabled ({EMBEDDING_MODEL})")
        lines.append(f"**Embeddings:** {total_vecs}/{total_mems} memories indexed")
        if total_vecs < total_mems:
            lines.append(f"_Run `remind_me_reindex` to embed the remaining {total_mems - total_vecs} memories._")
    else:
        lines.append(f"\n**Semantic search:** ✗ Unavailable (install onnxruntime, tokenizers, huggingface-hub, numpy, sqlite-vec)")

    return "\n".join(lines)


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
    "resource_stats",
    "resource_categories",
]
