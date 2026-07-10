"""
Tests for the ONNX embedding backend's batching behaviour.

No model download is required: the tokenizer/session load is stubbed and the
per-forward-pass call is replaced with a recorder, so these run fully offline
and deterministically. The focus is the memory-bounding contract of
``_Embedder.embed()`` — it must slice large inputs into forward passes of at
most ``EMBED_FORWARD_BATCH`` texts and concatenate the results, so a bulk
caller (e.g. the initial hub sync) can never build one giant tensor.
"""

from __future__ import annotations

import numpy as np

import remind_me_mcp.embeddings as emb_mod
from remind_me_mcp.embeddings import _Embedder


def _stub_embedder(monkeypatch, forward_batch: int, dim: int = 4) -> tuple[_Embedder, list[int]]:
    """An _Embedder whose forward pass records slice sizes instead of running ONNX."""
    monkeypatch.setattr(emb_mod, "EMBED_FORWARD_BATCH", forward_batch)
    e = _Embedder(dim=dim)
    monkeypatch.setattr(e, "_ensure_loaded", lambda: None)

    sizes: list[int] = []

    def _fake_forward(texts: list[str]) -> np.ndarray:
        sizes.append(len(texts))
        # One row per text; value encodes the text so concatenation order is checkable.
        return np.array([[float(len(t))] * dim for t in texts], dtype=np.float32)

    monkeypatch.setattr(e, "_embed_forward", _fake_forward)
    return e, sizes


def test_embed_slices_into_forward_batches(monkeypatch):
    e, sizes = _stub_embedder(monkeypatch, forward_batch=2)
    texts = ["a", "bb", "ccc", "dddd", "eeeee"]  # 5 texts, batch of 2
    vecs = e.embed(texts)

    assert sizes == [2, 2, 1]  # sliced, never one big pass
    assert vecs.shape == (5, 4)
    # Order preserved across the concatenation.
    np.testing.assert_array_equal(vecs[:, 0], [1.0, 2.0, 3.0, 4.0, 5.0])


def test_embed_single_pass_when_within_batch(monkeypatch):
    e, sizes = _stub_embedder(monkeypatch, forward_batch=32)
    vecs = e.embed(["x", "y", "z"])

    assert sizes == [3]  # a single forward pass, no slicing overhead
    assert vecs.shape == (3, 4)


def test_embed_empty_returns_empty_without_forward(monkeypatch):
    e, sizes = _stub_embedder(monkeypatch, forward_batch=2)
    vecs = e.embed([])

    assert sizes == []  # never touches the model
    assert vecs.shape == (0, 4)
    assert vecs.dtype == np.float32


def test_embed_forward_batch_of_one(monkeypatch):
    e, sizes = _stub_embedder(monkeypatch, forward_batch=1)
    vecs = e.embed(["a", "bb", "ccc"])

    assert sizes == [1, 1, 1]
    assert vecs.shape == (3, 4)
