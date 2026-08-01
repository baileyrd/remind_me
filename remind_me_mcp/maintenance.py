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


# Queue key -> (WHERE clause, the prompt that drains it). The prompt name is
# what the nudge points at: naming a single prompt is more actionable than
# naming the two or three tools its loop actually sequences.
_QUEUES: dict[str, tuple[str, str]] = {
    "undecomposed_captures": (UNDECOMPOSED_WHERE, "decompose_facts"),
    "unannotated_memories": (UNANNOTATED_WHERE, "backfill_graph"),
    "unnormalized_imports": (UNNORMALIZED_WHERE, "normalize_imports"),
    "unclassified_memories": (UNCLASSIFIED_WHERE, "classify_memories"),
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
    for key, (where, _prompt) in _QUEUES.items():
        try:
            row = db.execute(
                f"SELECT COUNT(*) AS cnt FROM memories m WHERE {where}"  # noqa: S608 — constants above, no user input
            ).fetchone()
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
    "pending_wiki_compile": "memories not folded into the wiki",
}

_QUEUE_PROMPTS = {key: prompt for key, (_where, prompt) in _QUEUES.items()}
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
    "pending_counts",
    "capture_health",
    "maybe_maintenance_notice",
    "reset_nudge_throttle",
]
