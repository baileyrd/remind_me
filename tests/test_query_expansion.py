"""
Tests for HyDE query expansion (lever E).

Generation is mocked (no Ollama daemon needed). The retrieval-side test proves
the mechanism deterministically: fusing the query embedding with a passage
phrased like the target memory pulls that memory to rank 1, where the plain
query embedding alone does not find it.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

import remind_me_mcp.query_expansion as qe
from remind_me_mcp.db import _embed_and_store, _fuse_query_embedding, _semantic_search


@pytest.fixture(autouse=True)
def _isolated_expansion_cache(monkeypatch):
    """Give every test a fresh expansion cache (module-level state, DI-08)."""
    monkeypatch.setattr(qe, "_EXPANSION_CACHE", {})


# ---------------------------------------------------------------------------
# expand_query / hyde_passage
# ---------------------------------------------------------------------------


def test_expand_query_disabled_returns_empty(monkeypatch):
    monkeypatch.setattr(qe, "EXPANSION_MODE", "")
    assert qe.expand_query("what city was I born in?") == []


def test_expand_query_blank_query_returns_empty(monkeypatch):
    monkeypatch.setattr(qe, "EXPANSION_MODE", "hyde")
    assert qe.expand_query("   ") == []


def test_expand_query_hyde_returns_passage(monkeypatch):
    monkeypatch.setattr(qe, "EXPANSION_MODE", "hyde")
    monkeypatch.setattr(qe, "_generate", lambda prompt: "  I was born in Lisbon.  ")
    assert qe.expand_query("what city was I born in?") == ["I was born in Lisbon."]


def test_expand_query_includes_query_in_prompt(monkeypatch):
    seen: dict = {}

    def fake_generate(prompt: str) -> str:
        seen["prompt"] = prompt
        return "passage"

    monkeypatch.setattr(qe, "EXPANSION_MODE", "hyde")
    monkeypatch.setattr(qe, "_generate", fake_generate)
    qe.expand_query("what city was I born in?")
    assert "what city was I born in?" in seen["prompt"]


def test_hyde_passage_caps_length(monkeypatch):
    monkeypatch.setattr(qe, "_generate", lambda prompt: "x" * 5000)
    passage = qe.hyde_passage("q")
    assert passage is not None
    assert len(passage) == qe.HYDE_MAX_CHARS


def test_hyde_passage_failure_returns_none(monkeypatch):
    def boom(prompt: str) -> str:
        raise ConnectionError("daemon down")

    monkeypatch.setattr(qe, "_generate", boom)
    assert qe.hyde_passage("q") is None


def test_expand_query_failure_falls_back_to_plain(monkeypatch):
    monkeypatch.setattr(qe, "EXPANSION_MODE", "hyde")
    monkeypatch.setattr(qe, "hyde_passage", lambda q: None)
    assert qe.expand_query("anything") == []


# ---------------------------------------------------------------------------
# expansion cache (DI-08)
# ---------------------------------------------------------------------------


def test_expand_query_caches_identical_queries(monkeypatch):
    """A repeated query reuses the cached passage instead of regenerating."""
    monkeypatch.setattr(qe, "EXPANSION_MODE", "hyde")
    monkeypatch.setattr(qe, "_EXPANSION_CACHE", {})
    calls: list[str] = []

    def fake_passage(q: str) -> str:
        calls.append(q)
        return "a generated passage"

    monkeypatch.setattr(qe, "hyde_passage", fake_passage)

    assert qe.expand_query("same question?") == ["a generated passage"]
    assert qe.expand_query("same question?") == ["a generated passage"]
    assert len(calls) == 1


def test_expand_query_does_not_cache_failures(monkeypatch):
    """A failed generation is retried next time (the daemon may come back)."""
    monkeypatch.setattr(qe, "EXPANSION_MODE", "hyde")
    monkeypatch.setattr(qe, "_EXPANSION_CACHE", {})
    calls: list[str] = []

    def failing_passage(q: str) -> None:
        calls.append(q)
        return None

    monkeypatch.setattr(qe, "hyde_passage", failing_passage)

    assert qe.expand_query("flaky") == []
    assert qe.expand_query("flaky") == []
    assert len(calls) == 2


def test_expand_query_cache_is_bounded(monkeypatch):
    """The cache evicts its oldest entry once the size cap is reached."""
    monkeypatch.setattr(qe, "EXPANSION_MODE", "hyde")
    monkeypatch.setattr(qe, "_EXPANSION_CACHE", {})
    monkeypatch.setattr(qe, "_EXPANSION_CACHE_MAX", 2)
    monkeypatch.setattr(qe, "hyde_passage", lambda q: f"passage for {q}")

    qe.expand_query("q1")
    qe.expand_query("q2")
    qe.expand_query("q3")

    assert len(qe._EXPANSION_CACHE) == 2
    assert "q1" not in qe._EXPANSION_CACHE


# ---------------------------------------------------------------------------
# Embedding fusion
# ---------------------------------------------------------------------------


def test_fuse_single_text_equals_embed_one(mock_embedder):
    fused = np.frombuffer(_fuse_query_embedding(mock_embedder, ["hello"]), dtype=np.float32)
    plain = np.frombuffer(mock_embedder.embed_one("hello"), dtype=np.float32)
    # Renormalising an already-unit vector only differs by float rounding.
    assert np.allclose(fused, plain, atol=1e-6)


def test_fuse_is_normalised_mean(mock_embedder):
    fused = np.frombuffer(
        _fuse_query_embedding(mock_embedder, ["alpha", "beta"]), dtype=np.float32
    )
    vecs = mock_embedder.embed(["alpha", "beta"])
    expected = vecs.mean(axis=0)
    expected /= np.linalg.norm(expected)
    assert np.allclose(fused, expected, atol=1e-6)
    assert np.isclose(np.linalg.norm(fused), 1.0, atol=1e-6)


class _RoleRecordingEmbedder:
    """Records the (texts, role) of every embed() call — asserts nothing itself."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str]] = []

    def embed(self, texts, *, role="passage"):
        self.calls.append((tuple(texts), role))
        return np.ones((len(texts), 4), dtype=np.float32)

    def embed_one(self, text, *, role="passage"):
        return self.embed([text], role=role)[0].tobytes()


def test_fuse_query_embedding_uses_query_role_for_query_only():
    rec = _RoleRecordingEmbedder()
    _fuse_query_embedding(rec, ["what city"])
    assert rec.calls == [(("what city",), "query")]


def test_fuse_query_embedding_uses_passage_role_for_extra_texts():
    """The literal query gets role="query"; any HyDE/extra passages get
    role="passage" since they're synthetic document-like text, not a query
    (query/document embedding prefix asymmetry, issue #51)."""
    rec = _RoleRecordingEmbedder()
    _fuse_query_embedding(rec, ["what city", "a passage about cities"])
    assert rec.calls == [
        (("what city",), "query"),
        (("a passage about cities",), "passage"),
    ]


# ---------------------------------------------------------------------------
# Retrieval effect — the deterministic headline case
# ---------------------------------------------------------------------------


def _insert_memory(db, mem_id: str, content: str) -> None:
    from remind_me_mcp.db import _now_iso

    now = _now_iso()
    db.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata,
                                 created_at, updated_at)
           VALUES (?, ?, 'general', '[]', 'manual', '{}', ?, ?)""",
        (mem_id, content, now, now),
    )
    db.commit()
    _embed_and_store(mem_id, content)


def test_extra_texts_pull_matching_memory_to_top(db_conn_with_vec, mock_embedder):
    """A HyDE passage phrased like the target memory makes it rank 1.

    FakeEmbedder vectors are random per text, so the question and the answer
    passage share no signal — the plain query ranks essentially at random. The
    fused query+passage vector correlates ~0.7 with the passage's embedding, so
    a memory worded exactly like the passage must win the KNN.
    """
    target = "I prefer window seats on long flights."
    _insert_memory(db_conn_with_vec, "target", target)
    for i in range(5):
        _insert_memory(db_conn_with_vec, f"noise{i}", f"unrelated filler memory {i}")

    results = _semantic_search("seating preference?", limit=3, extra_texts=[target])
    assert results[0]["id"] == "target"


def test_semantic_search_without_extras_unchanged(db_conn_with_vec, mock_embedder):
    """extra_texts=None searches with exactly the query embedding (legacy path)."""
    _insert_memory(db_conn_with_vec, "m1", "some stored memory")
    plain = _semantic_search("a query", limit=5)
    explicit = _semantic_search("a query", limit=5, extra_texts=None)
    assert [m["id"] for m in plain] == [m["id"] for m in explicit]


# ---------------------------------------------------------------------------
# memory_search integration
# ---------------------------------------------------------------------------


async def test_memory_search_passes_expansion(monkeypatch, db_conn, memory_factory):
    """memory_search feeds expand_query output into the semantic tier."""
    import remind_me_mcp.db as db_mod
    import remind_me_mcp.embeddings as emb_mod
    import remind_me_mcp.tools as tools_mod
    from remind_me_mcp.models import MemorySearchInput, ResponseFormat

    memory_factory(id="m1", content="the quick brown fox")

    seen: dict = {}

    def fake_semantic(query, limit=20, extra_texts=None, category=None, tags=None):
        seen["extra_texts"] = extra_texts
        return []

    # Expansion only runs when an embedder is available (DI-08) and semantic
    # search actually has a table to query (db_conn has no sqlite-vec
    # loaded, so this stands in for db_conn_with_vec without the real
    # extension dependency -- this test is about expand_query plumbing, not
    # real vector search).
    monkeypatch.setattr(emb_mod, "_get_embedder", lambda: object())
    monkeypatch.setattr(db_mod, "vec_search_available", lambda: True)
    monkeypatch.setattr(tools_mod, "expand_query", lambda q: ["hypothetical passage"])
    monkeypatch.setattr(tools_mod, "_semantic_search", fake_semantic)
    monkeypatch.setattr(tools_mod, "record_accesses", lambda *_a, **_k: 0)

    raw = await tools_mod.memory_search(
        MemorySearchInput(query="quick fox", response_format=ResponseFormat.JSON)
    )
    json.loads(raw)  # response stays well-formed
    assert seen["extra_texts"] == ["hypothetical passage"]


async def test_memory_search_skips_expansion_without_embedder(
    monkeypatch, db_conn, memory_factory
):
    """expand_query (a slow LLM generation) never runs when the semantic tier
    can't consume its output (DI-08)."""
    import remind_me_mcp.db as db_mod
    import remind_me_mcp.embeddings as emb_mod
    import remind_me_mcp.tools as tools_mod
    from remind_me_mcp.models import MemorySearchInput, ResponseFormat

    memory_factory(content="expansion gate test memory")

    monkeypatch.setattr(emb_mod, "_get_embedder", lambda: None)
    monkeypatch.setattr(db_mod, "_get_embedder", lambda: None)

    calls: list[str] = []

    def spy_expand(q: str) -> list[str]:
        calls.append(q)
        return []

    monkeypatch.setattr(tools_mod, "expand_query", spy_expand)
    monkeypatch.setattr(tools_mod, "record_accesses", lambda *_a, **_k: 0)

    raw = await tools_mod.memory_search(
        MemorySearchInput(query="expansion gate test", response_format=ResponseFormat.JSON)
    )
    json.loads(raw)
    assert calls == []
