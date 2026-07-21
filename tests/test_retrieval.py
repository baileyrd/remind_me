"""Tests for remind_me_mcp.retrieval -- RRF ranking, token budget, and envelope."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

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

    def test_rrf_fusion_defaults_to_rank(self):
        """Issue #49: score fusion is opt-in, so the default stays 'rank'."""
        os.environ.pop("REMIND_ME_RRF_FUSION", None)
        import importlib

        import remind_me_mcp.retrieval as mod
        importlib.reload(mod)
        assert mod.RRF_FUSION == "rank"

    def test_rrf_fusion_from_env(self):
        os.environ["REMIND_ME_RRF_FUSION"] = "score"
        try:
            import importlib

            import remind_me_mcp.retrieval as mod
            importlib.reload(mod)
            assert mod.RRF_FUSION == "score"
        finally:
            os.environ.pop("REMIND_ME_RRF_FUSION", None)
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


class TestRankRRFIdf:
    """Tests for the 5th (opt-in) RRF signal: IDF ranking via bm25() score."""

    def test_idf_rank_assigned(self):
        """rank_rrf assigns _idf_rank to each result."""
        from remind_me_mcp.retrieval import rank_rrf

        a = _mem("A", created_at=_ts(0), _bm25_score=-5.0)
        b = _mem("B", created_at=_ts(0), _bm25_score=-2.0)

        result = rank_rrf([a, b], [a, b], k=60)

        for m in result:
            assert "_idf_rank" in m, f"Missing _idf_rank on {m['id']}"

    def test_lower_bm25_score_gets_better_rank(self):
        """bm25() is lower-is-better; a lower score should get a better (lower) idf_rank."""
        from remind_me_mcp.retrieval import rank_rrf

        strong_match = _mem("STRONG", created_at=_ts(0), _bm25_score=-8.0)
        weak_match = _mem("WEAK", created_at=_ts(0), _bm25_score=-1.0)

        result = rank_rrf([strong_match, weak_match], [strong_match, weak_match], k=60)

        ranks = {m["id"]: m["_idf_rank"] for m in result}
        assert ranks["STRONG"] < ranks["WEAK"]

    def test_missing_bm25_score_sorts_last(self):
        """Semantic-only hits (no _bm25_score) get the worst idf_rank."""
        from remind_me_mcp.retrieval import rank_rrf

        has_score = _mem("SCORED", created_at=_ts(0), _bm25_score=-3.0)
        no_score = _mem("UNSCORED", created_at=_ts(0))  # semantic-only, no FTS hit

        result = rank_rrf([has_score], [has_score, no_score], k=60)

        ranks = {m["id"]: m["_idf_rank"] for m in result}
        assert ranks["SCORED"] < ranks["UNSCORED"]

    def test_idf_weight_defaults_to_zero(self):
        """With the default (opt-in-off) w_idf, differing bm25 scores don't move the RRF score."""
        from remind_me_mcp.retrieval import rank_rrf

        strong_match = _mem("STRONG", created_at=_ts(0), _bm25_score=-8.0)
        weak_match = _mem("WEAK", created_at=_ts(0), _bm25_score=-1.0)

        # Crossed keyword/semantic order so kw+sem contributions tie exactly;
        # recency/vitality disabled so only idf could break the tie.
        result = rank_rrf(
            [strong_match, weak_match],
            [weak_match, strong_match],
            k=60,
            w_recency=0.0,
            w_vitality=0.0,
        )
        scores = {m["id"]: m["_rrf_score"] for m in result}

        # Despite differing bm25, scores tie because w_idf defaults to 0.
        assert scores["STRONG"] == scores["WEAK"]

    def test_idf_weight_override_favors_stronger_match(self):
        """A positive w_idf makes the stronger bm25 match score higher."""
        from remind_me_mcp.retrieval import rank_rrf

        strong_match = _mem("STRONG", created_at=_ts(0), _bm25_score=-8.0)
        weak_match = _mem("WEAK", created_at=_ts(0), _bm25_score=-1.0)

        result = rank_rrf(
            [strong_match, weak_match], [strong_match, weak_match], k=60, w_idf=1.0
        )
        scores = {m["id"]: m["_rrf_score"] for m in result}

        assert scores["STRONG"] > scores["WEAK"]


# ---------------------------------------------------------------------------
# _minmax_normalize
# ---------------------------------------------------------------------------


class TestMinMaxNormalize:
    """Tests for the _minmax_normalize helper (score-fusion building block)."""

    def test_normalizes_to_zero_one_range(self):
        from remind_me_mcp.retrieval import _minmax_normalize

        result = _minmax_normalize({"A": 0.0, "B": 5.0, "C": 10.0})
        assert result == {"A": 0.0, "B": 0.5, "C": 1.0}

    def test_invert_flips_direction(self):
        """invert=True: the lowest raw value gets the highest (best) score."""
        from remind_me_mcp.retrieval import _minmax_normalize

        result = _minmax_normalize({"A": 0.0, "B": 5.0, "C": 10.0}, invert=True)
        assert result == {"A": 1.0, "B": 0.5, "C": 0.0}

    def test_empty_input_returns_empty(self):
        from remind_me_mcp.retrieval import _minmax_normalize

        assert _minmax_normalize({}) == {}

    def test_all_tied_values_score_one(self):
        """No spread to normalize -- every id gets 1.0 rather than a division by zero."""
        from remind_me_mcp.retrieval import _minmax_normalize

        assert _minmax_normalize({"A": 3.0, "B": 3.0}) == {"A": 1.0, "B": 1.0}

    def test_single_value_scores_one(self):
        from remind_me_mcp.retrieval import _minmax_normalize

        assert _minmax_normalize({"A": 7.0}) == {"A": 1.0}


# ---------------------------------------------------------------------------
# Score-based fusion (issue #49)
# ---------------------------------------------------------------------------


class TestRankRRFScoreFusion:
    """Tests for fusion='score' -- normalized-magnitude fusion instead of rank-only."""

    def test_defaults_to_rank_mode(self):
        """Without an explicit fusion arg (and RRF_FUSION unset), behavior is unchanged."""
        from remind_me_mcp.retrieval import rank_rrf

        a = _mem("A", created_at=_ts(0), _bm25_score=-8.0)
        b = _mem("B", created_at=_ts(0), _bm25_score=-1.0)

        result = rank_rrf([a, b], [a, b], k=60)
        assert "_fusion_mode" not in result[0]
        assert "_keyword_score" not in result[0]

    def test_score_mode_preserves_strong_match_magnitude(self):
        """The headline case: a much stronger semantic match must outrank a
        weaker one even if rank-only fusion would have tied them (adjacent
        rank positions), because score mode reads the real magnitude."""
        from remind_me_mcp.retrieval import rank_rrf

        strong = _mem("STRONG", created_at=_ts(0), semantic_distance=0.05)
        weak = _mem("WEAK", created_at=_ts(0), semantic_distance=0.95)

        # Adjacent rank positions (1 and 2) -- rank-only RRF would barely
        # distinguish them; score mode should read the large distance gap.
        result = rank_rrf(
            [], [strong, weak], k=60, fusion="score", w_recency=0.0, w_vitality=0.0
        )
        scores = {m["id"]: m["_rrf_score"] for m in result}
        assert scores["STRONG"] > scores["WEAK"]
        # The gap should be substantial (near the full normalized range),
        # unlike rank-only fusion's tiny 1/(k+1) vs 1/(k+2) difference.
        assert scores["STRONG"] - scores["WEAK"] > 0.5

    def test_score_mode_sets_score_debug_fields(self):
        from remind_me_mcp.retrieval import rank_rrf

        a = _mem("A", created_at=_ts(0), _bm25_score=-5.0, semantic_distance=0.2)
        result = rank_rrf([a], [a], k=60, fusion="score")

        m = result[0]
        assert m["_fusion_mode"] == "score"
        assert m["_keyword_score"] == pytest.approx(1.0)  # only candidate -> tied -> 1.0
        assert m["_semantic_score"] == pytest.approx(1.0)
        assert "_recency_score" in m
        assert "_vitality_score" in m

    def test_score_mode_still_sets_rank_fields(self):
        """Rank fields stay populated in score mode too, for debug/back-compat."""
        from remind_me_mcp.retrieval import rank_rrf

        a = _mem("A", created_at=_ts(0))
        result = rank_rrf([a], [a], k=60, fusion="score")

        m = result[0]
        assert "_keyword_rank" in m
        assert "_semantic_rank" in m
        assert "_recency_rank" in m
        assert "_vitality_rank" in m
        assert "_idf_rank" in m

    def test_missing_signal_scores_zero_not_dropped(self):
        """A semantic-only hit (no _bm25_score) gets keyword_score=0.0, the
        worst possible score -- mirroring rank mode's penalty-rank treatment,
        but it must still appear in the results."""
        from remind_me_mcp.retrieval import rank_rrf

        scored = _mem("SCORED", created_at=_ts(0), _bm25_score=-3.0)
        unscored = _mem("UNSCORED", created_at=_ts(0))  # semantic-only

        result = rank_rrf([scored], [scored, unscored], k=60, fusion="score")

        by_id = {m["id"]: m for m in result}
        assert by_id["UNSCORED"]["_keyword_score"] == 0.0
        assert by_id["SCORED"]["_keyword_score"] > 0.0

    def test_recency_score_favors_newer(self):
        from remind_me_mcp.retrieval import rank_rrf

        newer = _mem("NEW", created_at=_ts(0))
        older = _mem("OLD", created_at=_ts(365))

        result = rank_rrf(
            [newer, older], [newer, older], k=60, fusion="score", w_keyword=0.0, w_semantic=0.0
        )
        scores = {m["id"]: m["_rrf_score"] for m in result}
        assert scores["NEW"] > scores["OLD"]

    def test_vitality_score_favors_higher_vitality(self):
        from remind_me_mcp.retrieval import rank_rrf

        high = _mem("HIGH", created_at=_ts(0), vitality=0.9)
        low = _mem("LOW", created_at=_ts(0), vitality=0.1)

        result = rank_rrf(
            [high, low], [high, low], k=60, fusion="score",
            w_keyword=0.0, w_semantic=0.0, w_recency=0.0,
        )
        scores = {m["id"]: m["_rrf_score"] for m in result}
        assert scores["HIGH"] > scores["LOW"]

    def test_idf_weight_reuses_keyword_score_in_score_mode(self):
        """w_idf has no separate signal in score mode -- it's the same
        underlying bm25 magnitude as w_keyword, just an extra multiplier."""
        from remind_me_mcp.retrieval import rank_rrf

        a = _mem("A", created_at=_ts(0), _bm25_score=-8.0)
        b = _mem("B", created_at=_ts(0), _bm25_score=-1.0)

        result = rank_rrf(
            [a, b], [a, b], k=60, fusion="score",
            w_keyword=1.0, w_idf=1.0, w_semantic=0.0, w_recency=0.0, w_vitality=0.0,
        )
        by_id = {m["id"]: m for m in result}
        # Both weights apply the same normalized keyword_score, so the score
        # is exactly double the keyword_score alone.
        assert by_id["A"]["_rrf_score"] == pytest.approx(2 * by_id["A"]["_keyword_score"])

    def test_rrf_fusion_env_default_applies(self, monkeypatch):
        """With fusion=None, the module-level RRF_FUSION constant is used."""
        import remind_me_mcp.retrieval as retr

        monkeypatch.setattr(retr, "RRF_FUSION", "score")
        a = _mem("A", created_at=_ts(0), _bm25_score=-5.0)
        result = retr.rank_rrf([a], [a], k=60)
        assert result[0]["_fusion_mode"] == "score"

    def test_zero_candidates_returns_empty_in_score_mode(self):
        from remind_me_mcp.retrieval import rank_rrf

        assert rank_rrf([], [], fusion="score") == []


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
        """build_debug_signals returns dict with exactly the 9 expected keys."""
        from remind_me_mcp.retrieval import build_debug_signals

        mem = _mem("A", created_at=_ts(0))
        mem["_keyword_rank"] = 1
        mem["_semantic_rank"] = 1
        mem["_recency_rank"] = 1
        mem["_vitality_rank"] = 1
        mem["_idf_rank"] = 1

        signals = build_debug_signals(mem)

        assert set(signals.keys()) == {
            "semantic_rank", "keyword_rank", "recency_rank", "vitality_rank",
            "idf_rank", "rrf_score", "rerank_score", "search_method", "days_old",
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

    def test_strategy_and_weights_omitted_by_default(self):
        """Pre-Phase-6 callers (no strategy/weights args) see no new keys."""
        from remind_me_mcp.retrieval import build_debug_signals

        mem = _mem("A")

        signals = build_debug_signals(mem)

        assert "strategy" not in signals
        assert "weights_used" not in signals

    def test_strategy_and_weights_included_when_provided(self):
        from remind_me_mcp.retrieval import build_debug_signals

        mem = _mem("A")
        weights = {"w_keyword": 1.5, "w_semantic": 0.5}

        signals = build_debug_signals(mem, strategy="keyword_favored", weights=weights)

        assert signals["strategy"] == "keyword_favored"
        assert signals["weights_used"] == weights

    def test_fusion_score_fields_omitted_for_rank_mode(self):
        """A rank-mode result (no _fusion_mode key) adds no new debug keys —
        preserves the exact-9-key contract for pre-#49 callers."""
        from remind_me_mcp.retrieval import build_debug_signals

        mem = _mem("A", created_at=_ts(0))
        mem["_keyword_rank"] = 1
        mem["_semantic_rank"] = 1
        mem["_recency_rank"] = 1
        mem["_vitality_rank"] = 1
        mem["_idf_rank"] = 1

        signals = build_debug_signals(mem)

        assert "fusion_mode" not in signals
        assert "keyword_score" not in signals

    def test_fusion_score_fields_included_when_present(self):
        """Issue #49: score-mode results surface their normalized magnitudes."""
        from remind_me_mcp.retrieval import build_debug_signals

        mem = _mem("A", created_at=_ts(0))
        mem["_fusion_mode"] = "score"
        mem["_keyword_score"] = 0.8
        mem["_semantic_score"] = 0.6
        mem["_recency_score"] = 0.4
        mem["_vitality_score"] = 0.2

        signals = build_debug_signals(mem)

        assert signals["fusion_mode"] == "score"
        assert signals["keyword_score"] == 0.8
        assert signals["semantic_score"] == 0.6
        assert signals["recency_score"] == 0.4
        assert signals["vitality_score"] == 0.2

    def test_feedback_adjustment_omitted_by_default(self):
        """Issue #54: no new keys unless the memory actually carries a
        _feedback_adjustment (i.e. matching feedback was found)."""
        from remind_me_mcp.retrieval import build_debug_signals

        mem = _mem("A", created_at=_ts(0))

        signals = build_debug_signals(mem)

        assert "feedback_adjustment" not in signals

    def test_feedback_adjustment_included_when_present(self):
        from remind_me_mcp.retrieval import build_debug_signals

        mem = _mem("A", created_at=_ts(0))
        mem["_feedback_adjustment"] = 0.25

        signals = build_debug_signals(mem)

        assert signals["feedback_adjustment"] == 0.25


# ---------------------------------------------------------------------------
# choose_rrf_weights / resolve_strategy_weights (Phase 6: auto-routing)
# ---------------------------------------------------------------------------


class TestChooseRrfWeights:
    """Tests for the deterministic query-shape heuristic router.

    resolve_strategy_weights() reads the live RRF_W_* module constants, so
    every case here monkeypatches them to a fixed, known baseline first —
    tests must not depend on whatever the ambient default happens to be.
    """

    def _set_weights(self, monkeypatch, **overrides):
        import remind_me_mcp.retrieval as retr

        defaults = {"RRF_W_KEYWORD": 1.0, "RRF_W_SEMANTIC": 1.0, "RRF_W_RECENCY": 1.0, "RRF_W_VITALITY": 1.0, "RRF_W_IDF": 0.0}
        defaults.update(overrides)
        for name, value in defaults.items():
            monkeypatch.setattr(retr, name, value)

    def test_short_query_favors_keyword(self, monkeypatch):
        from remind_me_mcp.retrieval import choose_rrf_weights

        self._set_weights(monkeypatch)
        expected = {"w_keyword": 1.5, "w_semantic": 0.5, "w_recency": 1.0, "w_vitality": 1.0, "w_idf": 0.0}
        assert choose_rrf_weights("tailscale") == expected
        assert choose_rrf_weights("vpn setup") == expected

    def test_quoted_phrase_favors_keyword(self, monkeypatch):
        from remind_me_mcp.retrieval import choose_rrf_weights

        self._set_weights(monkeypatch)
        weights = choose_rrf_weights('"exact phrase match" in a longer natural sentence')
        assert weights["w_keyword"] == 1.5
        assert weights["w_semantic"] == 0.5

    def test_prefix_wildcard_favors_keyword(self, monkeypatch):
        from remind_me_mcp.retrieval import choose_rrf_weights

        self._set_weights(monkeypatch)
        weights = choose_rrf_weights("deploy* related notes from last quarter")
        assert weights["w_keyword"] == 1.5
        assert weights["w_semantic"] == 0.5

    def test_structured_always_favors_keyword_regardless_of_shape(self, monkeypatch):
        from remind_me_mcp.retrieval import choose_rrf_weights

        self._set_weights(monkeypatch)
        long_natural_language = "how did we decide to configure the VPN last month?"
        weights = choose_rrf_weights(long_natural_language, structured=True)
        assert weights["w_keyword"] == 1.5
        assert weights["w_semantic"] == 0.5

    def test_no_semantic_always_favors_keyword_regardless_of_shape(self, monkeypatch):
        from remind_me_mcp.retrieval import choose_rrf_weights

        self._set_weights(monkeypatch)
        long_natural_language = "how did we decide to configure the VPN last month?"
        weights = choose_rrf_weights(long_natural_language, has_semantic=False)
        assert weights["w_keyword"] == 1.5
        assert weights["w_semantic"] == 0.5

    def test_long_natural_language_query_favors_semantic(self, monkeypatch):
        from remind_me_mcp.retrieval import choose_rrf_weights

        self._set_weights(monkeypatch)
        weights = choose_rrf_weights("what did we decide about the VPN configuration last month")
        assert weights["w_keyword"] == 0.5
        assert weights["w_semantic"] == 1.5

    def test_question_shaped_query_favors_semantic(self, monkeypatch):
        from remind_me_mcp.retrieval import choose_rrf_weights

        self._set_weights(monkeypatch)
        weights = choose_rrf_weights("why is the vpn slow?")
        assert weights["w_keyword"] == 0.5
        assert weights["w_semantic"] == 1.5

    def test_mid_length_non_question_query_is_balanced(self, monkeypatch):
        from remind_me_mcp.retrieval import choose_rrf_weights

        self._set_weights(monkeypatch)
        # 3-5 words, no quotes/wildcards/question mark/temporal expression: no heuristic fires.
        weights = choose_rrf_weights("vpn config network diagnostics")
        assert weights == {"w_keyword": 1.0, "w_semantic": 1.0, "w_recency": 1.0, "w_vitality": 1.0, "w_idf": 0.0}

    def test_balanced_reproduces_live_defaults_exactly(self, monkeypatch):
        """Splatting the balanced resolution into rank_rrf is identical to
        no override at all — the "auto"-safe guarantee for typical queries."""
        import remind_me_mcp.retrieval as retr
        from remind_me_mcp.retrieval import resolve_strategy_weights

        self._set_weights(monkeypatch, RRF_W_IDF=1.0)  # a non-default value, to prove it's read live
        keyword = [_mem("A"), _mem("B")]
        semantic = [_mem("B"), _mem("A")]

        default = retr.rank_rrf(keyword, semantic)
        with_balanced = retr.rank_rrf(keyword, semantic, **resolve_strategy_weights("balanced"))

        assert [m["id"] for m in default] == [m["id"] for m in with_balanced]
        assert [m["_rrf_score"] for m in default] == [m["_rrf_score"] for m in with_balanced]

    def test_favored_presets_never_resurrect_a_profile_zeroed_signal(self, monkeypatch):
        """Regression guard: benchmarks/runner.py's --rrf-profile monkeypatches
        RRF_W_KEYWORD/RECENCY/VITALITY to 0 directly. A keyword/semantic
        rebalance must not silently un-zero a signal a profile deliberately
        dropped (0 * multiplier == 0, not the multiplier's raw value)."""
        from remind_me_mcp.retrieval import resolve_strategy_weights

        self._set_weights(monkeypatch, RRF_W_KEYWORD=0.0, RRF_W_RECENCY=0.0, RRF_W_VITALITY=0.0)

        keyword_favored = resolve_strategy_weights("keyword_favored")
        assert keyword_favored["w_keyword"] == 0.0  # stays zeroed, not bumped to 1.5x-of-zero-but-nonzero
        assert keyword_favored["w_recency"] == 0.0
        assert keyword_favored["w_vitality"] == 0.0

        semantic_favored = resolve_strategy_weights("semantic_favored")
        assert semantic_favored["w_keyword"] == 0.0
        assert semantic_favored["w_recency"] == 0.0
        assert semantic_favored["w_vitality"] == 0.0

    def test_strategy_presets_keys_match_rank_rrf_kwargs(self):
        """Every resolved preset is directly splattable into rank_rrf's weight kwargs."""
        import inspect

        from remind_me_mcp.retrieval import STRATEGY_PRESETS, rank_rrf, resolve_strategy_weights

        rank_rrf_kwargs = set(inspect.signature(rank_rrf).parameters) - {
            "keyword_results", "semantic_results", "k",
        }
        for name in STRATEGY_PRESETS:
            assert set(resolve_strategy_weights(name)) <= rank_rrf_kwargs, f"{name} has unexpected keys"


# ---------------------------------------------------------------------------
# Temporal-expression detection (issue #52)
# ---------------------------------------------------------------------------


class TestTemporalDetection:
    """Tests for _looks_temporal_shaped and its composition into choose_rrf_weights."""

    def _set_weights(self, monkeypatch, **overrides):
        import remind_me_mcp.retrieval as retr

        defaults = {"RRF_W_KEYWORD": 1.0, "RRF_W_SEMANTIC": 1.0, "RRF_W_RECENCY": 1.0, "RRF_W_VITALITY": 1.0, "RRF_W_IDF": 0.0}
        defaults.update(overrides)
        for name, value in defaults.items():
            monkeypatch.setattr(retr, name, value)

    @pytest.mark.parametrize(
        "query",
        [
            "before I moved to Seattle",
            "what did I do when I lived in Seattle",
            "last summer's vacation plans",
            "trip we took last year",
            "notes from last monday",
            "what happened this winter",
            "plans for next spring",
            "what did I say yesterday",
            "reminders set for tomorrow",
            "conversation from 2019",
            "meeting in january about budgets",
            "things since I started this job",
            "what changed after the migration",
            "topics discussed during onboarding",
            "a decision made 3 years ago",
        ],
    )
    def test_detects_temporal_expressions(self, query):
        from remind_me_mcp.retrieval import _looks_temporal_shaped

        assert _looks_temporal_shaped(query), f"expected temporal match: {query!r}"

    @pytest.mark.parametrize(
        "query",
        [
            "vpn configuration settings",
            "favorite pizza toppings",
            "how do I reset my password",
            "notes about the tailscale setup",
        ],
    )
    def test_non_temporal_queries_not_flagged(self, query):
        from remind_me_mcp.retrieval import _looks_temporal_shaped

        assert not _looks_temporal_shaped(query), f"unexpected temporal match: {query!r}"

    def test_may_excluded_to_avoid_modal_verb_false_positives(self):
        """'may' as a month is deliberately omitted -- 'may I ask' etc. would
        otherwise be a disproportionate false-positive source."""
        from remind_me_mcp.retrieval import _looks_temporal_shaped

        assert not _looks_temporal_shaped("may I get a coffee recommendation")

    def test_temporal_boosts_recency_on_keyword_favored_base(self, monkeypatch):
        """Short + temporal: keyword-favored base, recency additionally boosted."""
        from remind_me_mcp.retrieval import choose_rrf_weights

        self._set_weights(monkeypatch)
        weights = choose_rrf_weights("last summer")
        assert weights["w_keyword"] == 1.5  # keyword_favored base, unaffected
        assert weights["w_semantic"] == 0.5
        assert weights["w_recency"] == pytest.approx(1.5)  # 1.0 * 1.5 temporal boost

    def test_temporal_boosts_recency_on_semantic_favored_base(self, monkeypatch):
        """Long/question-shaped + temporal: semantic-favored base, recency also boosted."""
        from remind_me_mcp.retrieval import choose_rrf_weights

        self._set_weights(monkeypatch)
        weights = choose_rrf_weights("what did I do when I lived in Seattle last year")
        assert weights["w_keyword"] == 0.5
        assert weights["w_semantic"] == 1.5
        assert weights["w_recency"] == pytest.approx(1.5)

    def test_temporal_boosts_recency_on_balanced_base(self, monkeypatch):
        """Mid-length, non-question + temporal: balanced base, recency also boosted."""
        from remind_me_mcp.retrieval import choose_rrf_weights

        self._set_weights(monkeypatch)
        weights = choose_rrf_weights("vacation photos from last spring")
        assert weights["w_keyword"] == 1.0
        assert weights["w_semantic"] == 1.0
        assert weights["w_recency"] == pytest.approx(1.5)

    def test_non_temporal_query_recency_unaffected(self, monkeypatch):
        from remind_me_mcp.retrieval import choose_rrf_weights

        self._set_weights(monkeypatch)
        weights = choose_rrf_weights("vpn config network diagnostics")
        assert weights["w_recency"] == 1.0

    def test_temporal_boost_respects_zeroed_recency_profile(self, monkeypatch):
        """Regression guard mirroring test_favored_presets_never_resurrect_a_profile_zeroed_signal:
        a profile that zeroed recency (e.g. --rrf-profile retrieval/semantic)
        must stay zeroed -- 0 * 1.5 == 0, not resurrected."""
        from remind_me_mcp.retrieval import choose_rrf_weights

        self._set_weights(monkeypatch, RRF_W_RECENCY=0.0)
        weights = choose_rrf_weights("last summer")
        assert weights["w_recency"] == 0.0


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
