"""
benchmarks.ingest — Ingest strategies (how a haystack becomes memories).

A strategy converts an item's sessions into a flat list of memory *units*, each
tagged with the session it came from so retrieval can be scored at session
granularity. Three strategies are provided:

- ``verbatim``  — one memory per session (full session text). This is the
  apples-to-apples mode versus verbatim memory systems like MemPalace, and it
  isolates pure retrieval over whole sessions.
- ``turns``     — one memory per chat turn.
- ``atomic``    — one memory per sentence (a heuristic, OFFLINE proxy for the
  Claude-driven ``remind_me_decompose`` tool). Real decomposition asks an LLM
  to extract atomic facts; that needs an API call and so is out of scope for a
  fully offline, deterministic retrieval benchmark. The sentence splitter
  approximates the *granularity* of decomposition so we can measure whether
  finer-grained storage helps or hurts recall. Swap in a real LLM decomposer
  via :data:`DECOMPOSERS` when you want the true behavior.

Each unit keeps its ``session_id`` so the scorer can map any retrieved memory
back to the session whose evidence it represents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from benchmarks.longmemeval import LongMemEvalItem, Turn

# Sentence boundary: end punctuation followed by whitespace. Deliberately simple
# and dependency-free — good enough to approximate atomic-fact granularity.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


@dataclass
class MemoryUnit:
    """A single unit of text to store, tagged with its source session."""

    content: str
    session_id: str


def _turn_text(turn: Turn) -> str:
    """Render a turn as role-prefixed text (mirrors the importer's all_messages mode)."""
    role = turn.role.strip().capitalize() or "Unknown"
    return f"{role}: {turn.content.strip()}"


def _session_text(turns: list[Turn]) -> str:
    """Concatenate a whole session into one verbatim block."""
    return "\n".join(_turn_text(t) for t in turns if t.content.strip())


def split_sentences(text: str) -> list[str]:
    """Split *text* into sentences using a lightweight regex. Never returns empties."""
    parts = _SENTENCE_SPLIT.split(text.strip())
    return [p.strip() for p in parts if p.strip()]


def ingest_verbatim(item: LongMemEvalItem) -> list[MemoryUnit]:
    """One memory per session."""
    units: list[MemoryUnit] = []
    for sid, turns in item.iter_sessions():
        text = _session_text(turns)
        if text:
            units.append(MemoryUnit(content=text, session_id=sid))
    return units


def ingest_turns(item: LongMemEvalItem) -> list[MemoryUnit]:
    """One memory per turn."""
    units: list[MemoryUnit] = []
    for sid, turns in item.iter_sessions():
        for turn in turns:
            text = _turn_text(turn)
            if turn.content.strip():
                units.append(MemoryUnit(content=text, session_id=sid))
    return units


def ingest_atomic(item: LongMemEvalItem) -> list[MemoryUnit]:
    """One memory per sentence (heuristic, offline proxy for LLM decomposition)."""
    units: list[MemoryUnit] = []
    for sid, turns in item.iter_sessions():
        for turn in turns:
            for sentence in split_sentences(turn.content):
                # Keep a light role prefix so speaker context survives splitting.
                role = turn.role.strip().capitalize() or "Unknown"
                units.append(MemoryUnit(content=f"{role}: {sentence}", session_id=sid))
    return units


# Registry of available ingest strategies. A real LLM-backed decomposer can be
# registered here under a new key without touching the runner.
DECOMPOSERS: dict[str, Callable[[LongMemEvalItem], list[MemoryUnit]]] = {
    "verbatim": ingest_verbatim,
    "turns": ingest_turns,
    "atomic": ingest_atomic,
}


def get_strategy(name: str) -> Callable[[LongMemEvalItem], list[MemoryUnit]]:
    """Look up an ingest strategy by name, raising a clear error if unknown."""
    try:
        return DECOMPOSERS[name]
    except KeyError:
        valid = ", ".join(sorted(DECOMPOSERS))
        raise ValueError(f"Unknown ingest mode {name!r}. Valid modes: {valid}") from None
