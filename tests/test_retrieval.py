"""Tests for remind_me_mcp.retrieval -- RRF ranking, token budget, and envelope."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from remind_me_mcp.retrieval import SearchEnvelope

# ---------------------------------------------------------------------------
# Helpers to build fake memory dicts
# ---------------------------------------------------------------------------

def _mem(mid: str, content: str = "x", created_at: str | None = None, **extra) -> dict:
    """Build a minimal memory dict for testing."""
    now = datetime.now(tz=UTC)
    return {
        "id": mid,
        "content": content,
        "category": "general",
        "tags": "",
        "source": "manual",
        "metadata": "{}",
        "created_at": created_at or now.isoformat(),
        "updated_at": now.isoformat(),
        **extra,
    }


_NOW = datetime.now(tz=UTC)


def _ts(days_ago: int) -> str:
    return (_NOW - timedelta(days=days_ago)).isoformat()


# ---------------------------------------------------------------------------
# RRF basic scoring
# ---------------------------------------------------------------------------


class TestRankRRF:
    """Tests for rank_rrf function."""

    def test_basic_rrf_three_memories(self):
        """Given 3 memories ranked [A,B,C] by keyword and [B,A,C] by semantic,
        A and B should tie (approximately), C should be lower."""
        from remind_me_mcp.retrieval import rank_rrf

        a = _mem("A", created_at=_ts(3))
        b = _mem("B", created_at=_ts(3))
        c = _mem("C", created_at=_ts(3))

        keyword = [a, b, c]
        semantic = [b, a, c]

        result = rank_rrf(keyword, semantic, k=60)

        scores = {m["id"]: m["_rrf_score"] for m in result}
        # A and B should have same keyword+semantic contribution
        assert abs(scores["A"] - scores["B"]) < 0.01
        assert scores["C"] < scores["A"]

    def test_rrf_attaches_rank_metadata(self):
        """Each memory dict should have _rrf_score, _keyword_rank, _semantic_rank, _recency_rank."""
        from remind_me_mcp.retrieval import rank_rrf

        a = _mem("A", created_at=_ts(0))
        result = rank_rrf([a], [a], k=60)

        assert len(result) == 1
        m = result[0]
        assert "_rrf_score" in m
        assert "_keyword_rank" in m
        assert "_semantic_rank" in m
        assert "_recency_rank" in m

    def test_rrf_recency_tiebreak(self):
        """Two memories with equal keyword+semantic ranks but different dates:
        the newer one should score higher."""
        from remind_me_mcp.retrieval import rank_rrf

        newer = _mem("NEW", created_at=_ts(0))
        older = _mem("OLD", created_at=_ts(100))

        keyword = [newer, older]
        semantic = [newer, older]

        result = rank_rrf(keyword, semantic, k=60)

        assert result[0]["id"] == "NEW"
        assert result[1]["id"] == "OLD"
        assert result[0]["_rrf_score"] > result[1]["_rrf_score"]

    def test_rrf_empty_inputs(self):
        """Empty keyword and semantic lists should return empty list."""
        from remind_me_mcp.retrieval import rank_rrf

        result = rank_rrf([], [])
        assert result == []

    def test_rrf_keyword_only(self):
        """When semantic results are empty, RRF uses keyword + recency only."""
        from remind_me_mcp.retrieval import rank_rrf

        a = _mem("A", created_at=_ts(0))
        b = _mem("B", created_at=_ts(1))

        result = rank_rrf([a, b], [], k=60)

        assert len(result) == 2
        # A is rank 1 in keyword and rank 1 in recency (newer)
        assert result[0]["id"] == "A"

    def test_rrf_semantic_only(self):
        """When keyword results are empty, RRF uses semantic + recency only."""
        from remind_me_mcp.retrieval import rank_rrf

        a = _mem("A", created_at=_ts(0))
        b = _mem("B", created_at=_ts(1))

        result = rank_rrf([], [a, b], k=60)

        assert len(result) == 2
        assert result[0]["id"] == "A"

    def test_rrf_preserves_extra_keys(self):
        """Memory dicts with extra keys (e.g. semantic_distance) should keep them."""
        from remind_me_mcp.retrieval import rank_rrf

        a = _mem("A", created_at=_ts(0), semantic_distance=0.3, _search_method="hybrid")
        result = rank_rrf([a], [a], k=60)

        assert result[0]["semantic_distance"] == 0.3
        assert result[0]["_search_method"] == "hybrid"

    def test_rrf_k_parameter_overrides_default(self):
        """Passing k=10 should change the scoring vs default k=60."""
        from remind_me_mcp.retrieval import rank_rrf

        a = _mem("A", created_at=_ts(0))
        b = _mem("B", created_at=_ts(0))

        r60 = rank_rrf([a, b], [b, a], k=60)
        r10 = rank_rrf([a, b], [b, a], k=10)

        # Scores should differ (higher k = more uniform scores)
        s60_a = [m for m in r60 if m["id"] == "A"][0]["_rrf_score"]
        s10_a = [m for m in r10 if m["id"] == "A"][0]["_rrf_score"]
        assert s60_a != s10_a

    def test_rrf_deduplicates_by_id(self):
        """Memories appearing in both lists should only appear once in output."""
        from remind_me_mcp.retrieval import rank_rrf

        a = _mem("A", created_at=_ts(0))
        result = rank_rrf([a], [a], k=60)
        assert len(result) == 1

    def test_rrf_hybrid_hit_keeps_semantic_distance(self):
        """A memory in both lists merges the semantic occurrence's keys (DI-05).

        The keyword-tier dict has no semantic_distance; the semantic-tier dict
        does. Dedup must not drop it.
        """
        from remind_me_mcp.retrieval import rank_rrf

        kw = _mem("A", created_at=_ts(0), _search_method="keyword")
        sem = _mem("A", created_at=_ts(0), semantic_distance=0.42, _search_method="semantic")

        result = rank_rrf([kw], [sem], k=60)

        assert len(result) == 1
        assert result[0]["semantic_distance"] == 0.42

    def test_rrf_merge_does_not_overwrite_first_occurrence(self):
        """Merging the second occurrence never clobbers non-null keys from the first."""
        from remind_me_mcp.retrieval import rank_rrf

        kw = _mem("A", created_at=_ts(0), _search_method="keyword", extra="first")
        sem = _mem("A", created_at=_ts(0), _search_method="semantic", extra="second")

        result = rank_rrf([kw], [sem], k=60)

        assert result[0]["_search_method"] == "keyword"
        assert result[0]["extra"] == "first"


# ---------------------------------------------------------------------------
# RRF_K env var
# ---------------------------------------------------------------------------


class TestRRFKConfig:
    """Tests for REMIND_ME_RRF_K env var configuration."""

    def test_rrf_k_default(self):
        """Default RRF_K should be 60."""
        # Ensure env var not set, then reimport
        os.environ.pop("REMIND_ME_RRF_K", None)
        import importlib

        import remind_me_mcp.retrieval as mod
        importlib.reload(mod)
        assert mod.RRF_K == 60

    def test_rrf_k_from_env(self):
        """REMIND_ME_RRF_K env var should override default."""
        os.environ["REMIND_ME_RRF_K"] = "42"
        try:
            import importlib

            import remind_me_mcp.retrieval as mod
            importlib.reload(mod)
            assert mod.RRF_K == 42
        finally:
            os.environ.pop("REMIND_ME_RRF_K", None)
            importlib.reload(mod)


# ---------------------------------------------------------------------------
# Token budget
# ---------------------------------------------------------------------------


class TestApplyTokenBudget:
    """Tests for apply_token_budget function."""

    def test_budget_trims_correctly(self):
        """3 memories with ~100, 200, 300 tokens; budget=350 returns first 2,
        trimming the third (300 tokens would push total to 600)."""
        from remind_me_mcp.retrieval import apply_token_budget

        mems = [
            _mem("A", content="x" * 400),   # ~100 tokens
            _mem("B", content="x" * 800),   # ~200 tokens
            _mem("C", content="x" * 1200),  # ~300 tokens
        ]

        env = apply_token_budget(mems, budget=350)

        assert env["returned"] == 2
        assert env["trimmed"] == 1
        assert env["tokens_used"] == 300  # 100 + 200
        assert env["budget"] == 350
        assert env["total_candidates"] == 3
        assert len(env["memories"]) == 2

    def test_budget_zero_means_unlimited(self):
        """Budget=0 should return all memories."""
        from remind_me_mcp.retrieval import apply_token_budget

        mems = [_mem("A", content="x" * 4000), _mem("B", content="x" * 4000)]
        env = apply_token_budget(mems, budget=0)

        assert env["returned"] == 2
        assert env["trimmed"] == 0

    def test_budget_empty_input(self):
        """Empty input returns envelope with all zeros."""
        from remind_me_mcp.retrieval import apply_token_budget

        env = apply_token_budget([], budget=800)

        assert env["total_candidates"] == 0
        assert env["returned"] == 0
        assert env["trimmed"] == 0
        assert env["tokens_used"] == 0
        assert env["budget"] == 800
        assert env["memories"] == []

    def test_budget_default_is_800(self):
        """MemorySearchInput.token_budget defaults to 800."""
        from remind_me_mcp.models import MemorySearchInput

        m = MemorySearchInput(query="test")
        assert m.token_budget == 800

    def test_budget_includes_first_item_even_if_over(self):
        """If the first item exceeds budget, it should still be included
        (at least 1 result returned)."""
        from remind_me_mcp.retrieval import apply_token_budget

        mems = [_mem("A", content="x" * 4000)]  # ~1000 tokens
        env = apply_token_budget(mems, budget=100)

        assert env["returned"] == 1
        assert env["trimmed"] == 0


# ---------------------------------------------------------------------------
# SearchEnvelope structure
# ---------------------------------------------------------------------------


class TestSearchEnvelope:
    """Tests for SearchEnvelope type."""

    def test_envelope_has_required_fields(self):
        """SearchEnvelope should have all 5 metadata fields plus memories."""
        env: SearchEnvelope = {
            "memories": [],
            "total_candidates": 0,
            "returned": 0,
            "trimmed": 0,
            "tokens_used": 0,
            "budget": 800,
        }
        assert set(env.keys()) == {"memories", "total_candidates", "returned", "trimmed", "tokens_used", "budget"}

    def test_envelope_from_apply_token_budget(self):
        """apply_token_budget should return a valid SearchEnvelope."""
        from remind_me_mcp.retrieval import apply_token_budget

        mems = [_mem("A", content="hello world")]
        env = apply_token_budget(mems, budget=800)

        required_keys = {"memories", "total_candidates", "returned", "trimmed", "tokens_used", "budget"}
        assert required_keys.issubset(set(env.keys()))


# ---------------------------------------------------------------------------
# 4-signal RRF with vitality
# ---------------------------------------------------------------------------


class TestRankRRFVitality:
    """Tests for the 4th RRF signal: vitality ranking."""

    def test_vitality_rank_assigned(self):
        """rank_rrf assigns _vitality_rank to each result."""
        from remind_me_mcp.retrieval import rank_rrf

        a = _mem("A", created_at=_ts(0), vitality=0.8)
        b = _mem("B", created_at=_ts(0), vitality=0.5)

        result = rank_rrf([a, b], [a, b], k=60)

        for m in result:
            assert "_vitality_rank" in m, f"Missing _vitality_rank on {m['id']}"

    def test_higher_vitality_gets_better_rank(self):
        """Higher vitality memories get lower (better) vitality_rank."""
        from remind_me_mcp.retrieval import rank_rrf

        high = _mem("HIGH", created_at=_ts(0), vitality=0.9)
        low = _mem("LOW", created_at=_ts(0), vitality=0.1)

        result = rank_rrf([high, low], [high, low], k=60)

        ranks = {m["id"]: m["_vitality_rank"] for m in result}
        assert ranks["HIGH"] < ranks["LOW"], (
            f"HIGH vitality should rank better: HIGH={ranks['HIGH']} LOW={ranks['LOW']}"
        )

    def test_rrf_score_includes_vitality_signal(self):
        """RRF score sums 4 reciprocal ranks (keyword, semantic, recency, vitality).

        Two memories with identical keyword/semantic/recency should differ
        in RRF score when vitality differs.
        """
        from remind_me_mcp.retrieval import rank_rrf

        # Same created_at so recency is a toss-up; same list positions
        high_v = _mem("HV", created_at=_ts(0), vitality=1.0)
        low_v = _mem("LV", created_at=_ts(0), vitality=0.01)

        result = rank_rrf([high_v, low_v], [high_v, low_v], k=60)
        scores = {m["id"]: m["_rrf_score"] for m in result}

        # HV has better vitality rank, so its RRF score should be higher
        assert scores["HV"] > scores["LV"], (
            f"Higher vitality should produce higher RRF: HV={scores['HV']} LV={scores['LV']}"
        )

    def test_vitality_default_1_for_missing_field(self):
        """Memories without a 'vitality' field default to 1.0 (backwards compatible)."""
        from remind_me_mcp.retrieval import rank_rrf

        # No vitality key at all
        a = _mem("A", created_at=_ts(0))
        b = _mem("B", created_at=_ts(0), vitality=0.5)

        result = rank_rrf([a, b], [a, b], k=60)

        ranks = {m["id"]: m["_vitality_rank"] for m in result}
        # A defaults to 1.0 which is higher than B's 0.5
        assert ranks["A"] < ranks["B"]


# ---------------------------------------------------------------------------
# build_debug_signals
# ---------------------------------------------------------------------------


class TestBuildDebugSignals:
    """Tests for build_debug_signals function."""

    def test_extracts_rank_signals(self):
        """build_debug_signals extracts _keyword_rank, _semantic_rank, _recency_rank, _vitality_rank."""
        from remind_me_mcp.retrieval import build_debug_signals

        mem = _mem("A", created_at=_ts(5))
        mem["_keyword_rank"] = 3
        mem["_semantic_rank"] = 1
        mem["_recency_rank"] = 2
        mem["_vitality_rank"] = 4

        signals = build_debug_signals(mem)

        assert signals["keyword_rank"] == 3
        assert signals["semantic_rank"] == 1
        assert signals["recency_rank"] == 2
        assert signals["vitality_rank"] == 4

    def test_computes_days_old(self):
        """build_debug_signals computes days_old from created_at ISO string."""
        from remind_me_mcp.retrieval import build_debug_signals

        mem = _mem("A", created_at=_ts(10))
        mem["_keyword_rank"] = 1
        mem["_semantic_rank"] = 1
        mem["_recency_rank"] = 1
        mem["_vitality_rank"] = 1

        signals = build_debug_signals(mem)

        assert signals["days_old"] == 10

    def test_returns_correct_keys(self):
        """build_debug_signals returns dict with exactly the 8 expected keys."""
        from remind_me_mcp.retrieval import build_debug_signals

        mem = _mem("A", created_at=_ts(0))
        mem["_keyword_rank"] = 1
        mem["_semantic_rank"] = 1
        mem["_recency_rank"] = 1
        mem["_vitality_rank"] = 1

        signals = build_debug_signals(mem)

        assert set(signals.keys()) == {
            "semantic_rank", "keyword_rank", "recency_rank", "vitality_rank",
            "rrf_score", "rerank_score", "search_method", "days_old",
        }

    def test_exposes_score_and_method_signals(self):
        """HY-05: the stripped internal fields are surfaced via debug_signals."""
        from remind_me_mcp.retrieval import build_debug_signals

        mem = _mem("A", created_at=_ts(0))
        mem["_keyword_rank"] = 1
        mem["_semantic_rank"] = 1
        mem["_recency_rank"] = 1
        mem["_vitality_rank"] = 1
        mem["_rrf_score"] = 0.123
        mem["_rerank_score"] = 4.56
        mem["_search_method"] = "hybrid"

        signals = build_debug_signals(mem)

        assert signals["rrf_score"] == 0.123
        assert signals["rerank_score"] == 4.56
        assert signals["search_method"] == "hybrid"

    def test_missing_created_at_returns_none_days_old(self):
        """build_debug_signals handles missing created_at gracefully (days_old=None)."""
        from remind_me_mcp.retrieval import build_debug_signals

        mem = {"id": "A", "content": "test", "_keyword_rank": 1, "_semantic_rank": 1, "_recency_rank": 1, "_vitality_rank": 1}
        # No created_at key

        signals = build_debug_signals(mem)

        assert signals["days_old"] is None

    def test_unparseable_created_at_returns_none_days_old(self):
        """build_debug_signals handles unparseable created_at gracefully."""
        from remind_me_mcp.retrieval import build_debug_signals

        mem = _mem("A", created_at="not-a-date")
        mem["_keyword_rank"] = 1
        mem["_semantic_rank"] = 1
        mem["_recency_rank"] = 1
        mem["_vitality_rank"] = 1

        signals = build_debug_signals(mem)

        assert signals["days_old"] is None


# ---------------------------------------------------------------------------
# compute_tier_breakdown
# ---------------------------------------------------------------------------


class TestComputeTierBreakdown:
    """Tests for compute_tier_breakdown function."""

    def test_counts_by_search_method(self):
        """compute_tier_breakdown counts memories by _search_method."""
        from remind_me_mcp.retrieval import compute_tier_breakdown

        mems = [
            {**_mem("A"), "_search_method": "keyword"},
            {**_mem("B"), "_search_method": "semantic"},
            {**_mem("C"), "_search_method": "hybrid"},
            {**_mem("D"), "_search_method": "keyword"},
        ]

        result = compute_tier_breakdown(mems)

        assert result["keyword"] == 2
        assert result["semantic"] == 1
        assert result["hybrid"] == 1

    def test_defaults_to_zero_for_missing_tiers(self):
        """compute_tier_breakdown returns 0 for tiers with no memories."""
        from remind_me_mcp.retrieval import compute_tier_breakdown

        mems = [{**_mem("A"), "_search_method": "keyword"}]

        result = compute_tier_breakdown(mems)

        assert result["keyword"] == 1
        assert result["semantic"] == 0
        assert result["hybrid"] == 0

    def test_empty_list_returns_all_zeros(self):
        """compute_tier_breakdown on empty list returns all zeros."""
        from remind_me_mcp.retrieval import compute_tier_breakdown

        result = compute_tier_breakdown([])

        assert result == {"keyword": 0, "semantic": 0, "hybrid": 0}
