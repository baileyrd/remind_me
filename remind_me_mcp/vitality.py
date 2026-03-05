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

from remind_me_mcp.db import _get_db, _now_iso

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


# ---------------------------------------------------------------------------
# Database integration
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "BRIDGE_MULTIPLIER",
    "BRIDGE_THRESHOLD",
    "DECAY_RATES",
    "VITALITY_FLOOR",
    "compute_vitality",
    "get_effective_decay_rate",
    "is_dormant",
    "record_access",
]
