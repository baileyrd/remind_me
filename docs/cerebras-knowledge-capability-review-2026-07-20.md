# Cerebras Knowledge Capability Review — 2026-07-20

Comparison of [Cerebras Knowledge](https://www.cerebras.ai/blog/how-we-built-our-knowledge-base)
(Cerebras's internal RAG/knowledge-base system, handling 15,000+ queries/day)
against `remind_me`'s current feature set, looking for capabilities remind_me
may be missing.

Note: the link originally shared (`cerebras.ai/infcamp`) was an ad landing
page for Cerebras Inference (fast LLM inference hardware/API) — a different
category of product with no natural capability overlap with remind_me. This
review instead covers Cerebras's engineering write-up on their internal
knowledge base, which is a directly comparable system.

## Cerebras Knowledge architecture

- Single Postgres table holds embeddings + summaries + metadata from every
  source (Slack, GitHub, Google Docs, Jira, wikis, even hardware netlists) —
  one unified schema, immediately queryable
- **Pluggable connectors** — any team adds a source via a small PR (a Python
  module that emits rows matching the embeddings schema)
- **Real-time ingestion** — a Slack bot in Socket Mode streams messages over
  a live WebSocket; each event re-fetches the whole thread and stores it
  immediately, not on a poll cycle
- **Ingest-time LLM normalization** — raw threads get distilled into a
  structured document (a searchable one-line question, a summary, the
  resolution, and code/system references) — the *normalized* doc is what
  gets embedded, not the raw transcript
- **Retrieval** — an LLM planner reads the query and routes in parallel to
  specialized tools (`search_slack` direct, `search_code` via ripgrep,
  `search_unified` vector pipeline), fusing full-text + embedding + IDF +
  age-decay signals via RRF, then reranking
- **Context-preserving chunks** — a matched wiki-section chunk pulls in its
  two neighboring sections so headings/preconditions/caveats split by
  chunking aren't lost
- **Synthesis** — results are normalized into a common evidence format and
  passed to a final LLM that writes the answer, not just ranked snippets
- **Feedback loop** — every answer produces a signal (thumbs up/down,
  escalation, rewrite) fed back into content updates, retrieval tuning, and
  gap reports

## Gaps relative to remind_me

1. **Live/push-based source ingestion** — remind_me's watcher polls the
   filesystem (default 60s); Cerebras ingests via native webhooks/sockets
   (Slack Socket Mode) the instant something happens. remind_me has no
   native connectors for Slack/GitHub/Jira/Google Docs at all — only local
   file formats.

2. **Formal pluggable connector framework** — Cerebras made adding a new
   source a "submit a PR with a small plugin" pattern. remind_me's import
   pipeline is built around a fixed set of formats (chat/doc) plus watched
   directories, not an extensible connector interface for arbitrary external
   systems.

3. **Ingest-time LLM normalization for noisy sources** — Cerebras distills
   raw threads into a structured question/summary/resolution doc *before*
   embedding, which they say "sharply improves accuracy." remind_me's
   `auto_capture` does something similar in spirit (dialog + distilled
   summary) but doesn't extract that structured Q/resolution/refs shape, and
   generic imports embed raw chunked content directly.

4. **Query planner with tool routing** — remind_me has one unified search
   tool; Cerebras has an LLM planning step that picks among specialized
   retrieval tools per query. remind_me's RRF always blends the same fixed
   signal set rather than adaptively choosing a strategy (same theme as the
   auto-routing gap noted in the cognee review).

5. **IDF as an explicit fusion signal** — remind_me's RRF has
   keyword/semantic/recency/vitality; no explicit IDF weighting to suppress
   filler tokens (FTS5's ranking gets partway there implicitly, but it's not
   a separate tunable signal).

6. **Neighbor-aware chunk retrieval** — remind_me preserves heading context
   in a chunk's metadata at ingest time but doesn't pull in adjacent chunks
   at query time when a match risks losing surrounding context.

7. **Feedback loop on answers** — same gap flagged in the cognee review: no
   thumbs-up/down/escalation signal that feeds back into retrieval tuning or
   surfaces content gaps.

8. **Server-side answer synthesis** — Cerebras returns a synthesized answer;
   remind_me deliberately returns ranked memory snippets and leaves synthesis
   to the calling Claude session. Noted as a design difference rather than a
   gap — it may not be something to change given remind_me's role as an MCP
   memory layer, not a standalone Q&A bot.

## Sources

- [How we built our knowledge base (Cerebras)](https://www.cerebras.ai/blog/how-we-built-our-knowledge-base)
- [How Cerebras Built a 15K-Query/Day Internal Knowledge Base](https://mer.vin/2026/07/how-cerebras-built-a-15k-query-day-internal-knowledge-base/)
- [Thread Navigator summary](https://threadnavigator.com/thread/2077822555159945507/)
