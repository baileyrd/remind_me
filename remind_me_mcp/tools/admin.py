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
from typing import Any

from remind_me_mcp import ann_index, config
from remind_me_mcp import tools as _pkg
from remind_me_mcp.config import EMBED_BATCH_SIZE, SYNC_ENABLED
from remind_me_mcp.db import _now_iso
from remind_me_mcp.dbs_import import pull_dbs
from remind_me_mcp.exporter import EXPORT_INLINE_MAX, export_memories
from remind_me_mcp.importer import import_chat_file, import_directory
from remind_me_mcp.mempalace_import import pull_mempalace
from remind_me_mcp.models import (
    BulkImportDirInput,
    ChatImportInput,
    DbsImportInput,
    ExportInput,
    MemoryStatsInput,
    MempalaceImportInput,
    ResponseFormat,
    UndoImportInput,
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
    name="remind_me_import_mempalace",
    annotations={
        "title": "Import Memories from MemPalace",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def memory_import_mempalace(params: MempalaceImportInput) -> str:
    """Bulk-import memories from a MemPalace ChromaDB store, one page at a time.

    Reads MemPalace's persistent ChromaDB store directly (read-only) rather
    than one drawer at a time via MemPalace's own MCP tools — the only
    practical way to pull at real palace scale (tens of thousands of drawers
    per wing). Requires the optional `mempalace` extra
    (`pip install remind-me-mcp[mempalace]`).

    Drawers already matching remind_me's own memory frontmatter (id/created/
    category/source/tags) have those fields restored faithfully; everything
    else is stored as one memory per drawer, tagged with its wing/room.
    Already-imported drawers are skipped (tracked by drawer_id), so reruns —
    including paging through a large wing with limit/offset — are safe.

    Args:
        params (MempalaceImportInput): wing/room filters, paging (limit/
            offset), category/tags overrides, and dry_run.

    Returns:
        str: JSON summary — fetched, already_imported, to_import,
        native_format, opaque_format, imported, has_more (page again with a
        higher offset if true).
    """
    try:
        result = await asyncio.to_thread(
            pull_mempalace,
            wing=params.wing,
            room=params.room,
            limit=params.limit,
            offset=params.offset,
            category=params.category,
            tags=params.tags,
            dry_run=params.dry_run,
        )
        return json.dumps(result, indent=2)
    except ImportError:
        return json.dumps({
            "status": "error",
            "error": "chromadb not installed — run: uv pip install -e '.[mempalace]'",
        })
    except FileNotFoundError as e:
        return json.dumps({"status": "error", "error": str(e)})


@mcp.tool(
    name="remind_me_import_dbs",
    annotations={
        "title": "Import Memories from dbs",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def memory_import_dbs(params: DbsImportInput) -> str:
    """Bulk-import memories from a dbs (daily-backup-system) SQLite store.

    Reads dbs's items/sources tables directly (read-only) rather than going
    through dbs's own export files — each live item becomes a memory with
    dbs's source and tags preserved as first-class knowledge-graph entities
    (linked via the memory-entity graph), not flattened into note prose.

    Already-imported items are skipped when unchanged (tracked by
    (dbs source, external_id) plus a content hash), so reruns — including
    paging through a large database with limit/offset — are safe. An item
    whose content changed since its last import gets a fresh memory, with
    the previous one marked superseded, so edits are picked up too (unlike
    the file-export pipeline, which can only see items by their original
    creation date).

    Args:
        params (DbsImportInput): Path to the dbs SQLite database, optional
            source/item_type filters, paging (limit/offset), extra tags, and
            dry_run.

    Returns:
        str: JSON summary — fetched, already_imported, to_import, created,
        updated, imported (created + updated), has_more (page again with a
        higher offset if true).
    """
    try:
        result = await asyncio.to_thread(
            pull_dbs,
            db_path=params.db_path,
            source=params.source,
            item_type=params.item_type,
            limit=params.limit,
            offset=params.offset,
            tags=params.tags,
            dry_run=params.dry_run,
        )
        return json.dumps(result, indent=2)
    except FileNotFoundError as e:
        return json.dumps({"status": "error", "error": str(e)})
    except sqlite3.DatabaseError as e:
        # Covers both a locked dbs database (dbs itself writing
        # concurrently -- sqlite3.OperationalError, a DatabaseError
        # subclass) and a corrupt/non-SQLite file at db_path.
        return json.dumps({"status": "error", "error": f"Could not read dbs database: {e}"})


@mcp.tool(
    name="remind_me_list_connectors",
    annotations={
        "title": "List Import Connectors",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_list_connectors() -> str:
    """List every registered import connector (Phase 4).

    Connectors are pluggable parsers registered by kind string (see
    `remind_me_mcp.importer.register_connector`). This reports every
    registration, not just the ones reachable through
    `remind_me_import_chat`/`remind_me_import_directory` — a connector like
    `mempalace` is registered purely for discovery and is only actually
    invoked through its own dedicated tool (`remind_me_import_mempalace`).

    Returns:
        str: JSON with `connectors` (every registered kind) and
        `file_import_kinds` (the subset valid for `remind_me_import_chat`'s
        `kind` parameter, i.e. `IMPORT_KINDS` minus `"auto"`).
    """
    from remind_me_mcp.importer import _CONNECTORS, IMPORT_KINDS

    return json.dumps({
        "connectors": sorted(_CONNECTORS),
        "file_import_kinds": [k for k in IMPORT_KINDS if k != "auto"],
    }, indent=2)


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
    superseded_by), so an export is a complete logical backup. By default the
    entity graph is included too (FT-06): entities and memory-entity links
    follow the memories as record_type-tagged records ('entity' /
    'memory_entity'); set include_graph=false for a memories-only export.
    Embedding vectors are NOT exported — they are derived data; run
    remind_me_reindex after importing on the target machine to rebuild them.

    Each memory record also carries a 'role' key, making the file directly
    consumable by remind_me_import_chat / remind_me_import_directory (the
    generic {role, content} message format) for round-trip migration.
    Re-importing preserves memory content verbatim, but is lossy for
    everything else: the importer re-chunks long content and assigns fresh
    ids, category, tags, and source (the originals remain in the export file
    for manual restoration). Graph records restore on import — entities
    upsert (alias union-merge), links insert when the referenced memory still
    exists under its original id; dangling links are skipped and counted.

    Small exports are returned inline; pass file_path (inside the allowed
    export roots) to write larger exports to a file. Optional category/tags
    filters narrow the export (and scope the graph to the exported memories).

    Args:
        params (ExportInput): Format (json|jsonl), optional category/tag
            filters, optional destination file path, and the include_graph
            flag.

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
            include_graph=params.include_graph,
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
    total = db.execute(
        "SELECT COUNT(*) as cnt FROM memories WHERE deleted_at IS NULL"
    ).fetchone()["cnt"]
    categories = db.execute(
        "SELECT category, COUNT(*) as cnt FROM memories "
        "WHERE deleted_at IS NULL GROUP BY category ORDER BY cnt DESC"
    ).fetchall()
    sources = db.execute(
        "SELECT source, COUNT(*) as cnt FROM memories "
        "WHERE deleted_at IS NULL GROUP BY source ORDER BY cnt DESC"
    ).fetchall()
    imports = db.execute("SELECT COUNT(*) as cnt FROM chat_imports").fetchone()["cnt"]
    recent = db.execute(
        "SELECT id, category, substr(content, 1, 80) as preview, created_at FROM memories "
        "WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT 5"
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
    # at least one row in vec_chunks). Tombstoned memories (gap #11) are
    # skipped -- no point spending embed compute on something search will
    # never surface anyway.
    all_rows = db.execute(
        "SELECT id, rowid, content FROM memories WHERE deleted_at IS NULL"
    ).fetchall()
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
    name="remind_me_backup",
    annotations={
        "title": "Backup Database",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def remind_me_backup() -> str:
    """Create an on-demand, WAL-safe backup of the memory database.

    Uses SQLite's Connection.backup() API, so it's safe to run even while the
    server is actively serving other requests -- unlike a raw file copy, it
    can't capture a torn or partially-checkpointed page. Backups are written
    under MEMORY_DIR/backups/; only the most recent
    REMIND_ME_BACKUP_RETENTION_COUNT (default 10) are kept, oldest pruned
    automatically after each new backup.

    Returns:
        str: The backup file path and current backup count.
    """
    from remind_me_mcp.backup import create_backup, list_backups

    db = _pkg._get_db()
    path = await asyncio.to_thread(create_backup, db, label="manual")
    backups = await asyncio.to_thread(list_backups)
    return f"✓ Backup created: `{path}`\n\n**Total backups kept:** {len(backups)}"


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

    Also reports conversation-capture health (whether `remind_me_auto_capture`
    has ever run, and when it last did) and the depth of every maintenance
    queue, so an unconfigured capture instruction or a growing backlog is
    visible here rather than having to be inferred from an absence.

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

    # Backups (issue #17)
    from remind_me_mcp.backup import BACKUP_DIR, list_backups

    backups = await asyncio.to_thread(list_backups)
    if backups:
        lines.append(
            f"**Backups:** {len(backups)} at `{BACKUP_DIR}` — last: "
            f"{backups[0]['filename']} ({backups[0]['created_at']})"
        )
    else:
        lines.append("**Backups:** none yet — run `remind_me_backup` to create one")

    lines.append("\n**MCP (stdio):** ✓ Active (this connection)")

    # Tool-surface cost. The full surface runs ~21k tokens of context in every
    # session on every client, whether or not an admin tool is ever touched --
    # about 1.8x what a whole `remind_me_wiki_load` costs. Surfacing the number
    # here is what makes the profile knob discoverable at all; nobody goes
    # looking for an env var to solve a cost they cannot see.
    from remind_me_mcp import tool_profiles

    n_tools, approx_tokens = tool_profiles.surface_cost(mcp)
    if tool_profiles.TOOL_PROFILE == "full":
        lines.append(
            f"**Tool surface:** {n_tools} tools, ~{approx_tokens // 1000}k tokens of context "
            f"per session (profile `full`). Set `REMIND_ME_TOOL_PROFILE=standard` "
            f"(drops imports/sync/ops) or `core` (conversational only) to reclaim context."
        )
    else:
        lines.append(
            f"**Tool surface:** {n_tools} tools, ~{approx_tokens // 1000}k tokens of context "
            f"per session (profile `{tool_profiles.TOOL_PROFILE}` — a narrowed surface; "
            f"unset `REMIND_ME_TOOL_PROFILE` for all tools)."
        )

    # Embedding status — the availability probe may hit the network (PF-01).
    embedder = await asyncio.to_thread(_get_embedder)
    if embedder is not None:
        db = _pkg._get_db()
        total_mems = db.execute(
            "SELECT COUNT(*) as cnt FROM memories WHERE deleted_at IS NULL"
        ).fetchone()["cnt"]
        try:
            total_vecs = db.execute(
                "SELECT COUNT(DISTINCT memory_rowid) as cnt FROM vec_chunks"
            ).fetchone()["cnt"]
        except sqlite3.OperationalError as e:
            log.debug("vec_chunks table not available for status check: %s", e)
            total_vecs = 0
        lines.append(f"\n**Semantic search:** ✓ Enabled ({EMBEDDING_MODEL})")
        lines.append(f"**Embeddings:** {total_vecs}/{total_mems} memories indexed")

        # Embedding-model versioning (issue #18): stale vectors from a prior
        # model/dimension are cleared automatically at startup, which is why
        # this shows up as "missing embeddings" below -- flag the root cause
        # explicitly so it doesn't read as a plain never-embedded backlog.
        mismatch = _pkg.embedding_mismatch_info(db)
        if mismatch is not None:
            lines.append(
                f"_⚠ Embedding model changed ({mismatch['stored_backend']}/"
                f"{mismatch['stored_model']} dim={mismatch['stored_dim']} → "
                f"{mismatch['current_backend']}/{mismatch['current_model']} "
                f"dim={mismatch['current_dim']}); stale vectors were cleared. "
                f"Run `remind_me_reindex` to rebuild them._"
            )
        elif total_vecs < total_mems:
            lines.append(f"_Run `remind_me_reindex` to embed the remaining {total_mems - total_vecs} memories._")

        # Gap #10: optional ANN index — brute-force sqlite-vec scan otherwise.
        from remind_me_mcp import ann_index

        ann = ann_index.status()
        if ann["available"]:
            state = f"✓ {ann['size']} vector(s) indexed" if ann["loaded"] else "not built yet (loads on first search)"
            lines.append(
                f"**ANN index:** {state} — used once memories_vec passes "
                f"{ann['min_chunks_threshold']} chunks, brute-force scan below that"
            )
        else:
            lines.append(
                "**ANN index:** ✗ Unavailable (install the `ann` extra: `pip install usearch`) "
                "— always using the exact brute-force scan"
            )
    else:
        lines.append("\n**Semantic search:** ✗ Unavailable (install onnxruntime, tokenizers, huggingface-hub, numpy, sqlite-vec)")

    # Conversation capture health.
    #
    # remind_me_auto_capture only runs when the user has pasted the opt-in
    # instruction into their client, so "never configured" and "configured but
    # nothing captured yet" previously both presented as pure silence — there
    # was no surface anywhere that distinguished them. Deliberately reported,
    # not nudged about: capture is opt-in by design, so a vault with none is a
    # legitimate configuration rather than a backlog to nag about.
    from remind_me_mcp.maintenance import capture_health, pending_counts

    cap = capture_health(_pkg._get_db())
    if cap["ever_captured"]:
        lines.append(
            f"\n**Conversation capture:** ✓ {cap['captures']} capture(s) — "
            f"last: {cap['last_capture_at']}"
        )
    else:
        lines.append(
            "\n**Conversation capture:** none recorded — `remind_me_auto_capture` "
            "is opt-in; add the capture instruction to your client's custom "
            "instructions (see the README) if you expected captures here"
        )

    # Maintenance backlogs. Same counts the throttled nudge uses, so this and
    # the nudge can never disagree.
    pending_queues = {k: v for k, v in pending_counts(_pkg._get_db()).items() if v}
    if pending_queues:
        summary = ", ".join(
            f"{v} {k.replace('_', ' ')}" for k, v in sorted(pending_queues.items())
        )
        lines.append(f"**Maintenance pending:** {summary}")
    else:
        lines.append("**Maintenance pending:** nothing — every queue is drained")

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

    # Multi-node sync (SY-12)
    from remind_me_mcp.sync import get_sync_status

    sync = get_sync_status()
    if sync["enabled"]:
        outbox = sync["outbox"]
        drain = outbox["drain"]["verdict"]
        lines.append(
            f"\n**Sync:** ✓ Enabled — node `{sync['node_id']}` → {sync['hub_url']}, "
            f"every {sync['sync_interval_seconds']}s; "
            f"outbox {outbox['pending']} pending ({drain})"
        )
        errored = [r["remote_id"] for r in sync["remotes"] if r["last_error"]]
        if errored:
            lines.append(f"_⚠ Last cycle failed for: {', '.join(errored)}_")
        lines.append("_Details: `remind_me_sync_status`_")
    else:
        lines.append(f"\n**Sync:** ✗ Disabled ({sync['hint']})")

    # Push/webhook ingestion (FT-09, Phase 5a)
    from remind_me_mcp.webhook_server import get_webhook_status

    webhook = get_webhook_status()
    if webhook["enabled"]:
        state = "✓ Running" if webhook["running"] else "✗ Not running"
        lines.append(
            f"\n**Webhook ingestion:** {state} at {webhook['bind']}:{webhook['port']} — "
            f"{webhook['requests_ingested']} ingested / {webhook['requests_skipped']} skipped"
        )
        lines.append("_Details: `remind_me_webhook_status`_")
    else:
        lines.append(
            "\n**Webhook ingestion:** ✗ Disabled (set REMIND_ME_WEBHOOK_SECRET "
            "to enable push ingestion via POST /ingest)"
        )

    # OpenTelemetry tracing (cognee gap #9, Phase 7a)
    from remind_me_mcp import telemetry

    if telemetry.OTEL_ENABLED:
        state = (
            "✓ Enabled"
            if telemetry.is_enabled()
            else "✗ Enabled but unavailable (missing 'otel' extra or setup failed — see logs)"
        )
        lines.append(f"\n**OpenTelemetry tracing:** {state}")
    else:
        lines.append(
            "\n**OpenTelemetry tracing:** ✗ Disabled (set REMIND_ME_OTEL_ENABLED=1 "
            "and install the 'otel' extra to trace tool calls, sync cycles, and watcher scans)"
        )

    # LLM Wiki (FT-08)
    try:
        from remind_me_mcp import wiki

        # db may be unbound above (it is only fetched in the embedder branch).
        page_count = _pkg._get_db().execute(
            "SELECT COUNT(*) AS cnt FROM wiki_pages"
        ).fetchone()["cnt"]
        pending = wiki.pending_compile_count()
        lines.append(
            f"\n**LLM Wiki:** ✓ {page_count} page(s) at `{wiki.wiki_dir()}` "
            f"(load with `remind_me_wiki_load`)"
        )
        if pending:
            lines.append(
                f"_⚠ {pending} raw memory(ies) pending synthesis — run "
                f"`remind_me_wiki_compile` to fold them in._"
            )
        else:
            lines.append("_Wiki is current with the memory store._")
    except sqlite3.OperationalError as e:
        log.debug("Wiki tables not available for status check: %s", e)

    # Remote MCP connector (FT-05) + OAuth (FT-07)
    from remind_me_mcp.remote import get_remote_status

    remote = get_remote_status()
    if remote["enabled"]:
        lines.append(
            f"\n**Remote MCP connector:** ✓ Enabled — serves "
            f"http://{remote['host']}:{remote['port']}/mcp/<token> "
            f"(token at `{remote['token_file']}`; run with --serve-remote)"
        )
        if remote["oauth_enabled"]:
            lines.append(
                f"**Connector OAuth:** ✓ Enabled — issuer {remote['issuer']}, "
                f"{remote['oauth_clients']} registered client(s) "
                f"(list/revoke with `remind_me_revoke_clients`)"
            )
        else:
            lines.append(
                "**Connector OAuth:** ✗ Disabled (set REMIND_ME_REMOTE_ISSUER "
                "to the public HTTPS origin to enable per-client auth)"
            )
    elif remote["token_configured"]:
        lines.append(
            f"\n**Remote MCP connector:** ✗ Disabled (token exists at "
            f"`{remote['token_file']}`; set REMIND_ME_REMOTE_MCP=1 or run "
            f"with --serve-remote to enable)"
        )
    else:
        lines.append(
            "\n**Remote MCP connector:** ✗ Disabled (set REMIND_ME_REMOTE_MCP=1 "
            "and run with --serve-remote to expose a claude.ai custom connector)"
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
        interval, last scan time, ingest/skip/supersede counters, recent
        errors, and ``pending_wiki_compile`` (raw memories awaiting wiki
        synthesis — the watcher feeds the memory store, not the wiki). When
        disabled, includes a configuration hint.
    """
    from remind_me_mcp import wiki
    from remind_me_mcp.watcher import get_watch_status

    status = get_watch_status()
    status["pending_wiki_compile"] = wiki.pending_compile_count()
    return json.dumps(status, indent=2)


_UNDO_SOURCES = {
    # kind -> (tracking table, column holding the memory id, scope column)
    # mempalace/dbs record a memory_id per tracked row. chat_imports has no
    # memory_id: it keys on import_id, which the importer stamps onto
    # memories.doc_id, so the join goes the other way.
    "mempalace": ("mempalace_imports", "memory_id", "drawer_id"),
    "dbs": ("dbs_imports", "memory_id", "dbs_source"),
}


def _undo_matching_ids_mempalace(
    db: Any, import_id: str | None
) -> tuple[list[str], str]:
    """Resolve mempalace memory ids for undo, tolerating untracked batches.

    remind_me_import_mempalace records a (drawer_id -> memory_id) row in
    mempalace_imports for every drawer it writes, so the tracking-table
    join below is the precise, preferred signal. But content can also
    reach the store with a mempalace-shaped source/metadata without ever
    going through that code path — e.g. a historical bulk load that
    predates the mempalace_imports migration — leaving that table empty
    for a batch that is still unambiguously mempalace content: pull_mempalace
    stamps every write with source 'mempalace_import' (opaque) or
    'mempalace:<original source>' (native frontmatter), and always carries
    metadata.mempalace_drawer_id. Union both signals, deduplicated by
    memory id, so undo covers a batch regardless of which path wrote it.
    """
    ids: set[str] = set()
    if import_id:
        pattern = f"{import_id}%"
        rows = db.execute(
            """SELECT t.memory_id FROM mempalace_imports t
               JOIN memories m ON m.id = t.memory_id
               WHERE m.deleted_at IS NULL AND t.drawer_id LIKE ?""",
            (pattern,),
        ).fetchall()
        ids.update(r[0] for r in rows)
        rows = db.execute(
            """SELECT id FROM memories
               WHERE deleted_at IS NULL
                 AND (source = 'mempalace_import' OR source LIKE 'mempalace:%')
                 AND json_extract(metadata, '$.mempalace_drawer_id') LIKE ?""",
            (pattern,),
        ).fetchall()
        ids.update(r[0] for r in rows)
        return sorted(ids), f"mempalace scope {import_id!r}"

    rows = db.execute(
        """SELECT t.memory_id FROM mempalace_imports t
           JOIN memories m ON m.id = t.memory_id
           WHERE m.deleted_at IS NULL"""
    ).fetchall()
    ids.update(r[0] for r in rows)
    rows = db.execute(
        """SELECT id FROM memories
           WHERE deleted_at IS NULL
             AND (source = 'mempalace_import' OR source LIKE 'mempalace:%')"""
    ).fetchall()
    ids.update(r[0] for r in rows)
    return sorted(ids), "all mempalace imports"


def _undo_matching_ids(
    db: Any, kind: str, import_id: str | None
) -> tuple[list[str], str]:
    """Resolve the memory ids belonging to an import, plus a human scope label."""
    if kind == "chat":
        # doc_id carries the import_id for every chunk of a chat import.
        if import_id:
            rows = db.execute(
                "SELECT id FROM memories WHERE doc_id = ? AND deleted_at IS NULL",
                (import_id,),
            ).fetchall()
            return [r[0] for r in rows], f"chat import {import_id}"
        rows = db.execute(
            """SELECT m.id FROM memories m
               WHERE m.deleted_at IS NULL
                 AND m.doc_id IN (SELECT import_id FROM chat_imports)"""
        ).fetchall()
        return [r[0] for r in rows], "all chat imports"

    if kind == "mempalace":
        return _undo_matching_ids_mempalace(db, import_id)

    table, id_col, scope_col = _UNDO_SOURCES[kind]
    if import_id:
        # Prefix match so a dbs source can be targeted without naming every id.
        rows = db.execute(
            f"""SELECT t.{id_col} FROM {table} t
                JOIN memories m ON m.id = t.{id_col}
                WHERE m.deleted_at IS NULL AND t.{scope_col} LIKE ?""",  # noqa: S608 — table/col from a fixed literal map
            (f"{import_id}%",),
        ).fetchall()
        return [r[0] for r in rows], f"{kind} scope {import_id!r}"
    rows = db.execute(
        f"""SELECT t.{id_col} FROM {table} t
            JOIN memories m ON m.id = t.{id_col}
            WHERE m.deleted_at IS NULL"""  # noqa: S608 — table/col from a fixed literal map
    ).fetchall()
    return [r[0] for r in rows], f"all {kind} imports"


@mcp.tool(
    name="remind_me_undo_import",
    annotations={
        "title": "Undo an Import",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def remind_me_undo_import(params: UndoImportInput) -> str:
    """Roll back a previous import, removing its memories and its tracking rows.

    Imports are the one bulk write this server makes, and until now there was
    no bulk way to undo one — ``remind_me_delete`` takes a single id, which is
    unusable at import scale (a mempalace run can be tens of thousands of
    records).

    Deletion goes through the same path as ``remind_me_delete``
    (``db._purge_memory``), so chunk vectors, the ANN index, entity mention
    links and stored feedback are all cleaned up. A hand-written SQL DELETE
    would leave every one of those orphaned. On a sync-enabled node this is a
    soft delete: rows are tombstoned so the removal propagates to every other
    node, which also means **the space is not reclaimed until tombstones are
    compacted** (``TOMBSTONE_RETENTION_DAYS``, default 180).

    The import's tracking rows are removed too. That matters: import paths skip
    work they have already recorded, so leaving those rows behind would make
    the same content permanently un-importable.

    Defaults to a dry run. Work is resumable — call repeatedly until
    ``remaining`` is 0.

    Args:
        params (UndoImportInput): Which import, scope, dry-run flag, batch size.

    Returns:
        str: JSON — matched/removed/remaining counts, tracking rows removed,
        vectors removed, and whether this was a soft or hard delete.
    """
    db = _pkg._get_db()
    kind = str(params.import_kind)
    memory_ids, scope = _undo_matching_ids(db, kind, params.import_id)

    result: dict[str, Any] = {
        "import_kind": kind,
        "scope": scope,
        "matched": len(memory_ids),
        "dry_run": params.dry_run,
        "mode": "soft-delete (tombstone, propagates over sync)"
        if SYNC_ENABLED
        else "hard delete (sync disabled — nothing to propagate to)",
    }
    if params.dry_run:
        result["removed"] = 0
        result["remaining"] = len(memory_ids)
        result["hint"] = (
            "dry run — nothing changed. Re-run with dry_run=false to remove. "
            + (
                "Tombstoned rows keep their content until compaction "
                f"({config.TOMBSTONE_RETENTION_DAYS} days), so disk use will "
                "not drop immediately."
                if SYNC_ENABLED
                else "Rows are removed outright; run VACUUM to reclaim the file."
            )
        )
        return json.dumps(result, indent=2)

    batch = memory_ids[: params.limit]
    # Capture doc_ids BEFORE purging: a hard delete removes the rows outright,
    # so afterwards there is nothing left to read the import_id back from.
    doc_ids = (
        [
            r[0]
            for r in db.execute(
                f"SELECT DISTINCT doc_id FROM memories "  # noqa: S608 — placeholders only
                f"WHERE doc_id IS NOT NULL AND id IN ({','.join('?' * len(batch))})",
                batch,
            ).fetchall()
        ]
        if kind == "chat" and batch
        else []
    )

    now = _now_iso()
    removed_vec_rowids: list[int] = []
    removed = 0
    for memory_id in batch:
        row = db.execute(
            "SELECT rowid FROM memories WHERE id = ? AND deleted_at IS NULL",
            (memory_id,),
        ).fetchone()
        if row is None:
            continue
        removed_vec_rowids.extend(
            _pkg._purge_memory(db, memory_id, row[0], soft=SYNC_ENABLED, now=now)
        )
        removed += 1

    tracking_removed = _undo_forget_tracking(db, kind, batch, doc_ids)
    db.commit()
    # ANN mutations only after the commit succeeds — see db._delete_chunks.
    for vec_rowid in removed_vec_rowids:
        ann_index.remove_vector(db, vec_rowid)

    result["removed"] = removed
    result["remaining"] = max(0, len(memory_ids) - removed)
    result["tracking_rows_removed"] = tracking_removed
    result["vectors_removed"] = len(removed_vec_rowids)
    if result["remaining"]:
        result["hint"] = "call again to continue — work is resumable"
    return json.dumps(result, indent=2)


def _undo_forget_tracking(
    db: Any, kind: str, memory_ids: list[str], doc_ids: list[str]
) -> int:
    """Drop the import-tracking rows for ids that were just removed.

    Import paths treat a tracked id as "already done" and skip it, so leaving
    these behind would silently make the same content un-importable forever.

    ``doc_ids`` must be captured *before* the purge — for chat imports the
    link lives on ``memories.doc_id``, and a hard delete removes those rows,
    leaving nothing to read the import_id back from afterwards.
    """
    if not memory_ids:
        return 0
    if kind == "chat":
        if not doc_ids:
            return 0
        # chat_imports rows are per-file, not per-memory: drop an import only
        # once none of its chunks survive, so a partially-drained import keeps
        # its tracking row and cannot be duplicated by a re-import.
        marks = ",".join("?" * len(doc_ids))
        cur = db.execute(
            f"""DELETE FROM chat_imports
                 WHERE import_id IN ({marks})
                   AND import_id NOT IN (
                       SELECT doc_id FROM memories
                        WHERE doc_id IS NOT NULL AND deleted_at IS NULL
                   )""",  # noqa: S608 — placeholders only
            doc_ids,
        )
        return int(cur.rowcount or 0)
    table, id_col, _ = _UNDO_SOURCES[kind]
    marks = ",".join("?" * len(memory_ids))
    cur = db.execute(
        f"DELETE FROM {table} WHERE {id_col} IN ({marks})",  # noqa: S608 — table/col from a fixed literal map
        memory_ids,
    )
    return int(cur.rowcount or 0)


@mcp.tool(
    name="remind_me_sync_status",
    annotations={
        "title": "Sync Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_sync_status() -> str:
    """Report multi-node sync state (SY-12): outbox depth, drain rate, watermarks, errors.

    Answers the questions that previously required shell access and hand-written
    SQL against ``sync_outbox``: is the push backlog draining or wedged, when did
    each remote last succeed, and what failed most recently.

    The ``outbox.drain`` verdict is the useful part. A pending count alone is
    ambiguous — thousands of queued rows look identical whether they are
    draining normally or the push is stuck — so this persists the previous
    observation and reports direction (``draining`` / ``stalled`` / ``growing``
    / ``idle``) with a per-minute rate and ETA. The first call establishes a
    baseline and reports ``unknown``; call again after ~30s for a rate.

    Returns:
        str: JSON status — node_id, hub URL, sync interval, the outbox trigger
        gate, outbox depth + drain verdict, tombstone counts (total and
        compactable now), and per-remote push/pull watermarks with the last
        error. When sync is disabled, names the missing env vars instead.
    """
    from remind_me_mcp.sync import get_sync_status

    return json.dumps(get_sync_status(), indent=2)


@mcp.tool(
    name="remind_me_sync_reconcile",
    annotations={
        "title": "Reconcile Against Hub",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def remind_me_sync_reconcile() -> str:
    """Diff this node's record counts against the hub's and classify the drift (SY-14).

    Read-only on both sides — it calls the hub's ``GET /stats`` and compares
    against local counts. Replaces the manual exercise of running psql on the
    hub host, gathering local counts separately, and diffing two tables by eye.

    The ``verdict`` is the useful output, because the benign case and the real
    fault differ only by a sign:

    - ``in-sync`` — no drift
    - ``pull-lag`` — hub ahead, last pull recent; the ordinary state between
      cycles
    - ``node-ahead`` — this node holds records the hub does not, so pushes
      aren't landing; the direction that means data is at risk
    - ``fault`` — hub ahead but the last successful pull is stale (or never
      happened), so it isn't lag

    Returns:
        str: JSON — verdict with hints, per-category drift (only categories
        that disagree, plus a count of those that don't), totals, tombstones,
        entity/link/relation counts, outbox depth, last-pull age, and the hub's
        per-``origin_node`` breakdown (hub-only, so informational). When the hub
        can't be reached or is too old to have ``/stats``, returns a status and
        hint instead of a verdict.
    """
    from remind_me_mcp.sync import reconcile_with_hub

    return json.dumps(await reconcile_with_hub(), indent=2)


@mcp.tool(
    name="remind_me_webhook_status",
    annotations={
        "title": "Webhook Ingestion Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_webhook_status() -> str:
    """Report the push/webhook ingestion server's state (FT-09/Phase 5a).

    When REMIND_ME_WEBHOOK_SECRET is configured, a small HTTP server accepts
    POST /ingest requests (bearer-authenticated) and imports their content
    through the same pipeline as remind_me_import_chat — a way for external
    senders (chat-export tools, CI jobs, automations) to push content
    directly into memory over the network.

    Returns:
        str: JSON status — enabled/running flags, bind address/port, request
        counters (ingested/skipped/errored), and recent errors. When
        disabled, includes a configuration hint.
    """
    from remind_me_mcp.webhook_server import get_webhook_status

    return json.dumps(get_webhook_status(), indent=2)


@mcp.tool(
    name="remind_me_revoke_clients",
    annotations={
        "title": "List / Revoke OAuth Connector Clients",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_revoke_clients(client_id: str = "") -> str:
    """List OAuth clients registered with the remote connector, or revoke one (FT-07).

    Without a client_id, lists every client that registered against the
    remote connector's OAuth authorization server (claude.ai registers one
    per connector) with its live access/refresh token counts. With a
    client_id, deletes that client's registration and every token it holds —
    the client is locked out immediately (the running remote server re-reads
    the state file on each token check) and must re-register and re-obtain
    the owner's consent to reconnect.

    Args:
        client_id: The client to revoke. Empty (default) lists clients.

    Returns:
        str: JSON — the client list, a revocation summary, or an error.
    """
    from remind_me_mcp import config as cfg
    from remind_me_mcp.oauth import OAuthStateStore

    store = OAuthStateStore(cfg.MEMORY_DIR / "oauth.json")
    if not client_id:
        # File I/O off the event loop (PF-06 conventions).
        clients = await asyncio.to_thread(store.list_clients)
        return json.dumps(
            {
                "clients": clients,
                "state_file": str(store.path),
                "hint": "Pass client_id to revoke a client and all of its tokens.",
            },
            indent=2,
        )
    result = await asyncio.to_thread(store.revoke_client, client_id)
    if result is None:
        return json.dumps({"status": "error", "error": f"Unknown client_id: {client_id}"})
    return json.dumps({"status": "revoked", **result}, indent=2)


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
    if status.origin_url:
        lines.append(f"_Origin: `{status.origin_url}`_")

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
    if result.origin_url:
        lines.append(f"**Origin:** `{result.origin_url}`")

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
    total = db.execute(
        "SELECT COUNT(*) as cnt FROM memories WHERE deleted_at IS NULL"
    ).fetchone()["cnt"]
    return json.dumps({"total_memories": total, "db_path": str(DB_PATH)})


@mcp.resource("memory://categories")
async def resource_categories() -> str:
    """List all memory categories with counts."""
    db = _pkg._get_db()
    rows = db.execute(
        "SELECT category, COUNT(*) as cnt FROM memories "
        "WHERE deleted_at IS NULL GROUP BY category ORDER BY cnt DESC"
    ).fetchall()
    return json.dumps({r["category"]: r["cnt"] for r in rows}, indent=2)
