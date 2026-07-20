"""
Tests for configurable RRF signal weights.

The headline case is deterministic: a less-relevant but newer/higher-vitality
memory outranks the relevant one *only* because recency+vitality are weighted
equally with keyword/semantic. Dropping those two weights (the "retrieval
profile") restores relevance ordering.
"""

from __future__ import annotations

import remind_me_mcp.retrieval as retr
from benchmarks.before_after import _capture_state, _restore_state, _score
from benchmarks.synthetic import make_dataset


def _mem(mid: str, created_at: str, vitality: float) -> dict:
    return {"id": mid, "content": mid, "created_at": created_at, "vitality": vitality}


def _case() -> tuple[list[dict], list[dict]]:
    # A is the most relevant (rank 1 in both lists) but oldest + lowest vitality.
    # B is less relevant (rank 2) but newest + highest vitality.
    a = _mem("A", "2020-01-01T00:00:00+00:00", 0.2)
    b = _mem("B", "2021-01-01T00:00:00+00:00", 0.9)
    c = _mem("C", "2020-06-01T00:00:00+00:00", 0.5)
    keyword = [a, b, c]
    semantic = [a, b, c]
    return keyword, semantic


def test_default_weights_let_recency_vitality_win():
    keyword, semantic = _case()
    ranked = retr.rank_rrf(keyword, semantic)
    # With all four signals weighted equally, the newer/high-vitality distractor
    # B is pushed to the top despite A being more relevant.
    assert ranked[0]["id"] == "B"


def test_dropping_recency_vitality_restores_relevance():
    keyword, semantic = _case()
    ranked = retr.rank_rrf(keyword, semantic, w_recency=0.0, w_vitality=0.0)
    # Relevance (keyword+semantic) alone now decides — A wins.
    assert ranked[0]["id"] == "A"


def test_weights_default_to_module_constants(monkeypatch):
    keyword, semantic = _case()
    monkeypatch.setattr(retr, "RRF_W_RECENCY", 0.0)
    monkeypatch.setattr(retr, "RRF_W_VITALITY", 0.0)
    ranked = retr.rank_rrf(keyword, semantic)  # no explicit weights -> uses module constants
    assert ranked[0]["id"] == "A"


def test_idf_weight_defaults_to_zero_module_constant():
    """REMIND_ME_RRF_W_IDF is opt-in: the module constant defaults to 0.0,
    unlike the other four RRF_W_* constants which default to 1.0."""
    assert retr.RRF_W_IDF == 0.0


def test_idf_weight_module_constant_overridable(monkeypatch):
    keyword, semantic = _case()
    a, b, _c = keyword
    a["_bm25_score"] = -1.0  # weak match
    b["_bm25_score"] = -9.0  # strong match
    monkeypatch.setattr(retr, "RRF_W_IDF", 5.0)
    monkeypatch.setattr(retr, "RRF_W_RECENCY", 0.0)
    monkeypatch.setattr(retr, "RRF_W_VITALITY", 0.0)
    ranked = retr.rank_rrf(keyword, semantic)  # no explicit weights -> uses module constants
    # B has the stronger bm25 match, and a heavily-weighted IDF signal now
    # dominates over the tied keyword/semantic ranks.
    assert ranked[0]["id"] == "B"


async def test_rrf_comparison_runs_and_restores_state():
    """The before_after 'rrf' comparison runs and leaves module weights unchanged."""
    items = make_dataset(4)
    before_weights = (retr.RRF_W_RECENCY, retr.RRF_W_VITALITY)

    state = _capture_state("rrf")
    try:
        before = await _score(items, "verbatim", "none", [1, 3], 50, "rrf", "before")
        after = await _score(items, "verbatim", "none", [1, 3], 50, "rrf", "after")
    finally:
        _restore_state("rrf", state)

    assert before["overall"].count == after["overall"].count
    for bucket in (before["overall"], after["overall"]):
        assert 0.0 <= bucket.recall(1) <= 1.0
    # Weights restored to whatever they were before the comparison.
    assert before_weights == (retr.RRF_W_RECENCY, retr.RRF_W_VITALITY)
