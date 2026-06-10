"""
remind_me_mcp.tools.admin — stats / reindex / status / update / import / export
handlers and the two MCP resource handlers.

Patchable shared state and cross-module helpers are looked up through the
``remind_me_mcp.tools`` package namespace (``_pkg.<name>``) at call time so
monkeypatching ``remind_me_mcp.tools.<name>`` keeps working (HY-02).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

from remind_me_mcp import tools as _pkg
from remind_me_mcp.config import EMBED_BATCH_SIZE
from remind_me_mcp.exporter import EXPORT_INLINE_MAX, export_memories
from remind_me_mcp.importer import import_chat_file, import_directory
from remind_me_mcp.models import (
    BulkImportDirInput,
    ChatImportInput,
    ExportInput,
    MemoryStatsInput,
    ResponseFormat,
)
from remind_me_mcp.server import mcp
from remind_me_mcp.tools._shared import _maybe_update_notice, log


@mcp.tool(
    name="remind_me_import_chat",
    annotations={
        "title": "Import Chat Export or Document",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def memory_import_chat(params: ChatImportInput) -> str:
    """Import a chat export (JSON, JSONL, or Markdown) or a document/notes file into memory.

    Supports Claude's export format, OpenAI's export format, and generic {role, content} message
    arrays — plus generic documents (FT-02): Markdown notes are chunked per-section (heading
    context kept with each chunk and stored as metadata), plain text per-paragraph. With the
    default kind='auto', chat-style markdown imports as chat and notes files as documents.
    Deduplicates by file hash — re-importing the same file is a no-op.

    Args:
        params (ChatImportInput): File path, import kind, extraction mode, and tagging options.

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
            kind=params.kind.value,
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
        "title": "Bulk Import Directory",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def memory_import_directory(params: BulkImportDirInput) -> str:
    """Bulk import all chat export and document files from a directory.

    Scans for .json, .jsonl, .md, .markdown, and .txt files. With the default
    kind='auto' each file is routed individually: chat exports are chunked
    per-message, documents per-section/paragraph (FT-02). Skips
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
        kind=params.kind.value,
    )
    return json.dumps(summary, indent=2)


@mcp.tool(
    name="remind_me_export_memories",
    annotations={
        "title": "Export Memories",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def memory_export(params: ExportInput) -> str:
    """Export memories to JSON or JSONL for backup or migration to another machine.

    Every column of the memories table is included (id, content, category, tags,
    source, metadata, timestamps, and lifecycle fields like vitality and
    superseded_by), so an export is a complete logical backup. Embedding vectors
    are NOT exported — they are derived data; run remind_me_reindex after
    importing on the target machine to rebuild them.

    Each record also carries a 'role' key, making the file directly consumable
    by remind_me_import_chat / remind_me_import_directory (the generic
    {role, content} message format) for round-trip migration. Re-importing
    preserves memory content verbatim, but is lossy for everything else: the
    importer re-chunks long content and assigns fresh ids, category, tags, and
    source (the originals remain in the export file for manual restoration).

    Small exports are returned inline; pass file_path (inside the allowed
    export roots) to write larger exports to a file. Optional category/tags
    filters narrow the export.

    Args:
        params (ExportInput): Format (json|jsonl), optional category/tag
            filters, and optional destination file path.

    Returns:
        str: JSON result — inline export content, or a file-write summary.
    """
    try:
        # File I/O and the full-table scan are blocking — keep them off the
        # event loop (PF-01/PF-06 conventions).
        result = await asyncio.to_thread(
            export_memories,
            format=params.format.value,
            category=params.category,
            tags=params.tags,
            file_path=params.file_path,
            inline_max=EXPORT_INLINE_MAX,
        )
    except OSError as e:
        log.error("Export failed for %s: %s", params.file_path, e)
        return json.dumps({"status": "error", "error": f"Failed to write export: {e}"})
    return json.dumps(result, indent=2)


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

    db = _pkg._get_db()
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

    # Availability may probe Ollama or download the ONNX model — keep it off
    # the event loop (PF-01).
    embedder = await asyncio.to_thread(_get_embedder)
    if embedder is None:
        return (
            "Embedding model not available. Install dependencies:\n"
            "```\npip install onnxruntime tokenizers huggingface-hub numpy sqlite-vec\n```\n"
            "The model (~80MB) downloads automatically on first use."
        )

    db = _pkg._get_db()
    # Prune chunk vectors orphaned by old deletes — a reused rowid would
    # otherwise keep the deleted memory's embedding and be skipped below (DI-01).
    pruned = 0
    try:
        pruned = await asyncio.to_thread(_pkg._prune_orphan_chunks, db)
    except sqlite3.OperationalError as e:
        log.debug("Chunk tables not available for pruning: %s", e)

    # Find memories without chunk embeddings (a memory is "embedded" once it owns
    # at least one row in vec_chunks).
    all_rows = db.execute("SELECT id, rowid, content FROM memories").fetchall()
    embedded_rowids = set()
    try:
        embedded_rowids = {
            r[0] for r in db.execute("SELECT DISTINCT memory_rowid FROM vec_chunks").fetchall()
        }
    except sqlite3.OperationalError as e:
        log.debug("vec_chunks table not available: %s", e)

    missing = [
        (r["rowid"], r["content"]) for r in all_rows if r["rowid"] not in embedded_rowids
    ]

    if not missing:
        return f"✓ All {len(all_rows)} memories already have embeddings."

    created = 0
    for batch_start in range(0, len(missing), EMBED_BATCH_SIZE):
        batch = missing[batch_start : batch_start + EMBED_BATCH_SIZE]
        created += await asyncio.to_thread(_pkg._embed_and_store_rows, batch)

    return (
        f"✓ Reindex complete.\n\n"
        f"**Total memories:** {len(all_rows)}\n"
        f"**Already embedded:** {len(embedded_rowids)}\n"
        f"**Newly embedded:** {created}\n"
        f"**Failed:** {len(missing) - created}\n"
        f"**Orphaned chunks pruned:** {pruned}"
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

    status = _pkg.get_server_status()
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

    # Embedding status — the availability probe may hit the network (PF-01).
    embedder = await asyncio.to_thread(_get_embedder)
    if embedder is not None:
        db = _pkg._get_db()
        total_mems = db.execute("SELECT COUNT(*) as cnt FROM memories").fetchone()["cnt"]
        try:
            total_vecs = db.execute(
                "SELECT COUNT(DISTINCT memory_rowid) as cnt FROM vec_chunks"
            ).fetchone()["cnt"]
        except sqlite3.OperationalError as e:
            log.debug("vec_chunks table not available for status check: %s", e)
            total_vecs = 0
        lines.append(f"\n**Semantic search:** ✓ Enabled ({EMBEDDING_MODEL})")
        lines.append(f"**Embeddings:** {total_vecs}/{total_mems} memories indexed")
        if total_vecs < total_mems:
            lines.append(f"_Run `remind_me_reindex` to embed the remaining {total_mems - total_vecs} memories._")
    else:
        lines.append("\n**Semantic search:** ✗ Unavailable (install onnxruntime, tokenizers, huggingface-hub, numpy, sqlite-vec)")

    # Folder watcher (FT-03)
    from remind_me_mcp.watcher import get_watch_status

    watch = get_watch_status()
    if watch["enabled"]:
        state = "✓ Running" if watch["running"] else "✗ Not running"
        lines.append(
            f"\n**Folder watcher:** {state} — {len(watch['watch_dirs'])} dir(s), "
            f"every {watch['interval_seconds']}s, "
            f"{watch['files_ingested']} ingested / {watch['files_skipped']} skipped"
        )
        lines.append("_Details: `remind_me_watch_status`_")
    else:
        lines.append(
            "\n**Folder watcher:** ✗ Disabled (set REMIND_ME_WATCH_DIRS to "
            "auto-ingest a notes/docs folder)"
        )

    return "\n".join(lines)


@mcp.tool(
    name="remind_me_watch_status",
    annotations={
        "title": "Folder Watcher Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_watch_status() -> str:
    """Report the folder watcher's state (FT-03): watched dirs, scan counters, recent errors.

    The watcher polls the directories in REMIND_ME_WATCH_DIRS every
    REMIND_ME_WATCH_INTERVAL seconds and auto-ingests new or changed
    notes/docs files through the import pipeline (hash dedup applies; a
    changed file imports fresh and its previous import's memories are
    marked superseded).

    Returns:
        str: JSON status — enabled/running flags, watched dirs, scan
        interval, last scan time, ingest/skip/supersede counters, and
        recent errors. When disabled, includes a configuration hint.
    """
    from remind_me_mcp.watcher import get_watch_status

    return json.dumps(get_watch_status(), indent=2)


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
# Resource handlers
# ---------------------------------------------------------------------------


@mcp.resource("memory://stats")
async def resource_stats() -> str:
    """Quick stats for the memory store."""
    from remind_me_mcp.config import DB_PATH

    db = _pkg._get_db()
    total = db.execute("SELECT COUNT(*) as cnt FROM memories").fetchone()["cnt"]
    return json.dumps({"total_memories": total, "db_path": str(DB_PATH)})


@mcp.resource("memory://categories")
async def resource_categories() -> str:
    """List all memory categories with counts."""
    db = _pkg._get_db()
    rows = db.execute("SELECT category, COUNT(*) as cnt FROM memories GROUP BY category ORDER BY cnt DESC").fetchall()
    return json.dumps({r["category"]: r["cnt"] for r in rows}, indent=2)
