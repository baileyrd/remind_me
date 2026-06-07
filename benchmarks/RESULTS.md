# Benchmark Results

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
