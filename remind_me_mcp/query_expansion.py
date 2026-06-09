"""
remind_me_mcp.query_expansion — Optional HyDE query expansion for semantic search.

The weakest retrieval categories are questions phrased nothing like the memory
that answers them (short scattered preferences, multi-hop temporal questions).
HyDE (Hypothetical Document Embeddings) bridges that gap: a small local LLM
writes a short passage that *would* answer the question, and the passage's
embedding — which lives in document-space, not question-space — is averaged
with the query's before the vector search.

Off by default. Enable with ``REMIND_ME_QUERY_EXPANSION=hyde``; generation uses
the local Ollama daemon (same URL as the Ollama embedding backend). Any failure
(daemon down, model missing, timeout) silently falls back to the plain query —
expansion must never break or slow search beyond its timeout.
"""

from __future__ import annotations

import logging
import os

from remind_me_mcp.config import OLLAMA_URL

log = logging.getLogger("remind_me_mcp.query_expansion")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

EXPANSION_MODE: str = os.environ.get("REMIND_ME_QUERY_EXPANSION", "").lower()
"""Query expansion mode: '' (disabled, the default) or 'hyde'."""

HYDE_MODEL: str = os.environ.get("REMIND_ME_HYDE_MODEL", "llama3.2")
"""Ollama model used to write the hypothetical passage. Small instruct models work well."""

HYDE_TIMEOUT: float = float(os.environ.get("REMIND_ME_HYDE_TIMEOUT", "15"))
"""Seconds to wait for the generation before falling back to the plain query."""

HYDE_MAX_CHARS = 600  # Passage cap — one embedding window; longer adds noise, not signal.

_HYDE_PROMPT = (
    "Write a short passage (2-3 sentences) that could plausibly appear in a "
    "personal conversation log and that directly answers the question below. "
    "Invent plausible specifics. Output only the passage.\n\n"
    "Question: {query}\n\nPassage:"
)


# ---------------------------------------------------------------------------
# Generation + expansion
# ---------------------------------------------------------------------------


def _generate(prompt: str) -> str:
    """Run one non-streaming Ollama generation and return the raw response text."""
    import httpx

    # No num_predict cap: thinking models (qwen3, gemma) spend tokens on hidden
    # reasoning first, and a tight cap returns an empty response. The prompt asks
    # for 2-3 sentences and HYDE_MAX_CHARS truncates the rest.
    resp = httpx.post(
        f"{OLLAMA_URL.rstrip('/')}/api/generate",
        json={
            "model": HYDE_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.0},
        },
        timeout=HYDE_TIMEOUT,
    )
    resp.raise_for_status()
    return str(resp.json().get("response", ""))


def hyde_passage(query: str) -> str | None:
    """Generate a hypothetical answer passage for *query*, or None on any failure.

    Args:
        query: The natural-language search query.

    Returns:
        A non-empty passage capped at HYDE_MAX_CHARS, or None when generation
        fails or produces nothing usable.
    """
    try:
        passage = _generate(_HYDE_PROMPT.format(query=query)).strip()
    except Exception as e:  # Broad catch intentional: expansion must never break search
        log.warning("HyDE generation failed (%s); searching with the plain query.", e)
        return None
    return passage[:HYDE_MAX_CHARS] or None


def expand_query(query: str) -> list[str]:
    """Return extra retrieval texts for *query* per the configured mode.

    The returned texts are embedded alongside the query and averaged into a
    single search vector (see ``db._semantic_search``). Disabled mode returns
    ``[]``, which leaves the search vector exactly the query embedding.

    Args:
        query: The natural-language search query.

    Returns:
        A (possibly empty) list of expansion texts.
    """
    if EXPANSION_MODE != "hyde" or not query.strip():
        return []
    passage = hyde_passage(query)
    return [passage] if passage else []


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "EXPANSION_MODE",
    "HYDE_MODEL",
    "HYDE_TIMEOUT",
    "expand_query",
    "hyde_passage",
]
