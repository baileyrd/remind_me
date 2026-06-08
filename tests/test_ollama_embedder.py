"""
Tests for the Ollama embedding backend.

No Ollama daemon is required: the HTTP layer (`httpx.post`) is mocked, so these
run fully offline and deterministically.
"""

from __future__ import annotations

import httpx
import numpy as np
import pytest

import remind_me_mcp.embeddings as emb_mod
from remind_me_mcp.embeddings import OllamaEmbedder, _get_embedder


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _make_post(returned_dim: int, *, fail: bool = False):
    """Build a fake httpx.post that returns one vector per input of *returned_dim*."""

    def _post(url, json=None, timeout=None):  # noqa: A002 - mirror httpx.post signature
        if fail:
            raise httpx.ConnectError("connection refused")
        n = len(json["input"])
        embeddings = [[float(i + 1)] * returned_dim for i in range(n)]
        return _FakeResp({"embeddings": embeddings})

    return _post


def test_embed_returns_normalised_batch(monkeypatch):
    monkeypatch.setattr(httpx, "post", _make_post(4))
    e = OllamaEmbedder(model="nomic-embed-text", dim=4)
    vecs = e.embed(["alpha", "beta", "gamma"])

    assert vecs.shape == (3, 4)
    assert vecs.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(vecs, axis=1), [1.0, 1.0, 1.0], rtol=1e-5)


def test_embed_one_returns_bytes(monkeypatch):
    monkeypatch.setattr(httpx, "post", _make_post(8))
    e = OllamaEmbedder(dim=8)
    raw = e.embed_one("hello")
    assert isinstance(raw, bytes)
    assert len(raw) == 8 * 4  # 8 float32 values


def test_dim_mismatch_raises(monkeypatch):
    monkeypatch.setattr(httpx, "post", _make_post(3))  # model returns dim 3
    e = OllamaEmbedder(dim=4)  # but we expect 4
    with pytest.raises(ValueError, match="REMIND_ME_EMBEDDING_DIM"):
        e.embed(["x"])


def test_available_true_and_false(monkeypatch):
    monkeypatch.setattr(httpx, "post", _make_post(4))
    assert OllamaEmbedder(dim=4).available is True

    monkeypatch.setattr(httpx, "post", _make_post(4, fail=True))
    assert OllamaEmbedder(dim=4).available is False


def test_get_embedder_selects_ollama(monkeypatch):
    monkeypatch.setattr(emb_mod, "EMBEDDING_BACKEND", "ollama")
    monkeypatch.setattr(emb_mod, "_embedder", None)
    monkeypatch.setattr(httpx, "post", _make_post(emb_mod.EMBEDDING_DIM))

    got = _get_embedder()
    assert isinstance(got, OllamaEmbedder)


def test_get_embedder_ollama_unavailable_returns_none(monkeypatch):
    monkeypatch.setattr(emb_mod, "EMBEDDING_BACKEND", "ollama")
    monkeypatch.setattr(emb_mod, "_embedder", None)
    monkeypatch.setattr(httpx, "post", _make_post(emb_mod.EMBEDDING_DIM, fail=True))

    assert _get_embedder() is None
