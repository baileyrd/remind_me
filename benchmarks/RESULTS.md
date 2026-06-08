# Benchmark Results

## LongMemEval-S — headline retrieval numbers

Run on `longmemeval_s_cleaned.json` (the standard ~115k-token haystacks *with
distractor sessions*), 470 scored questions (30 abstention questions skipped),
hybrid retrieval (`--embedder real`), session-level Recall@k / MRR.

Reproduce:

```bash
python -m benchmarks.download_data --dataset s
python -m benchmarks.runner \
  --data benchmarks/data/longmemeval_s_cleaned.json \
  --ingest verbatim,atomic --embedder real --ks 1,3,5,10 --out results_s.json
```

| Mode | Question type | N | R@1 | R@3 | R@5 | R@10 | MRR |
| --- | --- | --- | --- | --- | --- | --- | --- |
| verbatim | overall | 470 | 0.687 | 0.943 | 0.970 | 0.991 | 0.814 |
| verbatim | knowledge-update | 72 | 0.833 | 1.000 | 1.000 | 1.000 | 0.910 |
| verbatim | multi-session | 121 | 0.793 | 0.950 | 0.983 | 0.992 | 0.877 |
| verbatim | single-session-assistant | 56 | 0.679 | 0.946 | 1.000 | 1.000 | 0.811 |
| verbatim | single-session-preference | 30 | 0.433 | 0.867 | 0.900 | 0.967 | 0.657 |
| verbatim | single-session-user | 64 | 0.500 | 0.938 | 0.969 | 0.984 | 0.704 |
| verbatim | temporal-reasoning | 127 | 0.661 | 0.921 | 0.945 | 0.992 | 0.795 |
| atomic | overall | 470 | 0.851 | 0.940 | 0.953 | 0.972 | 0.901 |
| atomic | knowledge-update | 72 | 0.972 | 0.986 | 0.986 | 0.986 | 0.980 |
| atomic | multi-session | 121 | 0.860 | 0.926 | 0.950 | 0.983 | 0.902 |
| atomic | single-session-assistant | 56 | 0.911 | 1.000 | 1.000 | 1.000 | 0.952 |
| atomic | single-session-preference | 30 | 0.567 | 0.733 | 0.800 | 0.833 | 0.674 |
| atomic | single-session-user | 64 | 0.828 | 0.953 | 0.969 | 1.000 | 0.897 |
| atomic | temporal-reasoning | 127 | 0.827 | 0.945 | 0.945 | 0.961 | 0.887 |

### Comparison with MemPalace (R@5)

| System | R@5 |
|---|---|
| MemPalace — semantic only | 0.966 |
| MemPalace — hybrid v4 | 0.984 |
| MemPalace — + LLM rerank | ≥0.99 |
| **Remind Me — verbatim** | **0.970** |
| **Remind Me — atomic** | 0.953 |

Remind Me's verbatim R@5 (**0.970**) is on par with MemPalace's semantic-only
figure and just under their hybrid-v4 — with **no LLM reranker**, which is
exactly the lever that takes MemPalace from 0.984 → ≥0.99. Treat this as "same
ballpark," not a precise win/loss: this harness scores **session-level** recall,
and if MemPalace's R@5 uses a different retrieval unit (rounds/drawers) the
denominators aren't identical.

### Verbatim vs. atomic — the main finding

Decomposition (the `atomic` mode) trades deep coverage for top-rank precision:

| Metric | verbatim | atomic | winner |
|---|---|---|---|
| R@1 | 0.687 | **0.851** | atomic **+0.164** |
| MRR | 0.814 | **0.901** | atomic +0.087 |
| R@5 | **0.970** | 0.953 | verbatim |
| R@10 | **0.991** | 0.972 | verbatim |

- **Atomic → precision at the top.** Sentence-level memories match the specific
  fact a question asks about, so the gold evidence ranks **#1 far more often**
  (+16 pts R@1). This is the regime that matters when injecting the top 1–3
  memories into an LLM.
- **Verbatim → coverage at depth.** Whole sessions are almost never missed by
  R@10, but surrounding text dilutes the match so they rank lower.

**Takeaway:** prefer the decompose/atomic path for the capture pipeline; for the
best of both, store atomic facts linked to their parent session
(`source_capture_id`), retrieve atomically for precision, then expand to the
parent via `remind_me_get_capture` for context.

Two caveats worth noting:

- `atomic` here is the **heuristic sentence splitter**, not the real
  Claude-driven `remind_me_decompose`. A real LLM extracting clean atomic facts
  would likely push R@1 *higher*, so 0.851 is effectively a **lower bound** on
  the decompose path.
- **Weak spot: `single-session-preference`** (verbatim R@1 0.433; atomic is
  *worse* at R@5, 0.800 vs 0.900). Preference statements are short, scattered,
  and phrased unlike the question, and splitting strips the context that made
  them findable. Only 30 questions (noisy), but the consistent loser — the best
  target for an LLM reranker or query expansion.

### Known improvement levers

1. **LLM reranking** on the top-k — the most direct path to close the gap to
   MemPalace's ≥0.99.
2. **Preference-query handling** (query expansion / reranking) — the weakest
   category for both ingest modes.
3. **Atomic-with-parent-expansion** — combine atomic R@1 with verbatim coverage.

## RRF retrieval profile — dropping recency + vitality

`rank_rrf` fused **four equally-weighted** signals: keyword, semantic, recency,
and vitality. Recency and vitality are the right features for a *living*
personal memory, but on a retrieval benchmark every memory is ingested at once
(recency ≈ ingest order) with vitality ≈ 1.0 — so those two signals are
relevance-irrelevant noise that together made up **half** of the fused score and
could demote correct evidence.

The signals are now individually weighted (defaults all `1.0`, so behavior is
unchanged), configurable per deployment via env vars
(`REMIND_ME_RRF_W_KEYWORD|SEMANTIC|RECENCY|VITALITY`) and selectable as a
profile in the benchmark.

**The lever is proven deterministically** in `tests/test_rrf_weights.py`: a
less-relevant but newer/higher-vitality memory ranks #1 under the default
weights, and the relevant memory reclaims #1 once recency+vitality are dropped.

Measure the effect on real data (one command, runs the same set with the signals
on then off, prints Recall@k/MRR deltas):

```bash
python -m benchmarks.before_after \
  --compare rrf \
  --data benchmarks/data/longmemeval_s_cleaned.json \
  --ingest atomic --embedder real --ks 1,3,5,10
```

> Not yet run on `longmemeval_s` here — measuring it requires the embedding model
> and dataset (network access). Expect the gain to concentrate in **R@1 / MRR**,
> where precise ordering matters; R@5/R@10 should be roughly unchanged since the
> right session is usually already in the candidate pool. Paste the resulting
> table here once you've run it. A full per-type run under the profile is also
> available via `python -m benchmarks.runner --rrf-profile retrieval ...`.

## FTS5 query-sanitization fix — before/after

Natural-language questions contain punctuation (`?`, `,`, `'`, `$`, `.`) that
FTS5 treats as operator syntax, so the raw query was an invalid `MATCH`
expression and the **keyword tier was silently skipped**. The fix retries an
invalid query as a sanitized OR-of-terms expression (see
`remind_me_mcp/tools.py::_sanitize_fts_query`).

Reproduce:

```bash
python -m benchmarks.before_after --ks 1,3,5            # bundled sample, FTS-only
```

### Bundled sample set (12 punctuated questions, `--embedder none`)

This isolates the keyword tier — the clearest demonstration of the fix.

| Variant | R@1 | R@3 | R@5 | MRR |
|---|---|---|---|---|
| **before** (no sanitize) | 0.000 | 0.000 | 0.000 | 0.000 |
| **after** (sanitize) | 0.917 | 1.000 | 1.000 | 0.958 |
| **Δ** | **+0.917** | **+1.000** | **+1.000** | **+0.958** |

Before the fix, *every* punctuated question failed the FTS5 parse and returned
zero keyword candidates. After, the keyword tier recovers them.

> The single R@1 miss (the `knowledge-update` question) is a genuine ranking
> call, not a parse failure: several sessions mention "analyst"/"title", so the
> right session lands at rank 2 — it's recovered by R@3.

### What this means with semantic search on

With `--embedder real`, retrieval was already carried by the vector tier, so
overall recall was decent *despite* the dead keyword tier. The fix restores the
keyword half of the hybrid, which (a) helps exact-term and rare-token questions
that embeddings can miss, and (b) makes the RRF fusion actually fuse two signals
instead of one. Run the same command with `--embedder real` on a LongMemEval
file to measure the effect inside full hybrid ranking on your machine.

## Notes / caveats

- The bundled sample is a small, hand-built set designed to exercise the keyword
  path deterministically and offline — treat it as a demonstration, not a
  leaderboard. Use real LongMemEval files (`benchmarks/download_data.py`) for
  headline numbers.
- All numbers here are **retrieval-only** (Recall@k / MRR at session
  granularity). End-to-end QA accuracy is out of scope (see `README.md`).
