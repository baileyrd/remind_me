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
    BASE_WEIGHT_SOURCE_PRIORS,
    BASE_WEIGHT_TYPE_PRIORS,
    BRIDGE_THRESHOLD,
    CO_RETRIEVAL_MAX_WEIGHT,
    DECAY_RATES,
    FEEDBACK_ADJUSTMENT_CAP,
    FEEDBACK_MAGNITUDE,
    FEEDBACK_SIMILARITY_THRESHOLD,
    VITALITY_FLOOR,
    apply_feedback_adjustment,
    build_vitality_report,
    compute_vitality,
    contextual_feedback_adjustment,
    get_effective_decay_rate,
    is_dormant,
    record_access,
    record_accesses,
    record_co_retrieval,
    record_contextual_feedback,
    record_feedback,
    seed_base_weight,
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
# seed_base_weight — importance prior at write time (issue #56)
# ---------------------------------------------------------------------------


def test_base_weight_type_priors_within_clamp_range() -> None:
    """Every type prior stays within [BASE_WEIGHT_MIN, BASE_WEIGHT_MAX]."""
    for key, value in BASE_WEIGHT_TYPE_PRIORS.items():
        assert BASE_WEIGHT_MIN <= value <= BASE_WEIGHT_MAX, f"{key!r} out of range: {value}"


def test_base_weight_source_priors_within_clamp_range() -> None:
    for key, value in BASE_WEIGHT_SOURCE_PRIORS.items():
        assert BASE_WEIGHT_MIN <= value <= BASE_WEIGHT_MAX, f"{key!r} out of range: {value}"


def test_seed_base_weight_decision_outranks_unclassified() -> None:
    """The headline case: a decision must start above a throwaway aside."""
    assert seed_base_weight(memory_type="decision") > seed_base_weight(memory_type="unclassified")


def test_seed_base_weight_prefers_memory_type_over_source() -> None:
    """A known, specific memory_type wins even when source is also known."""
    assert seed_base_weight(memory_type="decision", source="chat_import") == BASE_WEIGHT_TYPE_PRIORS["decision"]


def test_seed_base_weight_falls_back_to_source_when_type_unknown() -> None:
    assert seed_base_weight(memory_type=None, source="chat_import") == BASE_WEIGHT_SOURCE_PRIORS["chat_import"]


def test_seed_base_weight_unclassified_type_falls_back_to_source() -> None:
    """memory_type='unclassified' is treated the same as "not known yet"."""
    assert seed_base_weight(memory_type="unclassified", source="chat_import") == BASE_WEIGHT_SOURCE_PRIORS["chat_import"]


def test_seed_base_weight_manual_source_is_flat_default() -> None:
    assert seed_base_weight(source="manual") == 1.0


def test_seed_base_weight_no_signal_defaults_to_one() -> None:
    assert seed_base_weight() == 1.0
    assert seed_base_weight(memory_type="unclassified", source="some_unrecognized_source") == 1.0


def test_seed_base_weight_unrecognized_type_falls_back_to_source() -> None:
    """A memory_type string outside the known table (defensive -- shouldn't
    happen given the Literal-validated field, but the function must not
    raise) falls through to source, then the flat default."""
    assert seed_base_weight(memory_type="not_a_real_type", source="chat_import") == BASE_WEIGHT_SOURCE_PRIORS["chat_import"]
    assert seed_base_weight(memory_type="not_a_real_type") == 1.0


def test_seed_base_weight_chat_import_starts_below_manual() -> None:
    """Issue #56's own comparison: raw chat_import should start lower than manual entry."""
    assert seed_base_weight(source="chat_import") < seed_base_weight(source="manual")


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


# ---------------------------------------------------------------------------
# record_feedback(query=...) — query-contextual mode (issue #54)
# ---------------------------------------------------------------------------


def test_record_feedback_with_query_does_not_touch_base_weight(db_conn: sqlite3.Connection) -> None:
    """Feedback with a query is contextual-only -- base_weight is untouched."""
    mem_id = _insert_access_row(db_conn, "feedback-contextual-test")

    record_feedback(mem_id, "unhelpful", query="what's my favorite editor")

    row = db_conn.execute(
        "SELECT base_weight FROM memories WHERE id = ?", (mem_id,)
    ).fetchone()
    assert row["base_weight"] == 1.0


def test_record_feedback_with_query_returns_current_vitality(db_conn: sqlite3.Connection) -> None:
    """Since base_weight is unchanged, the returned vitality equals the stored value."""
    mem_id = _insert_access_row(db_conn, "feedback-contextual-vitality-test")
    stored = db_conn.execute(
        "SELECT vitality FROM memories WHERE id = ?", (mem_id,)
    ).fetchone()["vitality"]

    result = record_feedback(mem_id, "helpful", query="some query")

    assert result == pytest.approx(stored)


def test_record_feedback_with_query_logs_a_row(db_conn: sqlite3.Connection) -> None:
    mem_id = _insert_access_row(db_conn, "feedback-contextual-logs-test")

    record_feedback(mem_id, "helpful", query="what IDE did I mention last year")

    rows = db_conn.execute(
        "SELECT memory_id, query, signal, magnitude FROM memory_feedback WHERE memory_id = ?",
        (mem_id,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["query"] == "what IDE did I mention last year"
    assert rows[0]["signal"] == "helpful"
    assert rows[0]["magnitude"] == pytest.approx(FEEDBACK_MAGNITUDE)


def test_record_feedback_with_query_not_found(db_conn: sqlite3.Connection) -> None:
    assert record_feedback("nonexistent-id-xyz", "helpful", query="anything") is None


def test_record_feedback_without_query_is_unchanged(db_conn: sqlite3.Connection) -> None:
    """Regression guard: omitting query preserves the exact pre-#54 global mutation."""
    mem_id = _insert_access_row(db_conn, "feedback-backcompat-test")

    record_feedback(mem_id, "helpful")

    row = db_conn.execute(
        "SELECT base_weight FROM memories WHERE id = ?", (mem_id,)
    ).fetchone()
    assert row["base_weight"] == pytest.approx(1.0 * (1 + FEEDBACK_MAGNITUDE))
    assert db_conn.execute("SELECT COUNT(*) AS n FROM memory_feedback").fetchone()["n"] == 0


# ---------------------------------------------------------------------------
# _tokenize_query / _jaccard (internal helpers, exercised via public API)
# ---------------------------------------------------------------------------


def test_tokenize_and_jaccard_identical_queries_similarity_one() -> None:
    from remind_me_mcp.vitality import _jaccard, _tokenize_query

    a = _tokenize_query("what IDE did I mention last year?")
    b = _tokenize_query("What IDE did I mention last year?")
    assert _jaccard(a, b) == pytest.approx(1.0)


def test_tokenize_and_jaccard_disjoint_queries_similarity_zero() -> None:
    from remind_me_mcp.vitality import _jaccard, _tokenize_query

    a = _tokenize_query("favorite pizza toppings")
    b = _tokenize_query("vpn configuration settings")
    assert _jaccard(a, b) == 0.0


def test_jaccard_empty_sets_similarity_zero() -> None:
    from remind_me_mcp.vitality import _jaccard

    assert _jaccard(set(), {"a"}) == 0.0
    assert _jaccard(set(), set()) == 0.0


# ---------------------------------------------------------------------------
# record_contextual_feedback / contextual_feedback_adjustment (issue #54)
# ---------------------------------------------------------------------------


def test_record_contextual_feedback_inserts_row(db_conn: sqlite3.Connection) -> None:
    mem_id = _insert_access_row(db_conn, "contextual-insert-test")

    feedback_id = record_contextual_feedback(db_conn, mem_id, "what IDE did I use", "helpful")

    row = db_conn.execute(
        "SELECT id, memory_id, query, signal FROM memory_feedback WHERE id = ?",
        (feedback_id,),
    ).fetchone()
    assert row["memory_id"] == mem_id
    assert row["query"] == "what IDE did I use"
    assert row["signal"] == "helpful"


def test_contextual_feedback_adjustment_no_feedback_is_zero(db_conn: sqlite3.Connection) -> None:
    mem_id = _insert_access_row(db_conn, "no-feedback-test")
    assert contextual_feedback_adjustment(db_conn, mem_id, "any query") == 0.0


def test_contextual_feedback_adjustment_similar_query_applies(db_conn: sqlite3.Connection) -> None:
    """The headline case: feedback on a similar query counts."""
    mem_id = _insert_access_row(db_conn, "similar-query-test")
    record_contextual_feedback(db_conn, mem_id, "what IDE did I use last year", "helpful")

    adjustment = contextual_feedback_adjustment(db_conn, mem_id, "what IDE did I use last year")
    assert adjustment > 0.0


def test_contextual_feedback_adjustment_unrelated_query_does_not_apply(db_conn: sqlite3.Connection) -> None:
    """The headline case from the issue: unhelpful for one query must not
    demote a completely unrelated one."""
    mem_id = _insert_access_row(db_conn, "unrelated-query-test")
    record_contextual_feedback(db_conn, mem_id, "what's my favorite editor", "unhelpful")

    adjustment = contextual_feedback_adjustment(db_conn, mem_id, "what IDE did I mention last year")
    assert adjustment == 0.0


def test_contextual_feedback_adjustment_unhelpful_is_negative(db_conn: sqlite3.Connection) -> None:
    mem_id = _insert_access_row(db_conn, "negative-adjustment-test")
    record_contextual_feedback(db_conn, mem_id, "vpn configuration settings", "unhelpful")

    adjustment = contextual_feedback_adjustment(db_conn, mem_id, "vpn configuration settings")
    assert adjustment < 0.0


def test_contextual_feedback_adjustment_is_clamped(db_conn: sqlite3.Connection) -> None:
    """Many strong 'helpful' signals never push the adjustment past the cap."""
    mem_id = _insert_access_row(db_conn, "clamped-adjustment-test")
    for _ in range(20):
        record_contextual_feedback(
            db_conn, mem_id, "vpn configuration settings", "helpful", magnitude=1.0
        )

    adjustment = contextual_feedback_adjustment(db_conn, mem_id, "vpn configuration settings")
    assert adjustment == pytest.approx(FEEDBACK_ADJUSTMENT_CAP)


def test_contextual_feedback_adjustment_threshold_boundary(db_conn: sqlite3.Connection) -> None:
    """A query with overlap just below FEEDBACK_SIMILARITY_THRESHOLD contributes nothing."""
    from remind_me_mcp.vitality import _jaccard, _tokenize_query

    mem_id = _insert_access_row(db_conn, "threshold-boundary-test")
    record_contextual_feedback(db_conn, mem_id, "alpha beta gamma delta", "helpful")

    # Construct a query sharing exactly one of four tokens -- similarity 1/7 < 0.3.
    weak_query = "alpha epsilon zeta eta"
    sim = _jaccard(_tokenize_query("alpha beta gamma delta"), _tokenize_query(weak_query))
    assert sim < FEEDBACK_SIMILARITY_THRESHOLD

    assert contextual_feedback_adjustment(db_conn, mem_id, weak_query) == 0.0


def test_contextual_feedback_adjustment_missing_memory_is_zero(db_conn: sqlite3.Connection) -> None:
    assert contextual_feedback_adjustment(db_conn, "no-such-memory", "any query") == 0.0


# ---------------------------------------------------------------------------
# apply_feedback_adjustment (issue #54) -- the ranking-time integration point
# ---------------------------------------------------------------------------


def test_apply_feedback_adjustment_noop_without_feedback(db_conn: sqlite3.Connection) -> None:
    memories = [{"id": "A", "_rrf_score": 0.5}, {"id": "B", "_rrf_score": 0.3}]
    result = apply_feedback_adjustment("some query", memories)
    assert [m["_rrf_score"] for m in result] == [0.5, 0.3]
    assert "_feedback_adjustment" not in result[0]


def test_apply_feedback_adjustment_empty_memories_returns_empty() -> None:
    assert apply_feedback_adjustment("some query", []) == []


def test_apply_feedback_adjustment_empty_query_is_noop(db_conn: sqlite3.Connection) -> None:
    memories = [{"id": "A", "_rrf_score": 0.5}]
    assert apply_feedback_adjustment("", memories) == memories


def test_apply_feedback_adjustment_boosts_helpful_match(db_conn: sqlite3.Connection) -> None:
    mem_id = _insert_access_row(db_conn, "apply-boost-test")
    record_contextual_feedback(db_conn, mem_id, "vpn configuration settings", "helpful")

    memories = [{"id": mem_id, "_rrf_score": 0.5}]
    result = apply_feedback_adjustment("vpn configuration settings", memories)

    assert result[0]["_rrf_score"] > 0.5
    assert result[0]["_feedback_adjustment"] > 0.0


def test_apply_feedback_adjustment_demotes_unhelpful_match(db_conn: sqlite3.Connection) -> None:
    mem_id = _insert_access_row(db_conn, "apply-demote-test")
    record_contextual_feedback(db_conn, mem_id, "vpn configuration settings", "unhelpful")

    memories = [{"id": mem_id, "_rrf_score": 0.5}]
    result = apply_feedback_adjustment("vpn configuration settings", memories)

    assert result[0]["_rrf_score"] < 0.5


def test_apply_feedback_adjustment_resorts_by_adjusted_score(db_conn: sqlite3.Connection) -> None:
    """A lower-ranked memory with strong helpful feedback can overtake a
    higher-ranked one with no feedback."""
    helped = _insert_access_row(db_conn, "resort-helped-test")
    plain = _insert_access_row(db_conn, "resort-plain-test")
    for _ in range(10):
        record_contextual_feedback(
            db_conn, helped, "vpn configuration settings", "helpful", magnitude=1.0
        )

    memories = [
        {"id": plain, "_rrf_score": 0.6},
        {"id": helped, "_rrf_score": 0.5},
    ]
    result = apply_feedback_adjustment("vpn configuration settings", memories)

    # helped's score is boosted by up to FEEDBACK_ADJUSTMENT_CAP (40%):
    # 0.5 * 1.4 = 0.7 > plain's untouched 0.6.
    assert [m["id"] for m in result] == [helped, plain]


def test_apply_feedback_adjustment_ignores_dissimilar_query(db_conn: sqlite3.Connection) -> None:
    """The issue's headline case, exercised through the full pipeline function."""
    mem_id = _insert_access_row(db_conn, "apply-dissimilar-test")
    record_contextual_feedback(db_conn, mem_id, "what's my favorite editor", "unhelpful")

    memories = [{"id": mem_id, "_rrf_score": 0.5}]
    result = apply_feedback_adjustment("what IDE did I mention last year", memories)

    assert result[0]["_rrf_score"] == 0.5
    assert "_feedback_adjustment" not in result[0]


# ---------------------------------------------------------------------------
# build_vitality_report (issue #14) — dashboard vitality visualization
# ---------------------------------------------------------------------------


def test_build_vitality_report_empty_store(db_conn: sqlite3.Connection) -> None:
    report = build_vitality_report(db_conn)

    assert report["total_memories"] == 0
    assert report["active_count"] == 0
    assert report["dormant_count"] == 0
    assert report["average_vitality"] == 0.0
    assert report["vault_health_score"] == "0%"
    assert report["decay_distribution"] == {}


def test_build_vitality_report_buckets_sum_to_total(db_conn: sqlite3.Connection) -> None:
    """Every memory lands in exactly one bucket (DI-04: the top bucket is open-ended)."""
    _insert_access_row(db_conn, "bucket-fresh-1")
    _insert_access_row(db_conn, "bucket-fresh-2", access_count=5)

    report = build_vitality_report(db_conn)

    assert sum(report["vitality_buckets"].values()) == report["total_memories"] == 2
    assert set(report["vitality_buckets"]) == {
        "0.00-0.05",
        "0.05-0.25",
        "0.25-0.50",
        "0.50-0.75",
        "0.75+",
    }


def test_build_vitality_report_fresh_memories_are_active(db_conn: sqlite3.Connection) -> None:
    _insert_access_row(db_conn, "active-report-test")

    report = build_vitality_report(db_conn)

    assert report["active_count"] == 1
    assert report["dormant_count"] == 0
    assert report["vault_health_score"] == "100%"
    assert report["vitality_buckets"]["0.75+"] == 1


def test_build_vitality_report_dormant_memory_counted(db_conn: sqlite3.Connection) -> None:
    """A memory decayed far enough in the past is dormant, not active."""
    from datetime import UTC, datetime, timedelta

    old = (datetime.now(UTC) - timedelta(days=3650)).isoformat()
    mem_id = _make_id("long-dormant-report-test")
    db_conn.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata,
           created_at, updated_at, accessed_at, access_count, decay_rate, vitality, base_weight, status, memory_type)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (mem_id, "long dormant report test", "general", "[]", "manual", "{}",
         old, old, old, 0, 0.1, 1.0, 1.0, "active", "unclassified"),
    )
    db_conn.commit()

    report = build_vitality_report(db_conn)

    assert report["dormant_count"] == 1
    assert report["active_count"] == 0
    assert report["vitality_buckets"]["0.00-0.05"] == 1


def test_build_vitality_report_decay_distribution_by_type(db_conn: sqlite3.Connection) -> None:
    mem_id = _insert_access_row(db_conn, "decision-report-test")
    db_conn.execute("UPDATE memories SET memory_type = 'decision' WHERE id = ?", (mem_id,))
    db_conn.commit()

    report = build_vitality_report(db_conn)

    assert report["decay_distribution"] == {"decision": 1}


def test_build_vitality_report_matches_mcp_tool_output(db_conn: sqlite3.Connection) -> None:
    """The extracted function's shape matches what remind_me_vitality_report returns."""
    import asyncio
    import json as _json

    from remind_me_mcp.models import ResponseFormat, VitalityReportInput
    from remind_me_mcp.tools.lifecycle import remind_me_vitality_report

    _insert_access_row(db_conn, "shared-shape-report-test")

    report = build_vitality_report(db_conn)
    tool_output = asyncio.run(
        remind_me_vitality_report(VitalityReportInput(response_format=ResponseFormat.JSON))
    )
    tool_data = _json.loads(tool_output)

    assert tool_data == report


# ---------------------------------------------------------------------------
# record_co_retrieval (issue #9) — co-retrieval reinforcement
# ---------------------------------------------------------------------------


def test_record_co_retrieval_creates_association(db_conn: sqlite3.Connection) -> None:
    a = _insert_access_row(db_conn, "co-retrieval-a")
    b = _insert_access_row(db_conn, "co-retrieval-b")

    touched = record_co_retrieval([a, b])

    assert touched == 1
    row = db_conn.execute(
        "SELECT weight FROM memory_associations WHERE memory_id_a = ? AND memory_id_b = ?",
        tuple(sorted((a, b))),
    ).fetchone()
    assert row["weight"] == 1


def test_record_co_retrieval_reinforces_on_repeat(db_conn: sqlite3.Connection) -> None:
    a = _insert_access_row(db_conn, "co-retrieval-reinforce-a")
    b = _insert_access_row(db_conn, "co-retrieval-reinforce-b")

    record_co_retrieval([a, b])
    record_co_retrieval([a, b])
    record_co_retrieval([a, b])

    row = db_conn.execute(
        "SELECT weight FROM memory_associations WHERE memory_id_a = ? AND memory_id_b = ?",
        tuple(sorted((a, b))),
    ).fetchone()
    assert row["weight"] == 3


def test_record_co_retrieval_canonical_pair_order(db_conn: sqlite3.Connection) -> None:
    """(a, b) and (b, a) reinforce the SAME row, not two separate ones."""
    a = _insert_access_row(db_conn, "co-retrieval-order-a")
    b = _insert_access_row(db_conn, "co-retrieval-order-b")

    record_co_retrieval([a, b])
    record_co_retrieval([b, a])

    count = db_conn.execute("SELECT COUNT(*) FROM memory_associations").fetchone()[0]
    assert count == 1
    row = db_conn.execute("SELECT weight FROM memory_associations").fetchone()
    assert row["weight"] == 2


def test_record_co_retrieval_caps_weight(db_conn: sqlite3.Connection) -> None:
    a = _insert_access_row(db_conn, "co-retrieval-cap-a")
    b = _insert_access_row(db_conn, "co-retrieval-cap-b")

    for _ in range(CO_RETRIEVAL_MAX_WEIGHT + 10):
        record_co_retrieval([a, b])

    row = db_conn.execute("SELECT weight FROM memory_associations").fetchone()
    assert row["weight"] == CO_RETRIEVAL_MAX_WEIGHT


def test_record_co_retrieval_single_id_is_noop(db_conn: sqlite3.Connection) -> None:
    a = _insert_access_row(db_conn, "co-retrieval-single")

    touched = record_co_retrieval([a])

    assert touched == 0
    assert db_conn.execute("SELECT COUNT(*) FROM memory_associations").fetchone()[0] == 0


def test_record_co_retrieval_empty_is_noop() -> None:
    assert record_co_retrieval([]) == 0


def test_record_co_retrieval_creates_all_pairs(db_conn: sqlite3.Connection) -> None:
    ids = [_insert_access_row(db_conn, f"co-retrieval-triple-{i}") for i in range(3)]

    touched = record_co_retrieval(ids)

    assert touched == 3  # 3 choose 2
    count = db_conn.execute("SELECT COUNT(*) FROM memory_associations").fetchone()[0]
    assert count == 3


def test_record_co_retrieval_pair_cap_bounds_large_result_sets(
    db_conn: sqlite3.Connection,
) -> None:
    """Only the first _CO_RETRIEVAL_PAIR_CAP (10) ids participate in pairing."""
    ids = [_insert_access_row(db_conn, f"co-retrieval-large-{i}") for i in range(20)]

    touched = record_co_retrieval(ids)

    assert touched == 45  # 10 choose 2, not 20 choose 2 (190)
    count = db_conn.execute("SELECT COUNT(*) FROM memory_associations").fetchone()[0]
    assert count == 45
