"""
benchmarks.longmemeval — LongMemEval dataset loader.

LongMemEval (https://github.com/xiaowu0162/LongMemEval) ships its data as a
single JSON array. Each element is one question evaluated against a "haystack"
of chat sessions; the gold evidence is identified at *session* granularity via
``answer_session_ids``. Three sizes are published (``_oracle``, ``_s``, ``_m``)
that share this schema and differ only in haystack size.

This module is deliberately schema-tolerant: field names have varied slightly
across releases, so we accept a few aliases and fail loudly only when a truly
required field is missing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass
class Turn:
    """A single chat turn within a session."""

    role: str
    content: str
    has_answer: bool = False


@dataclass
class LongMemEvalItem:
    """One LongMemEval question plus its haystack of sessions.

    ``sessions`` and ``session_ids`` are index-aligned. ``answer_session_ids``
    is the set of sessions that contain the evidence for the answer.
    """

    question_id: str
    question_type: str
    question: str
    answer: str
    sessions: list[list[Turn]]
    session_ids: list[str]
    answer_session_ids: set[str]
    question_date: str | None = None

    @property
    def is_abstention(self) -> bool:
        """LongMemEval marks unanswerable ("abstention") questions with an ``_abs`` id."""
        return self.question_id.endswith("_abs")

    def iter_sessions(self) -> Iterator[tuple[str, list[Turn]]]:
        """Yield ``(session_id, turns)`` pairs in haystack order."""
        yield from zip(self.session_ids, self.sessions, strict=False)


def _first_present(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Return the first present key's value from *d*, else *default*."""
    for key in keys:
        if key in d and d[key] is not None:
            return d[key]
    return default


def _parse_turn(raw: dict[str, Any]) -> Turn:
    """Parse a single turn dict, tolerating role/content field aliases."""
    role = str(_first_present(raw, "role", "sender", "speaker", default="unknown"))
    content = _first_present(raw, "content", "text", "message", default="")
    if isinstance(content, list):
        # Some exports use a content-parts array; concatenate any text parts.
        parts = [p.get("text", "") if isinstance(p, dict) else str(p) for p in content]
        content = "\n".join(p for p in parts if p)
    has_answer = bool(_first_present(raw, "has_answer", "is_evidence", default=False))
    return Turn(role=role, content=str(content), has_answer=has_answer)


def parse_item(raw: dict[str, Any]) -> LongMemEvalItem:
    """Parse one raw LongMemEval record into a :class:`LongMemEvalItem`."""
    sessions_raw = _first_present(raw, "haystack_sessions", "sessions", default=[])
    sessions = [[_parse_turn(t) for t in session] for session in sessions_raw]

    session_ids = _first_present(raw, "haystack_session_ids", "session_ids", default=None)
    if session_ids is None:
        # Fall back to positional ids if the dataset omits explicit ones.
        session_ids = [f"session_{i}" for i in range(len(sessions))]
    session_ids = [str(s) for s in session_ids]

    answer_ids = _first_present(raw, "answer_session_ids", "evidence_session_ids", default=[])
    answer_session_ids = {str(s) for s in answer_ids}

    return LongMemEvalItem(
        question_id=str(_first_present(raw, "question_id", "id", default="unknown")),
        question_type=str(_first_present(raw, "question_type", "type", default="unknown")),
        question=str(_first_present(raw, "question", default="")),
        answer=str(_first_present(raw, "answer", default="")),
        sessions=sessions,
        session_ids=session_ids,
        answer_session_ids=answer_session_ids,
        question_date=_first_present(raw, "question_date"),
    )


def load_dataset(path: str | Path) -> list[LongMemEvalItem]:
    """Load a LongMemEval JSON file into a list of items.

    Accepts either a top-level JSON array, or a JSON object with a ``data`` /
    ``questions`` array.
    """
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = _first_present(raw, "data", "questions", "items", default=[])
    if not isinstance(raw, list):
        raise ValueError(f"Unexpected LongMemEval structure in {path}: expected a list of questions")
    return [parse_item(item) for item in raw]
