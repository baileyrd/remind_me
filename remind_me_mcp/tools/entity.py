"""
remind_me_mcp.tools.entity — remind_me_entity lookup tool (FT-04 part 2).

Patchable shared state and cross-module helpers are looked up through the
``remind_me_mcp.tools`` package namespace (``_pkg.<name>``) at call time so
monkeypatching ``remind_me_mcp.tools.<name>`` keeps working (HY-02).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import sqlite3

from remind_me_mcp import tools as _pkg
from remind_me_mcp.db import _entity_profile, _resolve_entity
from remind_me_mcp.models import (  # noqa: TC001  # FastMCP resolves these annotations at runtime for tool schemas
    EntityLookupInput,
    EntityTraverseInput,
)
from remind_me_mcp.server import mcp

# Default cap on relation edges returned by a traversal. Unlike the search
# expansion caps (which bound response cost against a token budget), this
# also bounds worst-case query volume across hops -- see EntityTraverseInput.
_RELATION_TRAVERSAL_CAP = 20


@mcp.tool(
    name="remind_me_entity",
    annotations={
        "title": "Look Up an Entity",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_entity(params: EntityLookupInput) -> str:
    """Look up a knowledge-graph entity by name or alias and return everything known about it.

    Resolution is case/whitespace-insensitive: the canonical name is tried
    first (deterministic-id lookup), then aliases. The payload contains the
    canonical entity record (id, name, kind, aliases), its facts (memories
    whose structured subject or object is the entity's canonical name), and
    the memories that mention it via the entity graph. Superseded memories
    are excluded (DI-02), and dangling links delivered out-of-order by sync
    are invisible.

    Args:
        params (EntityLookupInput): Entity name or alias, plus a result limit.

    Returns:
        str: JSON payload with entity, facts, memories, and
        total_linked_memories — or found=false with a message when no entity
        matches.
    """
    db = _pkg._get_db()
    profile = _entity_profile(db, params.name, limit=params.limit)
    if profile is None:
        return json.dumps(
            {
                "found": False,
                "query": params.name,
                "message": f"No entity found matching {params.name!r}.",
            },
            indent=2,
        )
    return json.dumps({"found": True, **profile}, indent=2, default=str)


# ---------------------------------------------------------------------------
# Multi-hop entity-relation traversal (cognee gap #2)
# ---------------------------------------------------------------------------


def _expand_via_entity_relations(
    db: sqlite3.Connection,
    seed_entity_ids: list[str],
    hops: int = 1,
    relation: str | None = None,
    cap: int = _RELATION_TRAVERSAL_CAP,
) -> list[dict[str, Any]]:
    """Breadth-first traversal of the typed entity-relation graph.

    Follows ``entity_relations`` edges in both directions (subject->object
    and object->subject) up to *hops* steps, so a traversal from "Bailey"
    surfaces both relations Bailey is the subject of and relations naming
    Bailey as the object. Each hop only queries the *newly* discovered
    entities from the previous hop (the seed-set stays out of later
    frontiers), so an edge is never refetched once both its endpoints have
    already been visited -- this is what makes the walk terminate on cycles
    without an explicit depth-first "seen" check per edge.

    Args:
        db: An open SQLite connection.
        seed_entity_ids: Entity ids to start the traversal from.
        hops: Maximum traversal depth (1-3 recommended; larger values are
            still safe -- bounded by *cap* and the shrinking frontier).
        relation: Optional exact-match filter on the relation label.
        cap: Maximum number of edges to return, across all hops.

    Returns:
        List of {subject_entity_id, subject_name, subject_kind, relation,
        object_entity_id, object_name, object_kind, hop} dicts, in
        breadth-first order (hop 1 edges first).
    """
    seen_entities: set[str] = set(seed_entity_ids)
    frontier: set[str] = set(seed_entity_ids)
    edges: list[dict[str, Any]] = []
    seen_edge_ids: set[str] = set()

    for hop in range(1, hops + 1):
        if not frontier or len(edges) >= cap:
            break
        placeholders = ",".join("?" * len(frontier))
        bindings: list[Any] = [*frontier, *frontier]
        relation_clause = ""
        if relation:
            relation_clause = " AND r.relation = ?"
            bindings.append(relation)

        rows = db.execute(
            f"""SELECT r.id, r.subject_entity_id, r.relation, r.object_entity_id,
                       s.name AS subject_name, s.kind AS subject_kind,
                       o.name AS object_name, o.kind AS object_kind
                FROM entity_relations r
                JOIN entities s ON s.id = r.subject_entity_id
                JOIN entities o ON o.id = r.object_entity_id
                WHERE (r.subject_entity_id IN ({placeholders})
                       OR r.object_entity_id IN ({placeholders})){relation_clause}
                ORDER BY r.created_at""",
            bindings,
        ).fetchall()

        next_frontier: set[str] = set()
        for r in rows:
            if r["id"] in seen_edge_ids:
                continue
            if len(edges) >= cap:
                break
            seen_edge_ids.add(r["id"])
            edges.append({
                "subject_entity_id": r["subject_entity_id"],
                "subject_name": r["subject_name"],
                "subject_kind": r["subject_kind"],
                "relation": r["relation"],
                "object_entity_id": r["object_entity_id"],
                "object_name": r["object_name"],
                "object_kind": r["object_kind"],
                "hop": hop,
            })
            for nbr in (r["subject_entity_id"], r["object_entity_id"]):
                if nbr not in seen_entities:
                    seen_entities.add(nbr)
                    next_frontier.add(nbr)
        frontier = next_frontier

    return edges


@mcp.tool(
    name="remind_me_entity_traverse",
    annotations={
        "title": "Traverse Entity Relations",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_entity_traverse(params: EntityTraverseInput) -> str:
    """Multi-hop traversal of the typed entity-relation graph from a starting entity.

    Unlike remind_me_entity (a single entity's direct facts and mentions) or
    remind_me_search's expand_entities (1-hop, memory-mediated co-mention),
    this follows entity_relations edges -- typed subject/relation/object
    triples written by remind_me_decompose/remind_me_annotate -- for up to
    `hops` steps in both directions. Use it for questions that require
    chaining relations rather than co-mention, e.g. "who introduced me to
    the person who recommended this tool".

    Args:
        params (EntityTraverseInput): Starting entity name/alias, hop depth
            (1-3), an optional exact relation-label filter, and a result cap.

    Returns:
        str: JSON with the starting entity, every relation edge discovered
        (each tagged with its hop depth), and the unique entities touched —
        or found=false with a message when the starting entity doesn't
        resolve.
    """
    db = _pkg._get_db()
    seed = _resolve_entity(db, params.name)
    if seed is None:
        return json.dumps(
            {
                "found": False,
                "query": params.name,
                "message": f"No entity found matching {params.name!r}.",
            },
            indent=2,
        )

    edges = _expand_via_entity_relations(
        db, [seed["id"]], hops=params.hops, relation=params.relation, cap=params.cap
    )

    entities: dict[str, dict[str, Any]] = {
        seed["id"]: {"id": seed["id"], "name": seed["name"], "kind": seed["kind"]}
    }
    for e in edges:
        entities.setdefault(
            e["subject_entity_id"],
            {"id": e["subject_entity_id"], "name": e["subject_name"], "kind": e["subject_kind"]},
        )
        entities.setdefault(
            e["object_entity_id"],
            {"id": e["object_entity_id"], "name": e["object_name"], "kind": e["object_kind"]},
        )

    return json.dumps(
        {
            "found": True,
            "entity": {"id": seed["id"], "name": seed["name"], "kind": seed["kind"]},
            "hops": params.hops,
            "edges": edges,
            "entities": list(entities.values()),
        },
        indent=2,
        default=str,
    )
