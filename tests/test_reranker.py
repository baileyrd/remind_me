"""
Tests for the cross-encoder reranker (lever D).

The headline case is deterministic: RRF orders candidates by fused list ranks
and never reads the query and a candidate together, so a distractor can sit at
#1. A cross-encoder scoring (query, content) pairs jointly demotes it. The
scorer is injectable, so these tests prove the reordering logic without any
model download; the ONNX engine itself degrades gracefully when unavailable.
"""

from __future__ import annotations

import numpy as np
import pytest

import remind_me_mcp.reranker as rr


def _mems(*ids: str) -> list[dict]:
    return [{"id": i, "content": f"content of {i}"} for i in ids]


def _scorer_preferring(*ordered_ids: str):
    """Build a scorer that scores texts by the position of their id in *ordered_ids*."""

    def scorer(query: str, texts: list[str]) -> np.ndarray:
        def score(text: str) -> float:
            for rank, mid in enumerate(ordered_ids):
                if mid in text:
                    return float(len(ordered_ids) - rank)
            return 0.0

        return np.array([score(t) for t in texts], dtype=np.float32)

    return scorer


def test_rerank_reorders_head_by_score():
    memories = _mems("A", "B", "C")
    ranked = rr.rerank("q", memories, top_k=3, scorer=_scorer_preferring("C", "B", "A"))
    assert [m["id"] for m in ranked] == ["C", "B", "A"]


def test_rerank_annotates_scores_and_preserves_tail():
    memories = _mems("A", "B", "C", "D", "E")
    ranked = rr.rerank("q", memories, top_k=3, scorer=_scorer_preferring("B", "A", "C"))
    # Head reordered by score; D and E never rescored, keep RRF order.
    assert [m["id"] for m in ranked] == ["B", "A", "C", "D", "E"]
    assert all("_rerank_score" in m for m in ranked[:3])
    assert all("_rerank_score" not in m for m in ranked[3:])
    assert len(ranked) == len(memories)


def test_rerank_stable_on_score_ties():
    memories = _mems("A", "B", "C")
    flat = lambda q, texts: np.zeros(len(texts), dtype=np.float32)  # noqa: E731
    ranked = rr.rerank("q", memories, top_k=3, scorer=flat)
    # Equal scores keep the incoming RRF order.
    assert [m["id"] for m in ranked] == ["A", "B", "C"]


def test_rerank_short_head_is_noop():
    memories = _mems("A")

    def explode(query, texts):  # pragma: no cover - must never be called
        raise AssertionError("scorer should not run for a single candidate")

    ranked = rr.rerank("q", memories, top_k=5, scorer=explode)
    assert [m["id"] for m in ranked] == ["A"]


def test_rerank_without_engine_keeps_order(monkeypatch):
    monkeypatch.setattr(rr, "_get_reranker", lambda: None)
    memories = _mems("A", "B", "C")
    ranked = rr.rerank("q", memories, top_k=3)
    assert [m["id"] for m in ranked] == ["A", "B", "C"]


def test_maybe_rerank_disabled_is_passthrough(monkeypatch):
    monkeypatch.setattr(rr, "RERANK_BACKEND", "")
    memories = _mems("A", "B")
    assert rr.maybe_rerank("q", memories) is memories


class _FakeEngine:
    def __init__(self, scorer):
        self._scorer = scorer

    def score(self, query: str, texts: list[str]) -> np.ndarray:
        return self._scorer(query, texts)


def test_maybe_rerank_enabled_uses_engine(monkeypatch):
    monkeypatch.setattr(rr, "RERANK_BACKEND", "onnx")
    monkeypatch.setattr(rr, "RERANK_TOP_K", 10)
    engine = _FakeEngine(_scorer_preferring("C", "A", "B"))
    monkeypatch.setattr(rr, "_get_reranker", lambda: engine)
    ranked = rr.maybe_rerank("q", _mems("A", "B", "C"))
    assert [m["id"] for m in ranked] == ["C", "A", "B"]


def test_maybe_rerank_swallows_engine_errors(monkeypatch):
    monkeypatch.setattr(rr, "RERANK_BACKEND", "onnx")

    class _Boom:
        def score(self, query, texts):
            raise RuntimeError("inference exploded")

    monkeypatch.setattr(rr, "_get_reranker", lambda: _Boom())
    memories = _mems("A", "B", "C")
    ranked = rr.maybe_rerank("q", memories)
    assert [m["id"] for m in ranked] == ["A", "B", "C"]


def test_maybe_rerank_respects_top_k(monkeypatch):
    monkeypatch.setattr(rr, "RERANK_BACKEND", "onnx")
    monkeypatch.setattr(rr, "RERANK_TOP_K", 2)
    engine = _FakeEngine(_scorer_preferring("C", "B", "A"))
    monkeypatch.setattr(rr, "_get_reranker", lambda: engine)
    ranked = rr.maybe_rerank("q", _mems("A", "B", "C"))
    # Only A and B are rescored (B beats A); C is tail and never moves up.
    assert [m["id"] for m in ranked] == ["B", "A", "C"]


async def test_memory_search_applies_reranker(monkeypatch, db_conn, memory_factory):
    """End-to-end: an enabled reranker reorders memory_search results."""
    import json

    import remind_me_mcp.tools as tools_mod
    from remind_me_mcp.models import MemorySearchInput, ResponseFormat

    memory_factory(id="m1", content="the quick brown fox jumps")
    memory_factory(id="m2", content="the quick red fox sleeps")

    monkeypatch.setattr(rr, "RERANK_BACKEND", "onnx")
    monkeypatch.setattr(rr, "RERANK_TOP_K", 10)
    # Score m2's content highest regardless of FTS order.
    engine = _FakeEngine(
        lambda q, texts: np.array(
            [2.0 if "red" in t else 1.0 for t in texts], dtype=np.float32
        )
    )
    monkeypatch.setattr(rr, "_get_reranker", lambda: engine)
    monkeypatch.setattr(tools_mod, "record_access", lambda *_a, **_k: None)

    raw = await tools_mod.memory_search(
        MemorySearchInput(
            query="quick fox", response_format=ResponseFormat.JSON, token_budget=0
        )
    )
    payload = json.loads(raw)
    assert payload["memories"][0]["id"] == "m2"
    assert payload["memories"][0]["_rerank_score"] == pytest.approx(2.0)
