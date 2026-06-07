# Retrieval Benchmark Harness

Measures how well `remind_me_search` retrieves the right memories, using the
**same** RRF + hybrid (FTS5 + `sqlite-vec`) stack the real MCP server uses. The
primary dataset is **LongMemEval**, and the headline metric is **Recall@k at
session granularity** — directly comparable to the "R@5" numbers other
long-term-memory systems (e.g. MemPalace) publish.

The harness lives outside the distributed wheel (`pyproject.toml` only packages
`remind_me_mcp`) — it's a research/dev tool, not a runtime dependency.

## What it measures

For each question, the harness:

1. **Resets** to an empty haystack.
2. **Ingests** that question's chat sessions as memories (one of three ingest
   strategies, below).
3. **Searches** with the question text via the real `remind_me_search` tool.
4. **Maps** each ranked memory back to the session it came from, dedups to a
   ranked list of sessions, and scores against the gold `answer_session_ids`.

Metrics: **Recall@k** (1.0 if any gold session is in the top-k) and **MRR**,
reported overall and broken down by LongMemEval question type.

## Install

From the repo root:

```bash
uv pip install -e ".[semantic]"           # core + sqlite-vec + onnxruntime
uv pip install pytest pytest-asyncio       # only needed to run the benchmark tests
```

`[semantic]` is required for a meaningful run — without it there is no vector
index and you measure FTS5 keyword search only.

## Quick check (no download, fully offline)

```bash
python -m benchmarks.runner --synthetic --embedder none --ingest verbatim,turns,atomic
```

The synthetic dataset is built so FTS5 alone finds the gold session
deterministically (Recall@1 == 1.0). Use it to confirm the pipeline works
before downloading the real data.

## Run on LongMemEval

1. Download the dataset from the
   [LongMemEval repo](https://github.com/xiaowu0162/LongMemEval) (e.g.
   `longmemeval_s.json` or `longmemeval_oracle.json`).
2. Run:

```bash
python -m benchmarks.runner \
  --data /path/to/longmemeval_s.json \
  --ingest verbatim,atomic \
  --embedder real \
  --ks 1,3,5,10 \
  --progress \
  --out results.json
```

`--embedder real` downloads `all-MiniLM-L6-v2` (~80 MB) on first use and needs
HuggingFace access. The output is a Markdown table (stdout) plus a detailed
JSON file (`--out`) containing per-question scores.

### Useful flags

| Flag | Purpose |
|------|---------|
| `--ingest` | Comma-separated modes to compare: `verbatim`, `turns`, `atomic` |
| `--embedder` | `real` (ONNX model), `fake` (offline plumbing only), `none` (FTS5-only) |
| `--ks` | Recall cutoffs (default `1,3,5,10`) |
| `--limit` | Candidate pool size per query (default `100`) |
| `--max-questions` | Cap the number of questions (smoke runs) |
| `--include-abstention` | Score `_abs` questions too (skipped by default) |
| `--out` | Write detailed per-question results JSON |

## Ingest strategies

| Mode | Granularity | Notes |
|------|-------------|-------|
| `verbatim` | one memory per session | Apples-to-apples with verbatim systems; isolates pure retrieval |
| `turns` | one memory per chat turn | Middle ground |
| `atomic` | one memory per sentence | **Heuristic, offline proxy** for `remind_me_decompose` |

### Important caveat on `atomic`

Remind Me's real decomposition (`remind_me_decompose`) is **Claude-driven**: the
tool receives atomic facts extracted by an LLM — it does not call a model
itself. A fully offline, deterministic retrieval benchmark therefore can't
reproduce true decomposition. The `atomic` mode approximates the *granularity*
of decomposition with a sentence splitter so you can measure whether
finer-grained storage helps or hurts recall. To benchmark real decomposition,
register an LLM-backed strategy in `benchmarks/ingest.py::DECOMPOSERS` and pass
its name to `--ingest`.

## Interpreting the numbers

- **Session-level Recall@k** is the comparison metric. MemPalace reports R@5;
  run with `--ks 5` (verbatim ingest, real embedder) for the closest analogue.
- NL questions rarely satisfy FTS5's implicit-AND keyword matching, so on real
  data the **semantic tier carries most recall** — expect `--embedder none` to
  score far lower than `--embedder real`. That contrast is itself a useful
  finding about the system.
- `--embedder fake` produces content-seeded random vectors. It validates the
  full `sqlite-vec` path offline but its scores are **not** meaningful for
  quality — the runner prints a reminder when you use it.

## Tests

```bash
pytest tests/test_benchmarks.py -q
```

These are fully offline (the end-to-end test uses `--embedder none`).

## Limitations / honesty notes

- Recall is scored at **session** granularity (LongMemEval's gold unit). Turn-
  and atomic-level ingest are mapped back to their source session for scoring.
- This harness measures **retrieval only**. End-to-end QA accuracy (feeding
  retrieved context to Claude and grading the answer) is intentionally out of
  scope to keep runs offline, deterministic, and free.
- ACT-R vitality decay is effectively neutral here: each query runs on a fresh
  haystack and `include_dormant=True`, so decay never hides a just-ingested
  result. Benchmarking decay's effect over time would need a different harness.
