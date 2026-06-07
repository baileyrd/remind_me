"""
benchmarks.synthetic — A tiny, self-contained dataset in LongMemEval shape.

Used for offline smoke testing and CI: it requires no download and is built so
that pure FTS5 keyword search (embedder ``none``) can find the gold session
deterministically — every question's distinctive token appears only in its
evidence session. This lets tests assert exact recall values without a model.
"""

from __future__ import annotations

from benchmarks.longmemeval import LongMemEvalItem, Turn

_FILLER_TOPICS = [
    "weekend hiking plans near the coast",
    "a recipe for sourdough bread",
    "tuning a road bike derailleur",
    "notes on a documentary about deep sea life",
    "planning a vegetable garden layout",
    "comparing two noise-cancelling headphones",
]


def _filler_session(idx: int) -> list[Turn]:
    """A distractor session with no benchmark-distinctive tokens."""
    topic = _FILLER_TOPICS[idx % len(_FILLER_TOPICS)]
    return [
        Turn(role="user", content=f"Let's talk about {topic}."),
        Turn(role="assistant", content=f"Sure, here are some thoughts on {topic}."),
    ]


def make_item(qnum: int, n_sessions: int = 6, question_type: str = "single-session-user") -> LongMemEvalItem:
    """Build one synthetic item whose evidence token appears only in one session."""
    token = f"zebracode{qnum}"
    question = f"what is {token}"

    sessions: list[list[Turn]] = []
    session_ids: list[str] = []
    evidence_index = qnum % n_sessions

    for i in range(n_sessions):
        sid = f"q{qnum}_s{i}"
        session_ids.append(sid)
        if i == evidence_index:
            sessions.append(
                [
                    Turn(role="user", content=f"By the way, {question}?"),
                    Turn(
                        role="assistant",
                        content=(
                            f"Good question — what is {token}: it is the internal "
                            f"codename for project number {qnum}."
                        ),
                        has_answer=True,
                    ),
                ]
            )
        else:
            sessions.append(_filler_session(i))

    return LongMemEvalItem(
        question_id=f"synthetic_{qnum}",
        question_type=question_type,
        question=question,
        answer=f"the codename for project {qnum}",
        sessions=sessions,
        session_ids=session_ids,
        answer_session_ids={session_ids[evidence_index]},
        question_date=None,
    )


def make_dataset(n_questions: int = 8) -> list[LongMemEvalItem]:
    """Build a small synthetic dataset, including one abstention item."""
    types = ["single-session-user", "single-session-assistant", "multi-session"]
    items = [make_item(i, question_type=types[i % len(types)]) for i in range(n_questions)]

    # One abstention question whose token appears nowhere — recall is undefined,
    # so the runner should skip it by default.
    abs_item = make_item(9999)
    abs_item.question_id = "synthetic_9999_abs"
    abs_item.answer_session_ids = set()
    # Strip the distinctive token from its evidence session so nothing matches.
    abs_item.sessions[9999 % 6] = _filler_session(0)
    items.append(abs_item)
    return items
