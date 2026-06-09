"""
benchmarks.runner — Orchestrate a LongMemEval retrieval benchmark.

For each ingest mode, the runner stands up a :class:`~benchmarks.harness.Harness`,
then for every question it: resets the haystack, ingests the question's sessions
under the chosen strategy, runs the real ``remind_me_search``, maps the ranked
memories back to their sessions, and scores Recall@k / MRR at session
granularity. Results are aggregated overall and per question type, printed as a
Markdown table, and optionally written to JSON.

Run ``python -m benchmarks.runner --help`` for options.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from benchmarks import ingest as ingest_mod
from benchmarks import metrics as metrics_mod
from benchmarks.harness import Harness
from benchmarks.longmemeval import LongMemEvalItem, load_dataset
from benchmarks.metrics import QueryResult
from benchmarks.synthetic import make_dataset

if TYPE_CHECKING:
    from collections.abc import Sequence


def _parse_ks(spec: str) -> list[int]:
    """Parse a comma-separated list of cutoffs like ``1,3,5,10``."""
    ks = sorted({int(x) for x in spec.split(",") if x.strip()})
    if not ks:
        raise ValueError("--ks must contain at least one positive integer")
    return ks


async def _run_mode(
    items: Sequence[LongMemEvalItem],
    mode: str,
    embedder: str,
    ks: Sequence[int],
    limit: int,
    skip_abstention: bool,
    progress: bool,
) -> list[QueryResult]:
    """Ingest + search every item under one ingest mode; return per-question results."""
    strategy = ingest_mod.get_strategy(mode)
    results: list[QueryResult] = []

    harness = Harness(embedder_mode=embedder)
    harness.setup()
    try:
        for i, item in enumerate(items):
            if skip_abstention and item.is_abstention:
                continue

            harness.reset()
            id_to_session = harness.ingest_batch(strategy(item))

            memories = await harness.search(item.question, limit=limit)
            ranked_sessions = metrics_mod.dedup_preserve_order(
                id_to_session.get(m.get("id", ""), m.get("metadata", {}).get("session_id", ""))
                for m in memories
            )
            ranked_sessions = [s for s in ranked_sessions if s]

            results.append(
                QueryResult(
                    question_id=item.question_id,
                    question_type=item.question_type,
                    ranked_sessions=ranked_sessions,
                    relevant=set(item.answer_session_ids),
                    n_candidates=len(memories),
                )
            )
            if progress and (i + 1) % 25 == 0:
                print(f"  [{mode}] {i + 1}/{len(items)} questions...", file=sys.stderr)
    finally:
        harness.teardown()

    return results


def _results_to_payload(
    by_mode_results: dict[str, list[QueryResult]],
    ks: Sequence[int],
) -> dict:
    """Build a JSON-serialisable payload of aggregates + per-question detail."""
    payload: dict = {"ks": list(ks), "modes": {}}
    for mode, results in by_mode_results.items():
        buckets = metrics_mod.aggregate(results, ks)
        payload["modes"][mode] = {
            "aggregates": {
                qtype: {
                    "count": b.count,
                    "recall": {str(k): round(b.recall(k), 4) for k in ks},
                    "mrr": round(b.mrr, 4),
                }
                for qtype, b in buckets.items()
            },
            "questions": [
                {
                    "question_id": r.question_id,
                    "question_type": r.question_type,
                    "recall": {str(k): r.recall(k) for k in ks},
                    "mrr": round(r.mrr, 4),
                    "n_candidates": r.n_candidates,
                }
                for r in results
            ],
        }
    return payload


async def run(args: argparse.Namespace) -> int:
    """Execute the benchmark per parsed CLI args. Returns a process exit code."""
    ks = _parse_ks(args.ks)
    modes = [m.strip() for m in args.ingest.split(",") if m.strip()]

    if args.synthetic:
        items: list[LongMemEvalItem] = make_dataset(args.max_questions or 8)
    elif args.data:
        items = load_dataset(args.data)
        if args.max_questions:
            items = items[: args.max_questions]
    else:
        print("error: provide --data PATH or --synthetic", file=sys.stderr)
        return 2

    answerable = sum(1 for it in items if not (args.skip_abstention and it.is_abstention))
    print(
        f"Loaded {len(items)} questions ({answerable} scored) | "
        f"modes={modes} | embedder={args.embedder} | ks={ks}",
        file=sys.stderr,
    )

    import remind_me_mcp.retrieval as retr
    import remind_me_mcp.tools as tools_mod

    saved_sanitize = tools_mod.FTS_SANITIZE_FALLBACK
    saved_weights = (retr.RRF_W_KEYWORD, retr.RRF_W_RECENCY, retr.RRF_W_VITALITY)
    tools_mod.FTS_SANITIZE_FALLBACK = not args.no_sanitize
    if args.rrf_profile in ("retrieval", "semantic"):
        # Drop the relevance-irrelevant signals for a pure-retrieval ranking.
        retr.RRF_W_RECENCY = 0.0
        retr.RRF_W_VITALITY = 0.0
    if args.rrf_profile == "semantic":
        # Semantic-only: also drop the keyword tier so ranking is pure vector
        # search — the apples-to-apples mirror of MemPalace's ChromaDB headline
        # protocol (verbatim sessions + all-MiniLM-L6-v2). See RESULTS.md.
        retr.RRF_W_KEYWORD = 0.0
    try:
        by_mode_results: dict[str, list[QueryResult]] = {}
        for mode in modes:
            t0 = time.time()
            by_mode_results[mode] = await _run_mode(
                items, mode, args.embedder, ks, args.limit, args.skip_abstention, args.progress
            )
            print(f"  mode '{mode}' done in {time.time() - t0:.1f}s", file=sys.stderr)
    finally:
        tools_mod.FTS_SANITIZE_FALLBACK = saved_sanitize
        retr.RRF_W_KEYWORD, retr.RRF_W_RECENCY, retr.RRF_W_VITALITY = saved_weights

    by_mode_buckets = {
        mode: metrics_mod.aggregate(results, ks) for mode, results in by_mode_results.items()
    }
    table = metrics_mod.format_markdown_table(by_mode_buckets, ks)
    print("\n" + table + "\n")

    if args.embedder == "fake":
        print(
            "NOTE: embedder=fake uses meaningless vectors — these numbers validate "
            "the pipeline only, not retrieval quality. Use embedder=real for quality.",
            file=sys.stderr,
        )

    if args.out:
        payload = _results_to_payload(by_mode_results, ks)
        payload["table_markdown"] = table
        Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Wrote detailed results to {args.out}", file=sys.stderr)

    return 0


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser for the benchmark CLI."""
    p = argparse.ArgumentParser(
        prog="python -m benchmarks.runner",
        description="LongMemEval retrieval benchmark for Remind Me (Recall@k / MRR).",
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("--data", type=str, help="Path to a LongMemEval JSON file")
    src.add_argument("--synthetic", action="store_true", help="Use the built-in synthetic dataset")

    p.add_argument(
        "--ingest",
        default="verbatim,atomic",
        help="Comma-separated ingest modes: verbatim, turns, atomic (default: verbatim,atomic)",
    )
    p.add_argument(
        "--embedder",
        choices=["real", "fake", "none", "ollama"],
        default="real",
        help="Embedder: real ONNX model, ollama (local daemon), deterministic fake, or none/FTS-only",
    )
    p.add_argument("--ks", default="1,3,5,10", help="Comma-separated recall cutoffs (default: 1,3,5,10)")
    p.add_argument("--limit", type=int, default=100, help="Candidate pool size per query (default: 100)")
    p.add_argument("--max-questions", type=int, default=0, help="Cap number of questions (0 = all)")
    p.add_argument(
        "--include-abstention",
        dest="skip_abstention",
        action="store_false",
        help="Include _abs (unanswerable) questions in scoring (skipped by default)",
    )
    p.add_argument("--progress", action="store_true", help="Print progress to stderr")
    p.add_argument("--out", type=str, help="Write detailed results JSON to this path")
    p.add_argument(
        "--no-sanitize",
        action="store_true",
        help="Disable the FTS5 query-sanitization fallback (legacy behavior; for before/after)",
    )
    p.add_argument(
        "--rrf-profile",
        choices=["default", "retrieval", "semantic"],
        default="default",
        help=(
            "RRF signal profile: 'default' (all four signals), 'retrieval' (drop "
            "recency+vitality), or 'semantic' (semantic vector search only — drop "
            "keyword+recency+vitality; mirrors MemPalace's ChromaDB headline protocol)"
        ),
    )
    p.set_defaults(skip_abstention=True)
    return p


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    from benchmarks import quiet_dependency_logs

    quiet_dependency_logs()
    args = build_parser().parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
