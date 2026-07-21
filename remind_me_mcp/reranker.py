"""
remind_me_mcp.reranker — Optional cross-encoder reranking of the top-k results.

RRF fuses *independent* rank lists, so it never reads the query and a candidate
together. A cross-encoder does: it scores each (query, memory) pair jointly,
which is far more precise at ordering the handful of candidates that matter.
This module reranks the head of the RRF-ranked list with an ONNX cross-encoder
(default: ``BAAI/bge-reranker-base``, a modern (2023) cross-encoder that's
meaningfully stronger than the previous 2019 ``ms-marco-MiniLM-L6-v2`` default
while still small enough to run on CPU).

On by default: only the top ``REMIND_ME_RERANK_TOP_K`` (default 20) RRF
candidates are ever rescored, so the added latency is bounded and small
regardless of how large the underlying result pool is. Disable with
``REMIND_ME_RERANK=""`` for latency-sensitive deployments. The model downloads
from HuggingFace Hub on first use and caches in MODEL_DIR, exactly like the
embedder. Any load or inference failure (missing dependencies, no network, no
ONNX export for the configured model) degrades gracefully to the un-reranked
order — reranking can never break search, only skip enhancing it.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import numpy as np

from remind_me_mcp.config import MODEL_DIR

log = logging.getLogger("remind_me_mcp.reranker")

AVAILABILITY_FAILURE_TTL = 30.0
"""Seconds a failed model load stays cached before a retry (PF-01, mirrors
embeddings.py) — now that reranking is on by default, an offline/no-network
process must not retry a real HuggingFace download on every single search."""

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

RERANK_BACKEND: str = os.environ.get("REMIND_ME_RERANK", "onnx").lower()
"""Reranker backend: 'onnx' (in-process cross-encoder, the default) or ''
(disabled) for latency-sensitive deployments. Rescoring is bounded to the top
RERANK_TOP_K candidates, so the default-on cost is small and constant
regardless of result-pool size; any load/inference failure degrades
gracefully to the un-reranked RRF order."""

RERANK_MODEL: str = os.environ.get("REMIND_ME_RERANK_MODEL", "BAAI/bge-reranker-base")
"""HuggingFace repo of the cross-encoder. Must ship onnx/model.onnx + tokenizer.json."""

RERANK_TOP_K: int = int(os.environ.get("REMIND_ME_RERANK_TOP_K", "20"))
"""How many of the top RRF-ranked candidates are rescored. The tail keeps its order."""

_RERANK_BATCH = 16  # (query, doc) pairs per ONNX forward pass — bounds memory.


# ---------------------------------------------------------------------------
# Cross-encoder engine
# ---------------------------------------------------------------------------


class CrossEncoderReranker:
    """Lazy-loading ONNX cross-encoder. Scores (query, document) pairs jointly."""

    def __init__(self, model_name: str | None = None) -> None:
        """Configure the reranker; the model loads on the first score() call.

        Args:
            model_name: HuggingFace repo name. Defaults to module-level RERANK_MODEL.
        """
        self.model_name = model_name or RERANK_MODEL
        # Typed as Any: onnxruntime/tokenizers objects assigned lazily in _ensure_loaded().
        self._session: Any = None
        self._tokenizer: Any = None
        self._input_names: set[str] = set()
        self._ready = False
        # Failure caching (PF-01, mirrors embeddings._Embedder): missing
        # dependencies are permanent for this process; other load failures
        # (no network, no ONNX export for this model, ...) are retried only
        # after AVAILABILITY_FAILURE_TTL instead of on every single search —
        # reranking is on by default now, so an offline process must not
        # attempt a real HuggingFace download on every call.
        self._deps_missing = False
        self._failed_until = 0.0

    def _ensure_loaded(self) -> None:
        """Lazily download and load the ONNX model + tokenizer (same cache as the embedder).

        Load failures are cached (PF-01): an ImportError marks the reranker
        permanently unavailable for this process, while any other failure is
        only retried after AVAILABILITY_FAILURE_TTL seconds.
        """
        if self._ready:
            return
        if self._deps_missing:
            raise RuntimeError("Reranker dependencies are not installed (cached failure)")
        if time.monotonic() < self._failed_until:
            raise RuntimeError(
                "Reranker model failed to load recently (cached failure; will retry later)"
            )
        try:
            import onnxruntime as ort
            from huggingface_hub import hf_hub_download
            from tokenizers import Tokenizer

            MODEL_DIR.mkdir(parents=True, exist_ok=True)
            cache_dir = str(MODEL_DIR)

            log.info("Loading reranker model: %s", self.model_name)
            model_path = hf_hub_download(
                self.model_name, "onnx/model.onnx", cache_dir=cache_dir
            )
            tokenizer_path = hf_hub_download(
                self.model_name, "tokenizer.json", cache_dir=cache_dir
            )

            self._session = ort.InferenceSession(
                model_path, providers=["CPUExecutionProvider"]
            )
            # Some exports omit token_type_ids — feed only what the graph declares.
            self._input_names = {i.name for i in self._session.get_inputs()}
            self._tokenizer = Tokenizer.from_file(tokenizer_path)
            self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
            # longest_first truncation keeps the query intact for long documents.
            self._tokenizer.enable_truncation(max_length=512)
            self._ready = True
            log.info("Reranker model loaded")

        except ImportError as e:
            self._deps_missing = True
            log.warning(
                "Reranker dependencies not installed (%s). "
                "Install with: pip install onnxruntime tokenizers huggingface-hub numpy. "
                "Results keep their RRF order.",
                e,
            )
            raise
        except Exception as e:  # Broad catch intentional: ONNX Runtime raises non-stdlib exceptions
            self._failed_until = time.monotonic() + AVAILABILITY_FAILURE_TTL
            log.warning("Failed to load reranker model: %s. Results keep their RRF order.", e)
            raise

    def score(self, query: str, texts: list[str]) -> np.ndarray:
        """Score (query, text) pairs; higher means more relevant.

        Args:
            query: The search query.
            texts: Candidate documents to score against the query.

        Returns:
            Float32 array of shape (len(texts),) of raw relevance logits.
            Logits are monotonic in relevance, so they sort directly.
        """
        self._ensure_loaded()
        scores: list[np.ndarray] = []
        for start in range(0, len(texts), _RERANK_BATCH):
            batch = texts[start : start + _RERANK_BATCH]
            encoded = self._tokenizer.encode_batch([(query, t) for t in batch])
            feeds = {
                "input_ids": np.array([e.ids for e in encoded], dtype=np.int64),
                "attention_mask": np.array(
                    [e.attention_mask for e in encoded], dtype=np.int64
                ),
            }
            if "token_type_ids" in self._input_names:
                # Pair segment ids (0 = query, 1 = document) — required for BERT heads.
                feeds["token_type_ids"] = np.array(
                    [e.type_ids for e in encoded], dtype=np.int64
                )
            outputs = self._session.run(None, feeds)
            scores.append(np.asarray(outputs[0], dtype=np.float32).reshape(-1))
        return np.concatenate(scores) if scores else np.empty(0, dtype=np.float32)

    @property
    def available(self) -> bool:
        """Return True if the reranker model is loaded and ready."""
        try:
            self._ensure_loaded()
            return True
        except Exception:  # Broad catch intentional: graceful-degradation boundary
            return False


# ---------------------------------------------------------------------------
# Module-level singleton + rerank operations
# ---------------------------------------------------------------------------

_reranker: CrossEncoderReranker | None = None


def _get_reranker() -> CrossEncoderReranker | None:
    """Get or create the global reranker; None when it cannot be loaded."""
    global _reranker
    if _reranker is None:
        _reranker = CrossEncoderReranker()
    return _reranker if _reranker.available else None


def rerank(
    query: str,
    memories: list[dict],
    *,
    top_k: int | None = None,
    scorer=None,
) -> list[dict]:
    """Reorder the first *top_k* memories by cross-encoder score; keep the tail as-is.

    Each rescored memory gains a ``_rerank_score`` key. Ties and the untouched
    tail preserve their incoming (RRF) order, so reranking never *loses* a
    candidate — it only reorders the head.

    Args:
        query: The search query.
        memories: Ranked memory dicts (best first), each with a ``content`` key.
        top_k: How many head candidates to rescore. Defaults to RERANK_TOP_K.
        scorer: Callable ``(query, texts) -> array`` returning one score per
            text. Defaults to the ONNX cross-encoder; injectable for tests.

    Returns:
        The reordered list (same dicts, same length).
    """
    if top_k is None:
        top_k = RERANK_TOP_K
    head = memories[:top_k]
    if len(head) < 2:
        return list(memories)

    if scorer is None:
        engine = _get_reranker()
        if engine is None:
            return list(memories)
        scorer = engine.score

    scores = scorer(query, [m.get("content", "") for m in head])
    for mem, s in zip(head, scores, strict=True):
        mem["_rerank_score"] = float(s)
    # Stable sort: equal scores keep their RRF order.
    head = sorted(head, key=lambda m: m["_rerank_score"], reverse=True)
    return head + list(memories[top_k:])


def maybe_rerank(query: str, memories: list[dict]) -> list[dict]:
    """Apply the configured reranker, or return *memories* unchanged when disabled.

    This is the single integration point for search: it checks the backend
    setting, and any failure (model unavailable, inference error) falls back to
    the incoming order.

    Args:
        query: The search query.
        memories: RRF-ranked memory dicts (best first).

    Returns:
        Reranked (or untouched) list of the same memories.
    """
    if RERANK_BACKEND != "onnx" or not memories:
        return memories
    try:
        return rerank(query, memories)
    except Exception as e:  # Broad catch intentional: reranking must never break search
        log.warning("Reranking failed (%s); keeping RRF order.", e)
        return memories


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "RERANK_BACKEND",
    "RERANK_MODEL",
    "RERANK_TOP_K",
    "CrossEncoderReranker",
    "maybe_rerank",
    "rerank",
]
