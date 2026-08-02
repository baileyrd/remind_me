"""
remind_me_mcp.tools.recalibrate — importance recalibration candidate surfacing (issue #200).

``vitality.py`` seeds ``base_weight`` at write time from ``memory_type``/
``source`` (an importance prior) and adjusts it via explicit
``remind_me_feedback`` signals, but nothing previously re-evaluated whether a
memory's *original* importance classification had gone stale -- e.g. a memory
classified as a "decision" that was later reversed by a different memory, or
a "fact" that's since been superseded in spirit but not via the formal
triple-supersession mechanism.

**Architectural note, read before touching this module.** The GitHub issue's
literal text proposes "a periodic (scheduler-loop-hosted) LLM-driven pass."
This server has no in-server LLM dependency and never calls an LLM API
itself -- see ``tools/normalize.py``'s module docstring, which established
this same posture for ``remind_me_normalize_batch``: "the LLM work happens
client-side ... no in-server LLM dependency, consistent with the project's
zero-ops design." A Python background thread (``scheduler.py``) has no LLM to
call; it can only run deterministic code. So this is built as the established
two-phase pattern instead (``remind_me_normalize_batch``/
``remind_me_normalize_apply``, ``remind_me_consolidate``'s dry-run mode):
``remind_me_recalibrate_candidates`` surfaces structured candidate data using
a deterministic heuristic (:data:`remind_me_mcp.maintenance.RECALIBRATION_CANDIDATE_WHERE`),
and the **calling Claude session** does the actual reasoning about whether a
given memory's classification still looks right.

Unlike ``normalize``/``consolidate``, there is deliberately **no third "apply"
tool** here. The issue itself names the natural apply half: the EXISTING
``remind_me_reclassify``/``remind_me_reclassify_batch`` tools already write a
new ``memory_type`` (and matching ``decay_rate``); building a redundant
write-path here would duplicate that. A reviewer who wants a pure
importance-only correction with no type change can also reach for the
EXISTING ``remind_me_feedback`` tool (an "unhelpful"/"helpful" signal with no
``query`` adjusts ``base_weight`` globally, exactly the mechanism
``vitality.record_feedback`` already implements).

Patchable shared state and cross-module helpers are looked up through the
``remind_me_mcp.tools`` package namespace (``_pkg.<name>``) at call time so
monkeypatching ``remind_me_mcp.tools.<name>`` keeps working (HY-02).
"""

from __future__ import annotations

import json

from remind_me_mcp import tools as _pkg
from remind_me_mcp.maintenance import RECALIBRATION_CANDIDATE_WHERE
from remind_me_mcp.models import (  # noqa: TC001  # FastMCP resolves this annotation at runtime for the tool schema
    RecalibrateCandidatesInput,
)
from remind_me_mcp.server import mcp

# Defined in maintenance.py, not here: the maintenance nudge counts this same
# queue, and a second copy of the clause would let the count Claude is shown
# drift from the batch this tool actually returns (same convention
# tools/normalize.py's _UNNORMALIZED_WHERE follows).
_RECALIBRATION_CANDIDATE_WHERE = RECALIBRATION_CANDIDATE_WHERE


@mcp.tool(
    name="remind_me_recalibrate_candidates",
    annotations={
        "title": "Get Importance Recalibration Candidates",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_recalibrate_candidates(params: RecalibrateCandidatesInput) -> str:
    """Fetch a batch of memories whose importance classification may be stale.

    Surfaces memories that look important (a high write-time ``base_weight``
    prior, or a durability-implying ``memory_type`` like "decision"/"fact")
    yet have gone quiet (no access/creation activity within the staleness
    window) and have never received a real-world ``remind_me_feedback``
    signal -- used here as a proxy for "never actually reviewed" per the
    issue's own suggestion. This is read-only: review each candidate's
    content, then apply anything that needs to change with the EXISTING
    ``remind_me_reclassify``/``remind_me_reclassify_batch`` tools (a type
    change) or ``remind_me_feedback`` (a pure importance nudge with no type
    change). Most candidates will look fine as-is -- this only narrows an
    otherwise-unbounded set down to a reviewable batch, it does not itself
    judge whether any given memory is actually stale.

    Args:
        params: Batch size (default 20, max 100).

    Returns:
        JSON string with a candidates array and total_candidates count.
    """
    db = _pkg._get_db()

    total_row = db.execute(
        f"SELECT COUNT(*) as cnt FROM memories m WHERE {_RECALIBRATION_CANDIDATE_WHERE}"
    ).fetchone()

    rows = db.execute(
        f"""SELECT id, substr(content, 1, 500) as content_snippet, category,
                   memory_type, base_weight, access_count, accessed_at, created_at
            FROM memories m
            WHERE {_RECALIBRATION_CANDIDATE_WHERE}
            ORDER BY m.base_weight DESC, m.accessed_at ASC
            LIMIT ?""",
        (params.limit,),
    ).fetchall()

    candidates = [
        {
            "id": row["id"],
            "content_snippet": row["content_snippet"],
            "category": row["category"],
            "memory_type": row["memory_type"],
            "base_weight": row["base_weight"],
            "access_count": row["access_count"],
            "accessed_at": row["accessed_at"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]

    result = {
        "candidates": candidates,
        "total_candidates": total_row["cnt"],
    }
    return json.dumps(result, indent=2)
