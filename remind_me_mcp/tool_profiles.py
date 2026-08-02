"""
remind_me_mcp.tool_profiles — optionally narrow the advertised tool surface.

The full surface is 49 tools costing roughly 21k tokens of context in *every*
session, on every client, whether or not an admin tool is ever touched. For a
server whose whole job is putting memories into context that is an awkward
ratio: ``remind_me_wiki_load`` defaults to a 12k-token budget, so the tool
definitions cost about 1.8x an entire wiki load before a single memory is
retrieved.

This is deliberately **not** a fix for tool-selection accuracy. The tools that
genuinely compete — ``remind_me_search``, ``remind_me_list``, ``remind_me_get``
and ``remind_me_entity``, which all read as "find things" — are *all* in
:data:`CORE`, so no profile can separate them. That confusion is addressed by
disambiguating their descriptions instead (see each tool's docstring). What a
profile buys is context, and only context.

Three profiles, defaulting to ``full`` so that no existing deployment changes
behaviour by upgrading:

``full``
    Everything. Today's behaviour.
``standard``
    Drops the admin/ops tier (imports, sync, backup, updater). Keeps the
    prompt-driven maintenance loops, so a maintenance pass still works.
``core``
    Conversational surface only. The maintenance prompts are hidden too, since
    a prompt that sequences tools the client cannot see is worse than absent.

Pruning happens once, after every handler has registered, by removing entries
from the FastMCP managers — so a hidden tool is genuinely gone (unlistable and
uncallable) rather than merely undocumented.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

log = logging.getLogger("remind_me_mcp.tool_profiles")

VALID_PROFILES = ("full", "standard", "core")

_raw_profile = os.environ.get("REMIND_ME_TOOL_PROFILE", "full").strip().lower()
if _raw_profile not in VALID_PROFILES:
    log.warning(
        "Unknown REMIND_ME_TOOL_PROFILE=%r; falling back to 'full'. Valid: %s",
        _raw_profile,
        ", ".join(VALID_PROFILES),
    )
    _raw_profile = "full"

TOOL_PROFILE: str = _raw_profile
"""Active profile: ``full`` (default), ``standard``, or ``core``."""


# The conversational surface: what a normal session actually reaches for.
#
# remind_me_server_status is in here deliberately even though it is otherwise
# an ops tool — it is the surface that *reports which profile is active*, and a
# profile you cannot diagnose from inside a session is a trap.
CORE = frozenset({
    "remind_me_search",
    "remind_me_add",
    "remind_me_get",
    "remind_me_list",
    "remind_me_update",
    "remind_me_delete",
    "remind_me_entity",
    "remind_me_entity_traverse",
    "remind_me_feedback",
    "remind_me_auto_capture",
    "remind_me_get_capture",
    "remind_me_stats",
    "remind_me_server_status",
    "remind_me_wiki_load",
    "remind_me_wiki_read",
    "remind_me_wiki_search",
    "remind_me_wiki_list",
})

# The LLM-driven maintenance loops, each fronted by an MCP prompt.
MAINTENANCE = frozenset({
    "remind_me_decompose",
    "remind_me_decompose_batch",
    "remind_me_normalize_batch",
    "remind_me_normalize_apply",
    "remind_me_extract_batch",
    "remind_me_annotate",
    "remind_me_reclassify",
    "remind_me_reclassify_batch",
    "remind_me_consolidate",
    "remind_me_vitality_report",
    "remind_me_wiki_write",
    "remind_me_wiki_compile",
    "remind_me_wiki_delete",
})

# Prompts driving the MAINTENANCE tier — hidden alongside it under `core`.
MAINTENANCE_PROMPTS = frozenset({
    "decompose_facts",
    "normalize_imports",
    "backfill_graph",
    "classify_memories",
    "compile_wiki",
    "consolidate_duplicates",
})


def allowed_tools(profile: str | None = None) -> frozenset[str] | None:
    """Tool names the *profile* advertises, or None when everything is allowed.

    Anything not in CORE or MAINTENANCE is treated as admin/ops — so a newly
    added tool defaults to the most-restricted tier and cannot silently smuggle
    itself into a narrowed surface just by existing.
    """
    p = profile or TOOL_PROFILE
    if p == "core":
        return CORE
    if p == "standard":
        return CORE | MAINTENANCE
    return None


def apply_profile(mcp: FastMCP, profile: str | None = None) -> dict[str, int]:
    """Prune the registered tools/prompts down to *profile*.

    Called once from ``remind_me_mcp.tools`` after every handler has registered.
    A no-op under ``full``.

    Args:
        mcp: The FastMCP instance whose managers should be pruned.
        profile: Override the module-level profile (used by tests).

    Returns:
        ``{"tools": n_kept, "tools_hidden": n, "prompts_hidden": n}``.
    """
    p = profile or TOOL_PROFILE
    allowed = allowed_tools(p)

    tools = mcp._tool_manager._tools
    prompts = mcp._prompt_manager._prompts

    if allowed is None:
        return {"tools": len(tools), "tools_hidden": 0, "prompts_hidden": 0}

    hidden = [name for name in tools if name not in allowed]
    for name in hidden:
        del tools[name]

    hidden_prompts: list[str] = []
    if p == "core":
        hidden_prompts = [name for name in prompts if name in MAINTENANCE_PROMPTS]
        for name in hidden_prompts:
            del prompts[name]

    if hidden or hidden_prompts:
        log.info(
            "Tool profile %r: %d tool(s) and %d prompt(s) hidden, %d tool(s) advertised",
            p,
            len(hidden),
            len(hidden_prompts),
            len(tools),
        )
    return {
        "tools": len(tools),
        "tools_hidden": len(hidden),
        "prompts_hidden": len(hidden_prompts),
    }


def surface_cost(mcp: FastMCP) -> tuple[int, int]:
    """Return ``(tool_count, approx_tokens)`` for the currently advertised surface.

    Approximates what the client is billed for the tool list on every session:
    name + description + input schema, at the usual 4-chars-per-token estimate
    used elsewhere in the codebase. Reported by ``remind_me_server_status`` so
    the cost of the surface is something an operator can see rather than infer.
    """
    import json

    tools = mcp._tool_manager._tools.values()
    chars = sum(
        len(t.name) + len(t.description or "") + len(json.dumps(t.parameters, default=str))
        for t in tools
    )
    return len(mcp._tool_manager._tools), chars // 4


__all__ = [
    "TOOL_PROFILE",
    "surface_cost",
    "VALID_PROFILES",
    "CORE",
    "MAINTENANCE",
    "MAINTENANCE_PROMPTS",
    "allowed_tools",
    "apply_profile",
]
