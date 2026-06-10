"""
remind_me_mcp.tools.entity — remind_me_entity lookup tool (FT-04 part 2).

Patchable shared state and cross-module helpers are looked up through the
``remind_me_mcp.tools`` package namespace (``_pkg.<name>``) at call time so
monkeypatching ``remind_me_mcp.tools.<name>`` keeps working (HY-02).
"""

from __future__ import annotations

import json

from remind_me_mcp import tools as _pkg
from remind_me_mcp.db import _entity_profile
from remind_me_mcp.models import (  # noqa: TC001  # FastMCP resolves these annotations at runtime for tool schemas
    EntityLookupInput,
)
from remind_me_mcp.server import mcp


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
