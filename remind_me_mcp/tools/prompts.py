"""
remind_me_mcp.tools.prompts — MCP prompts for the multi-step maintenance loops.

Every LLM-driven maintenance workflow in remind_me is a *sequence*: a batch
tool surfaces work, the calling agent does the reasoning, an apply tool writes
the result back, and some loops then advance a watermark. The tools have always
been there, but the sequencing lived only in the README and the operator's
head — so running a loop correctly meant remembering both the tool names and
their order.

These prompts move that knowledge into the server. MCP clients surface them as
user-invocable workflows (slash commands in Claude Code), so each loop becomes
discoverable and self-describing instead of tribal knowledge.

Prompt arguments arrive from the client as strings (per the MCP spec), so they
are annotated ``str`` and interpolated into the prompt text rather than coerced
to ``int``/``float`` — the text tells the agent what value to pass to the
underlying tool, and the tool's own pydantic model does the real validation.

Prompts are named without the ``remind_me_`` prefix the tools carry: clients
already namespace them by server (``/mcp__remind-me__compile_wiki``), so the
prefix would only stutter.
"""

from __future__ import annotations

from remind_me_mcp.server import mcp


@mcp.prompt(
    name="decompose_facts",
    title="Decompose captures into atomic facts",
    description=(
        "Break conversation captures into atomic facts with SPO triples and "
        "entity mentions (remind_me_decompose_batch -> remind_me_decompose)."
    ),
)
def decompose_facts(batch_size: str = "20") -> str:
    """Drive the capture -> atomic-fact decomposition loop."""
    return f"""\
Run the decomposition loop over conversation captures that have not been
broken down yet.

1. Call `remind_me_decompose_batch` with `batch_size={batch_size}` to fetch
   captures awaiting decomposition. If it returns none, say so and stop.
2. For each capture, read the dialog and extract the durable, atomic facts it
   establishes — preferences, decisions, resolutions, and stable attributes.
   Skip transient chatter, pleasantries, and anything already obvious from
   another fact.
3. Call `remind_me_decompose` for each capture with those facts. For every
   fact, set `subject`/`predicate`/`object` and list the `entities` it
   mentions (people, projects, tools, orgs, places) so the fact joins the
   knowledge graph rather than sitting as loose prose.

Keep predicates specific and reuse the same predicate wording for the same
kind of relation — a triple that shares a subject and predicate with an
existing one but differs in object automatically supersedes it, so vague
predicates cause real memories to be hidden.

Report how many captures you processed and how many facts you wrote.
"""


@mcp.prompt(
    name="normalize_imports",
    title="Normalize noisy raw imports",
    description=(
        "Distill raw chat/document imports into clean question/summary/"
        "resolution memories (remind_me_normalize_batch -> "
        "remind_me_normalize_apply)."
    ),
)
def normalize_imports(batch_size: str = "20") -> str:
    """Drive the raw-import -> normalized-memory loop."""
    return f"""\
Run the ingest-time normalization loop over raw imported content.

1. Call `remind_me_normalize_batch` with `batch_size={batch_size}` to fetch
   un-normalized import chunks. If it returns none, say so and stop.
2. For each chunk, distill it into a `{{question, summary, resolution?}}`
   shape: a searchable one-line question the chunk answers, a summary of the
   substance, and the resolution when the source actually settles something.
   Add `refs` for any code, file, or system references worth keeping, and
   `entities` for the people/projects/tools it mentions — the raw import is
   never entity-linked automatically, so without this the normalized memory
   stays invisible to `remind_me_entity`.
3. Call `remind_me_normalize_apply` with those distillations.

This is non-destructive: the raw chunk is kept and linked, not replaced. Skip
any chunk with no durable content rather than manufacturing a summary for it.

Report how many chunks you normalized and how many you skipped.
"""


@mcp.prompt(
    name="backfill_graph",
    title="Backfill the entity knowledge graph",
    description=(
        "Add SPO triples and entity mentions to memories that have neither "
        "(remind_me_extract_batch -> remind_me_annotate)."
    ),
)
def backfill_graph(batch_size: str = "20") -> str:
    """Drive the entity/triple backfill loop over un-annotated memories."""
    return f"""\
Run the knowledge-graph backfill loop over memories that carry no SPO triple
and no entity mentions.

1. Call `remind_me_extract_batch` with `batch_size={batch_size}` to fetch the
   backfill queue. If it returns none, say so and stop.
2. For each memory, decide what it actually asserts. Where it states a fact
   about someone or something, derive a `subject`/`predicate`/`object` triple
   and list the `entities` it mentions with their kind (person, project, tool,
   org, place) and any aliases you can see.
3. Call `remind_me_annotate` with the batch of annotations.

Leave a memory un-annotated rather than inventing a triple for it — a
speculative triple is worse than none, because a triple sharing a subject and
predicate with an existing one but differing in object silently supersedes it.

Entity names normalize to deterministic ids, so use the person's or project's
canonical name consistently and put spelling variants in `aliases`.

Report how many memories you annotated and how many you deliberately left
alone.
"""


@mcp.prompt(
    name="classify_memories",
    title="Classify unclassified memories",
    description=(
        "Assign memory types to memories that have none "
        "(remind_me_reclassify_batch -> remind_me_reclassify)."
    ),
)
def classify_memories(batch_size: str = "20") -> str:
    """Drive the memory-type classification loop."""
    return f"""\
Run the classification loop over memories with no memory type set.

1. Call `remind_me_reclassify_batch` with `batch_size={batch_size}` to fetch
   unclassified memories. If it returns none, say so and stop.
2. Read each one and choose the memory type that matches what it actually is —
   the batch response lists the valid types.
3. Apply each classification with `remind_me_reclassify`.

Memory type seeds an importance prior used in ranking, so the distinction that
matters most is durable-and-consequential (a decision, a stable preference)
versus incidental. When a memory genuinely sits between two types, pick the
one describing why it would be retrieved later.

Report the counts per type you assigned.
"""


@mcp.prompt(
    name="compile_wiki",
    title="Compile pending memories into the wiki",
    description=(
        "Synthesise raw memories into wiki pages and advance the watermark "
        "(remind_me_wiki_compile -> remind_me_wiki_write -> mark integrated)."
    ),
)
def compile_wiki(limit: str = "20") -> str:
    """Drive the two-phase wiki synthesis loop."""
    return f"""\
Run the wiki compile loop, folding pending raw memories into synthesised
pages.

1. Call `remind_me_wiki_compile` with `limit={limit}` and
   `mark_integrated=false`. It returns the maintainer schema, the current page
   index, and the pending raw sources. If nothing is pending, say so and stop.
2. Follow the schema it returns — it is the contract, and it takes precedence
   over anything here. Decide for each source which page(s) it belongs to, and
   whether the source is a knowledge page or a repeatable-task procedure page.
3. Write or revise those pages with `remind_me_wiki_write`. Distil rather than
   paste; revise existing summaries instead of appending duplicates; add
   `[[cross-links]]`; flag contradictions inline as the schema directs. Prefer
   patching an existing page over creating a near-duplicate one.
4. Only once the pages are written, call `remind_me_wiki_compile` again with
   `mark_integrated=true` to advance the watermark past the batch.

Step 4 is the one that is unsafe to run early: it marks the sources integrated
whether or not you actually wrote them into a page. If you stop partway,
simply do not mark — phase 1 is idempotent and re-serves the same sources.

Report which pages you created versus revised.
"""


@mcp.prompt(
    name="consolidate_duplicates",
    title="Consolidate near-duplicate memories",
    description=(
        "Preview semantic clusters and merge them with LLM-authored summaries "
        "(remind_me_consolidate dry run -> merge)."
    ),
)
def consolidate_duplicates(similarity_threshold: str = "0.85") -> str:
    """Drive the two-phase consolidation loop."""
    return f"""\
Run the consolidation loop over near-duplicate memories.

1. Call `remind_me_consolidate` with `dry_run=true` and
   `similarity_threshold={similarity_threshold}` to preview the clusters. If
   there are none, say so and stop.
2. Review every cluster before merging anything. A cluster is only a genuine
   duplicate if its members say the same thing; memories that are merely
   topically similar, or that record the same subject at different points in
   time, must not be merged. Drop those from the batch.
3. For each cluster you are keeping, write a summary that preserves every
   distinct detail across its members — this is a real synthesis, not a
   concatenation, and it becomes the surviving memory's content.
4. Call `remind_me_consolidate` again with `dry_run=false` and a `summaries`
   entry per cluster you are merging. A cluster with no matching summary is
   skipped rather than merged, so omitting one is the safe way to decline it.

Merging is not reversible through this tool. If the preview looks wrong at a
given threshold, raise it and re-preview rather than merging selectively by
eye.

Report which clusters you merged and which you declined, with the reason.
"""


@mcp.prompt(
    name="recalibrate_importance",
    title="Review importance recalibration candidates",
    description=(
        "Review old, high-importance memories that have never received a "
        "feedback signal, and reclassify anything whose importance looks "
        "stale (remind_me_recalibrate_candidates -> remind_me_reclassify)."
    ),
)
def recalibrate_importance(limit: str = "20") -> str:
    """Drive the importance recalibration review loop."""
    return f"""\
Run the importance recalibration loop over old, high-importance memories that
have never received a real-world feedback signal.

1. Call `remind_me_recalibrate_candidates` with `limit={limit}` to fetch
   candidates. If it returns none, say so and stop.
2. For each candidate, read its content and decide whether its current
   memory_type/importance still looks right — e.g. a "decision" that was
   later reversed by a different memory, or a "fact" that's since been
   superseded in spirit but not via the formal supersession mechanism. Most
   candidates will look fine as-is; only flag genuine drift.
3. For anything that needs a different memory_type, call `remind_me_reclassify`
   (or review a fresh `remind_me_reclassify_batch` if a type genuinely needs
   reconsidering). For a pure importance correction with no type change, call
   `remind_me_feedback` with a "helpful"/"unhelpful" signal and no `query` to
   adjust `base_weight` directly instead.

This is a review pass, not a bulk rewrite — leave a memory alone rather than
reclassifying it on a weak signal. There is no separate "apply" tool for this
loop: reclassify/feedback, which already exist, are the write path.

Report how many candidates you reviewed and how many you actually changed.
"""


__all__ = [
    "decompose_facts",
    "normalize_imports",
    "backfill_graph",
    "classify_memories",
    "compile_wiki",
    "consolidate_duplicates",
    "recalibrate_importance",
]
