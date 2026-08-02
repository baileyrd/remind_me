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

import threading
import time

import numpy as np

import remind_me_mcp.embeddings as emb_mod
from remind_me_mcp.embeddings import _Embedder, _get_embedder, _prefix_for


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


# ---------------------------------------------------------------------------
# Query/document embedding prefix asymmetry (issue #51)
# ---------------------------------------------------------------------------


def test_prefix_for_known_model_families():
    assert _prefix_for("nomic-embed-text", "query") == "search_query: "
    assert _prefix_for("nomic-embed-text", "passage") == "search_document: "
    assert _prefix_for("intfloat/e5-base-v2", "query") == "query: "
    assert _prefix_for("intfloat/e5-base-v2", "passage") == "passage: "
    # BGE only prefixes the query side; passages are embedded as-is.
    assert _prefix_for("BAAI/bge-large-en-v1.5", "query") != ""
    assert _prefix_for("BAAI/bge-large-en-v1.5", "passage") == ""


def test_prefix_for_unknown_model_is_empty():
    assert _prefix_for("sentence-transformers/all-MiniLM-L6-v2", "query") == ""
    assert _prefix_for("sentence-transformers/all-MiniLM-L6-v2", "passage") == ""


def _stub_embedder_capturing_text(monkeypatch, model_name: str) -> tuple[_Embedder, list[str]]:
    """An _Embedder whose forward pass records the exact texts it receives."""
    e = _Embedder(model_name=model_name, dim=4)
    monkeypatch.setattr(e, "_ensure_loaded", lambda: None)

    seen: list[str] = []

    def _fake_forward(texts: list[str]) -> np.ndarray:
        seen.extend(texts)
        return np.zeros((len(texts), 4), dtype=np.float32)

    monkeypatch.setattr(e, "_embed_forward", _fake_forward)
    return e, seen


def test_embed_applies_query_prefix_for_known_model(monkeypatch):
    e, seen = _stub_embedder_capturing_text(monkeypatch, "nomic-embed-text")
    e.embed(["hello"], role="query")
    assert seen == ["search_query: hello"]


def test_embed_applies_passage_prefix_for_known_model(monkeypatch):
    e, seen = _stub_embedder_capturing_text(monkeypatch, "nomic-embed-text")
    e.embed(["hello"], role="passage")
    assert seen == ["search_document: hello"]


def test_embed_default_role_is_passage(monkeypatch):
    e, seen = _stub_embedder_capturing_text(monkeypatch, "nomic-embed-text")
    e.embed(["hello"])
    assert seen == ["search_document: hello"]


def test_embed_one_threads_role(monkeypatch):
    e, seen = _stub_embedder_capturing_text(monkeypatch, "nomic-embed-text")
    e.embed_one("hello", role="query")
    assert seen == ["search_query: hello"]


def test_embed_no_prefix_for_default_onnx_model(monkeypatch):
    """The ONNX default (all-MiniLM-L6-v2) has no query/passage convention."""
    e, seen = _stub_embedder_capturing_text(
        monkeypatch, "sentence-transformers/all-MiniLM-L6-v2"
    )
    e.embed(["hello"], role="query")
    e.embed(["hello"], role="passage")
    assert seen == ["hello", "hello"]


# ---------------------------------------------------------------------------
# Concurrent-load safety (issue #153)
# ---------------------------------------------------------------------------


def test_ensure_loaded_serializes_concurrent_first_callers(monkeypatch):
    """Regression guard for issue #153.

    Two threads racing a cold _ensure_loaded() (the webhook server, folder
    watcher, sync thread, and every asyncio.to_thread API worker can all
    reach here concurrently on first use) used to both pass the "not ready
    yet" check and both proceed into the load body -- e.g. both downloading
    the same model file into the same cache path at once. The fix serializes
    on an instance lock with a double-checked _ready flag, so only one
    thread ever actually executes the load body; the other blocks and then
    observes _ready=True instead of loading again.
    """
    e = _Embedder(dim=4)
    load_call_count = 0
    load_call_lock = threading.Lock()

    def slow_load(self=e):
        nonlocal load_call_count
        with load_call_lock:
            load_call_count += 1
        time.sleep(0.1)  # wide enough for a second thread to reach the check
        self._ready = True

    monkeypatch.setattr(e, "_load_locked", slow_load)

    threads = [threading.Thread(target=e._ensure_loaded) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert load_call_count == 1
    assert e._ready is True


def test_get_embedder_constructs_the_singleton_only_once_under_concurrency(
    monkeypatch,
):
    """Regression guard for issue #153's other half: _get_embedder's
    singleton construction is also locked, so concurrent first-callers
    share one embedder instance instead of racing separate constructions."""
    monkeypatch.setattr(emb_mod, "_embedder", None)
    monkeypatch.setattr(emb_mod, "EMBEDDING_BACKEND", "onnx")

    constructed = []
    real_embedder_cls = emb_mod._Embedder

    class _CountingEmbedder(real_embedder_cls):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            constructed.append(self)
            time.sleep(0.05)  # widen the race window

        def _ensure_loaded(self):
            pass  # skip the real load entirely for this test

    monkeypatch.setattr(emb_mod, "_Embedder", _CountingEmbedder)

    results = []
    threads = [
        threading.Thread(target=lambda: results.append(_get_embedder()))
        for _ in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(constructed) == 1
    assert len({id(r) for r in results}) == 1
