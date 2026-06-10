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
import time
from typing import Any

import numpy as np

from remind_me_mcp.config import (
    EMBED_CHUNK_CHARS,
    EMBED_CHUNK_OVERLAP,
    EMBED_MAX_CHUNKS,
    EMBEDDING_BACKEND,
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    MODEL_DIR,
    OLLAMA_EMBED_MODEL,
    OLLAMA_URL,
)

log = logging.getLogger("remind_me_mcp.embeddings")

# Availability/initialisation results are cached briefly so hot paths (every
# search calls _get_embedder()) don't repeat network probes (Ollama) or
# HuggingFace downloads (ONNX) on each invocation (PF-01). The monotonic
# clock makes the TTLs immune to wall-clock adjustments.
AVAILABILITY_SUCCESS_TTL = 60.0
"""Seconds a successful availability probe stays cached."""
AVAILABILITY_FAILURE_TTL = 30.0
"""Seconds a failed availability probe / model load stays cached before a retry."""


# ---------------------------------------------------------------------------
# Sliding-window chunking
# ---------------------------------------------------------------------------


def chunk_text(
    text: str,
    *,
    max_chars: int = EMBED_CHUNK_CHARS,
    overlap: int = EMBED_CHUNK_OVERLAP,
    max_chunks: int = EMBED_MAX_CHUNKS,
) -> list[str]:
    """Split text into overlapping character windows for multi-vector embedding.

    Content at or under ``max_chars`` returns a single chunk ``[text]`` — so
    short memories (and ``atomic`` facts) embed exactly as before. Longer content
    is cut into windows of up to ``max_chars`` characters that overlap by
    ``overlap`` characters, so evidence straddling a boundary still lands whole in
    at least one window. Each cut prefers the nearest whitespace before the limit
    to avoid splitting mid-word. At most ``max_chunks`` windows are produced; any
    remaining tail is dropped (callers should log when truncation is possible).

    Args:
        text: The content to split.
        max_chars: Maximum characters per window.
        overlap: Characters of overlap between consecutive windows.
        max_chunks: Hard cap on the number of windows returned.

    Returns:
        A list of non-empty chunk strings (always at least one for non-blank text).
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    # Clamp overlap to a sane range so the window always advances.
    step = max(1, max_chars - max(0, min(overlap, max_chars - 1)))
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n and len(chunks) < max_chunks:
        end = min(start + max_chars, n)
        # Prefer to break on whitespace before the hard limit (but not so early
        # that the window becomes tiny).
        if end < n:
            ws = text.rfind(" ", start + step, end)
            if ws != -1:
                end = ws
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= n:
            break
        # Advance with overlap, then snap the new start to a word boundary so the
        # next window never begins mid-word (which would emit junk fragments).
        next_start = end - overlap if end - overlap > start else end
        sp = text.rfind(" ", start, next_start)
        start = sp + 1 if sp > start else next_start
    return chunks

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
        # Typed as Any: onnxruntime/tokenizers objects assigned lazily in _ensure_loaded().
        self._session: Any = None
        self._tokenizer: Any = None
        self._ready = False
        # Failure caching (PF-01): missing dependencies are permanent for this
        # process; other load failures (e.g. offline HuggingFace download) are
        # retried only after AVAILABILITY_FAILURE_TTL instead of on every call.
        self._deps_missing = False
        self._failed_until = 0.0

    def _ensure_loaded(self) -> None:
        """Lazily load the ONNX model and tokenizer from HuggingFace Hub.

        Load failures are cached (PF-01): an ImportError marks the embedder
        permanently unavailable for this process, while any other failure
        (network, corrupt model, ...) is only retried after
        AVAILABILITY_FAILURE_TTL seconds, so an offline machine doesn't
        re-attempt a HuggingFace download on every search.
        """
        if self._ready:
            return
        if self._deps_missing:
            raise RuntimeError("Embedding dependencies are not installed (cached failure)")
        if time.monotonic() < self._failed_until:
            raise RuntimeError(
                "Embedding model failed to load recently (cached failure; will retry later)"
            )
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
            self._tokenizer.enable_truncation(max_length=512)
            self._ready = True
            log.info("Embedding model loaded (%d dimensions)", self.dim)

        except ImportError as e:
            self._deps_missing = True
            log.warning(
                "Embedding dependencies not installed (%s). "
                "Install with: pip install onnxruntime tokenizers huggingface-hub numpy. "
                "Semantic search will be unavailable; FTS5 keyword search still works.",
                e,
            )
            raise
        except Exception as e:  # Broad catch intentional: ONNX Runtime raises non-stdlib exceptions (e.g., onnxruntime.capi.onnxruntime_pybind11_state.InvalidGraph)
            self._failed_until = time.monotonic() + AVAILABILITY_FAILURE_TTL
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
            texts: List of strings to embed (each truncated to 512 tokens; use
                chunk_text() upstream to embed longer content as several windows).

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
        # Availability cache (PF-01): every embed() outcome refreshes it, so
        # `available` only sends a real "ping" probe when the cache is cold.
        self._available: bool | None = None
        self._avail_expires = 0.0

    def _note_availability(self, ok: bool) -> None:
        """Record an embed outcome in the availability cache (PF-01)."""
        self._available = ok
        self._avail_expires = time.monotonic() + (
            AVAILABILITY_SUCCESS_TTL if ok else AVAILABILITY_FAILURE_TTL
        )

    def embed(self, texts: list[str]) -> np.ndarray:
        """Embed a batch of texts via Ollama, returning L2-normalised float32 vectors."""
        import httpx

        try:
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
        except Exception:
            self._note_availability(False)
            raise
        self._note_availability(True)
        norms = np.linalg.norm(arr, axis=1, keepdims=True).clip(min=1e-9)
        return (arr / norms).astype(np.float32)

    def embed_one(self, text: str) -> bytes:
        """Embed a single text and return raw float32 bytes for sqlite-vec storage."""
        return self.embed([text])[0].tobytes()

    @property
    def available(self) -> bool:
        """Return True if the Ollama daemon answers an embedding request.

        The result is cached (PF-01): a success is trusted for
        AVAILABILITY_SUCCESS_TTL seconds and a failure for
        AVAILABILITY_FAILURE_TTL seconds, so the hot search path doesn't pay
        a real HTTP "ping" round-trip on every call.
        """
        if self._available is not None and time.monotonic() < self._avail_expires:
            return self._available
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
    "chunk_text",
]
