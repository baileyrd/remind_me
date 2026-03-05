"""Tests for remind_me_mcp.retrieval -- RRF ranking, token budget, and envelope."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest


# ---------------------------------------------------------------------------
# Helpers to build fake memory dicts
# ---------------------------------------------------------------------------

def _mem(mid: str, content: str = "x", created_at: str | None = None, **extra) -> dict:
    """Build a minimal memory dict for testing."""
    now = datetime.now(tz=timezone.utc)
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


_NOW = datetime.now(tz=timezone.utc)


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
        from remind_me_mcp.retrieval import SearchEnvelope

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
