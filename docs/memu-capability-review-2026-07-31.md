# memU Capability Review — 2026-07-31

Comparison of [NevaMind-AI/memU](https://github.com/NevaMind-AI/memU) against
`remind_me`'s current feature set, looking for capabilities remind_me may be
missing.

## What memU is

memU is a much narrower tool than remind_me: "personal memory, stored as
Wiki across sessions, agents, and devices," with the core retrieval/storage
logic deliberately kept to ~500 lines. It is not a general memory store —
it's a **skills wiki for coding agents**. The pitch is close to remind_me's
LLM Wiki feature (same Karpathy "LLM Wiki" lineage), but skills-over-raw-memory
is memU's *entire* product rather than one layer of a larger system.

- **Host adapters, not a chat-export importer** — memU ships sidecar
  binaries/bridges for specific coding-agent hosts (Codex, Claude Code,
  Cursor, OpenClaw, Hermes, ...) that tail each host's native session logs
  directly and slice them into retrievable "job" files, on a schedule. There's
  no manual export/import step.
- **Skill-shaped output, not free-form pages** — the write pipeline is a
  five-step loop: capture a new session → prepare a self-contained "job" →
  the *agent* decides whether to create a new skill or patch an existing one
  → the agent writes the skill as Markdown (with edge cases/branches) → memU
  indexes it for retrieval. memU itself never calls an LLM; all synthesis
  judgment stays with the calling agent, and the service is pure
  storage/embedding/ranking (`MemoryService`, three entry points:
  `list_all_recall_files`, `progressive_retrieve`, `commit_results`).
- **Storage** — in-memory (tests), SQLite (local default), or Postgres
  (concurrent/shared), selected via env config, with a strict
  parity/repository-contract requirement across all three backends.
- **Cross-agent sharing** — any agent pointed at the same backend
  (SQLite/Postgres) reads and writes the same skill index, so a skill learned
  in Claude Code is retrievable from Cursor or Codex on the same machine.
  A `detect` CLI utility identifies which host it's running under and what
  memorize/retrieve capabilities that host supports.
- **Distribution** — an `npm` package alongside the Python package, plus a
  hosted option at memu.so for managed storage/viewing without self-hosting.

## Capabilities memU has that remind_me lacks

1. **Native host adapters for other coding agents** — memU tails Codex's,
   Cursor's, OpenClaw's, and Hermes's own session logs directly, no export
   step. remind_me only has a generic chat-export/Markdown/JSON importer
   (`remind_me_import_chat`, watched folders) — nothing that hooks into a
   specific *other* agent's native log format the way memU's host adapters
   do. Claude Code/Desktop are remind_me's only "live" integration, via MCP
   itself. Tracked as [#109](https://github.com/baileyrd/remind_me/issues/109).

2. **Host auto-detection** — memU's `detect` utility figures out which agent
   host it's running under and what that host supports. remind_me has no
   equivalent; each deployment is configured by hand (env vars, MCP client
   config) with no runtime environment probing. Tracked as
   [#110](https://github.com/baileyrd/remind_me/issues/110).

3. **Skill-shaped synthesis with explicit patch-vs-create judgment** —
   memU's write loop makes the calling agent explicitly decide "is this a new
   skill or a patch to an existing one," and skills are written to capture
   edge cases and branches of a *procedure*. remind_me's `remind_me_wiki_write`
   supports create-or-replace of a page, and `remind_me_wiki_compile` surfaces
   pending memories for Claude to fold in, but there's no dedicated
   patch-vs-create decision step and no procedure-shaped (steps/edge-cases/
   branches) template — remind_me's wiki pages are general knowledge/entity
   pages, not task-runbook skills. Tracked as
   [#108](https://github.com/baileyrd/remind_me/issues/108).

4. **npm/JS distribution** — memU ships a JS/TS-consumable package alongside
   Python. remind_me is Python/MCP-only (same gap already flagged in the
   cognee review re: client SDKs). Tracked as
   [#111](https://github.com/baileyrd/remind_me/issues/111).

5. **Hosted managed option (memu.so)** — a zero-setup cloud target for teams
   that don't want to self-host. remind_me is self-hosted only (same theme as
   the cognee review's cloud/managed-deploy gap, but memU's version is
   narrowly a hosted skills wiki rather than general cloud infra). Tracked as
   [#112](https://github.com/baileyrd/remind_me/issues/112).

6. **Extreme minimalism as a design/audit property** — memU advertises its
   core logic as inspectable in ~500 lines with a hard "no LLM calls in the
   service" invariant enforced as an architectural rule. remind_me is a much
   larger surface (43 tools, hybrid search, entity graph, vitality, sync,
   dashboard) — not a capability gap exactly, but worth naming: memU is
   optimized to be trivially auditable/forkable, remind_me is optimized to be
   comprehensive. A team wanting "read the whole memory engine in an
   afternoon" would pick memU's model over remind_me's.

## What remind_me already covers that memU doesn't

memU has no entity/knowledge graph, no SPO triples, no ACT-R-style
vitality/decay model, no consolidation/dedup, no hybrid FTS+vector RRF fusion
(it's embedding-only retrieval over skill name/description), no dashboard UI,
no multi-machine sync protocol (Postgres-as-shared-backend is the closest
equivalent, which is simpler but has none of remind_me's offline-first outbox
/conflict handling), no webhook ingestion, no OAuth remote connector, and no
published benchmark harness. remind_me is the more capable general-purpose
memory layer; memU is a sharper, smaller tool purpose-built for one job
(turning coding-agent session history into reusable procedural skills).

## Likely worth pursuing

The one idea worth lifting on its own merits is **#3, a skill-shaped wiki
template** — a `procedure`/`runbook` page kind (or just a documented
`SCHEMA.md` convention) that nudges `remind_me_wiki_compile` toward
"steps + edge cases + branches" output when the source memories describe a
repeatable task, alongside the existing free-form knowledge pages. That's a
prompt/schema change layered on infrastructure remind_me already has, not new
plumbing. Native host adapters (#1) and auto-detection (#2) are
higher-effort and only pay off if remind_me's scope expands beyond
Claude.ai/Code/Desktop toward being a shared memory layer for other coding
agents too — a scope decision, not just an implementation task. #4/#5
restate gaps already noted in the cognee review rather than adding a new
one.

## Sources

- [NevaMind-AI/memU](https://github.com/NevaMind-AI/memU) — README.md,
  AGENTS.md, SKILL.md
