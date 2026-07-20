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
    BASE_WEIGHT_MAX,
    BASE_WEIGHT_MIN,
    BRIDGE_THRESHOLD,
    DECAY_RATES,
    FEEDBACK_MAGNITUDE,
    VITALITY_FLOOR,
    compute_vitality,
    get_effective_decay_rate,
    is_dormant,
    record_access,
    record_accesses,
    record_feedback,
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
# effective_vitality — read-time decay (DI-04)
# ---------------------------------------------------------------------------


def _decayed_memory(days_ago: float, **overrides) -> dict:
    """Build a memory dict whose accessed_at lies *days_ago* in the past."""
    from datetime import UTC, datetime, timedelta

    accessed = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    mem = {
        "accessed_at": accessed,
        "created_at": accessed,
        "access_count": 0,
        "decay_rate": 0.1,
        "base_weight": 1.0,
    }
    mem.update(overrides)
    return mem


def test_effective_vitality_decays_with_elapsed_days() -> None:
    """effective_vitality applies real elapsed-days decay since accessed_at."""
    from datetime import UTC, datetime, timedelta

    from remind_me_mcp.vitality import effective_vitality

    now = datetime.now(UTC)
    mem = {
        "accessed_at": (now - timedelta(days=30)).isoformat(),
        "access_count": 0,
        "decay_rate": 0.1,
        "base_weight": 1.0,
    }
    expected = compute_vitality(1.0, 0, 0.1, 30.0)
    assert effective_vitality(mem, now=now) == pytest.approx(expected)


def test_effective_vitality_fresh_access_is_snapshot() -> None:
    """A just-accessed memory has effective vitality equal to its at-access snapshot."""
    from remind_me_mcp.vitality import effective_vitality

    mem = _decayed_memory(0.0, access_count=3)
    assert effective_vitality(mem) == pytest.approx(compute_vitality(1.0, 3, 0.1, 0.0), rel=1e-3)


def test_effective_vitality_falls_back_to_created_at() -> None:
    """Without accessed_at, decay is measured from created_at."""
    from datetime import UTC, datetime, timedelta

    from remind_me_mcp.vitality import effective_vitality

    now = datetime.now(UTC)
    mem = {
        "accessed_at": None,
        "created_at": (now - timedelta(days=10)).isoformat(),
        "access_count": 0,
        "decay_rate": 0.1,
        "base_weight": 1.0,
    }
    expected = compute_vitality(1.0, 0, 0.1, 10.0)
    assert effective_vitality(mem, now=now) == pytest.approx(expected)


def test_effective_vitality_applies_bridge_protection() -> None:
    """Bridge-protected memories (access_count >= threshold) decay at half rate."""
    from datetime import UTC, datetime

    from remind_me_mcp.vitality import effective_vitality

    now = datetime.now(UTC)
    mem = _decayed_memory(20.0, access_count=BRIDGE_THRESHOLD)
    expected = compute_vitality(1.0, BRIDGE_THRESHOLD, 0.05, 20.0)
    assert effective_vitality(mem, now=now) == pytest.approx(expected, rel=1e-3)


def test_effective_vitality_missing_fields_defaults() -> None:
    """A dict without vitality columns yields the default fresh vitality of 1.0."""
    from remind_me_mcp.vitality import effective_vitality

    assert effective_vitality({"created_at": _now_iso()}) == pytest.approx(1.0, rel=1e-3)


def test_effective_vitality_old_memory_goes_dormant() -> None:
    """A long-unaccessed memory decays below the dormancy floor."""
    from remind_me_mcp.vitality import effective_vitality

    mem = _decayed_memory(365.0)
    assert is_dormant(effective_vitality(mem))


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


# ---------------------------------------------------------------------------
# record_accesses — batched database integration (PF-02)
# ---------------------------------------------------------------------------


def _insert_access_row(db_conn: sqlite3.Connection, content: str, access_count: int = 0) -> str:
    """Insert a memory row with vitality columns populated; return its id."""
    now = _now_iso()
    mem_id = _make_id(content)
    db_conn.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata,
           created_at, updated_at, accessed_at, access_count, decay_rate, vitality, base_weight, status, memory_type)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (mem_id, content, "general", "[]", "manual", "{}", now, now, now,
         access_count, 0.1, 1.0, 1.0, "active", "unclassified"),
    )
    db_conn.commit()
    return mem_id


def test_record_accesses_batch_updates_all(db_conn: sqlite3.Connection) -> None:
    """record_accesses updates accessed_at/access_count/vitality for every id."""
    ids = [_insert_access_row(db_conn, f"batch-access-test-{i}") for i in range(3)]

    updated = record_accesses(ids)
    assert updated == 3

    for mem_id in ids:
        row = db_conn.execute(
            "SELECT access_count, vitality, accessed_at, status FROM memories WHERE id = ?",
            (mem_id,),
        ).fetchone()
        assert row["access_count"] == 1
        assert row["vitality"] > 0
        assert row["accessed_at"] is not None
        assert row["status"] == "active"


def test_record_accesses_matches_record_access(db_conn: sqlite3.Connection) -> None:
    """The batched path computes the same vitality as the single-id path."""
    single_id = _insert_access_row(db_conn, "equivalence-single", access_count=4)
    batch_id = _insert_access_row(db_conn, "equivalence-batch", access_count=4)

    single_vitality = record_access(single_id)
    assert record_accesses([batch_id]) == 1

    row = db_conn.execute(
        "SELECT vitality, access_count FROM memories WHERE id = ?", (batch_id,)
    ).fetchone()
    assert row["access_count"] == 5
    assert row["vitality"] == single_vitality


def test_record_accesses_skips_unknown_ids(db_conn: sqlite3.Connection) -> None:
    """Unknown ids are skipped; known ones in the same batch still update."""
    known = _insert_access_row(db_conn, "partial-batch-known")

    assert record_accesses([known, "nonexistent-id-abc"]) == 1
    row = db_conn.execute(
        "SELECT access_count FROM memories WHERE id = ?", (known,)
    ).fetchone()
    assert row["access_count"] == 1


def test_record_accesses_empty_list_is_noop(db_conn: sqlite3.Connection) -> None:
    """An empty id list returns 0 without touching the database."""
    assert record_accesses([]) == 0


def test_record_accesses_applies_bridge_protection(db_conn: sqlite3.Connection) -> None:
    """Crossing BRIDGE_THRESHOLD in a batch halves the decay rate, like record_access."""
    mem_id = _insert_access_row(
        db_conn, "batch-bridge-test", access_count=BRIDGE_THRESHOLD - 1
    )

    assert record_accesses([mem_id]) == 1
    row = db_conn.execute(
        "SELECT access_count FROM memories WHERE id = ?", (mem_id,)
    ).fetchone()
    assert row["access_count"] == BRIDGE_THRESHOLD


# ---------------------------------------------------------------------------
# record_feedback — signed base_weight adjustment
# ---------------------------------------------------------------------------


def test_record_feedback_helpful_increases_base_weight(db_conn: sqlite3.Connection) -> None:
    """A 'helpful' signal multiplies base_weight up by (1 + magnitude)."""
    mem_id = _insert_access_row(db_conn, "feedback-helpful-test")

    result = record_feedback(mem_id, "helpful")
    assert result is not None
    assert isinstance(result, float)

    row = db_conn.execute(
        "SELECT base_weight FROM memories WHERE id = ?", (mem_id,)
    ).fetchone()
    assert row["base_weight"] == pytest.approx(1.0 * (1 + FEEDBACK_MAGNITUDE))


def test_record_feedback_unhelpful_decreases_base_weight(db_conn: sqlite3.Connection) -> None:
    """An 'unhelpful' signal multiplies base_weight down by (1 - magnitude)."""
    mem_id = _insert_access_row(db_conn, "feedback-unhelpful-test")

    record_feedback(mem_id, "unhelpful")

    row = db_conn.execute(
        "SELECT base_weight FROM memories WHERE id = ?", (mem_id,)
    ).fetchone()
    assert row["base_weight"] == pytest.approx(1.0 * (1 - FEEDBACK_MAGNITUDE))


def test_record_feedback_helpful_is_capped_at_max(db_conn: sqlite3.Connection) -> None:
    """Repeated 'helpful' signals never push base_weight above BASE_WEIGHT_MAX."""
    mem_id = _insert_access_row(db_conn, "feedback-cap-test")
    db_conn.execute(
        "UPDATE memories SET base_weight = ? WHERE id = ?", (BASE_WEIGHT_MAX, mem_id)
    )
    db_conn.commit()

    record_feedback(mem_id, "helpful")

    row = db_conn.execute(
        "SELECT base_weight FROM memories WHERE id = ?", (mem_id,)
    ).fetchone()
    assert row["base_weight"] == BASE_WEIGHT_MAX


def test_record_feedback_unhelpful_is_floored_at_min(db_conn: sqlite3.Connection) -> None:
    """Repeated 'unhelpful' signals never push base_weight below BASE_WEIGHT_MIN."""
    mem_id = _insert_access_row(db_conn, "feedback-floor-test")
    db_conn.execute(
        "UPDATE memories SET base_weight = ? WHERE id = ?", (BASE_WEIGHT_MIN, mem_id)
    )
    db_conn.commit()

    record_feedback(mem_id, "unhelpful")

    row = db_conn.execute(
        "SELECT base_weight FROM memories WHERE id = ?", (mem_id,)
    ).fetchone()
    assert row["base_weight"] == BASE_WEIGHT_MIN


def test_record_feedback_does_not_touch_access_count(db_conn: sqlite3.Connection) -> None:
    """Feedback is not an access: access_count and accessed_at are untouched."""
    mem_id = _insert_access_row(db_conn, "feedback-no-access-test", access_count=3)
    before = db_conn.execute(
        "SELECT access_count, accessed_at FROM memories WHERE id = ?", (mem_id,)
    ).fetchone()

    record_feedback(mem_id, "helpful")

    after = db_conn.execute(
        "SELECT access_count, accessed_at FROM memories WHERE id = ?", (mem_id,)
    ).fetchone()
    assert after["access_count"] == before["access_count"]
    assert after["accessed_at"] == before["accessed_at"]


def test_record_feedback_updates_vitality_and_status(db_conn: sqlite3.Connection) -> None:
    """record_feedback recomputes vitality/status from the new base_weight."""
    mem_id = _insert_access_row(db_conn, "feedback-vitality-test")

    result = record_feedback(mem_id, "unhelpful")

    row = db_conn.execute(
        "SELECT vitality, status FROM memories WHERE id = ?", (mem_id,)
    ).fetchone()
    assert row["vitality"] == pytest.approx(result)
    assert row["status"] in ("active", "dormant")


def test_record_feedback_not_found(db_conn: sqlite3.Connection) -> None:
    """record_feedback returns None for a non-existent memory_id."""
    assert record_feedback("nonexistent-id-xyz", "helpful") is None
