"""
remind_me_mcp.embeddings — ONNX embedding engine.

Provides a lazy-loading embedding engine backed by ONNX Runtime.
The model is downloaded from HuggingFace Hub on first use and cached
in the configured MODEL_DIR. All heavy imports are deferred to first
call so the module can be imported even when optional dependencies
are not installed.
"""

from __future__ import annotations

import logging

import numpy as np

from remind_me_mcp.config import EMBEDDING_DIM, EMBEDDING_MODEL, MODEL_DIR

log = logging.getLogger("remind_me_mcp.embeddings")

# ---------------------------------------------------------------------------
# Embedder class
# ---------------------------------------------------------------------------


class _Embedder:
    """Lightweight ONNX-based embedding engine. Downloads model on first use."""

    def __init__(self, model_name: str = EMBEDDING_MODEL, dim: int = EMBEDDING_DIM):
        self.model_name = model_name
        self.dim = dim
        self._session = None
        self._tokenizer = None
        self._ready = False

    def _ensure_loaded(self) -> None:
        """Lazily load the ONNX model and tokenizer from HuggingFace Hub."""
        if self._ready:
            return
        try:
            from huggingface_hub import hf_hub_download
            from tokenizers import Tokenizer
            import onnxruntime as ort

            MODEL_DIR.mkdir(parents=True, exist_ok=True)
            cache_dir = str(MODEL_DIR)

            log.info("Loading embedding model: %s", self.model_name)
            model_path = hf_hub_download(
                self.model_name, "onnx/model.onnx", cache_dir=cache_dir
            )
            tokenizer_path = hf_hub_download(
                self.model_name, "tokenizer.json", cache_dir=cache_dir
            )

            self._session = ort.InferenceSession(
                model_path, providers=["CPUExecutionProvider"]
            )
            self._tokenizer = Tokenizer.from_file(tokenizer_path)
            self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
            self._tokenizer.enable_truncation(max_length=256)
            self._ready = True
            log.info("Embedding model loaded (%d dimensions)", self.dim)

        except ImportError as e:
            log.warning(
                "Embedding dependencies not installed (%s). "
                "Install with: pip install onnxruntime tokenizers huggingface-hub numpy. "
                "Semantic search will be unavailable; FTS5 keyword search still works.",
                e,
            )
            raise
        except Exception as e:
            log.warning(
                "Failed to load embedding model: %s. Semantic search unavailable.", e
            )
            raise

    def embed(self, texts: list[str]) -> np.ndarray:
        """Embed a batch of texts. Returns (N, dim) float32 array, L2-normalized."""
        self._ensure_loaded()
        encoded = self._tokenizer.encode_batch(texts)
        input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
        token_type_ids = np.zeros_like(input_ids)

        outputs = self._session.run(None, {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "token_type_ids": token_type_ids,
        })

        # Mean pooling over token embeddings
        embeddings = outputs[0]  # shape: (batch, seq_len, dim)
        mask_expanded = attention_mask[:, :, np.newaxis].astype(np.float32)
        pooled = (embeddings * mask_expanded).sum(axis=1) / mask_expanded.sum(axis=1).clip(min=1e-9)

        # L2 normalize
        norms = np.linalg.norm(pooled, axis=1, keepdims=True).clip(min=1e-9)
        return (pooled / norms).astype(np.float32)

    def embed_one(self, text: str) -> bytes:
        """Embed a single text and return as bytes for sqlite-vec."""
        vec = self.embed([text])[0]
        return vec.tobytes()

    @property
    def available(self) -> bool:
        """Return True if the embedding model is loaded and ready."""
        try:
            self._ensure_loaded()
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_embedder: _Embedder | None = None


def _get_embedder() -> _Embedder | None:
    """Get or create the global embedder. Returns None if dependencies missing."""
    global _embedder
    if _embedder is None:
        _embedder = _Embedder()
    try:
        _embedder._ensure_loaded()
        return _embedder
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "_Embedder",
    "_get_embedder",
]
