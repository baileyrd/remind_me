"""
remind_me_mcp.tools.search — remind_me_search and its structured-query helpers.

Patchable shared state and cross-module helpers are looked up through the
``remind_me_mcp.tools`` package namespace (``_pkg.<name>``) at call time so
monkeypatching ``remind_me_mcp.tools.<name>`` keeps working (HY-02).
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from typing import Any

from remind_me_mcp import tools as _pkg
from remind_me_mcp.db import _normalize_entity_name, _resolve_entity, _row_to_dict
from remind_me_mcp.formatting import _fmt_memory_md
from remind_me_mcp.models import (
    FeedbackInput,
    MemorySearchInput,
    ResponseFormat,
    RetrievalStrategy,
)
from remind_me_mcp.reranker import RERANK_TOP_K
from remind_me_mcp.retrieval import (
    apply_token_budget,
    build_debug_signals,
    choose_rrf_weights,
    compute_tier_breakdown,
    rank_rrf,
    resolve_strategy_weights,
)
from remind_me_mcp.server import mcp
from remind_me_mcp.tools._shared import _maybe_update_notice, _public_memory, log
from remind_me_mcp.vitality import effective_vitality, is_dormant

# ---------------------------------------------------------------------------
# Structured query detection and lookup
# ---------------------------------------------------------------------------

# Regex for structured query patterns: subject:VALUE, predicate:VALUE, or
# entity:VALUE (FT-04). Values can be quoted ("multi word") or unquoted
# single words.
_STRUCTURED_PATTERN = re.compile(
    r'(subject|predicate|entity):"([^"]+)"|(subject|predicate|entity):(\S+)'
)


def _detect_structured_query(query: str) -> dict[str, str] | None:
    """Parse query for subject:/predicate:/entity: structured patterns.

    Values can be quoted (subject:"Bailey Robertson") or unquoted single words
    (subject:Bailey). Returns a dict with found fields, or None if no structured
    pattern is detected.

    Args:
        query: The raw search query string.

    Returns:
        Dict with 'subject', 'predicate', and/or 'entity' keys if structured
        patterns found, None otherwise.
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
    entity: dict[str, Any] | None = None,
) -> list[dict]:
    """Perform indexed SQL lookup for structured fact triples.

    Builds a WHERE clause dynamically based on which fields are provided.
    Always excludes superseded facts (superseded_by IS NOT NULL). The
    optional resolved *entity* (FT-04) AND-composes with subject/predicate:
    a memory matches when it is linked to the entity via memory_entities OR
    its SPO subject/object equals the entity's canonical name
    (case-insensitive; the canonical name is already whitespace-collapsed).

    Args:
        db: An open SQLite connection.
        subject: Subject value to match, or None to skip.
        predicate: Predicate value to match, or None to skip.
        limit: Maximum number of results to return.
        entity: A resolved entity row (from ``_resolve_entity``), or None.

    Returns:
        List of memory dicts from matching rows.
    """
    conditions: list[str] = ["m.superseded_by IS NULL"]
    bindings: list[Any] = []

    if subject is not None:
        conditions.append("m.subject = ?")
        bindings.append(subject)
    if predicate is not None:
        conditions.append("m.predicate = ?")
        bindings.append(predicate)
    if entity is not None:
        canon = _normalize_entity_name(str(entity["name"]))
        conditions.append(
            "(EXISTS (SELECT 1 FROM memory_entities me"
            " WHERE me.memory_id = m.id AND me.entity_id = ?)"
            " OR lower(m.subject) = ? OR lower(m.object) = ?)"
        )
        bindings.extend([entity["id"], canon, canon])

    where_clause = " AND ".join(conditions)
    bindings.append(limit)

    rows = db.execute(
        f"SELECT m.* FROM memories m WHERE {where_clause} LIMIT ?",
        bindings,
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _strip_structured_prefixes(query: str) -> str:
    """Remove subject:/predicate:/entity: VALUE patterns from a query string.

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
# Shared response helpers (HY-02): the structured-lookup path and the hybrid
# path previously duplicated these blocks nearly verbatim.
# ---------------------------------------------------------------------------


def _record_envelope_access(envelope: dict[str, Any]) -> None:
    """Record access for an envelope's returned results (fire-and-forget, one
    batched transaction — PF-02/PF-04)."""
    returned_ids = [m["id"] for m in envelope["memories"]]
    if returned_ids:
        _pkg._spawn_task(asyncio.to_thread(_pkg.record_accesses, returned_ids))


def _envelope_json(envelope: dict[str, Any], extra: dict[str, Any] | None = None) -> str:
    """Serialize a token-budget envelope as the JSON response body.

    *extra* keys (tier_breakdown / dormant_excluded on the hybrid path) are
    inserted between the budget fields and the memories list, preserving the
    original key order of both paths.
    """
    payload: dict[str, Any] = {
        "total_candidates": envelope["total_candidates"],
        "returned": envelope["returned"],
        "trimmed": envelope["trimmed"],
        "tokens_used": envelope["tokens_used"],
        "budget": envelope["budget"],
    }
    if extra:
        payload.update(extra)
    # HY-05: internal ranking fields stay out of the payload;
    # verbose=True exposes them via debug_signals instead.
    payload["memories"] = [_public_memory(m) for m in envelope["memories"]]
    return json.dumps(payload, indent=2, default=str)


# ---------------------------------------------------------------------------
# 1-hop entity-graph expansion (FT-04, opt-in via expand_entities)
# ---------------------------------------------------------------------------

# Maximum number of extra memories appended by 1-hop expansion. Kept small:
# expansions sit OUTSIDE the token-budget envelope, so the cap (plus the
# 300-char snippets) is what bounds their response cost.
_ENTITY_EXPANSION_CAP = 5


def _expand_via_entities(
    db: sqlite3.Connection,
    memories: list[dict],
    cap: int = _ENTITY_EXPANSION_CAP,
) -> list[dict[str, Any]]:
    """Collect up to *cap* 1-hop entity-graph neighbors of the given results.

    Finds entities mentioned by the seed memories, then other non-superseded
    memories sharing those entities (newest first), excluding the seeds
    themselves. Each item carries the linking entity name(s) in
    ``via_entities``. INNER joins on entities and memories keep dangling
    links (sync may deliver a link before its endpoints) invisible.

    Access recording (PF-02): expanded hits are deliberately NOT recorded —
    they are a discovery aid surfaced by graph adjacency, not direct matches
    for the user's query, and recording them would inflate the vitality of
    every neighbor on each expanded search.

    Args:
        db: An open SQLite connection.
        memories: The main ranked results (the seeds).
        cap: Maximum number of expansion items to return.

    Returns:
        List of {id, content_snippet, category, created_at, via_entities}
        dicts; empty when there are no seeds or no neighbors.
    """
    seed_ids = [m["id"] for m in memories]
    if not seed_ids:
        return []
    ph = ",".join("?" * len(seed_ids))
    rows = db.execute(
        f"""SELECT m.id, substr(m.content, 1, 300) AS content_snippet,
                   m.category, m.created_at, e.name AS entity_name
            FROM memory_entities seed
            JOIN memory_entities nbr ON nbr.entity_id = seed.entity_id
            JOIN entities e ON e.id = seed.entity_id
            JOIN memories m ON m.id = nbr.memory_id
            WHERE seed.memory_id IN ({ph})
              AND nbr.memory_id NOT IN ({ph})
              AND m.superseded_by IS NULL
            ORDER BY m.created_at DESC, m.id, e.name""",
        [*seed_ids, *seed_ids],
    ).fetchall()

    expanded: dict[str, dict[str, Any]] = {}
    for r in rows:
        item = expanded.get(r["id"])
        if item is None:
            if len(expanded) >= cap:
                continue
            item = {
                "id": r["id"],
                "content_snippet": r["content_snippet"],
                "category": r["category"],
                "created_at": r["created_at"],
                "via_entities": [],
            }
            expanded[r["id"]] = item
        if r["entity_name"] not in item["via_entities"]:
            item["via_entities"].append(r["entity_name"])
    return list(expanded.values())


def _fmt_expansion_md(related: list[dict[str, Any]]) -> str:
    """Render the related_via_entities section for markdown responses."""
    lines = [
        f"**Related via entities** (1-hop expansion, max {_ENTITY_EXPANSION_CAP}):"
    ]
    for item in related:
        snippet = " ".join(str(item["content_snippet"]).split())
        if len(snippet) > 120:
            snippet = snippet[:120] + "…"
        lines.append(
            f"- `{item['id']}` {snippet} _(via: {', '.join(item['via_entities'])})_"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Neighbor-aware chunk expansion (opt-in via include_neighbors)
# ---------------------------------------------------------------------------

# Maximum number of extra memories appended by neighbor expansion. Kept small
# for the same reason as _ENTITY_EXPANSION_CAP: expansions sit OUTSIDE the
# token-budget envelope, so the cap (plus the 300-char snippets) is what
# bounds their response cost.
_NEIGHBOR_EXPANSION_CAP = 5

# How many chunk positions on either side of a seed to include.
_NEIGHBOR_WINDOW = 1


def _expand_via_neighbors(
    db: sqlite3.Connection,
    memories: list[dict],
    window: int = _NEIGHBOR_WINDOW,
    cap: int = _NEIGHBOR_EXPANSION_CAP,
) -> list[dict[str, Any]]:
    """Collect up to *cap* sibling chunks (same doc_id, adjacent chunk_index).

    Only seed memories produced by an import carry a doc_id/chunk_index
    (manually added memories and other single-row sources have neither, so
    they are skipped). For each such seed, fetches sibling rows from the
    same source document within +/- *window* chunk positions, excluding the
    seeds themselves and superseded rows.

    Access recording (PF-02): like _expand_via_entities, expanded hits are
    deliberately NOT recorded -- they are a discovery aid surfaced by
    document adjacency, not direct matches for the user's query.

    Args:
        db: An open SQLite connection.
        memories: The main ranked results (the seeds).
        window: How many chunk positions on either side to include.
        cap: Maximum number of expansion items to return.

    Returns:
        List of {id, content_snippet, category, created_at, doc_id,
        chunk_index} dicts; empty when no seed carries a doc_id.
    """
    seed_ids = {m["id"] for m in memories}
    expanded: dict[str, dict[str, Any]] = {}

    for m in memories:
        if len(expanded) >= cap:
            break
        doc_id = m.get("doc_id")
        chunk_index = m.get("chunk_index")
        if doc_id is None or chunk_index is None:
            continue

        rows = db.execute(
            """SELECT id, substr(content, 1, 300) AS content_snippet,
                      category, created_at, doc_id, chunk_index
               FROM memories
               WHERE doc_id = ?
                 AND chunk_index BETWEEN ? AND ?
                 AND superseded_by IS NULL
               ORDER BY chunk_index""",
            (doc_id, chunk_index - window, chunk_index + window),
        ).fetchall()

        for r in rows:
            if r["id"] in seed_ids or r["id"] in expanded:
                continue
            if len(expanded) >= cap:
                break
            expanded[r["id"]] = {
                "id": r["id"],
                "content_snippet": r["content_snippet"],
                "category": r["category"],
                "created_at": r["created_at"],
                "doc_id": r["doc_id"],
                "chunk_index": r["chunk_index"],
            }

    return list(expanded.values())


def _fmt_neighbor_expansion_md(related: list[dict[str, Any]]) -> str:
    """Render the related_via_neighbors section for markdown responses."""
    lines = [
        f"**Related via document neighbors** (adjacent chunks, max {_NEIGHBOR_EXPANSION_CAP}):"
    ]
    for item in related:
        snippet = " ".join(str(item["content_snippet"]).split())
        if len(snippet) > 120:
            snippet = snippet[:120] + "…"
        lines.append(
            f"- `{item['id']}` {snippet} _(doc: {item['doc_id']}, chunk: {item['chunk_index']})_"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------


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

    db = _pkg._get_db()

    # --- Structured query detection (subject:X, predicate:Y, entity:Z) ---
    structured_fields = _detect_structured_query(params.query)
    if structured_fields:
        entity_row: dict[str, Any] | None = None
        if "entity" in structured_fields:
            entity_row = _resolve_entity(db, structured_fields["entity"])
            if entity_row is None:
                # Unresolvable entity filter — empty result with a clear
                # message (no fallback: the user asked for a specific entity).
                message = (
                    f"No entity found matching {structured_fields['entity']!r}."
                )
                if params.response_format == ResponseFormat.JSON:
                    return _envelope_json(
                        {
                            "total_candidates": 0,
                            "returned": 0,
                            "trimmed": 0,
                            "tokens_used": 0,
                            "budget": params.token_budget,
                            "memories": [],
                        },
                        extra={"message": message},
                    )
                return f"_No memories found. {message}_"

        structured_results = _structured_lookup(
            db,
            subject=structured_fields.get("subject"),
            predicate=structured_fields.get("predicate"),
            limit=params.limit,
            entity=entity_row,
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

            _record_envelope_access(envelope)

            # FT-04: opt-in 1-hop entity-graph expansion (expanded hits are
            # not access-recorded — see _expand_via_entities).
            related: list[dict[str, Any]] = []
            if params.expand_entities:
                related = _expand_via_entities(db, envelope["memories"])

            # Opt-in neighbor-aware chunk expansion (expanded hits are not
            # access-recorded — see _expand_via_neighbors).
            related_neighbors: list[dict[str, Any]] = []
            if params.include_neighbors:
                related_neighbors = _expand_via_neighbors(db, envelope["memories"])

            if params.response_format == ResponseFormat.JSON:
                extra: dict[str, Any] = {}
                if params.expand_entities:
                    extra["related_via_entities"] = related
                if params.include_neighbors:
                    extra["related_via_neighbors"] = related_neighbors
                return _envelope_json(envelope, extra=extra or None)

            if not envelope["memories"]:
                return "_No memories found._"

            parts: list[str] = []
            parts.append(f"**{envelope['returned']} results** via structured lookup")
            parts.append(f"_~{envelope['tokens_used']} tokens used (budget: {envelope['budget']})_\n")
            for m in envelope["memories"]:
                parts.append(_fmt_memory_md(m).rstrip())
                parts.append("")
            if related:
                parts.append(_fmt_expansion_md(related))
            if related_neighbors:
                parts.append(_fmt_neighbor_expansion_md(related_neighbors))
            return _maybe_update_notice("\n---\n".join(parts))

        # Structured query detected but no results -- fall through to normal search
        # Strip structured prefixes from query before passing to FTS
        params = params.model_copy(update={"query": _strip_structured_prefixes(params.query)})
        if not params.query:
            # Nothing left after stripping -- return empty
            if params.response_format == ResponseFormat.JSON:
                return _envelope_json(
                    {"total_candidates": 0, "returned": 0, "trimmed": 0, "tokens_used": 0, "budget": params.token_budget, "memories": []}
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
            f"""SELECT m.*, bm25(memories_fts) AS _bm25_score FROM memories m
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
        safe_query = _sanitize_fts_query(params.query) if _pkg.FTS_SANITIZE_FALLBACK else ""
        if safe_query:
            try:
                fts_memories = _run_fts(safe_query)
            except sqlite3.OperationalError as e:
                log.warning("FTS5 query syntax error for query %r (sanitized %r): %s",
                            params.query, safe_query, e)
        else:
            log.warning("FTS5 query syntax error for query %r: %s", params.query, raw_err)

    # --- Semantic vector search (optionally HyDE-expanded) ---
    # Embedder availability can mean a network probe (Ollama) or a model load
    # (ONNX) — never run it on the event loop (PF-01). HyDE output is only
    # consumed by the semantic tier — skip the (slow) generation entirely
    # when no embedder is available (DI-08).
    def _probe_embedder_and_expand(query: str) -> tuple[bool, list[str]]:
        if _get_embedder() is None:
            return False, []
        return True, _pkg.expand_query(query)

    sem_available, extra_texts = await asyncio.to_thread(
        _probe_embedder_and_expand, params.query
    )
    sem_memories = await asyncio.to_thread(
        _pkg._semantic_search,
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

    # --- RRF ranking (Phase 6: strategy selects the weight profile) ---
    if params.strategy == RetrievalStrategy.AUTO:
        weights = choose_rrf_weights(
            params.query, structured=False, has_semantic=sem_available
        )
    else:
        weights = resolve_strategy_weights(params.strategy.value)
    ranked = rank_rrf(filtered_fts, filtered_sem, **weights)

    # Mark hybrid results (appeared in both FTS and semantic)
    for m in ranked:
        mid = m["id"]
        if mid in fts_ids and mid in sem_ids:
            m["_search_method"] = "hybrid"

    # --- Optional cross-encoder rerank of the top candidates (lever D) ---
    # Rerank a pool larger than the response limit so the cross-encoder can
    # promote candidates beyond the head, THEN truncate (DI-07).
    rerank_pool = max(params.limit, RERANK_TOP_K)
    ranked = await asyncio.to_thread(_pkg.maybe_rerank, params.query, ranked[:rerank_pool])

    # --- Apply limit, then token budget ---
    ranked = ranked[:params.limit]

    if params.token_budget == 0:
        envelope = apply_token_budget(ranked, 0)
    else:
        envelope = apply_token_budget(ranked, params.token_budget)

    _record_envelope_access(envelope)

    # --- FT-04: opt-in 1-hop entity-graph expansion (appended after ranking,
    # never reordering the main results; expanded hits are not
    # access-recorded — see _expand_via_entities) ---
    related = []  # also annotated in the structured-path branch above
    if params.expand_entities:
        related = _expand_via_entities(db, envelope["memories"])

    # --- Opt-in neighbor-aware chunk expansion (expanded hits are not
    # access-recorded — see _expand_via_neighbors) ---
    related_neighbors = []  # also annotated in the structured-path branch above
    if params.include_neighbors:
        related_neighbors = _expand_via_neighbors(db, envelope["memories"])

    # --- Attach debug signals if verbose (Phase 6: includes the resolved
    # strategy/weight profile actually used for this search) ---
    if params.verbose:
        for m in envelope["memories"]:
            m["debug_signals"] = build_debug_signals(
                m, strategy=params.strategy.value, weights=weights
            )

    # --- Compute tier breakdown (always) ---
    tier_breakdown = compute_tier_breakdown(envelope["memories"])

    # --- Format response ---
    if params.response_format == ResponseFormat.JSON:
        extra = {  # also annotated in the structured-path branch above
            "tier_breakdown": tier_breakdown,
            "dormant_excluded": dormant_excluded,
        }
        if params.expand_entities:
            extra["related_via_entities"] = related
        if params.include_neighbors:
            extra["related_via_neighbors"] = related_neighbors
        return _envelope_json(envelope, extra=extra)

    if not envelope["memories"]:
        return "_No memories found._"

    parts: list[str] = []  # type: ignore[no-redef]  # also annotated in the structured-path branch above
    # Availability was probed off-loop above (PF-01); non-empty semantic
    # results also prove the semantic tier ran.
    method_label = (
        "hybrid (keyword + semantic)"
        if sem_available or sem_memories
        else "keyword only"
    )
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
                f"idf={signals.get('idf_rank', '?')} "
                f"| {signals.get('days_old', '?')} days old "
                f"| strategy={signals.get('strategy', '?')}_"
            )
        parts.append("")  # blank line separator

    if related:
        parts.append(_fmt_expansion_md(related))
    if related_neighbors:
        parts.append(_fmt_neighbor_expansion_md(related_neighbors))

    # Always append tier breakdown summary line
    parts.append(
        f"_Tiers: {tier_breakdown['keyword']} keyword, "
        f"{tier_breakdown['semantic']} semantic, "
        f"{tier_breakdown['hybrid']} hybrid "
        f"| {dormant_excluded} dormant excluded_"
    )

    return _maybe_update_notice("\n---\n".join(parts))


# ---------------------------------------------------------------------------
# Search feedback (helpful/unhelpful signal into vitality/ranking)
# ---------------------------------------------------------------------------


@mcp.tool(
    name="remind_me_feedback",
    annotations={
        "title": "Record Search Feedback",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def remind_me_feedback(params: FeedbackInput) -> str:
    """Mark a memory as helpful or unhelpful for a search result.

    Unlike plain access recording (which happens automatically and is always
    positive), this is a signed signal: "helpful" nudges the memory's
    base_weight up, "unhelpful" nudges it down, and both are reflected in
    vitality (and therefore future RRF ranking) immediately.

    Args:
        params (FeedbackInput): The memory id, signal, and optional query context.

    Returns:
        str: JSON with the memory id and its updated vitality, or an error message.
    """
    new_vitality = _pkg.record_feedback(params.memory_id, params.signal)
    if new_vitality is None:
        return json.dumps({"error": f"Memory not found: {params.memory_id}"})
    return json.dumps(
        {
            "memory_id": params.memory_id,
            "signal": params.signal,
            "vitality": new_vitality,
        }
    )
