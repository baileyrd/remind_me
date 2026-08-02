"""
remind_me_mcp.maintenance — pending-work counts, nudges, and capture health.

Two problems this module exists to solve.

**The nudges were on tools nobody calls.** ``pending_wiki_compile`` was
computed only inside ``remind_me_server_status`` and ``remind_me_watch_status``
— tools a conversational session has no reason to invoke — so a growing
backlog of un-decomposed captures or un-compiled memories was invisible in
practice. :func:`maybe_maintenance_notice` surfaces it on the responses Claude
actually reads, reusing the one-shot piggyback idiom
``updater.pop_update_notice`` already established.

**Capture success was unobservable.** ``remind_me_auto_capture`` only runs if
the user pasted the opt-in instruction into their client, and nothing
distinguished "capture is working" from "the instruction was never pasted" —
both look like an absence. :func:`capture_health` makes that difference
visible.

This module owns the *definitions* of what counts as pending. The batch tools
import their WHERE clauses from here rather than the other way round, so a
count shown in a nudge can never drift from the batch the corresponding tool
would actually return. It deliberately depends on nothing in
``remind_me_mcp.tools`` (which would be circular) — only ``db`` and ``wiki``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import TYPE_CHECKING

from remind_me_mcp.config import _env_int

if TYPE_CHECKING:
    import sqlite3

log = logging.getLogger("remind_me_mcp.maintenance")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

NUDGES_ENABLED: bool = os.environ.get(
    "REMIND_ME_MAINTENANCE_NUDGES", "true"
).lower() not in ("false", "0", "no")
"""Whether tool responses may carry a maintenance nudge. Set false to silence."""

NUDGE_INTERVAL = _env_int("REMIND_ME_MAINTENANCE_NUDGE_INTERVAL", 3600)
"""Minimum seconds between nudge *checks*.

This bounds the cost as well as the noise: the COUNT queries below only run
when the interval has elapsed, so the hot path (every search, every add) pays
one monotonic-clock comparison and nothing else in between.
"""

NUDGE_THRESHOLD = _env_int("REMIND_ME_MAINTENANCE_NUDGE_THRESHOLD", 25)
"""Queue depth a backlog must reach before it is worth mentioning.

A handful of pending items is the normal steady state of a system being used;
nudging at 1 would train the reader to ignore nudges, which is the failure
mode this whole module is trying to avoid.
"""


# ---------------------------------------------------------------------------
# Pending-work definitions (single source of truth)
# ---------------------------------------------------------------------------

# Captures not yet broken into atomic facts: rows that are captures
# (capture_id set), are not themselves decomposed children, and have no
# children pointing back at them. Mirrors remind_me_decompose_batch.
UNDECOMPOSED_WHERE = """
    m.capture_id IS NOT NULL
    AND m.source_capture_id IS NULL
    AND NOT EXISTS (
        SELECT 1 FROM memories c WHERE c.source_capture_id = m.capture_id
    )
"""

# Memories eligible for entity/SPO annotation: not superseded, not raw
# verbatim dialogs (annotate the summary/facts instead), and not yet
# annotated — no SPO triple AND no entity mentions. A category='fact' row
# that already has SPO is excluded by the subject/predicate/object check.
UNANNOTATED_WHERE = """
    m.superseded_by IS NULL
    AND m.deleted_at IS NULL
    AND m.category != 'dialog'
    AND m.subject IS NULL AND m.predicate IS NULL AND m.object IS NULL
    AND NOT EXISTS (
        SELECT 1 FROM memory_entities me WHERE me.memory_id = m.id
    )
"""

# Raw imports eligible for normalization: not superseded, from the file
# import pipeline (document_import/chat_import — FT-02), and not already
# normalized (no existing memory points back at it via normalized_from).
#
# Written as NOT IN over a non-correlated subquery rather than a correlated
# NOT EXISTS: with tens of thousands of rows, a correlated subquery forces
# SQLite to re-scan the normalized_from index once per candidate row (even
# with idx_memories_normalized_from in place it's a per-row SCAN, not a
# SEEK), which pegs a core for minutes. The NOT IN form lets SQLite
# materialize the id set once (LIST SUBQUERY + bloom filter) and probe it
# per row instead — same result, ~1000x faster on this table size.
UNNORMALIZED_WHERE = """
    m.superseded_by IS NULL
    AND m.deleted_at IS NULL
    AND m.source IN ('document_import', 'chat_import')
    AND m.id NOT IN (
        SELECT json_extract(metadata, '$.normalized_from') FROM memories
        WHERE json_extract(metadata, '$.normalized_from') IS NOT NULL
    )
"""

# Memories still carrying the default classification. Mirrors
# remind_me_reclassify_batch.
UNCLASSIFIED_WHERE = "m.memory_type = 'unclassified'"

# Memories worth an importance recalibration pass (issue #200): nothing
# today re-evaluates whether a memory's *original* importance classification
# has gone stale (the issue's own examples: a "decision" later reversed by a
# different memory, or a "fact" superseded in spirit but not via the formal
# triple-supersession mechanism). This heuristic is deliberately
# deterministic -- it only narrows an unbounded set down to a reviewable
# batch; the actual judgment about whether a given memory's importance is
# still right happens client-side, in remind_me_recalibrate_candidates'
# calling Claude session (see tools/recalibrate.py).
RECALIBRATION_MIN_BASE_WEIGHT = 1.15
"""base_weight floor for the "importance" half of the heuristic -- matches
vitality.BASE_WEIGHT_TYPE_PRIORS' fact/insight seed (1.15), i.e. the point at
which the write-time prior itself already treats a memory as more than
default-important, not an arbitrarily chosen cutoff."""

RECALIBRATION_DURABLE_TYPES: tuple[str, ...] = ("decision", "fact")
"""memory_type values whose category implies durability on its own, even
when base_weight hasn't (yet) been bumped by seeding or feedback -- these are
the issue's own two examples of a classification that can go stale."""

RECALIBRATION_STALE_DAYS = 90
"""Days since last access (or creation, if never accessed) before an
important-looking memory is old enough, relative to its importance, to be
worth a second look -- a memory that's still being actively used is
presumably still classified correctly."""

_RECALIBRATION_TYPES_SQL = ", ".join(f"'{t}'" for t in RECALIBRATION_DURABLE_TYPES)

# NOT EXISTS against memory_feedback is a correlated subquery, but (unlike
# the UNNORMALIZED_WHERE json_extract case issue #120 fixed) memory_id there
# carries a real index (idx_memory_feedback_memory_id) and the table is tiny
# relative to memories, so this resolves as a per-row index SEEK rather than
# a table SCAN -- the same shape UNANNOTATED_WHERE already relies on for its
# own correlated NOT EXISTS.
RECALIBRATION_CANDIDATE_WHERE = f"""
    m.superseded_by IS NULL
    AND m.deleted_at IS NULL
    AND (
        m.base_weight >= {RECALIBRATION_MIN_BASE_WEIGHT}
        OR m.memory_type IN ({_RECALIBRATION_TYPES_SQL})
    )
    AND (julianday('now') - julianday(COALESCE(m.accessed_at, m.created_at))) >= {RECALIBRATION_STALE_DAYS}
    AND NOT EXISTS (
        SELECT 1 FROM memory_feedback mf WHERE mf.memory_id = m.id
    )
"""

# Free-text contradiction candidates (issue #201): db._supersede_contradicting_facts
# already auto-supersedes a memory whenever a new/updated SPO triple shares an
# existing one's (subject, predicate) but has a different object -- see that
# function's docstring and the README's "Contradiction-based supersession"
# section. That mechanism only fires on exact triple structure. It says
# nothing about two pieces of free-text prose that conflict without ever
# sharing a formal subject/predicate ("I moved to Boston" vs. "I live in
# Seattle" as two unstructured memories, neither carrying SPO columns) -- this
# queue targets exactly that gap.
#
# The comparison space is bounded by the entity graph (FT-04) rather than
# all-pairs: two memories are only worth comparing if they mention at least
# one entity in common (a candidate pair that shares no entity is extremely
# unlikely to be a direct contradiction, and all-pairs over the whole vault
# would be O(n^2)). This is a JOIN over memory_entities, not a single-table
# WHERE like the queues above -- see CONTRADICTION_CANDIDATE_PAIRS_SQL and the
# is_full_query flag in _QUEUES below.
#
# Pairs already covered by the exact-triple mechanism are excluded so this
# queue doesn't re-surface what db.py already resolved automatically. Two
# points worth being explicit about:
#   1. A pair where BOTH sides have a matching normalized (subject, predicate)
#      but a *different* object cannot actually be observed here: the moment
#      the second one was written, _supersede_contradicting_facts would have
#      set superseded_by on the first, and this queue (like every other one)
#      only considers superseded_by IS NULL rows. So excluding "matching
#      subject+predicate" pairs is filtering out same-object verbatim
#      restatements (not a contradiction worth flagging) and the ordering-
#      cheap way to encode both cases in one clause, not a defense against
#      pairs that could otherwise slip through.
#   2. The comparison here approximates db._normalize_entity_name (lowercase
#      + whitespace-collapse) with lower()/trim() in SQL rather than
#      replicating it exactly -- this queue only narrows a candidate set for
#      human/Claude review, so an imprecise exclusion is a false negative
#      (worst case: a genuinely-covered pair also gets surfaced here, which
#      the calling session will just recognize and skip), not a correctness
#      bug the way it would be in the write-path supersession check itself.
CONTRADICTION_CANDIDATE_PAIRS_SQL = """
    SELECT DISTINCT me1.memory_id AS id_a, me2.memory_id AS id_b
    FROM memory_entities me1
    JOIN memory_entities me2
        ON me2.entity_id = me1.entity_id AND me2.memory_id > me1.memory_id
    JOIN memories m1 ON m1.id = me1.memory_id
    JOIN memories m2 ON m2.id = me2.memory_id
    WHERE m1.superseded_by IS NULL AND m1.deleted_at IS NULL
      AND m2.superseded_by IS NULL AND m2.deleted_at IS NULL
      AND m1.category != 'dialog' AND m2.category != 'dialog'
      AND NOT (
          m1.subject IS NOT NULL AND m1.predicate IS NOT NULL
          AND m2.subject IS NOT NULL AND m2.predicate IS NOT NULL
          AND lower(trim(m1.subject)) = lower(trim(m2.subject))
          AND lower(trim(m1.predicate)) = lower(trim(m2.predicate))
      )
"""

CONTRADICTION_CANDIDATE_COUNT_SQL = (
    f"SELECT COUNT(*) AS cnt FROM ({CONTRADICTION_CANDIDATE_PAIRS_SQL}) p"  # noqa: S608 — constants only
)


# Queue key -> (WHERE clause or full count query, the prompt that drains it,
# whether the first element is a full "SELECT COUNT..." query rather than a
# bare WHERE fragment to splice into "FROM memories m WHERE {...}"). The
# prompt name is what the nudge points at: naming a single prompt is more
# actionable than naming the two or three tools its loop actually sequences.
_QUEUES: dict[str, tuple[str, str, bool]] = {
    "undecomposed_captures": (UNDECOMPOSED_WHERE, "decompose_facts", False),
    "unannotated_memories": (UNANNOTATED_WHERE, "backfill_graph", False),
    "unnormalized_imports": (UNNORMALIZED_WHERE, "normalize_imports", False),
    "unclassified_memories": (UNCLASSIFIED_WHERE, "classify_memories", False),
    "recalibration_candidates": (
        RECALIBRATION_CANDIDATE_WHERE,
        "recalibrate_importance",
        False,
    ),
    "contradiction_candidates": (
        CONTRADICTION_CANDIDATE_COUNT_SQL,
        "review_contradictions",
        True,
    ),
}


def pending_counts(db: sqlite3.Connection) -> dict[str, int]:
    """Count every maintenance queue, including the wiki compile backlog.

    Args:
        db: An open SQLite connection.

    Returns:
        Queue key -> pending row count. A queue whose query fails (e.g. a
        table missing on a partially-migrated database) is reported as 0
        rather than propagating: a status/nudge helper must never be the
        thing that breaks a search.
    """
    counts: dict[str, int] = {}
    for key, (where_or_query, _prompt, is_full_query) in _QUEUES.items():
        try:
            query = (
                where_or_query
                if is_full_query
                else f"SELECT COUNT(*) AS cnt FROM memories m WHERE {where_or_query}"  # noqa: S608 — constants above, no user input
            )
            row = db.execute(query).fetchone()
            counts[key] = int(row["cnt"]) if row is not None else 0
        except Exception:  # noqa: BLE001 — never let a count break the caller
            log.debug("Pending count failed for %s", key, exc_info=True)
            counts[key] = 0

    try:
        from remind_me_mcp import wiki

        counts["pending_wiki_compile"] = wiki.pending_compile_count()
    except Exception:  # noqa: BLE001 — same rule as above
        log.debug("Wiki pending count failed", exc_info=True)
        counts["pending_wiki_compile"] = 0

    return counts


# ---------------------------------------------------------------------------
# Capture health
# ---------------------------------------------------------------------------


def capture_health(db: sqlite3.Connection) -> dict[str, object]:
    """Report whether conversation capture is actually happening.

    ``remind_me_auto_capture`` only runs when the user has pasted the opt-in
    instruction into their client, and a client where that never happened is
    indistinguishable from one where it did but no conversation was worth
    capturing — both produce silence. Returning the count and the most recent
    capture time makes "never configured" a visible state rather than an
    inference.

    Args:
        db: An open SQLite connection.

    Returns:
        ``{"captures": int, "last_capture_at": str | None, "ever_captured": bool}``.
        Counts distinct ``capture_id`` values, so the dialog/summary pair a
        single capture writes counts once, not twice.
    """
    try:
        row = db.execute(
            "SELECT COUNT(DISTINCT capture_id) AS cnt, MAX(created_at) AS last "
            "FROM memories WHERE capture_id IS NOT NULL AND deleted_at IS NULL"
        ).fetchone()
    except Exception:  # noqa: BLE001 — status helpers never raise
        log.debug("Capture health query failed", exc_info=True)
        return {"captures": 0, "last_capture_at": None, "ever_captured": False}

    captures = int(row["cnt"]) if row is not None else 0
    return {
        "captures": captures,
        "last_capture_at": row["last"] if row is not None else None,
        "ever_captured": captures > 0,
    }


# ---------------------------------------------------------------------------
# Throttled nudge
# ---------------------------------------------------------------------------

# Named throttle timers. Keyed rather than a single global because the
# maintenance nudge and the feedback hint are independent advisories with
# different cadences, and one claiming the slot must not silence the other.
_last_check_at: dict[str, float] = {}
_check_lock = threading.Lock()


def _due(name: str, interval: int) -> bool:
    """Claim the throttle slot for *name*, returning whether it was due.

    Claiming happens here rather than at the caller's success path on purpose:
    it bounds how often the *work behind* the check runs, not just how often a
    notice is emitted. See :func:`maybe_maintenance_notice` for why that
    matters on the search hot path.
    """
    now = time.monotonic()
    with _check_lock:
        last = _last_check_at.get(name, 0.0)
        if last and (now - last) < interval:
            return False
        _last_check_at[name] = now
        return True

# Human-readable label per queue, used to build the nudge line.
_LABELS = {
    "undecomposed_captures": "captures not decomposed into facts",
    "unannotated_memories": "memories with no entity/triple annotation",
    "unnormalized_imports": "raw imports not normalized",
    "unclassified_memories": "memories unclassified",
    "recalibration_candidates": "memories due for an importance review",
    "contradiction_candidates": "possibly-contradictory memory pairs",
    "pending_wiki_compile": "memories not folded into the wiki",
}

_QUEUE_PROMPTS = {key: prompt for key, (_where, prompt, _is_full) in _QUEUES.items()}
_QUEUE_PROMPTS["pending_wiki_compile"] = "compile_wiki"


def reset_nudge_throttle() -> None:
    """Clear every throttle timer so the next call re-checks. Used by tests."""
    with _check_lock:
        _last_check_at.clear()


def maybe_maintenance_notice(db: sqlite3.Connection) -> str | None:
    """Return a maintenance nudge if one is due, else ``None``.

    Throttled to one *check* per :data:`NUDGE_INTERVAL` seconds — the timer is
    claimed before the counts are queried, so a quiet vault costs the same as a
    busy one and the hot path never pays for repeated COUNTs. Only queues at or
    above :data:`NUDGE_THRESHOLD` are mentioned.

    Args:
        db: An open SQLite connection.

    Returns:
        A short markdown nudge naming the deepest backlogs and the prompt that
        drains each, or None when nudges are disabled, throttled, or nothing
        has crossed the threshold.
    """
    if not NUDGES_ENABLED:
        return None

    # Claiming the slot before querying is what keeps the COUNTs from running
    # on every single search when there is nothing pending.
    if not _due("maintenance", NUDGE_INTERVAL):
        return None

    counts = pending_counts(db)
    backlogs = sorted(
        ((k, v) for k, v in counts.items() if v >= NUDGE_THRESHOLD),
        key=lambda kv: kv[1],
        reverse=True,
    )
    if not backlogs:
        return None

    lines = ["**Maintenance pending** — run when convenient:"]
    for key, count in backlogs[:3]:
        lines.append(
            f"- {count} {_LABELS.get(key, key)} → `{_QUEUE_PROMPTS.get(key, '')}` prompt"
        )
    return "\n".join(lines)


FEEDBACK_HINT_INTERVAL = _env_int("REMIND_ME_FEEDBACK_HINT_INTERVAL", 7200)
"""Minimum seconds between feedback hints on search responses.

Longer than the maintenance interval by default: a maintenance backlog is a
task to do, whereas this is a standing affordance, and a standing affordance
repeated too often is just wallpaper.
"""


def maybe_feedback_hint() -> str | None:
    """Return an occasional reminder that search results can be rated.

    ``remind_me_feedback`` tunes ranking, but nothing in a normal session ever
    asks for it, so the signal it depends on effectively never arrives and the
    query-contextual ranking path stays untrained. Surfacing the affordance
    where the memory ids actually are — on a search result — is the cheapest
    way to close that loop.

    Throttled on its own timer and deliberately worded toward the
    query-contextual form, which is the mode worth having: a global adjustment
    penalises a memory for every future query, not just this kind of question.

    Returns:
        A one-line markdown hint, or None when disabled or throttled.
    """
    if not NUDGES_ENABLED:
        return None
    if not _due("feedback", FEEDBACK_HINT_INTERVAL):
        return None
    return (
        "_If one of these was clearly right or clearly wrong, "
        "`remind_me_feedback(memory_id=..., signal=..., query=...)` tunes future "
        "ranking. Pass `query` so the signal stays scoped to this kind of question._"
    )


__all__ = [
    "NUDGES_ENABLED",
    "NUDGE_INTERVAL",
    "NUDGE_THRESHOLD",
    "FEEDBACK_HINT_INTERVAL",
    "maybe_feedback_hint",
    "UNDECOMPOSED_WHERE",
    "UNANNOTATED_WHERE",
    "UNNORMALIZED_WHERE",
    "UNCLASSIFIED_WHERE",
    "RECALIBRATION_MIN_BASE_WEIGHT",
    "RECALIBRATION_DURABLE_TYPES",
    "RECALIBRATION_STALE_DAYS",
    "RECALIBRATION_CANDIDATE_WHERE",
    "CONTRADICTION_CANDIDATE_PAIRS_SQL",
    "CONTRADICTION_CANDIDATE_COUNT_SQL",
    "pending_counts",
    "capture_health",
    "maybe_maintenance_notice",
    "reset_nudge_throttle",
]
