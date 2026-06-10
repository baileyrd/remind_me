"""
remind_me_mcp.tools — All 20 MCP tool handlers and 2 resource handlers.

All handlers are registered on the `mcp` instance imported from server.py.
This module imports mcp from server (not the other way around) to avoid
circular imports.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import sqlite3
from typing import Any

from remind_me_mcp.config import CLIENT, NODE_ID
from remind_me_mcp.consolidation import find_clusters, merge_cluster, pick_canonical
from remind_me_mcp.db import (
    _delete_chunks,
    _embed_and_store,
    _embed_and_store_rows,
    _get_db,
    _make_id,
    _now_iso,
    _prune_orphan_chunks,
    _row_to_dict,
    _semantic_search,
)
from remind_me_mcp.formatting import _fmt_memories, _fmt_memory_md
from remind_me_mcp.importer import import_chat_file, import_directory
from remind_me_mcp.models import (
    AutoCaptureInput,
    BulkImportDirInput,
    ChatImportInput,
    ConsolidateInput,
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
from remind_me_mcp.query_expansion import expand_query
from remind_me_mcp.reranker import RERANK_TOP_K, maybe_rerank
from remind_me_mcp.retrieval import (
    apply_token_budget,
    build_debug_signals,
    compute_tier_breakdown,
    rank_rrf,
)
from remind_me_mcp.server import mcp
from remind_me_mcp.updater import pop_update_notice
from remind_me_mcp.vitality import (
    DECAY_RATES,
    compute_vitality,
    effective_vitality,
    get_effective_decay_rate,
    is_dormant,
    record_access,
)

log = logging.getLogger("remind_me_mcp.tools")

EMBED_BATCH_SIZE = 32

# When True, a query that isn't valid FTS5 (e.g. a natural-language question with
# punctuation) is retried as a sanitized OR-of-terms expression instead of being
# dropped. Disable to restore the legacy "skip keyword tier on syntax error"
# behavior — used by the before/after benchmark to quantify the fix's impact.
FTS_SANITIZE_FALLBACK = True


# ---------------------------------------------------------------------------
# Structured query detection and lookup
# ---------------------------------------------------------------------------

# Regex for structured query patterns: subject:VALUE or predicate:VALUE
# Values can be quoted ("multi word") or unquoted single words.
_STRUCTURED_PATTERN = re.compile(
    r'(subject|predicate):"([^"]+)"|(subject|predicate):(\S+)'
)


def _detect_structured_query(query: str) -> dict[str, str] | None:
    """Parse query for subject:VALUE and/or predicate:VALUE structured patterns.

    Values can be quoted (subject:"Bailey Robertson") or unquoted single words
    (subject:Bailey). Returns a dict with found fields, or None if no structured
    pattern is detected.

    Args:
        query: The raw search query string.

    Returns:
        Dict with 'subject' and/or 'predicate' keys if structured patterns found,
        None otherwise.
    """
    result: dict[str, str] = {}
    for match in _STRUCTURED_PATTERN.finditer(query):
        if match.group(1):
            # Quoted value
            result[match.group(1)] = match.group(2)
        elif match.group(3):
            # Unquoted value
            result[match.group(3)] = match.group(4)
    return result if result else None


def _structured_lookup(
    db: sqlite3.Connection,
    subject: str | None,
    predicate: str | None,
    limit: int,
) -> list[dict]:
    """Perform indexed SQL lookup for structured fact triples.

    Builds a WHERE clause dynamically based on which fields are provided.
    Always excludes superseded facts (superseded_by IS NOT NULL).

    Args:
        db: An open SQLite connection.
        subject: Subject value to match, or None to skip.
        predicate: Predicate value to match, or None to skip.
        limit: Maximum number of results to return.

    Returns:
        List of memory dicts from matching rows.
    """
    conditions: list[str] = ["superseded_by IS NULL"]
    bindings: list[Any] = []

    if subject is not None:
        conditions.append("subject = ?")
        bindings.append(subject)
    if predicate is not None:
        conditions.append("predicate = ?")
        bindings.append(predicate)

    where_clause = " AND ".join(conditions)
    bindings.append(limit)

    rows = db.execute(
        f"SELECT * FROM memories WHERE {where_clause} LIMIT ?",
        bindings,
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _strip_structured_prefixes(query: str) -> str:
    """Remove subject:VALUE and predicate:VALUE patterns from a query string.

    Used when structured lookup returns no results and we fall back to FTS/semantic.

    Args:
        query: The raw search query string containing structured prefixes.

    Returns:
        The query with structured prefixes removed and extra whitespace cleaned.
    """
    stripped = _STRUCTURED_PATTERN.sub("", query).strip()
    # Collapse multiple spaces
    return re.sub(r"\s+", " ", stripped)


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


def _sanitize_fts_query(query: str) -> str:
    """Convert an arbitrary query into a safe FTS5 MATCH expression.

    Natural-language questions contain characters that FTS5 treats as operator
    syntax (``?``, ``,``, ``'``, ``$``, ``.`` …), which makes the raw query an
    invalid MATCH expression. This helper extracts the word tokens, wraps each
    in double quotes (so a token like ``or``/``and``/``near`` can't be parsed as
    an operator), and joins them with ``OR`` so any term can match. BM25 ranking
    still orders results by term importance, so common words don't dominate.

    Returns an empty string if the query has no usable tokens.

    Args:
        query: The raw user query.

    Returns:
        A valid FTS5 MATCH string, or "" when nothing is searchable.
    """
    tokens = re.findall(r"\w+", query, flags=re.UNICODE)
    if not tokens:
        return ""
    # Escape any embedded double quotes per FTS5 string rules ("" = literal ").
    return " OR ".join(f'"{t.replace(chr(34), chr(34) * 2)}"' for t in tokens)


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

    # --- Structured query detection (subject:X, predicate:Y) ---
    structured_fields = _detect_structured_query(params.query)
    if structured_fields:
        structured_results = _structured_lookup(
            db,
            subject=structured_fields.get("subject"),
            predicate=structured_fields.get("predicate"),
            limit=params.limit,
        )
        if structured_results:
            # Read-time vitality decay (DI-04): the stored column is an
            # at-access snapshot; recompute with real elapsed days.
            for m in structured_results:
                m["vitality"] = effective_vitality(m)

            # Apply filters (category, tags, dormant, vitality)
            filtered = _apply_filters(structured_results, params.category, params.tags)
            if not params.include_dormant:
                filtered = [m for m in filtered if not is_dormant(m["vitality"])]
            if params.min_vitality > 0:
                filtered = [
                    m for m in filtered
                    if m["vitality"] >= params.min_vitality
                ]

            # Wrap in token budget envelope and return
            envelope = apply_token_budget(filtered, params.token_budget)

            # Record access for returned results (fire-and-forget)
            returned_ids = [m["id"] for m in envelope["memories"]]
            if returned_ids:
                async def _record_accesses(ids: list[str]) -> None:
                    for mid in ids:
                        await asyncio.to_thread(record_access, mid)
                asyncio.create_task(_record_accesses(returned_ids))

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
            parts.append(f"**{envelope['returned']} results** via structured lookup")
            parts.append(f"_~{envelope['tokens_used']} tokens used (budget: {envelope['budget']})_\n")
            for m in envelope["memories"]:
                parts.append(_fmt_memory_md(m).rstrip())
                parts.append("")
            return _maybe_update_notice("\n---\n".join(parts))

        # Structured query detected but no results -- fall through to normal search
        # Strip structured prefixes from query before passing to FTS
        params = params.model_copy(update={"query": _strip_structured_prefixes(params.query)})
        if not params.query:
            # Nothing left after stripping -- return empty
            if params.response_format == ResponseFormat.JSON:
                return json.dumps(
                    {"total_candidates": 0, "returned": 0, "trimmed": 0, "tokens_used": 0, "budget": params.token_budget, "memories": []},
                    indent=2,
                )
            return "_No memories found._"

    # Dormancy and min_vitality are filtered in Python after the SQL fetch, so
    # over-fetch when they can prune candidates (DI-03); category/tag filters
    # go straight into the SQL of both tiers below.
    fetch_limit = params.limit
    if params.min_vitality > 0 or not params.include_dormant:
        fetch_limit = params.limit * 4

    # --- FTS5 keyword search ---
    fts_memories: list[dict] = []

    def _run_fts(match_query: str) -> list[dict]:
        conditions = ""
        bindings: list[Any] = [match_query]
        if params.category:
            conditions += " AND m.category = ?"
            bindings.append(params.category)
        for i, tag in enumerate(params.tags or []):
            alias = f"mt{i}"
            conditions += (
                f" AND EXISTS (SELECT 1 FROM memory_tags {alias}"
                f" WHERE {alias}.memory_id = m.id AND {alias}.tag = ?)"
            )
            bindings.append(tag)
        bindings.append(fetch_limit)
        rows = db.execute(
            f"""SELECT m.* FROM memories m
               JOIN memories_fts fts ON m.rowid = fts.rowid
               WHERE memories_fts MATCH ?
               AND m.superseded_by IS NULL{conditions}
               ORDER BY rank
               LIMIT ?""",
            bindings,
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    try:
        # Try the query as-is first so documented FTS5 syntax (OR, NOT, "phrase",
        # prefix*) keeps working for power users.
        fts_memories = _run_fts(params.query)
    except sqlite3.OperationalError as raw_err:
        # Raw query isn't valid FTS5 (typical for natural-language questions with
        # punctuation). Retry with a sanitized OR-of-terms expression instead of
        # giving up — this keeps the keyword tier contributing to hybrid ranking.
        # The fallback can be disabled (e.g. for benchmarking) via FTS_SANITIZE_FALLBACK.
        safe_query = _sanitize_fts_query(params.query) if FTS_SANITIZE_FALLBACK else ""
        if safe_query:
            try:
                fts_memories = _run_fts(safe_query)
            except sqlite3.OperationalError as e:
                log.warning("FTS5 query syntax error for query %r (sanitized %r): %s",
                            params.query, safe_query, e)
        else:
            log.warning("FTS5 query syntax error for query %r: %s", params.query, raw_err)

    # --- Semantic vector search (optionally HyDE-expanded) ---
    # HyDE output is only consumed by the semantic tier — skip the (slow)
    # generation entirely when no embedder is available (DI-08).
    extra_texts: list[str] = []
    if _get_embedder() is not None:
        extra_texts = await asyncio.to_thread(expand_query, params.query)
    sem_memories = await asyncio.to_thread(
        _semantic_search,
        params.query,
        limit=fetch_limit,
        extra_texts=extra_texts,
        category=params.category,
        tags=params.tags,
    )

    # --- Tag search method on raw results before RRF ---
    fts_ids = {m["id"] for m in fts_memories}
    sem_ids = {m["id"] for m in sem_memories}
    for m in fts_memories:
        m["_search_method"] = "keyword"
    for m in sem_memories:
        m["_search_method"] = "semantic"

    # --- Read-time vitality decay (DI-04): the stored column is an at-access
    # snapshot; recompute with real elapsed days so decay, dormancy, and the
    # RRF vitality signal reflect reality. ---
    for m in (*fts_memories, *sem_memories):
        m["vitality"] = effective_vitality(m)

    # Category/tag filters are already applied in the SQL of both tiers (DI-03).
    filtered_fts = fts_memories
    filtered_sem = sem_memories

    # --- Count dormant memories BEFORE exclusion (unique by ID) ---
    dormant_ids: set[str] = set()
    for m in filtered_fts + filtered_sem:
        if is_dormant(m["vitality"]):
            dormant_ids.add(m["id"])
    dormant_excluded = len(dormant_ids) if not params.include_dormant else 0

    # --- Dormant exclusion BEFORE RRF ranking ---
    if not params.include_dormant:
        filtered_fts = [m for m in filtered_fts if not is_dormant(m["vitality"])]
        filtered_sem = [m for m in filtered_sem if not is_dormant(m["vitality"])]

    # --- Min vitality filter ---
    if params.min_vitality > 0:
        filtered_fts = [
            m for m in filtered_fts if m["vitality"] >= params.min_vitality
        ]
        filtered_sem = [
            m for m in filtered_sem if m["vitality"] >= params.min_vitality
        ]

    # --- RRF ranking ---
    ranked = rank_rrf(filtered_fts, filtered_sem)

    # Mark hybrid results (appeared in both FTS and semantic)
    for m in ranked:
        mid = m["id"]
        if mid in fts_ids and mid in sem_ids:
            m["_search_method"] = "hybrid"

    # --- Optional cross-encoder rerank of the top candidates (lever D) ---
    # Rerank a pool larger than the response limit so the cross-encoder can
    # promote candidates beyond the head, THEN truncate (DI-07).
    rerank_pool = max(params.limit, RERANK_TOP_K)
    ranked = await asyncio.to_thread(maybe_rerank, params.query, ranked[:rerank_pool])

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

    # --- Attach debug signals if verbose ---
    if params.verbose:
        for m in envelope["memories"]:
            m["debug_signals"] = build_debug_signals(m)

    # --- Compute tier breakdown (always) ---
    tier_breakdown = compute_tier_breakdown(envelope["memories"])

    # --- Format response ---
    if params.response_format == ResponseFormat.JSON:
        return json.dumps(
            {
                "total_candidates": envelope["total_candidates"],
                "returned": envelope["returned"],
                "trimmed": envelope["trimmed"],
                "tokens_used": envelope["tokens_used"],
                "budget": envelope["budget"],
                "tier_breakdown": tier_breakdown,
                "dormant_excluded": dormant_excluded,
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
        parts.append(f"_{' · '.join(extras)}_")
        if params.verbose:
            signals = m.get("debug_signals", {})
            parts.append(
                f"_Ranks: kw={signals.get('keyword_rank', '?')} "
                f"sem={signals.get('semantic_rank', '?')} "
                f"rec={signals.get('recency_rank', '?')} "
                f"vit={signals.get('vitality_rank', '?')} "
                f"| {signals.get('days_old', '?')} days old_"
            )
        parts.append("")  # blank line separator

    # Always append tier breakdown summary line
    parts.append(
        f"_Tiers: {tier_breakdown['keyword']} keyword, "
        f"{tier_breakdown['semantic']} semantic, "
        f"{tier_breakdown['hybrid']} hybrid "
        f"| {dormant_excluded} dormant excluded_"
    )

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
    row = db.execute(
        "SELECT rowid FROM memories WHERE id = ?", (params.memory_id,)
    ).fetchone()
    if row is None:
        return f"Memory `{params.memory_id}` not found."
    # Remove chunk vectors first — FTS and tags are cleaned by triggers, but
    # vec_chunks/memories_vec are not, and SQLite reuses freed rowids (DI-01).
    with contextlib.suppress(sqlite3.OperationalError):
        _delete_chunks(db, row[0])
    db.execute("DELETE FROM memories WHERE id = ?", (params.memory_id,))
    db.commit()
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
    await asyncio.to_thread(_embed_and_store, dialog_id, params.conversation)

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
    # Prune chunk vectors orphaned by old deletes — a reused rowid would
    # otherwise keep the deleted memory's embedding and be skipped below (DI-01).
    pruned = 0
    try:
        pruned = await asyncio.to_thread(_prune_orphan_chunks, db)
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
        created += await asyncio.to_thread(_embed_and_store_rows, batch)

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

    # Effective (read-time) vitality per memory — the stored column is a
    # stale at-access snapshot (DI-04).
    rows = db.execute("SELECT * FROM memories").fetchall()
    total = len(rows)
    vitalities = [effective_vitality(_row_to_dict(r)) for r in rows]

    # Core counts (dormancy from effective vitality, not the stored status)
    dormant_count = sum(1 for v in vitalities if is_dormant(v))
    active_count = total - dormant_count

    # Average vitality
    avg_vitality = round(sum(vitalities) / total, 2) if total > 0 else 0.0

    # Decay distribution by memory_type
    type_rows = db.execute(
        "SELECT memory_type, COUNT(*) as cnt FROM memories GROUP BY memory_type"
    ).fetchall()
    decay_distribution = {r["memory_type"]: r["cnt"] for r in type_rows}

    # Vitality buckets. The top bucket is open-ended: accessed memories exceed
    # 1.0 (one access -> sqrt(2) ~= 1.41), so a closed bucket would lose them
    # and the counts wouldn't sum to the total (DI-04).
    bucket_ranges = [
        ("0.00-0.05", 0.0, 0.05),
        ("0.05-0.25", 0.05, 0.25),
        ("0.25-0.50", 0.25, 0.50),
        ("0.50-0.75", 0.50, 0.75),
    ]
    top_bucket = "0.75+"
    vitality_buckets: dict[str, int] = {label: 0 for label, _, _ in bucket_ranges}
    vitality_buckets[top_bucket] = 0
    for v in vitalities:
        for label, low, high in bucket_ranges:
            if low <= v < high:
                vitality_buckets[label] += 1
                break
        else:
            vitality_buckets[top_bucket] += 1

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
# Consolidation tools (Phase 14 Plan 02)
# ---------------------------------------------------------------------------


@mcp.tool(
    name="remind_me_consolidate",
    annotations={
        "title": "Consolidate Duplicate Memories",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def remind_me_consolidate(params: ConsolidateInput) -> str:
    """Find clusters of semantically similar memories and optionally merge them.

    In dry_run mode (default), reports clusters with canonical and member details
    without modifying data. In auto-merge mode (dry_run=False), merges content
    into the canonical memory, sets superseded_by on members, sums access counts,
    recalculates vitality, and re-embeds the canonical.

    Only active, non-superseded memories are considered for consolidation.

    Args:
        params: Consolidation parameters (similarity_threshold, dry_run, category, limit).

    Returns:
        JSON string with cluster report (dry_run) or merge results (auto-merge).
    """
    db = _get_db()

    # Build query to fetch active, non-superseded memories with embeddings
    sql = """
        SELECT m.id, m.content, m.vitality, m.access_count, m.accessed_at,
               m.category, m.tags, m.memory_type, m.decay_rate, m.base_weight,
               mv.embedding
        FROM memories m
        JOIN vec_chunks vc ON vc.memory_rowid = m.rowid AND vc.chunk_ix = 0
        JOIN memories_vec mv ON mv.rowid = vc.vec_rowid
        WHERE m.status = 'active'
          AND m.superseded_by IS NULL
    """
    query_params: list[Any] = []

    if params.category is not None:
        sql += "  AND m.category = ?\n"
        query_params.append(params.category)

    sql += "  LIMIT ?"
    query_params.append(params.limit)

    rows = db.execute(sql, query_params).fetchall()

    if not rows:
        return json.dumps({"clusters_found": 0, "message": "No eligible memories found"})

    # Build memories list and embeddings dict
    memories: list[dict[str, Any]] = []
    embeddings: dict[str, bytes] = {}

    for row in rows:
        raw_tags = row["tags"]
        if isinstance(raw_tags, str):
            try:
                tags = json.loads(raw_tags)
            except (json.JSONDecodeError, TypeError):
                tags = []
        else:
            tags = raw_tags if raw_tags else []

        mem = {
            "id": row["id"],
            "content": row["content"],
            "vitality": row["vitality"],
            "access_count": row["access_count"],
            "accessed_at": row["accessed_at"],
            "category": row["category"],
            "tags": tags,
            "memory_type": row["memory_type"],
            "decay_rate": row["decay_rate"],
            "base_weight": row["base_weight"],
        }
        memories.append(mem)
        embeddings[row["id"]] = bytes(row["embedding"])

    # Find clusters
    clusters = find_clusters(memories, embeddings, params.similarity_threshold)

    if not clusters:
        return json.dumps({"clusters_found": 0, "message": "No similar memories found above threshold"})

    # --- Dry run mode ---
    if params.dry_run:
        cluster_reports: list[dict[str, Any]] = []

        import numpy as np

        for cluster in clusters:
            canonical = pick_canonical(cluster)
            members = [m for m in cluster if m["id"] != canonical["id"]]

            # Compute similarity between canonical and each member for display
            canonical_emb = embeddings[canonical["id"]]
            dim = len(canonical_emb) // 4  # float32 = 4 bytes
            canonical_vec = np.frombuffer(canonical_emb, dtype=np.float32).reshape(dim)

            member_reports: list[dict[str, Any]] = []
            for member in members:
                member_emb = embeddings[member["id"]]
                member_vec = np.frombuffer(member_emb, dtype=np.float32).reshape(dim)
                similarity = float(np.dot(canonical_vec, member_vec))

                member_reports.append({
                    "id": member["id"],
                    "content": member["content"][:200],
                    "vitality": member["vitality"],
                    "similarity": round(similarity, 4),
                })

            cluster_reports.append({
                "canonical": {
                    "id": canonical["id"],
                    "content": canonical["content"][:200],
                    "vitality": canonical["vitality"],
                },
                "members": member_reports,
                "cluster_size": len(cluster),
            })

        result = {
            "clusters_found": len(clusters),
            "dry_run": True,
            "clusters": cluster_reports,
        }
        return json.dumps(result, indent=2)

    # --- Auto-merge mode ---
    now = _now_iso()
    total_superseded = 0
    canonical_ids: list[str] = []

    for cluster in clusters:
        canonical = pick_canonical(cluster)
        members = [m for m in cluster if m["id"] != canonical["id"]]

        merged = merge_cluster(canonical, members)

        # Update canonical: content, access_count, tags, updated_at
        db.execute(
            """UPDATE memories
               SET content = ?, access_count = ?, tags = ?, updated_at = ?
               WHERE id = ?""",
            (
                merged["merged_content"],
                merged["total_access_count"],
                json.dumps(merged["merged_tags"]),
                now,
                canonical["id"],
            ),
        )

        # Recalculate vitality for canonical with new access_count
        effective_rate = get_effective_decay_rate(
            canonical["decay_rate"], merged["total_access_count"]
        )
        new_vitality = compute_vitality(
            base_weight=canonical["base_weight"],
            access_count=merged["total_access_count"],
            decay_rate=effective_rate,
            days_since_last_access=0.0,
        )

        db.execute(
            """UPDATE memories
               SET vitality = ?, status = 'active'
               WHERE id = ?""",
            (new_vitality, canonical["id"]),
        )

        # Set superseded_by on each member
        for member in members:
            db.execute(
                """UPDATE memories
                   SET superseded_by = ?, updated_at = ?
                   WHERE id = ?""",
                (canonical["id"], now, member["id"]),
            )

        total_superseded += len(members)
        canonical_ids.append(canonical["id"])

        # Fire-and-forget re-embed canonical with merged content
        asyncio.create_task(
            asyncio.to_thread(_embed_and_store, canonical["id"], merged["merged_content"])
        )

    db.commit()

    result = {
        "clusters_merged": len(clusters),
        "memories_superseded": total_superseded,
        "canonical_ids": canonical_ids,
        "dry_run": False,
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
    "remind_me_consolidate",
    "resource_stats",
    "resource_categories",
]
