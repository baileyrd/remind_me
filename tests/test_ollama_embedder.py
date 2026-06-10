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


# ---------------------------------------------------------------------------
# PF-01: availability caching (TTL on the monotonic clock)
# ---------------------------------------------------------------------------


class _FakeClock:
    """Stand-in for the time module with a controllable monotonic() value."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now


def _counting_post(returned_dim: int, calls: dict, *, fail: bool = False):
    inner = _make_post(returned_dim, fail=fail)

    def _post(url, json=None, timeout=None):  # noqa: A002 - mirror httpx.post
        calls["n"] += 1
        return inner(url, json=json, timeout=timeout)

    return _post


def test_available_success_is_cached(monkeypatch):
    """A successful probe is reused within the success TTL — no re-probe per call."""
    calls = {"n": 0}
    monkeypatch.setattr(httpx, "post", _counting_post(4, calls))
    e = OllamaEmbedder(dim=4)

    assert e.available is True
    assert e.available is True
    assert e.available is True
    assert calls["n"] == 1


def test_available_success_cache_expires(monkeypatch):
    """After the success TTL the daemon is probed again."""
    clock = _FakeClock()
    monkeypatch.setattr(emb_mod, "time", clock)
    calls = {"n": 0}
    monkeypatch.setattr(httpx, "post", _counting_post(4, calls))
    e = OllamaEmbedder(dim=4)

    assert e.available is True
    clock.now += emb_mod.AVAILABILITY_SUCCESS_TTL + 1
    assert e.available is True
    assert calls["n"] == 2


def test_available_failure_is_cached_then_reprobed(monkeypatch):
    """A failed probe is cached for the (shorter) failure TTL, then retried."""
    clock = _FakeClock()
    monkeypatch.setattr(emb_mod, "time", clock)
    calls = {"n": 0}
    monkeypatch.setattr(httpx, "post", _counting_post(4, calls, fail=True))
    e = OllamaEmbedder(dim=4)

    assert e.available is False
    assert e.available is False
    assert calls["n"] == 1  # second call served from the failure cache

    # Daemon comes back after the failure TTL — availability recovers.
    clock.now += emb_mod.AVAILABILITY_FAILURE_TTL + 1
    monkeypatch.setattr(httpx, "post", _counting_post(4, calls))
    assert e.available is True


def test_embed_refreshes_availability_cache(monkeypatch):
    """A real embed() outcome feeds the cache, so available needs no extra ping."""
    calls = {"n": 0}
    monkeypatch.setattr(httpx, "post", _counting_post(4, calls))
    e = OllamaEmbedder(dim=4)

    e.embed(["real workload"])
    assert calls["n"] == 1
    assert e.available is True
    assert calls["n"] == 1  # no additional "ping" request


def test_onnx_load_failure_is_cached(monkeypatch, tmp_path):
    """PF-01: a failed ONNX model download isn't retried on every call."""
    pytest.importorskip("onnxruntime")
    import huggingface_hub

    monkeypatch.setattr(emb_mod, "MODEL_DIR", tmp_path / "models")
    calls = {"n": 0}

    def boom(*args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("offline — no HuggingFace access")

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", boom)

    e = emb_mod._Embedder()
    assert e.available is False
    assert e.available is False
    assert calls["n"] == 1  # second check hit the failure cache

    # After the failure TTL the load is attempted again.
    clock = _FakeClock()
    clock.now = e._failed_until + 1
    monkeypatch.setattr(emb_mod, "time", clock)
    assert e.available is False
    assert calls["n"] == 2


def test_onnx_missing_deps_cached_permanently(monkeypatch):
    """An ImportError marks the ONNX embedder unavailable for the process."""
    e = emb_mod._Embedder()
    e._deps_missing = True
    assert e.available is False
