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

from remind_me_mcp.config import (
    EMBEDDING_BACKEND,
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    MODEL_DIR,
    OLLAMA_EMBED_MODEL,
    OLLAMA_URL,
)

log = logging.getLogger("remind_me_mcp.embeddings")

# ---------------------------------------------------------------------------
# Embedder class
# ---------------------------------------------------------------------------


class _Embedder:
    """Lightweight ONNX-based embedding engine. Downloads model on first use."""

    def __init__(self, model_name: str = EMBEDDING_MODEL, dim: int = EMBEDDING_DIM) -> None:
        """Initialise the embedder with model configuration.

        The model is not loaded at construction time — loading is deferred
        to the first call to embed() or embed_one() via _ensure_loaded().

        Args:
            model_name: HuggingFace model repository name or local path.
            dim: Expected output embedding dimension (must match the model).
        """
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
            import onnxruntime as ort
            from huggingface_hub import hf_hub_download
            from tokenizers import Tokenizer

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
        except Exception as e:  # Broad catch intentional: ONNX Runtime raises non-stdlib exceptions (e.g., onnxruntime.capi.onnxruntime_pybind11_state.InvalidGraph)
            log.warning(
                "Failed to load embedding model: %s. Semantic search unavailable.", e
            )
            raise

    def embed(self, texts: list[str]) -> np.ndarray:
        """Embed a batch of texts using the ONNX model.

        Tokenizes the texts, runs a forward pass through the ONNX session,
        applies mean pooling over token embeddings, and L2-normalises the
        result so cosine similarity equals dot product.

        Args:
            texts: List of strings to embed (will be truncated to 256 tokens).

        Returns:
            Float32 numpy array of shape (N, dim), L2-normalised.
        """
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
        """Embed a single text and return the vector as raw bytes.

        Convenience wrapper around embed() that packs the float32 numpy
        array into bytes for direct insertion into the sqlite-vec virtual
        table (which expects struct-packed float32 blobs).

        Args:
            text: The text to embed.

        Returns:
            Raw bytes of the float32 embedding vector, suitable for
            insertion into a sqlite-vec embedding column.
        """
        vec = self.embed([text])[0]
        return vec.tobytes()

    @property
    def available(self) -> bool:
        """Return True if the embedding model is loaded and ready."""
        try:
            self._ensure_loaded()
            return True
        except Exception:  # Broad catch intentional: graceful-degradation boundary — returns False on any ONNX failure
            return False


# ---------------------------------------------------------------------------
# Ollama embedding backend
# ---------------------------------------------------------------------------


class OllamaEmbedder:
    """Embedding engine backed by a local Ollama daemon.

    Calls Ollama's batch embedding endpoint (``POST /api/embed``) instead of
    running a model in-process. This unlocks stronger/multilingual retrievers
    (e.g. ``nomic-embed-text``, ``bge-m3``) and avoids any HuggingFace download.

    The returned vector length MUST equal :data:`EMBEDDING_DIM` (which is baked
    into the sqlite-vec table); a mismatch raises a clear error so the user fixes
    ``REMIND_ME_EMBEDDING_DIM`` and reindexes rather than silently corrupting the
    index.
    """

    def __init__(
        self,
        model: str = OLLAMA_EMBED_MODEL,
        url: str = OLLAMA_URL,
        dim: int = EMBEDDING_DIM,
        timeout: float = 60.0,
    ) -> None:
        """Configure the Ollama embedder (no network call until first embed)."""
        self.model = model
        self.url = url.rstrip("/")
        self.dim = dim
        self.timeout = timeout

    def embed(self, texts: list[str]) -> np.ndarray:
        """Embed a batch of texts via Ollama, returning L2-normalised float32 vectors."""
        import httpx

        resp = httpx.post(
            f"{self.url}/api/embed",
            json={"model": self.model, "input": texts},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        vectors = resp.json().get("embeddings")
        if not vectors:
            raise ValueError(f"Ollama returned no embeddings for model {self.model!r}")

        arr = np.asarray(vectors, dtype=np.float32)
        if arr.ndim != 2 or arr.shape[1] != self.dim:
            raise ValueError(
                f"Ollama model {self.model!r} returned dim {arr.shape[-1]}, "
                f"but REMIND_ME_EMBEDDING_DIM is {self.dim}. Set the dimension to match "
                "the model and run remind_me_reindex on a fresh vector table."
            )
        norms = np.linalg.norm(arr, axis=1, keepdims=True).clip(min=1e-9)
        return (arr / norms).astype(np.float32)

    def embed_one(self, text: str) -> bytes:
        """Embed a single text and return raw float32 bytes for sqlite-vec storage."""
        return self.embed([text])[0].tobytes()

    @property
    def available(self) -> bool:
        """Return True if the Ollama daemon answers an embedding request."""
        try:
            self.embed(["ping"])
            return True
        except Exception as e:  # Broad catch: any connection/HTTP/dim error => unavailable
            log.warning("Ollama embedder unavailable (%s). Semantic search disabled.", e)
            return False


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_embedder: _Embedder | OllamaEmbedder | None = None


def _get_embedder() -> _Embedder | OllamaEmbedder | None:
    """Get or create the global embedder for the configured backend.

    Returns None when the backend is unavailable (missing ONNX deps, or an
    unreachable Ollama daemon), so callers degrade to FTS5 keyword search.
    """
    global _embedder
    if _embedder is None:
        _embedder = OllamaEmbedder() if EMBEDDING_BACKEND == "ollama" else _Embedder()

    if isinstance(_embedder, OllamaEmbedder):
        return _embedder if _embedder.available else None

    try:
        _embedder._ensure_loaded()
        return _embedder
    except Exception:  # Broad catch intentional: returns None on any ONNX load failure — caller checks None
        return None


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "OllamaEmbedder",
    "_Embedder",
    "_get_embedder",
]
