# Benchmark Results

## LongMemEval-S — headline retrieval numbers

Run on `longmemeval_s_cleaned.json` (the standard ~115k-token haystacks *with
distractor sessions*), 470 scored questions (30 abstention questions skipped),
hybrid retrieval (`--embedder real`), session-level Recall@k / MRR.

> ⚠️ **This headline table predates the sliding-window chunking change** (it is the
> hybrid `--rrf-profile default` run, one vector per session). Chunking has not been
> re-measured under the hybrid profile here — expect verbatim to improve as it does
> in the model-matched semantic-only run below. For current, chunked numbers see
> **"Equal-footing comparison with MemPalace"**.

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

### Equal-footing comparison with MemPalace (R@5)

All Remind Me rows below are **measured, model-matched** (`all-MiniLM-L6-v2`, ONNX),
semantic-only, session-level R@5 over the same 470 scored LongMemEval-S questions
(`results_onnx_semantic.json`).

| System | R@5 | Retrieval |
|---|---|---|
| MemPalace — headline ("zero API") | 0.966 | verbatim sessions + ChromaDB vector search, semantic-only, `all-MiniLM-L6-v2` |
| MemPalace — held-out (450 q) | 0.984 | same |
| MemPalace — + Claude Haiku rerank | 1.000 | + LLM reranker (their words: "teaching to the test") |
| Plain keyword search (their baseline) | 0.938 | BM25-style, no embeddings |
| **Remind Me — atomic, semantic-only** | **0.992** | many small embeddings per session — beats headline, ties reranked, no LLM |
| **Remind Me — verbatim, semantic-only (chunked)** | **0.964** | sliding-window chunks, any-chunk-hit — ties the headline (was 0.923 with one vector/session; see "Sliding-window chunking" below) |
| Remind Me — verbatim, semantic-only, `snowflake-arctic-embed:33m` | 0.821 | secondary: this branch's tiny 33M Ollama model (not model-matched) |

Full per-type breakdown of the two model-matched runs (verbatim is now **chunked**):

| Mode | N | R@1 | R@3 | R@5 | R@10 | MRR |
|---|---|---|---|---|---|---|
| verbatim (semantic-only, chunked) | 470 | 0.851 | 0.940 | 0.964 | 0.983 | 0.901 |
| atomic (semantic-only) | 470 | 0.925 | 0.989 | 0.992 | 0.996 | 0.956 |

**What MemPalace's headline actually measures.** Their 96.6% R@5 run stores each
session verbatim and retrieves with a plain ChromaDB `collection.query()` using
the **`all-MiniLM-L6-v2`** embedding model — *no* palace-specific logic (wings /
rooms / drawers are not exercised), no write-time LLM, and no reranker. It is, in
effect, a vector-search baseline over ~50 candidate sessions per question. On the
same data **plain keyword search already scores 0.938**, so R@5 here barely
separates memory systems — it mostly measures the embedding model and the unit of
retrieval.

**The result: it was the retrieval unit, not the model.** Matched on model and
scored at session level, semantic-only:

- **Verbatim → 0.964 after chunking (was 0.923), now level with MemPalace's 0.966.**
  Remind Me originally stored **one embedding per whole session**, and MiniLM
  truncates at ~256 tokens, so on LongMemEval-S's long, distractor-padded sessions
  the single vector often never saw the evidence — capping verbatim at 0.923, ~4 pts
  under. MemPalace's verbatim path embeds each session as **multiple chunks/rounds**
  (any chunk hitting = a session hit). Once Remind Me does the same (see
  "Sliding-window chunking" below), verbatim rises to **0.964 R@5 — a statistical tie
  with the 0.966 headline**, confirming the gap was purely granularity, same model.
- **Atomic → 0.991, above MemPalace's 0.966 headline and 0.984 held-out, and level
  with their LLM-reranked 1.000 — with no LLM at write or rerank time** (R@1 also
  jumps to 0.926). Remind Me's `atomic` decomposition (sentence-level embeddings) is
  the architectural analog to MemPalace's chunking, taken further, and on the
  identical model it wins.

**How to reproduce** (force the ONNX backend so the flag isn't overridden by a local
`REMIND_ME_EMBEDDING_BACKEND=ollama`, then use the semantic profile):

```bash
REMIND_ME_EMBEDDING_BACKEND=onnx python -m benchmarks.runner \
  --data benchmarks/data/longmemeval_s_cleaned.json \
  --ingest verbatim,atomic --embedder real --ks 1,3,5,10 --rrf-profile semantic
```

Caveats: (a) the task is easy — keyword-only is already 0.938 — so absolute R@5 is
not very discriminating; (b) denominator is 470 (30 abstention questions skipped) vs.
MemPalace's ~500 / 450 held-out, so match the abstention handling before reading
exact decimals; (c) the **hybrid** numbers elsewhere in this file (verbatim 0.970,
atomic 0.953) come from an earlier run whose embedding backend was not verified to be
MiniLM — don't mix them into this model-matched comparison without re-running. That
clean semantic-only atomic (0.991) *exceeds* that prior hybrid atomic (0.953) is
itself consistent with the recency+vitality dilution documented below.

### Sliding-window chunking (lever B) — closing the verbatim gap

The verbatim gap above was a **granularity** artifact: one truncated vector per
session. That is now fixed. Long content is split into overlapping character
windows (`chunk_text`, defaults 1600 chars / 200 overlap, ≤16 windows), each
embedded as its own vector and linked to the parent memory via the new
`vec_chunks` map; the tokenizer cap was also raised 256 → 512. Semantic search
runs KNN over the per-chunk vectors and dedupes to the best chunk per memory —
**any-chunk-hit**, exactly MemPalace's "any chunk matching = a session hit". A
memory whose evidence sits in the tail is now retrievable (regression-tested in
`tests/test_chunking.py`). `atomic` is unaffected: its facts are short, so they
still yield a single chunk.

Tunable via `REMIND_ME_EMBED_CHUNK_CHARS` / `_OVERLAP` / `REMIND_ME_EMBED_MAX_CHUNKS`.

**Measured before/after** (model-matched, semantic-only, 470 scored questions,
`--rrf-profile semantic`). "Before" = one vector per session; "after" = chunked:

| Mode | Metric | Before | After | Δ |
|---|---|---|---|---|
| verbatim | R@1 | 0.760 | **0.851** | **+0.091** |
| verbatim | R@3 | 0.896 | 0.940 | +0.044 |
| verbatim | R@5 | 0.923 | **0.964** | **+0.041** |
| verbatim | R@10 | 0.968 | 0.983 | +0.015 |
| verbatim | MRR | 0.834 | 0.901 | +0.068 |
| atomic | R@5 | 0.991 | 0.992 | +0.001 |
| atomic | MRR | 0.956 | 0.956 | 0.000 |

The prediction held exactly: **verbatim R@5 0.923 → 0.964** (now a statistical tie
with MemPalace's 0.966 headline on 470 questions), with the largest gains at the top
of the ranking (**R@1 +0.091, MRR +0.068**) where the truncated tail used to lose the
evidence. **Atomic is unchanged** (±0.001 noise) — its facts are short, so they were
already single-chunk. The previously weakest category also recovered sharply:
verbatim `single-session-preference` went **R@1 0.433 → 0.700, R@5 0.900 → 0.967**.

Cost note: chunking multiplied verbatim's embedding work ~16× (up to 16 windows per
long session), so the model-matched verbatim run took ~2.4 h on CPU ONNX; atomic is
unaffected. Existing DBs migrate automatically (v7 → v8: legacy 1:1 vectors backfill
as chunk 0); `remind_me_reindex` re-chunks them with the new windows.

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

## Cross-encoder reranker over top-k (lever D)

RRF fuses independent rank lists, so it never scores the query and a candidate
*together* — the precision ceiling at rank 1. The shipped reranker
(`remind_me_mcp/reranker.py`) rescores the top `REMIND_ME_RERANK_TOP_K`
(default 20) RRF candidates with an ONNX cross-encoder and reorders only that
head; the tail keeps its RRF order, so reranking can never lose a candidate,
only promote one. The reordering logic is proven deterministically in
`tests/test_reranker.py` with an injected scorer.

**On by default as of the application capability review (issue #50).**
Previously off-by-default with `cross-encoder/ms-marco-MiniLM-L6-v2` (2019);
`RESULTS.md` had flagged reranking as the single most-cited unused lever for
retrieval quality despite being built, tested, and gated behind
`REMIND_ME_RERANK=onnx`. It's now on by default (`BAAI/bge-reranker-base`, a
2023 cross-encoder — still small enough for CPU, but meaningfully stronger
than the previous default), since rescoring only ever touches the bounded
`RERANK_TOP_K` head regardless of result-pool size, so the added latency is
small and constant. Disable with `REMIND_ME_RERANK=""` for latency-sensitive
deployments. `benchmarks/runner.py`'s `--rerank` flag explicitly forces the
backend on/off for lever isolation regardless of the library default, so
other lever comparisons (`before_after.py`) stay unaffected.

Measure the effect on real data (A/B on the chunked semantic-only baseline):

```bash
python -m benchmarks.before_after \
  --compare rerank \
  --data benchmarks/data/longmemeval_s_cleaned.json \
  --ingest verbatim --embedder real --rrf-profile semantic --ks 1,3,5,10
```

> Not yet run on `longmemeval_s` here (this environment has no HuggingFace
> network access to download either cross-encoder) — the cross-encoder model
> downloads from HuggingFace on first use. Expect the gain to concentrate in
> **R@1 / MRR** (the cross-encoder reorders the head; R@k for k ≥ top-k is
> unchanged by construction), and the `BAAI/bge-reranker-base` swap to widen
> that gain further versus the old `ms-marco-MiniLM-L6-v2` numbers. This is
> the most direct path to close the remaining gap to MemPalace's LLM-reranked
> ≥0.99 / 1.000. Watch `single-session-preference` in particular. Paste the
> resulting table here once run. A full per-type run is available via
> `python -m benchmarks.runner --rerank ...`.

## HyDE query expansion (lever E)

The weakest categories (`single-session-preference`, multi-hop
`temporal-reasoning`) are questions phrased nothing like the memory that
answers them. HyDE (`remind_me_mcp/query_expansion.py`) has a small local LLM
(Ollama, `REMIND_ME_HYDE_MODEL`, default `llama3.2`) write a short hypothetical
answer passage; the passage embedding — which lives in document-space, not
question-space — is averaged with the query embedding before the KNN. Off by
default; enable with `REMIND_ME_QUERY_EXPANSION=hyde`. Any generation failure
falls back to the plain query. The retrieval mechanism is proven
deterministically in `tests/test_query_expansion.py`.

Measure the effect on real data (requires an Ollama daemon with the model pulled):

```bash
python -m benchmarks.before_after \
  --compare hyde \
  --data benchmarks/data/longmemeval_s_cleaned.json \
  --ingest verbatim --embedder real --rrf-profile semantic --ks 1,3,5,10
```

> Not yet run on `longmemeval_s` here. This is the lower-confidence lever:
> A/B it against the chunked semantic-only baseline and keep it only if the
> weak categories move without hurting the strong ones. Note the generation
> step adds one LLM call per query, so the run is slower than the other
> comparisons. Paste the resulting table here once run.

## FTS5 query-sanitization fix — before/after

Natural-language questions contain punctuation (`?`, `,`, `'`, `$`, `.`) that
FTS5 treats as operator syntax, so the raw query was an invalid `MATCH`
expression and the **keyword tier was silently skipped**. The fix retries an
invalid query as a sanitized OR-of-terms expression (see
`remind_me_mcp/tools/search.py::_sanitize_fts_query`).

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

## Comparison against a shared benchmark standard (cognee gap #10)

The MemPalace comparison above ("Equal-footing comparison with MemPalace") is a
genuine apples-to-apples measurement: same LongMemEval-S questions, same
embedding model, same metric (session-level R@k). That's the bar for a
comparison to mean anything.

**cognee** publishes results on **BEAM** ("Beyond a Million Tokens" —
[mohammadtavakoli78/BEAM](https://github.com/mohammadtavakoli78/BEAM), ICLR
2026) — per cognee's own materials, it beats SOTA on BEAM's 100K-token setting
by 6.5% and matches SOTA at 10M tokens. We deliberately do **not** turn that
into a side-by-side table with the LongMemEval-S numbers above: BEAM evaluates
full agent memory pipelines up to 10M tokens across "10 distinct memory
dimensions" with its own task/metric protocol, not session-level Recall@k/MRR
at LongMemEval-S's ~115K-token scale. Lining those numbers up in one table
would imply a comparability that isn't there — different benchmark, different
metric, different context scale. (For context, cognee also cites ~90% accuracy
on "graph-enhanced queries" vs. ~60% for standard RAG on unspecified internal
tasks — a marketing figure, not a benchmark result, so it's not included here
either.)

What **is** directly comparable: LongMemEval itself is the shared standard —
any system's published LongMemEval-S/M numbers use the same questions, gold
sessions, and Recall@k/MRR definition remind_me's harness reports. The
headline and equal-footing tables above (`python -m benchmarks.runner --data
benchmarks/data/longmemeval_s_cleaned.json ...`, reproducible per the commands
inline) are that comparison point — reproduce another system's LongMemEval
numbers under the same harness convention (session-level scoring, same
abstention handling) and the two are directly comparable, unlike BEAM vs.
LongMemEval.

A lightweight scheduled CI job (`.github/workflows/benchmark-smoke.yml`) runs
the bundled 12-question sample weekly as a regression tripwire for the
retrieval pipeline (not a substitute for the full LongMemEval-S run above,
which takes hours on CPU and needs the real dataset download) — see that
workflow file for what it does and doesn't cover.

## Notes / caveats

- The bundled sample is a small, hand-built set designed to exercise the keyword
  path deterministically and offline — treat it as a demonstration, not a
  leaderboard. Use real LongMemEval files (`benchmarks/download_data.py`) for
  headline numbers.
- All numbers here are **retrieval-only** (Recall@k / MRR at session
  granularity). End-to-end QA accuracy is out of scope (see `README.md`).
