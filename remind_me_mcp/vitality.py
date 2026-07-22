"""
remind_me_mcp.vitality -- ACT-R vitality computation and access recording.

This module implements a memory vitality model inspired by the ACT-R cognitive
architecture. Each memory has a vitality score that decays exponentially over
time but is boosted by repeated access. Frequently accessed memories develop
"bridge protection" -- their decay rate is halved once they cross an access
count threshold, representing consolidation into long-term memory.

The core formula is:

    vitality = base_weight * (access_count + 1)^0.5 * exp(-decay_rate * days_since_last_access)

Key concepts:
  - **Vitality**: A float score reflecting how "alive" a memory is. Higher = more relevant.
  - **Decay rate**: How fast a memory fades. Set per memory_type (decisions persist, action items fade fast).
  - **Bridge protection**: Memories accessed >= BRIDGE_THRESHOLD times get their decay rate halved.
  - **Dormancy**: Memories below VITALITY_FLOOR are flagged dormant and excluded from default search.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from remind_me_mcp.db import _get_db, _make_id, _now_iso, _row_to_dict

if TYPE_CHECKING:
    import sqlite3

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VITALITY_FLOOR: float = 0.05
"""Vitality threshold below which a memory is considered dormant."""

BRIDGE_THRESHOLD: int = 10
"""Access count at or above which bridge protection applies (decay rate halved)."""

BRIDGE_MULTIPLIER: float = 0.5
"""Multiplier applied to decay_rate when access_count >= BRIDGE_THRESHOLD."""

DECAY_RATES: dict[str, float] = {
    "decision": 0.02,
    "preference": 0.03,
    "fact": 0.05,
    "insight": 0.07,
    "learning": 0.08,
    "blocker": 0.15,
    "action_item": 0.20,
    "unclassified": 0.10,
}
"""Mapping of memory_type to default decay rate. Lower values persist longer."""

BASE_WEIGHT_TYPE_PRIORS: dict[str, float] = {
    "decision": 1.3,
    "blocker": 1.2,
    "fact": 1.15,
    "insight": 1.15,
    "preference": 1.1,
    "learning": 1.05,
    "action_item": 1.0,
    "unclassified": 1.0,
}
"""Seeds base_weight from memory_type at write time (issue #56) -- every new
memory otherwise starts at a flat 1.0 regardless of kind, so a throwaway
aside competes evenly in ranking with a real decision until feedback/access
signal accrues enough to differentiate them. Multipliers on the default 1.0
base_weight, in the same universe as record_feedback's adjustments, so a
seeded prior composes naturally with later feedback rather than needing
separate bookkeeping."""

BASE_WEIGHT_SOURCE_PRIORS: dict[str, float] = {
    "manual": 1.0,
    "chat_import": 0.85,
    "document_import": 0.9,
    "webhook": 0.9,
}
"""Fallback seed from source (issue #56) for write paths where memory_type
isn't known yet (e.g. remind_me_add, before any remind_me_reclassify pass) --
a deliberately-entered manual memory keeps the historical flat 1.0 default;
raw bulk imports start slightly lower since they're unreviewed and often
noisy, until reclassify/normalize surfaces the real signal."""


def seed_base_weight(*, memory_type: str | None = None, source: str | None = None) -> float:
    """Compute the write-time base_weight prior for a new memory (issue #56).

    Prefers a known, specific ``memory_type`` (e.g. ``remind_me_decompose``
    already classifies facts at write time); otherwise falls back to a
    ``source``-based prior; otherwise the original flat 1.0 default (an
    unrecognized/absent source, or ``memory_type="unclassified"``, is
    indistinguishable from "no signal available" -- exactly the pre-#56
    behavior).

    Args:
        memory_type: The memory's classified type, if already known at
            write time. ``None`` or ``"unclassified"`` means "not known yet."
        source: The memory's origin (``"manual"``, ``"chat_import"``, ...).

    Returns:
        A base_weight value, typically in ``[BASE_WEIGHT_MIN, BASE_WEIGHT_MAX]``
        though this function doesn't clamp -- the priors table itself is
        kept within that range.
    """
    if memory_type and memory_type != "unclassified" and memory_type in BASE_WEIGHT_TYPE_PRIORS:
        return BASE_WEIGHT_TYPE_PRIORS[memory_type]
    if source and source in BASE_WEIGHT_SOURCE_PRIORS:
        return BASE_WEIGHT_SOURCE_PRIORS[source]
    return 1.0

FEEDBACK_MAGNITUDE: float = 0.15
"""Default fractional adjustment applied to base_weight per feedback signal."""

BASE_WEIGHT_MAX: float = 3.0
"""Ceiling applied to base_weight after positive ("helpful") feedback."""

BASE_WEIGHT_MIN: float = 0.1
"""Floor applied to base_weight after negative ("unhelpful") feedback."""

FEEDBACK_SIMILARITY_THRESHOLD: float = 0.3
"""Minimum Jaccard token-overlap between the current query and a stored
feedback event's query before that event counts toward
:func:`contextual_feedback_adjustment` -- below this, the past query is
considered a different-enough context that the feedback shouldn't apply
(gap #6: the whole point is *not* punishing/rewarding an unrelated query)."""

FEEDBACK_ADJUSTMENT_CAP: float = 0.4
"""Maximum absolute fractional adjustment :func:`apply_feedback_adjustment`
applies to a memory's ``_rrf_score`` (i.e. at most a +/-40% swing), however
much matching feedback has accumulated. A multiplicative cap rather than an
absolute one so it composes safely regardless of RRF fusion mode's score
scale (rank mode's tiny 1/(k+rank) sums vs. score mode's larger [0, N] sums,
see retrieval.py)."""


# ---------------------------------------------------------------------------
# Pure computation functions
# ---------------------------------------------------------------------------


def compute_vitality(
    base_weight: float,
    access_count: int,
    decay_rate: float,
    days_since_last_access: float,
) -> float:
    """Compute memory vitality using the ACT-R inspired formula.

    Formula: base_weight * (access_count + 1)^0.5 * exp(-decay_rate * days_since_last_access)

    The square-root scaling on access_count provides diminishing returns --
    the first few accesses boost vitality significantly, but subsequent
    accesses have progressively less impact.

    Args:
        base_weight: Base importance weight for the memory (default 1.0).
        access_count: Number of times the memory has been accessed.
        decay_rate: Exponential decay rate (higher = faster decay).
        days_since_last_access: Days elapsed since the memory was last accessed.

    Returns:
        The computed vitality score as a non-negative float.
    """
    return base_weight * (access_count + 1) ** 0.5 * math.exp(-decay_rate * days_since_last_access)


def get_effective_decay_rate(decay_rate: float, access_count: int) -> float:
    """Return the effective decay rate, applying bridge protection if applicable.

    Memories that have been accessed at least BRIDGE_THRESHOLD times receive
    bridge protection: their decay rate is halved, representing consolidation
    into long-term memory.

    Args:
        decay_rate: The base decay rate for the memory.
        access_count: Number of times the memory has been accessed.

    Returns:
        The effective decay rate (halved if bridge-protected, unchanged otherwise).
    """
    if access_count >= BRIDGE_THRESHOLD:
        return decay_rate * BRIDGE_MULTIPLIER
    return decay_rate


def is_dormant(vitality: float) -> bool:
    """Check whether a memory is dormant based on its vitality score.

    A dormant memory has decayed below VITALITY_FLOOR and should be excluded
    from default search results (though still retrievable with include_dormant).

    Args:
        vitality: The current vitality score of the memory.

    Returns:
        True if the memory is dormant (vitality < VITALITY_FLOOR), False otherwise.
    """
    return vitality < VITALITY_FLOOR


def effective_vitality(memory: dict, now: datetime | None = None) -> float:
    """Compute a memory's read-time vitality with real elapsed-days decay.

    The stored ``vitality`` column is a snapshot taken when the memory was last
    accessed (computed with days_since=0), so it never decays on its own. This
    recomputes the ACT-R formula using the days actually elapsed since
    ``accessed_at`` (falling back to ``created_at``), with bridge protection
    applied. Use this wherever vitality drives ranking, dormancy checks,
    ``min_vitality`` filtering, or reporting.

    Args:
        memory: A memory dict (e.g. from ``_row_to_dict``). Missing vitality
            columns fall back to schema defaults.
        now: Clock override for tests. Defaults to the current UTC time.

    Returns:
        The effective vitality score as a non-negative float.
    """
    if now is None:
        now = datetime.now(UTC)

    access_count = memory.get("access_count") or 0
    base_weight = memory.get("base_weight") or 1.0
    decay_rate = memory.get("decay_rate")
    if decay_rate is None:
        decay_rate = DECAY_RATES.get(
            memory.get("memory_type") or "unclassified", DECAY_RATES["unclassified"]
        )
    effective_rate = get_effective_decay_rate(decay_rate, access_count)

    days = 0.0
    last = memory.get("accessed_at") or memory.get("created_at")
    if last:
        try:
            last_dt = datetime.fromisoformat(str(last))
        except (TypeError, ValueError):
            last_dt = None
        if last_dt is not None:
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=UTC)
            days = max(0.0, (now - last_dt).total_seconds() / 86400.0)

    return compute_vitality(base_weight, access_count, effective_rate, days)


def build_vitality_report(db: sqlite3.Connection) -> dict:
    """Compute the vault vitality report: counts, buckets, and health score.

    Shared by ``remind_me_vitality_report`` (tools/lifecycle.py) and the
    ``GET /api/vitality`` REST route (issue #14) so the bucket math lives in
    exactly one place instead of being duplicated between the MCP tool and
    the dashboard's REST surface.

    Args:
        db: An open SQLite connection.

    Returns:
        A dict with ``total_memories``, ``active_count``, ``dormant_count``,
        ``average_vitality``, ``vault_health_score`` (e.g. ``"82%"``),
        ``decay_distribution`` (``{memory_type: count}``), and
        ``vitality_buckets`` (``{bucket_label: count}``).
    """
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

    return {
        "total_memories": total,
        "active_count": active_count,
        "dormant_count": dormant_count,
        "average_vitality": avg_vitality,
        "vault_health_score": vault_health_score,
        "decay_distribution": decay_distribution,
        "vitality_buckets": vitality_buckets,
    }


# ---------------------------------------------------------------------------
# Database integration
# ---------------------------------------------------------------------------


def record_accesses(memory_ids: list[str]) -> int:
    """Batch-record accesses for several memories in one transaction (PF-02).

    Equivalent to calling :func:`record_access` once per id, but performs a
    single SELECT plus one ``executemany`` UPDATE and one commit instead of a
    round-trip (SELECT + UPDATE + commit) per memory. The search hot path
    records every returned hit, so for a 20-result search this turns ~60
    statements/20 commits into 2 statements/1 commit.

    Args:
        memory_ids: Text primary keys of the memories to record access for.

    Returns:
        The number of memories actually updated (unknown ids are skipped).
    """
    if not memory_ids:
        return 0

    db = _get_db()
    placeholders = ",".join("?" for _ in memory_ids)
    rows = db.execute(
        f"SELECT id, access_count, decay_rate, base_weight FROM memories "
        f"WHERE id IN ({placeholders})",
        memory_ids,
    ).fetchall()
    if not rows:
        return 0

    now = _now_iso()
    updates: list[tuple[str, int, float, str, str]] = []
    for row in rows:
        new_access_count = row["access_count"] + 1
        effective_rate = get_effective_decay_rate(row["decay_rate"], new_access_count)
        new_vitality = compute_vitality(
            base_weight=row["base_weight"],
            access_count=new_access_count,
            decay_rate=effective_rate,
            days_since_last_access=0.0,
        )
        new_status = "dormant" if is_dormant(new_vitality) else "active"
        updates.append((now, new_access_count, new_vitality, new_status, row["id"]))

    db.executemany(
        """UPDATE memories
           SET accessed_at = ?, access_count = ?, vitality = ?, status = ?
           WHERE id = ?""",
        updates,
    )
    db.commit()
    return len(updates)


# ---------------------------------------------------------------------------
# Co-retrieval reinforcement (issue #9)
# ---------------------------------------------------------------------------

CO_RETRIEVAL_MAX_WEIGHT = 50
"""Hard cap on a memory pair's association weight. The issue flags "how to
weight, decay, and avoid runaway feedback loops" as the real design risk
here; a simple bounded counter with no time-decay sidesteps both weighting
and decay complexity for this pass -- deliberately scoped down from the
issue's "Large" full design (its own text calls that "a project of its own,
not a quick add")."""

_CO_RETRIEVAL_PAIR_CAP = 10
"""Only the first N ids of a result set participate in pairing -- bounds the
O(n^2) pair count per search (10 choose 2 = 45 pairs) regardless of how
large the caller's result set is."""


def record_co_retrieval(memory_ids: list[str]) -> int:
    """Reinforce co-retrieval associations between memories returned together.

    True ACT-R-style memory reinforces associations *between* items
    retrieved together, not just each item independently -- ``memory_entities``
    links a memory to entities it mentions, not to other memories via search
    co-occurrence, so nothing previously captured "these two memories tend
    to be useful together."

    This deliberately never feeds back into RRF ranking/scoring: only
    ``remind_me_search``'s ``expand_co_retrieval`` surfaces the recorded
    weights, in a separate ``related_via_co_retrieval`` section outside the
    ranked results -- the same posture as ``expand_entities``/
    ``include_neighbors``. That one-way flow (search results -> recorded
    associations -> surfaced as *suggestions*, never as a ranking input) is
    what keeps the "runaway feedback loop" the issue explicitly warns about
    off the table entirely, without needing any decay math to counteract it.

    Args:
        memory_ids: Ids of memories returned together in one search
            (already ranked/limited by the caller). Only the first
            ``_CO_RETRIEVAL_PAIR_CAP`` participate in pairing.

    Returns:
        The number of association rows created or reinforced (0 if fewer
        than 2 ids were given).
    """
    ids = memory_ids[:_CO_RETRIEVAL_PAIR_CAP]
    if len(ids) < 2:
        return 0

    db = _get_db()
    now = _now_iso()
    touched = 0
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = sorted((ids[i], ids[j]))
            db.execute(
                """INSERT INTO memory_associations (memory_id_a, memory_id_b, weight, updated_at)
                   VALUES (?, ?, 1, ?)
                   ON CONFLICT(memory_id_a, memory_id_b) DO UPDATE SET
                       weight = MIN(weight + 1, ?),
                       updated_at = excluded.updated_at""",
                (a, b, now, CO_RETRIEVAL_MAX_WEIGHT),
            )
            touched += 1
    db.commit()
    return touched


def record_access(memory_id: str) -> float | None:
    """Record an access to a memory, updating its vitality in the database.

    Increments access_count, sets accessed_at to now, applies bridge protection
    if applicable, recomputes vitality via compute_vitality (with days_since=0
    since we just accessed it), and determines dormancy status.

    Args:
        memory_id: The text primary key of the memory to record access for.

    Returns:
        The new vitality value, or None if the memory was not found.
    """
    db = _get_db()

    row = db.execute(
        "SELECT accessed_at, access_count, decay_rate, base_weight FROM memories WHERE id = ?",
        (memory_id,),
    ).fetchone()

    if row is None:
        return None

    new_access_count = row["access_count"] + 1
    now = _now_iso()

    # Apply bridge protection
    effective_rate = get_effective_decay_rate(row["decay_rate"], new_access_count)

    # Recompute vitality (days_since=0 since we just accessed it)
    new_vitality = compute_vitality(
        base_weight=row["base_weight"],
        access_count=new_access_count,
        decay_rate=effective_rate,
        days_since_last_access=0.0,
    )

    # Determine status
    new_status = "dormant" if is_dormant(new_vitality) else "active"

    db.execute(
        """UPDATE memories
           SET accessed_at = ?, access_count = ?, vitality = ?, status = ?
           WHERE id = ?""",
        (now, new_access_count, new_vitality, new_status, memory_id),
    )
    db.commit()

    return new_vitality


def record_feedback(
    memory_id: str,
    signal: Literal["helpful", "unhelpful"],
    magnitude: float = FEEDBACK_MAGNITUDE,
    *,
    query: str | None = None,
) -> float | None:
    """Record helpful/unhelpful feedback on a memory.

    Unlike :func:`record_access` (an unsigned, always-positive reinforcement
    signal derived from ``access_count``), feedback is a *signed* signal.
    Two modes, selected by whether *query* is given (gap #6):

    - **No query** (back-compat, unchanged): adjusts ``base_weight`` --
      the multiplicative importance term in :func:`compute_vitality` --
      globally, exactly as before this parameter existed. ``access_count``
      is deliberately untouched: it feeds ``sqrt(access_count + 1)`` and has
      no sensible "negative access" interpretation.
    - **With a query**: query-contextual instead of global. A memory can be
      a poor match for "what's my favorite editor" but a perfect match for
      "what IDE did I mention last year" -- global demotion would punish the
      second case for the first's feedback. Logs the event (see
      :func:`record_contextual_feedback`) instead of touching
      ``base_weight``; the effect is applied only for future queries
      similar enough to this one, at ranking time
      (:func:`apply_feedback_adjustment`). ``base_weight``/``vitality``
      are unchanged, so the memory's current vitality is returned as-is.

    Args:
        memory_id: The text primary key of the memory to record feedback for.
        signal: "helpful" scales base_weight up (capped at BASE_WEIGHT_MAX);
            "unhelpful" scales it down (floored at BASE_WEIGHT_MIN). In
            query-contextual mode, the sign/magnitude are stored for a
            future similarity-weighted read instead.
        magnitude: Fractional adjustment (0-1) -- applied to base_weight in
            global mode, stored as-is for the similarity weighting in
            query-contextual mode.
        query: The search query this feedback relates to. When given, this
            is query-contextual feedback (gap #6) rather than a global
            base_weight mutation.

    Returns:
        The memory's current vitality value, or None if the memory was not found.
    """
    db = _get_db()

    row = db.execute(
        "SELECT access_count, decay_rate, base_weight, vitality FROM memories WHERE id = ?",
        (memory_id,),
    ).fetchone()

    if row is None:
        return None

    if query:
        record_contextual_feedback(db, memory_id, query, signal, magnitude)
        return row["vitality"]

    if signal == "helpful":
        new_base_weight = min(BASE_WEIGHT_MAX, row["base_weight"] * (1 + magnitude))
    else:
        new_base_weight = max(BASE_WEIGHT_MIN, row["base_weight"] * (1 - magnitude))

    # Snapshot recompute, same convention as record_access: days_since=0.
    # accessed_at/access_count are untouched -- feedback is not an access.
    effective_rate = get_effective_decay_rate(row["decay_rate"], row["access_count"])
    new_vitality = compute_vitality(
        base_weight=new_base_weight,
        access_count=row["access_count"],
        decay_rate=effective_rate,
        days_since_last_access=0.0,
    )
    new_status = "dormant" if is_dormant(new_vitality) else "active"

    db.execute(
        """UPDATE memories
           SET base_weight = ?, vitality = ?, status = ?
           WHERE id = ?""",
        (new_base_weight, new_vitality, new_status, memory_id),
    )
    db.commit()

    return new_vitality


# ---------------------------------------------------------------------------
# Query-contextual feedback (gap #6)
# ---------------------------------------------------------------------------

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


def _tokenize_query(query: str) -> set[str]:
    """Lowercase, alphanumeric-only tokenization for coarse query clustering.

    Deliberately simple (no stemming/stopwords/embeddings): this only needs
    to distinguish "similar enough to be the same context" from "a different
    question," and works identically whether or not semantic search
    (an embedder) is configured.
    """
    return {t for t in _TOKEN_PATTERN.findall(query.lower()) if len(t) > 1}


def _jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity (intersection over union) of two token sets."""
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def record_contextual_feedback(
    db: sqlite3.Connection,
    memory_id: str,
    query: str,
    signal: Literal["helpful", "unhelpful"],
    magnitude: float = FEEDBACK_MAGNITUDE,
) -> str:
    """Log one query-contextual feedback event (gap #6).

    Does not touch ``base_weight``/``vitality`` -- the event is read back by
    :func:`apply_feedback_adjustment` at ranking time, weighted by how
    similar a *future* query is to this one, rather than baked into a
    single global mutation. Caller is responsible for confirming the memory
    exists (:func:`record_feedback` does this via its own lookup).

    Args:
        db: An open SQLite connection.
        memory_id: The memory this feedback is about.
        query: The search query this feedback relates to.
        signal: "helpful" or "unhelpful".
        magnitude: Stored as-is for the similarity-weighted read.

    Returns:
        The new feedback row's id.
    """
    feedback_id = _make_id(f"{memory_id}:{query}")
    tokens = " ".join(sorted(_tokenize_query(query)))
    now = _now_iso()
    db.execute(
        """INSERT INTO memory_feedback
               (id, memory_id, query, query_tokens, signal, magnitude, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (feedback_id, memory_id, query, tokens, signal, magnitude, now),
    )
    db.commit()
    return feedback_id


def contextual_feedback_adjustment(db: sqlite3.Connection, memory_id: str, query: str) -> float:
    """Sum similarity-weighted feedback for *memory_id* against *query*.

    Each stored feedback event with a Jaccard token overlap at or above
    :data:`FEEDBACK_SIMILARITY_THRESHOLD` against *query* contributes
    ``+/-magnitude * similarity`` (helpful/unhelpful); events below the
    threshold (a different-enough past query) contribute nothing -- this is
    the mechanism that keeps feedback query-contextual instead of global.

    Args:
        db: An open SQLite connection.
        memory_id: The memory to look up feedback for.
        query: The current search query.

    Returns:
        The total adjustment, clamped to
        ``+/-FEEDBACK_ADJUSTMENT_CAP``. ``0.0`` if there's no feedback for
        this memory, or none of it is similar enough to *query* to count.
    """
    rows = db.execute(
        "SELECT query_tokens, signal, magnitude FROM memory_feedback WHERE memory_id = ?",
        (memory_id,),
    ).fetchall()
    if not rows:
        return 0.0

    query_tokens = _tokenize_query(query)
    total = 0.0
    for row in rows:
        past_tokens = set(row["query_tokens"].split())
        similarity = _jaccard(query_tokens, past_tokens)
        if similarity < FEEDBACK_SIMILARITY_THRESHOLD:
            continue
        sign = 1.0 if row["signal"] == "helpful" else -1.0
        total += sign * row["magnitude"] * similarity

    return max(-FEEDBACK_ADJUSTMENT_CAP, min(FEEDBACK_ADJUSTMENT_CAP, total))


def apply_feedback_adjustment(query: str, memories: list[dict]) -> list[dict]:
    """Nudge each memory's ``_rrf_score`` by its query-contextual feedback, then re-sort.

    Mirrors :func:`reranker.maybe_rerank`'s signature and pipeline position
    (query first, RRF-ranked memories, returns a reordered list) -- meant to
    run *before* reranking, so the cross-encoder (when enabled) still has
    final say over the head; this only perturbs the RRF order feeding into
    it. A memory with no matching feedback (the common case) is untouched.

    The adjustment is multiplicative (``score * (1 + adjustment)``) rather
    than additive, so it composes safely regardless of RRF fusion mode's
    score scale (see :data:`FEEDBACK_ADJUSTMENT_CAP`).

    Args:
        query: The current search query.
        memories: RRF-ranked memory dicts (best first), each with an ``id``
            and (usually) a ``_rrf_score`` key.

    Returns:
        The same memory dicts, reordered by adjusted score. Memories
        without a ``_rrf_score`` are treated as ``0.0`` for sorting purposes
        only (defensive; every real caller sets it via ``rank_rrf``).
    """
    if not memories or not query:
        return memories

    db = _get_db()
    for mem in memories:
        adjustment = contextual_feedback_adjustment(db, mem["id"], query)
        if adjustment:
            mem["_rrf_score"] = mem.get("_rrf_score", 0.0) * (1 + adjustment)
            mem["_feedback_adjustment"] = adjustment

    memories.sort(key=lambda m: m.get("_rrf_score", 0.0), reverse=True)
    return memories


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "BASE_WEIGHT_MAX",
    "BASE_WEIGHT_MIN",
    "BASE_WEIGHT_SOURCE_PRIORS",
    "BASE_WEIGHT_TYPE_PRIORS",
    "BRIDGE_MULTIPLIER",
    "BRIDGE_THRESHOLD",
    "DECAY_RATES",
    "FEEDBACK_ADJUSTMENT_CAP",
    "FEEDBACK_MAGNITUDE",
    "FEEDBACK_SIMILARITY_THRESHOLD",
    "VITALITY_FLOOR",
    "apply_feedback_adjustment",
    "compute_vitality",
    "contextual_feedback_adjustment",
    "effective_vitality",
    "get_effective_decay_rate",
    "is_dormant",
    "record_access",
    "record_accesses",
    "record_contextual_feedback",
    "record_feedback",
    "seed_base_weight",
]
