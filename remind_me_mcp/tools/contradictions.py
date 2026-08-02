"""
remind_me_mcp.tools.contradictions — free-text contradiction candidate
surfacing (issue #201).

**The gap this closes.** db._supersede_contradicting_facts already
auto-supersedes a memory whenever a new/updated SPO triple shares an
existing memory's (subject, predicate) but has a *different* object -- see
that function's docstring and the README's "Contradiction-based
supersession" section. That mechanism is deliberately narrow: it only fires
on exact structured-triple matches. It says nothing about two pieces of
free-text prose that conflict without ever being decomposed into a shared
subject/predicate -- e.g. "I moved to Boston last month" as one memory and
"My apartment in Seattle has great light" as another, neither carrying SPO
columns. This module targets exactly that gap.

**Architectural note, read before touching this module.** The GitHub issue's
literal text proposes "a periodic (scheduler-loop-hosted) LLM pass." As with
issue #200 (see tools/recalibrate.py's module docstring, which established
this same posture first), this server has no in-server LLM dependency and
never calls an LLM API itself -- a Python background thread
(``scheduler.py``) has no LLM to call, only deterministic code to run. So
this is built as the same two-phase pattern:
``remind_me_contradiction_candidates`` surfaces bounded candidate PAIRS using
a deterministic heuristic (:data:`remind_me_mcp.maintenance.CONTRADICTION_CANDIDATE_PAIRS_SQL`),
and the **calling Claude session** does the actual judgment about whether a
given pair genuinely conflicts -- prose contradiction detection is far less
certain than exact triple matching, so this surfaces pairs that MIGHT
conflict for review, it never auto-supersedes anything itself. There is no
scheduler wiring here, and this module imports nothing from ``scheduler.py``.

**Bounding the comparison space.** All-pairs comparison across an entire
vault is O(n^2) and mostly wasted work -- two memories that never mention any
entity in common are extremely unlikely to be a direct contradiction. This
tool instead pairs memories that share at least one linked entity in the
entity graph (FT-04, ``memory_entities``), the same graph
``remind_me_entity`` traverses -- see ``maintenance.py`` for the join.

**Deliberately no third "apply"/"resolve" tool.** Unlike
``remind_me_normalize_batch``/``remind_me_normalize_apply``, this pairs with
tools that already exist: once a genuine contradiction is confirmed, the
calling session corrects it with the EXISTING ``remind_me_update`` (edit the
stale memory in place), ``remind_me_delete`` (remove it outright), or
``remind_me_add`` with an explicit ``subject``/``predicate``/``object``
triple naming the correct fact (which lets the formal supersession mechanism
take over for any future exact-triple conflicts on the same claim). Building
a redundant write path here would just duplicate those.

Patchable shared state and cross-module helpers are looked up through the
``remind_me_mcp.tools`` package namespace (``_pkg.<name>``) at call time so
monkeypatching ``remind_me_mcp.tools.<name>`` keeps working (HY-02).
"""

from __future__ import annotations

import json

from remind_me_mcp import tools as _pkg
from remind_me_mcp.maintenance import CONTRADICTION_CANDIDATE_PAIRS_SQL
from remind_me_mcp.models import (  # noqa: TC001  # FastMCP resolves this annotation at runtime for the tool schema
    ContradictionCandidatesInput,
)
from remind_me_mcp.server import mcp

# Defined in maintenance.py, not here: the maintenance nudge counts this same
# pairing, and a second copy of the query would let the count Claude is shown
# drift from the batch this tool actually returns (same convention
# tools/recalibrate.py's _RECALIBRATION_CANDIDATE_WHERE follows).
_CONTRADICTION_CANDIDATE_PAIRS_SQL = CONTRADICTION_CANDIDATE_PAIRS_SQL


@mcp.tool(
    name="remind_me_contradiction_candidates",
    annotations={
        "title": "Get Contradiction Candidates",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_contradiction_candidates(params: ContradictionCandidatesInput) -> str:
    """Fetch bounded pairs of memories that might contradict each other.

    Structured SPO triples already get automatic contradiction detection at
    write time (same subject+predicate, different object -> auto-supersede;
    see the README's "Contradiction-based supersession" section). This tool
    covers what that mechanism can't see: free-text memories with no triple
    structure, or triple-structured memories whose predicates were worded
    differently for what's actually the same claim.

    Candidate pairs are bounded two ways, to stay far short of comparing
    every memory to every other one:

    1. Both memories must share at least one linked entity in the entity
       graph (the same graph `remind_me_entity` traverses) -- two memories
       that never mention anything in common are unlikely to conflict.
    2. Pairs already resolvable by the exact-triple supersession mechanism
       are excluded, so this doesn't re-surface what `remind_me_add`/
       `remind_me_decompose`/`remind_me_annotate` already resolved
       automatically.

    This is read-only and returns pairs that MIGHT conflict, not pairs that
    ARE confirmed contradictions -- prose comparison is inherently less
    certain than exact triple matching, so the actual judgment is left to
    you, the calling session. Most pairs will turn out unrelated or merely
    topically similar; only act on a pair once you've actually read both and
    concluded they assert incompatible things. When one does, there is no
    dedicated "apply" tool here -- use the EXISTING `remind_me_update` to
    correct the stale memory in place, `remind_me_delete` to remove it, or
    `remind_me_add` with an explicit subject/predicate/object triple naming
    the corrected fact (which also lets future exact-triple conflicts on
    that claim auto-supersede).

    Args:
        params: Batch size (default 20, max 100).

    Returns:
        JSON string with a candidates array of {memory_a, memory_b} pairs
        (each side carrying enough content/context to judge) and a
        total_candidates count.
    """
    db = _pkg._get_db()

    total_row = db.execute(
        f"SELECT COUNT(*) AS cnt FROM ({_CONTRADICTION_CANDIDATE_PAIRS_SQL}) p"  # noqa: S608 — constants only
    ).fetchone()

    pair_rows = db.execute(
        f"""SELECT id_a, id_b FROM ({_CONTRADICTION_CANDIDATE_PAIRS_SQL})
            ORDER BY id_a, id_b
            LIMIT ?""",  # noqa: S608 — constants only
        (params.limit,),
    ).fetchall()

    def _side(memory_id: str) -> dict:
        row = db.execute(
            """SELECT id, substr(content, 1, 500) as content_snippet, category,
                      memory_type, subject, predicate, object, created_at
               FROM memories WHERE id = ?""",
            (memory_id,),
        ).fetchone()
        return {
            "id": row["id"],
            "content_snippet": row["content_snippet"],
            "category": row["category"],
            "memory_type": row["memory_type"],
            "subject": row["subject"],
            "predicate": row["predicate"],
            "object": row["object"],
            "created_at": row["created_at"],
        }

    def _shared_entities(id_a: str, id_b: str) -> list[str]:
        rows = db.execute(
            """SELECT DISTINCT e.name FROM memory_entities me1
               JOIN memory_entities me2 ON me2.entity_id = me1.entity_id
               JOIN entities e ON e.id = me1.entity_id
               WHERE me1.memory_id = ? AND me2.memory_id = ?
               ORDER BY e.name""",
            (id_a, id_b),
        ).fetchall()
        return [row["name"] for row in rows]

    candidates = [
        {
            "memory_a": _side(row["id_a"]),
            "memory_b": _side(row["id_b"]),
            "shared_entities": _shared_entities(row["id_a"], row["id_b"]),
        }
        for row in pair_rows
    ]

    result = {
        "candidates": candidates,
        "total_candidates": total_row["cnt"] if total_row is not None else 0,
    }
    return json.dumps(result, indent=2)
