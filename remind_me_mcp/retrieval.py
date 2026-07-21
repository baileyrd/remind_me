"""remind_me_mcp.retrieval -- RRF ranking, recency signal, and token budget trimming for search results."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TypedDict

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

RRF_K: int = int(os.environ.get("REMIND_ME_RRF_K", "60"))
"""Reciprocal Rank Fusion smoothing constant. Higher values produce more uniform scores."""

# Per-signal RRF weights. The default (all 1.0) reproduces the original
# four-signal fusion. Recency and vitality are valuable for a *living* personal
# memory, but they are relevance-irrelevant on a pure retrieval benchmark and
# dilute the keyword/semantic signals — set their weights to 0 for a
# retrieval-quality profile (e.g. REMIND_ME_RRF_W_RECENCY=0).
RRF_W_KEYWORD: float = float(os.environ.get("REMIND_ME_RRF_W_KEYWORD", "1.0"))
RRF_W_SEMANTIC: float = float(os.environ.get("REMIND_ME_RRF_W_SEMANTIC", "1.0"))
RRF_W_RECENCY: float = float(os.environ.get("REMIND_ME_RRF_W_RECENCY", "1.0"))
RRF_W_VITALITY: float = float(os.environ.get("REMIND_ME_RRF_W_VITALITY", "1.0"))

# IDF signal, derived from FTS5's bm25() score (lower = better match). Unlike
# the four signals above, this defaults to 0 (off) rather than 1 -- it's a new
# lever layered on top of already-tuned defaults, and flipping it on by
# default would silently shift existing benchmark numbers. Opt in with
# REMIND_ME_RRF_W_IDF=1 (or any positive weight).
RRF_W_IDF: float = float(os.environ.get("REMIND_ME_RRF_W_IDF", "0.0"))


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class SearchEnvelope(TypedDict):
    """Metadata envelope wrapping ranked search results after token budget trimming."""

    memories: list[dict]
    total_candidates: int
    returned: int
    trimmed: int
    tokens_used: int
    budget: int


# ---------------------------------------------------------------------------
# RRF ranking
# ---------------------------------------------------------------------------


def rank_rrf(
    keyword_results: list[dict],
    semantic_results: list[dict],
    *,
    k: int | None = None,
    w_keyword: float | None = None,
    w_semantic: float | None = None,
    w_recency: float | None = None,
    w_vitality: float | None = None,
    w_idf: float | None = None,
) -> list[dict]:
    """Fuse keyword, semantic, recency, vitality, and IDF ranked lists via Reciprocal Rank Fusion.

    Each memory receives five rank signals:
      - keyword_rank: position in *keyword_results* (1-indexed)
      - semantic_rank: position in *semantic_results* (1-indexed)
      - recency_rank: position when all unique memories are sorted by created_at DESC
      - vitality_rank: position when all unique memories are sorted by vitality DESC
        (higher vitality = better rank). Memories without a ``vitality`` key default to 1.0.
      - idf_rank: position when all unique memories are sorted by FTS5 ``bm25()``
        score ascending (lower = better match). Memories with no ``_bm25_score``
        (semantic-only hits, no FTS match) sort last. Off by default (see
        ``RRF_W_IDF``) -- keyword_rank already reflects FTS5 relevance order,
        so this only matters once a caller opts in with a positive weight.

    The RRF score is ``sum(weight / (k + rank))`` across all five signals.
    Memories absent from a list receive a penalty rank of ``len(list) + 1``.

    Args:
        keyword_results: Memories ranked by keyword/FTS relevance (best first).
        semantic_results: Memories ranked by semantic similarity (best first).
        k: RRF smoothing constant. Defaults to module-level ``RRF_K``.
        w_keyword, w_semantic, w_recency, w_vitality, w_idf: Per-signal weights.
            Each defaults to its module-level ``RRF_W_*`` constant. Set a weight
            to 0 to drop that signal (e.g. recency/vitality for a retrieval
            profile). ``w_idf`` defaults to 0 (off).

    Returns:
        De-duplicated list of memory dicts sorted by RRF score descending,
        each augmented with ``_rrf_score``, ``_keyword_rank``,
        ``_semantic_rank``, ``_recency_rank``, ``_vitality_rank``, and
        ``_idf_rank`` keys.
    """
    if k is None:
        k = RRF_K
    if w_keyword is None:
        w_keyword = RRF_W_KEYWORD
    if w_semantic is None:
        w_semantic = RRF_W_SEMANTIC
    if w_recency is None:
        w_recency = RRF_W_RECENCY
    if w_vitality is None:
        w_vitality = RRF_W_VITALITY
    if w_idf is None:
        w_idf = RRF_W_IDF

    # Collect unique memories by id, preserving dict contents. A memory hit by
    # both tiers merges the second occurrence's keys (e.g. semantic_distance)
    # without overwriting non-null keys from the first.
    seen: dict[str, dict] = {}
    for mem in [*keyword_results, *semantic_results]:
        existing = seen.get(mem["id"])
        if existing is None:
            seen[mem["id"]] = dict(mem)
        else:
            for key, value in mem.items():
                if existing.get(key) is None:
                    existing[key] = value

    if not seen:
        return []

    # Build rank maps (1-indexed)
    keyword_rank: dict[str, int] = {
        mem["id"]: i + 1 for i, mem in enumerate(keyword_results)
    }
    semantic_rank: dict[str, int] = {
        mem["id"]: i + 1 for i, mem in enumerate(semantic_results)
    }

    # Penalty ranks for absent memories
    kw_penalty = len(keyword_results) + 1
    sem_penalty = len(semantic_results) + 1

    # Recency ranking: sort all unique memories by created_at DESC
    all_mems = sorted(
        seen.values(),
        key=lambda m: m.get("created_at", ""),
        reverse=True,
    )
    recency_rank: dict[str, int] = {
        mem["id"]: i + 1 for i, mem in enumerate(all_mems)
    }

    # Vitality ranking: sort all unique memories by vitality DESC (default 1.0)
    vitality_sorted = sorted(
        seen.values(),
        key=lambda m: m.get("vitality", 1.0),
        reverse=True,
    )
    vitality_rank: dict[str, int] = {
        mem["id"]: i + 1 for i, mem in enumerate(vitality_sorted)
    }

    # IDF ranking: sort all unique memories by bm25() score ASCENDING (lower =
    # better match). Memories with no FTS hit (semantic-only) have no
    # _bm25_score and sort last, via the +inf default.
    idf_sorted = sorted(
        seen.values(),
        key=lambda m: m["_bm25_score"] if m.get("_bm25_score") is not None else float("inf"),
    )
    idf_rank: dict[str, int] = {
        mem["id"]: i + 1 for i, mem in enumerate(idf_sorted)
    }

    # Compute RRF scores (5 signals)
    results: list[dict] = []
    for mid, mem in seen.items():
        kr = keyword_rank.get(mid, kw_penalty)
        sr = semantic_rank.get(mid, sem_penalty)
        rr = recency_rank[mid]
        vr = vitality_rank[mid]
        ir = idf_rank[mid]

        score = (
            w_keyword / (k + kr)
            + w_semantic / (k + sr)
            + w_recency / (k + rr)
            + w_vitality / (k + vr)
            + w_idf / (k + ir)
        )

        mem["_rrf_score"] = score
        mem["_keyword_rank"] = kr
        mem["_semantic_rank"] = sr
        mem["_recency_rank"] = rr
        mem["_vitality_rank"] = vr
        mem["_idf_rank"] = ir
        results.append(mem)

    # Sort by RRF score descending (stable sort preserves insertion order for ties)
    results.sort(key=lambda m: m["_rrf_score"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Auto-routing retrieval strategy (Phase 6)
# ---------------------------------------------------------------------------

# Named RRF weight profiles, expressed as MULTIPLIERS applied on top of the
# LIVE RRF_W_* module constants (read at call time by
# :func:`resolve_strategy_weights`) rather than fixed absolute numbers. This
# matters: those constants are exactly what env vars and
# ``benchmarks/runner.py --rrf-profile`` override/monkeypatch, and a
# multiplicative nudge composes with that instead of silently overriding it
# -- e.g. under ``--rrf-profile semantic`` (w_keyword forced to 0), a
# "keyword_favored" route still leaves w_keyword at 0 (0 * 1.5 == 0) rather
# than resurrecting a signal the profile deliberately zeroed. A key absent
# from a preset has an implicit multiplier of 1.0 (no change).
_BALANCED_MULTIPLIERS: dict[str, float] = {}

# Quoted phrases, prefix* wildcards, and structured/no-semantic queries are
# exact-match-shaped -- lean on keyword relevance. Semantic isn't dropped to
# 0: even a keyword-shaped query can have a semantically-relevant hit worth
# surfacing, just weighted lower.
_KEYWORD_FAVORED_MULTIPLIERS: dict[str, float] = {"w_keyword": 1.5, "w_semantic": 0.5}

# Long, natural-language, question-shaped queries rarely share exact terms
# with the memory they're looking for -- lean on semantic similarity.
_SEMANTIC_FAVORED_MULTIPLIERS: dict[str, float] = {"w_keyword": 0.5, "w_semantic": 1.5}

STRATEGY_PRESETS: dict[str, dict[str, float]] = {
    "balanced": _BALANCED_MULTIPLIERS,
    "keyword_favored": _KEYWORD_FAVORED_MULTIPLIERS,
    "semantic_favored": _SEMANTIC_FAVORED_MULTIPLIERS,
}
"""Maps a ``MemorySearchInput.strategy`` value (the non-``"auto"`` explicit
pins -- the escape hatch, also handy for A/B testing in ``benchmarks/``) to
its RRF weight multipliers. These are multipliers, not final weights --
resolve with :func:`resolve_strategy_weights`, don't splat this dict
directly into :func:`rank_rrf`."""


def resolve_strategy_weights(strategy: str) -> dict[str, float]:
    """Resolve a named strategy preset into concrete RRF weights.

    Applies the preset's multiplier (from :data:`STRATEGY_PRESETS`) on top
    of the current ``RRF_W_*`` module constants, read at call time so env
    overrides and test/benchmark monkeypatches are always respected --
    ``"balanced"`` (an empty multiplier dict) reproduces them exactly.

    Args:
        strategy: One of :data:`STRATEGY_PRESETS`'s keys.

    Returns:
        Concrete weights (``w_keyword``/``w_semantic``/``w_recency``/
        ``w_vitality``/``w_idf``) suitable for splatting into
        :func:`rank_rrf`.
    """
    multipliers = STRATEGY_PRESETS[strategy]
    base = {
        "w_keyword": RRF_W_KEYWORD,
        "w_semantic": RRF_W_SEMANTIC,
        "w_recency": RRF_W_RECENCY,
        "w_vitality": RRF_W_VITALITY,
        "w_idf": RRF_W_IDF,
    }
    return {key: value * multipliers.get(key, 1.0) for key, value in base.items()}


# A query this short reads as a keyword/id lookup rather than a natural-
# language question -- there usually isn't enough text for semantic
# similarity to add value over exact term matching.
_KEYWORD_SHAPE_MAX_WORDS = 2

# A query this long, or one that reads as a question, is natural-language
# shaped -- it rarely shares exact terms with the memory it's looking for.
_SEMANTIC_SHAPE_MIN_WORDS = 6


def _looks_keyword_shaped(query: str) -> bool:
    """True for quoted phrases, prefix* wildcards, or very short queries.

    These read as exact-match/keyword-style lookups (FTS5 phrase/prefix
    syntax, or a bare word or two) rather than natural-language questions.
    """
    return '"' in query or "*" in query or len(query.split()) <= _KEYWORD_SHAPE_MAX_WORDS


def _looks_semantic_shaped(query: str) -> bool:
    """True for long or question-shaped natural-language queries."""
    return len(query.split()) >= _SEMANTIC_SHAPE_MIN_WORDS or query.rstrip().endswith("?")


def choose_rrf_weights(
    query: str,
    *,
    structured: bool = False,
    has_semantic: bool = True,
) -> dict[str, float]:
    """Heuristically route a query to an RRF weight profile (Phase 6).

    A deterministic heuristic on the query's observable shape -- not an
    in-server LLM planner call, which would add latency/cost/opacity to a
    deliberately lightweight retrieval layer (the same reasoning that keeps
    server-side answer synthesis out of scope). Extends the same "route by
    query shape" idea already used by ``_detect_structured_query``.

    Args:
        query: The search query (structured subject:/predicate:/entity:
            prefixes already stripped, if any were present).
        structured: True when the query used structured (subject:/
            predicate:/entity:) syntax -- these are keyword-shaped by
            construction, even after stripping for the fallback search.
        has_semantic: False when no semantic tier is available (no
            embedder) -- a semantic weight is meaningless then, so always
            favor keyword regardless of query shape.

    Returns:
        Concrete weights from :func:`resolve_strategy_weights`, suitable
        for splatting into :func:`rank_rrf`.
    """
    if structured or not has_semantic or _looks_keyword_shaped(query):
        return resolve_strategy_weights("keyword_favored")
    if _looks_semantic_shaped(query):
        return resolve_strategy_weights("semantic_favored")
    return resolve_strategy_weights("balanced")


# ---------------------------------------------------------------------------
# Token budget trimming
# ---------------------------------------------------------------------------


def apply_token_budget(ranked_memories: list[dict], budget: int) -> SearchEnvelope:
    """Trim ranked memories to fit within a token budget.

    Token count is estimated as ``len(content) // 4``.  When *budget* is 0,
    all memories are returned (unlimited).  At least one memory is always
    returned if the input is non-empty, even if it exceeds the budget.

    Args:
        ranked_memories: Memories sorted by relevance (best first).
        budget: Maximum token budget. 0 means unlimited.

    Returns:
        A :class:`SearchEnvelope` with the trimmed memories and metadata.
    """
    total = len(ranked_memories)

    if total == 0:
        return SearchEnvelope(
            memories=[],
            total_candidates=0,
            returned=0,
            trimmed=0,
            tokens_used=0,
            budget=budget,
        )

    if budget == 0:
        # Unlimited
        tokens = sum(len(m.get("content", "")) // 4 for m in ranked_memories)
        return SearchEnvelope(
            memories=list(ranked_memories),
            total_candidates=total,
            returned=total,
            trimmed=0,
            tokens_used=tokens,
            budget=budget,
        )

    kept: list[dict] = []
    tokens_used = 0

    for mem in ranked_memories:
        est = len(mem.get("content", "")) // 4
        if kept and tokens_used + est > budget:
            break
        kept.append(mem)
        tokens_used += est

    return SearchEnvelope(
        memories=kept,
        total_candidates=total,
        returned=len(kept),
        trimmed=total - len(kept),
        tokens_used=tokens_used,
        budget=budget,
    )


# ---------------------------------------------------------------------------
# Debug signals & tier breakdown
# ---------------------------------------------------------------------------


def build_debug_signals(
    memory: dict,
    *,
    strategy: str | None = None,
    weights: dict[str, float | None] | None = None,
) -> dict:
    """Extract ranking debug signals from an RRF-ranked memory dict.

    Returns a dict with keys: semantic_rank, keyword_rank, recency_rank,
    vitality_rank, rrf_score, rerank_score, search_method, and days_old.
    If ``created_at`` is missing or unparseable, ``days_old`` is set to
    ``None``. This is the public surface for the internal underscore-prefixed
    ranking fields, which are stripped from JSON responses (HY-05).

    Args:
        memory: A memory dict augmented by :func:`rank_rrf` with rank metadata.
        strategy: The resolved ``MemorySearchInput.strategy`` value (Phase
            6), when the caller wants it surfaced. Omitted (None) leaves it
            out of the result entirely, so pre-Phase-6 callers see no change.
        weights: The RRF weight profile actually used for this search (a
            :data:`STRATEGY_PRESETS`-shaped dict; ``None`` entries mean "the
            module default was used"). Omitted the same way as *strategy*.

    Returns:
        Dict of debug signal values for transparency/explainability.
    """
    days_old: int | None = None
    created_at = memory.get("created_at")
    if created_at is not None:
        try:
            created_dt = datetime.fromisoformat(str(created_at))
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=UTC)
            days_old = (datetime.now(UTC) - created_dt).days
        except (ValueError, TypeError):
            days_old = None

    signals = {
        "semantic_rank": memory.get("_semantic_rank"),
        "keyword_rank": memory.get("_keyword_rank"),
        "recency_rank": memory.get("_recency_rank"),
        "vitality_rank": memory.get("_vitality_rank"),
        "idf_rank": memory.get("_idf_rank"),
        "rrf_score": memory.get("_rrf_score"),
        "rerank_score": memory.get("_rerank_score"),
        "search_method": memory.get("_search_method"),
        "days_old": days_old,
    }
    if strategy is not None:
        signals["strategy"] = strategy
    if weights is not None:
        signals["weights_used"] = weights
    return signals


def compute_tier_breakdown(memories: list[dict]) -> dict[str, int]:
    """Count memories by their ``_search_method`` value.

    Args:
        memories: List of memory dicts, each with a ``_search_method`` key.

    Returns:
        Dict with keys ``keyword``, ``semantic``, ``hybrid`` and integer counts.
        Missing tiers default to 0.
    """
    counts: dict[str, int] = {"keyword": 0, "semantic": 0, "hybrid": 0}
    for mem in memories:
        method = mem.get("_search_method", "")
        if method in counts:
            counts[method] += 1
    return counts


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "RRF_K",
    "SearchEnvelope",
    "rank_rrf",
    "apply_token_budget",
    "build_debug_signals",
    "compute_tier_breakdown",
    "STRATEGY_PRESETS",
    "resolve_strategy_weights",
    "choose_rrf_weights",
]
