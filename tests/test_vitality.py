"""
Unit tests for remind_me_mcp.vitality -- ACT-R vitality computation and access recording.

Tests cover the pure computation functions (compute_vitality, get_effective_decay_rate,
is_dormant), the DECAY_RATES constant mapping, and the record_access database integration.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import pytest

from remind_me_mcp.db import _make_id, _now_iso
from remind_me_mcp.vitality import (
    BRIDGE_THRESHOLD,
    DECAY_RATES,
    VITALITY_FLOOR,
    compute_vitality,
    get_effective_decay_rate,
    is_dormant,
    record_access,
)

if TYPE_CHECKING:
    import sqlite3


# ---------------------------------------------------------------------------
# compute_vitality — pure ACT-R formula
# ---------------------------------------------------------------------------


def test_compute_vitality_brand_new_memory() -> None:
    """Brand-new memory (access_count=0, days_since=0, base_weight=1.0, decay_rate=0.1) returns 1.0."""
    result = compute_vitality(base_weight=1.0, access_count=0, decay_rate=0.1, days_since_last_access=0.0)
    assert result == pytest.approx(1.0)


def test_compute_vitality_decreases_over_time() -> None:
    """Vitality at days_since=30 is lower than at days_since=1 (same other params)."""
    v_1day = compute_vitality(base_weight=1.0, access_count=0, decay_rate=0.1, days_since_last_access=1.0)
    v_30days = compute_vitality(base_weight=1.0, access_count=0, decay_rate=0.1, days_since_last_access=30.0)
    assert v_30days < v_1day


def test_compute_vitality_increases_with_accesses() -> None:
    """More accesses (access_count=10) yield higher vitality than fewer (access_count=1), same days."""
    v_1 = compute_vitality(base_weight=1.0, access_count=1, decay_rate=0.1, days_since_last_access=5.0)
    v_10 = compute_vitality(base_weight=1.0, access_count=10, decay_rate=0.1, days_since_last_access=5.0)
    assert v_10 > v_1


def test_compute_vitality_respects_base_weight() -> None:
    """base_weight=2.0 returns exactly double the value of base_weight=1.0."""
    v1 = compute_vitality(base_weight=1.0, access_count=3, decay_rate=0.1, days_since_last_access=5.0)
    v2 = compute_vitality(base_weight=2.0, access_count=3, decay_rate=0.1, days_since_last_access=5.0)
    assert v2 == pytest.approx(2.0 * v1)


def test_compute_vitality_formula_exact() -> None:
    """Verify the ACT-R formula against a hand-calculated value."""
    # base_weight * (access_count + 1)^0.5 * exp(-decay_rate * days)
    bw, ac, dr, days = 1.5, 4, 0.05, 10.0
    expected = bw * (ac + 1) ** 0.5 * math.exp(-dr * days)
    result = compute_vitality(base_weight=bw, access_count=ac, decay_rate=dr, days_since_last_access=days)
    assert result == pytest.approx(expected)


# ---------------------------------------------------------------------------
# get_effective_decay_rate — bridge protection
# ---------------------------------------------------------------------------


def test_bridge_protection_halves_decay_above_threshold() -> None:
    """get_effective_decay_rate halves decay_rate when access_count >= BRIDGE_THRESHOLD."""
    rate = get_effective_decay_rate(decay_rate=0.1, access_count=BRIDGE_THRESHOLD)
    assert rate == pytest.approx(0.05)


def test_bridge_protection_unchanged_below_threshold() -> None:
    """get_effective_decay_rate returns original decay_rate when access_count < BRIDGE_THRESHOLD."""
    rate = get_effective_decay_rate(decay_rate=0.1, access_count=BRIDGE_THRESHOLD - 1)
    assert rate == pytest.approx(0.1)


def test_bridge_protection_above_threshold() -> None:
    """get_effective_decay_rate halves decay_rate well above the threshold."""
    rate = get_effective_decay_rate(decay_rate=0.2, access_count=50)
    assert rate == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# DECAY_RATES constant
# ---------------------------------------------------------------------------


def test_decay_rates_maps_memory_types() -> None:
    """DECAY_RATES dict maps expected memory types to specific float rates."""
    expected_types = {
        "decision", "preference", "fact", "insight", "learning",
        "blocker", "action_item", "unclassified",
    }
    assert set(DECAY_RATES.keys()) == expected_types
    for key, value in DECAY_RATES.items():
        assert isinstance(value, float), f"DECAY_RATES[{key!r}] should be float, got {type(value)}"
        assert value > 0, f"DECAY_RATES[{key!r}] should be positive"


def test_decay_rates_ordering() -> None:
    """Decisions decay slowest, action_items decay fastest."""
    assert DECAY_RATES["decision"] < DECAY_RATES["action_item"]
    assert DECAY_RATES["preference"] < DECAY_RATES["blocker"]


# ---------------------------------------------------------------------------
# is_dormant
# ---------------------------------------------------------------------------


def test_is_dormant_below_floor() -> None:
    """is_dormant returns True when vitality < VITALITY_FLOOR."""
    assert is_dormant(VITALITY_FLOOR - 0.01) is True


def test_is_dormant_above_floor() -> None:
    """is_dormant returns False when vitality >= VITALITY_FLOOR."""
    assert is_dormant(VITALITY_FLOOR) is False
    assert is_dormant(1.0) is False


# ---------------------------------------------------------------------------
# record_access — database integration
# ---------------------------------------------------------------------------


def test_record_access_updates_db(db_conn: sqlite3.Connection) -> None:
    """record_access updates accessed_at, increments access_count, recomputes vitality."""
    now = _now_iso()
    mem_id = _make_id("record-access-test")
    db_conn.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata,
           created_at, updated_at, accessed_at, access_count, decay_rate, vitality, base_weight, status, memory_type)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (mem_id, "Access test", "general", "[]", "manual", "{}", now, now, now, 0, 0.1, 1.0, 1.0, "active", "unclassified"),
    )
    db_conn.commit()

    result = record_access(mem_id)
    assert result is not None
    assert isinstance(result, float)

    row = db_conn.execute(
        "SELECT access_count, vitality, accessed_at FROM memories WHERE id = ?", (mem_id,)
    ).fetchone()
    assert row[0] == 1, f"Expected access_count=1, got {row[0]}"
    assert row[1] > 0, "Vitality should be positive"
    assert row[2] is not None, "accessed_at should be set"


def test_record_access_not_found(db_conn: sqlite3.Connection) -> None:
    """record_access returns None for a non-existent memory_id."""
    result = record_access("nonexistent-id-xyz")
    assert result is None


def test_record_access_bridge_protection(db_conn: sqlite3.Connection) -> None:
    """record_access applies bridge protection for high-access memories."""
    now = _now_iso()
    mem_id = _make_id("bridge-access-test")
    # Insert with access_count just below threshold (will be incremented to threshold)
    db_conn.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata,
           created_at, updated_at, accessed_at, access_count, decay_rate, vitality, base_weight, status, memory_type)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (mem_id, "Bridge test", "general", "[]", "manual", "{}", now, now, now,
         BRIDGE_THRESHOLD - 1, 0.1, 0.5, 1.0, "active", "unclassified"),
    )
    db_conn.commit()

    result = record_access(mem_id)
    assert result is not None

    # After this access, access_count == BRIDGE_THRESHOLD, so bridge protection should apply
    row = db_conn.execute(
        "SELECT access_count FROM memories WHERE id = ?", (mem_id,)
    ).fetchone()
    assert row[0] == BRIDGE_THRESHOLD


def test_record_access_increments_multiple_times(db_conn: sqlite3.Connection) -> None:
    """record_access increments access_count correctly on multiple calls."""
    now = _now_iso()
    mem_id = _make_id("multi-access-test")
    db_conn.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata,
           created_at, updated_at, accessed_at, access_count, decay_rate, vitality, base_weight, status, memory_type)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (mem_id, "Multi access test", "general", "[]", "manual", "{}", now, now, now, 0, 0.1, 1.0, 1.0, "active", "unclassified"),
    )
    db_conn.commit()

    record_access(mem_id)
    record_access(mem_id)
    record_access(mem_id)

    row = db_conn.execute(
        "SELECT access_count FROM memories WHERE id = ?", (mem_id,)
    ).fetchone()
    assert row[0] == 3, f"Expected access_count=3, got {row[0]}"
