"""
benchmarks.before_after — Quantify the FTS5 query-sanitization fix.

Runs the *same* dataset twice against the real ``remind_me_search``:

- **before** — ``FTS_SANITIZE_FALLBACK`` off: a natural-language question with
  punctuation is an invalid FTS5 expression, so the keyword tier is skipped.
- **after**  — fallback on (the shipped default): the query is retried as a
  sanitized OR-of-terms expression, so the keyword tier contributes.

It prints a side-by-side Recall@k / MRR table plus the overall deltas. Run with
``--embedder none`` to isolate the keyword tier (the cleanest demonstration);
``--embedder real`` shows the effect inside full hybrid ranking.

    python -m benchmarks.before_after                 # uses the bundled sample set
    python -m benchmarks.before_after --data benchmarks/data/longmemeval_oracle.json --embedder real
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from benchmarks import metrics as metrics_mod
from benchmarks.longmemeval import load_dataset
from benchmarks.runner import _parse_ks, _run_mode
from benchmarks.synthetic import make_dataset

_SAMPLE = Path(__file__).resolve().parent / "sample" / "longmemeval_sample.json"


async def _score(items, mode, embedder, ks, limit, sanitize):
    """Run one pass with the sanitize fallback set to *sanitize*; return buckets."""
    import remind_me_mcp.tools as tools_mod

    saved = tools_mod.FTS_SANITIZE_FALLBACK
    tools_mod.FTS_SANITIZE_FALLBACK = sanitize
    try:
        results = await _run_mode(
            items, mode=mode, embedder=embedder, ks=ks, limit=limit,
            skip_abstention=True, progress=False,
        )
    finally:
        tools_mod.FTS_SANITIZE_FALLBACK = saved
    return metrics_mod.aggregate(results, ks)


async def run(args: argparse.Namespace) -> int:
    """Execute the before/after comparison."""
    ks = _parse_ks(args.ks)

    if args.data:
        items = load_dataset(args.data)
    elif args.sample:
        items = load_dataset(_SAMPLE)
    else:
        items = make_dataset(args.max_questions or 8)
    if args.max_questions:
        items = items[: args.max_questions]

    print(
        f"Before/after on {len(items)} questions | ingest={args.ingest} | "
        f"embedder={args.embedder} | ks={ks}",
        file=sys.stderr,
    )

    before = await _score(items, args.ingest, args.embedder, ks, args.limit, sanitize=False)
    after = await _score(items, args.ingest, args.embedder, ks, args.limit, sanitize=True)

    table = metrics_mod.format_markdown_table(
        {"before (no sanitize)": before, "after (sanitize)": after}, ks
    )
    print("\n" + table + "\n")

    b, a = before["overall"], after["overall"]
    print("Overall deltas (after − before):")
    for k in ks:
        print(f"  R@{k}: {b.recall(k):.3f} → {a.recall(k):.3f}  (Δ {a.recall(k) - b.recall(k):+.3f})")
    print(f"  MRR : {b.mrr:.3f} → {a.mrr:.3f}  (Δ {a.mrr - b.mrr:+.3f})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser."""
    p = argparse.ArgumentParser(
        prog="python -m benchmarks.before_after",
        description="Before/after comparison of the FTS5 query-sanitization fix.",
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("--data", type=str, help="LongMemEval JSON file")
    src.add_argument("--synthetic", dest="sample", action="store_false", help="Use the synthetic set")
    p.add_argument("--ingest", default="verbatim", help="Ingest mode (default: verbatim)")
    p.add_argument("--embedder", choices=["real", "fake", "none"], default="none")
    p.add_argument("--ks", default="1,3,5", help="Recall cutoffs (default: 1,3,5)")
    p.add_argument("--limit", type=int, default=100, help="Candidate pool size (default: 100)")
    p.add_argument("--max-questions", type=int, default=0, help="Cap number of questions")
    p.set_defaults(sample=True)
    return p


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = build_parser().parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
