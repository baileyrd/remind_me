"""
benchmarks.embedders — Offline, deterministic embedder for plumbing validation.

This mirrors the embedder contract used by ``remind_me_mcp`` (``embed`` /
``embed_one`` / ``available``) but needs no model download. It produces
content-seeded, L2-normalised 384-d vectors so the full sqlite-vec path can be
exercised offline. The vectors carry no real semantic signal, so this embedder
is for validating the pipeline — never for reporting retrieval quality.
"""

from __future__ import annotations

import hashlib

import numpy as np

from remind_me_mcp.config import EMBEDDING_DIM


class DeterministicEmbedder:
    """A model-free embedder: identical text always yields the identical vector."""

    @property
    def available(self) -> bool:
        """Always available — no model to load."""
        return True

    def embed(self, texts: list[str]) -> np.ndarray:
        """Return an (N, EMBEDDING_DIM) float32 array of L2-normalised vectors."""
        rows: list[np.ndarray] = []
        for text in texts:
            seed = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
            rng = np.random.default_rng(seed=seed)
            vec = rng.standard_normal(EMBEDDING_DIM).astype(np.float32)
            norm = float(np.linalg.norm(vec))
            if norm > 1e-9:
                vec /= norm
            rows.append(vec)
        return np.stack(rows, axis=0)

    def embed_one(self, text: str) -> bytes:
        """Embed one text and return raw float32 bytes for sqlite-vec storage."""
        return self.embed([text])[0].tobytes()
