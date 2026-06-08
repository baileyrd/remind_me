"""
benchmarks.metrics — Retrieval metrics and aggregation.

All metrics operate on a *ranked list of session IDs* (best first, already
deduplicated) and a *set of relevant session IDs* (the gold evidence sessions
for a question). This keeps the metric layer free of any Remind Me internals
so it can be unit-tested in isolation.

Recall@k here is the standard long-term-memory retrieval definition: 1.0 if at
least one relevant session appears within the top-k retrieved sessions, else
0.0. This matches how LongMemEval-style "R@k" numbers are reported.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence


def dedup_preserve_order(items: Iterable[str]) -> list[str]:
    """Return *items* with duplicates removed, keeping first-occurrence order."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def first_relevant_rank(ranked_sessions: Sequence[str], relevant: set[str]) -> int | None:
    """Return the 1-based rank of the first relevant session, or None if absent."""
    for idx, sid in enumerate(ranked_sessions, start=1):
        if sid in relevant:
            return idx
    return None


def recall_at_k(ranked_sessions: Sequence[str], relevant: set[str], k: int) -> float:
    """Return 1.0 if any relevant session is within the top-*k*, else 0.0."""
    if not relevant:
        return 0.0
    rank = first_relevant_rank(ranked_sessions[:k], relevant)
    return 1.0 if rank is not None else 0.0


def reciprocal_rank(ranked_sessions: Sequence[str], relevant: set[str]) -> float:
    """Return 1/rank of the first relevant session, or 0.0 if none retrieved."""
    rank = first_relevant_rank(ranked_sessions, relevant)
    return 1.0 / rank if rank is not None else 0.0


@dataclass
class QueryResult:
    """The retrieval outcome for a single benchmark question."""

    question_id: str
    question_type: str
    ranked_sessions: list[str]
    relevant: set[str]
    n_candidates: int

    def recall(self, k: int) -> float:
        """Recall@k for this question."""
        return recall_at_k(self.ranked_sessions, self.relevant, k)

    @property
    def mrr(self) -> float:
        """Reciprocal rank for this question."""
        return reciprocal_rank(self.ranked_sessions, self.relevant)


@dataclass
class MetricBucket:
    """Accumulates per-question scores for one (mode, question_type) cell."""

    count: int = 0
    recall_sums: dict[int, float] = field(default_factory=dict)
    mrr_sum: float = 0.0

    def add(self, result: QueryResult, ks: Sequence[int]) -> None:
        """Fold one query result into this bucket."""
        self.count += 1
        self.mrr_sum += result.mrr
        for k in ks:
            self.recall_sums[k] = self.recall_sums.get(k, 0.0) + result.recall(k)

    def recall(self, k: int) -> float:
        """Mean Recall@k over the questions in this bucket."""
        if self.count == 0:
            return 0.0
        return self.recall_sums.get(k, 0.0) / self.count

    @property
    def mrr(self) -> float:
        """Mean reciprocal rank over the questions in this bucket."""
        if self.count == 0:
            return 0.0
        return self.mrr_sum / self.count


def aggregate(results: Sequence[QueryResult], ks: Sequence[int]) -> dict[str, MetricBucket]:
    """Aggregate per-question results into overall + per-question-type buckets.

    Returns a dict keyed by question type, plus a special ``"overall"`` key.
    """
    buckets: dict[str, MetricBucket] = {"overall": MetricBucket()}
    for r in results:
        buckets["overall"].add(r, ks)
        buckets.setdefault(r.question_type, MetricBucket()).add(r, ks)
    return buckets


def format_markdown_table(
    by_mode: dict[str, dict[str, MetricBucket]],
    ks: Sequence[int],
) -> str:
    """Render a comparison table: rows = (mode, question type), cols = Recall@k + MRR.

    *by_mode* maps an ingest-mode name to its aggregated buckets (the output of
    :func:`aggregate`). Modes are rendered as grouped sections.
    """
    headers = ["Mode", "Question type", "N", *[f"R@{k}" for k in ks], "MRR"]
    lines = ["| " + " | ".join(headers) + " |"]
    lines.append("| " + " | ".join("---" for _ in headers) + " |")

    for mode, buckets in by_mode.items():
        # "overall" first, then the rest sorted for stable output.
        ordered = ["overall"] + sorted(t for t in buckets if t != "overall")
        for qtype in ordered:
            bucket = buckets[qtype]
            row = [
                mode,
                qtype,
                str(bucket.count),
                *[f"{bucket.recall(k):.3f}" for k in ks],
                f"{bucket.mrr:.3f}",
            ]
            lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)
