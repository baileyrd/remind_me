"""
Tests for the tool-profile gate.

The profile exists for one reason — context cost — and explicitly not for
tool-selection accuracy, since the tools that actually compete
(search/list/get/entity) are all in CORE and no profile can separate them.
The tests below pin both halves of that: the savings are real, and the
confusable cluster survives every profile.

Pruning is destructive to module-level manager state, so every test that
applies a non-default profile must restore the full surface afterwards.
"""

from __future__ import annotations

import json

import pytest

import remind_me_mcp.tools  # noqa: F401 — registers every handler
from remind_me_mcp import tool_profiles
from remind_me_mcp.server import mcp


def _surface() -> set[str]:
    return set(mcp._tool_manager._tools)


def _prompts() -> set[str]:
    return set(mcp._prompt_manager._prompts)


@pytest.fixture
def restore_surface():
    """Snapshot and restore the manager state a profile prunes in place."""
    tools = dict(mcp._tool_manager._tools)
    prompts = dict(mcp._prompt_manager._prompts)
    yield
    mcp._tool_manager._tools.clear()
    mcp._tool_manager._tools.update(tools)
    mcp._prompt_manager._prompts.clear()
    mcp._prompt_manager._prompts.update(prompts)


# ---------------------------------------------------------------------------
# Tier definitions
# ---------------------------------------------------------------------------


def test_default_profile_is_full() -> None:
    """Upgrading must never narrow an existing deployment's surface."""
    assert tool_profiles.TOOL_PROFILE == "full"
    assert tool_profiles.allowed_tools("full") is None


def test_confusable_cluster_survives_every_profile() -> None:
    """The whole reason a profile is NOT an accuracy fix.

    search/list/get/entity all read as "find things"; every one is in CORE, so
    no profile can separate them. Disambiguated descriptions do that instead.
    """
    cluster = {
        "remind_me_search",
        "remind_me_list",
        "remind_me_get",
        "remind_me_entity",
    }
    for profile in tool_profiles.VALID_PROFILES:
        allowed = tool_profiles.allowed_tools(profile)
        if allowed is None:
            continue
        assert cluster <= allowed, f"{profile} dropped part of the cluster"


def test_status_tool_is_core() -> None:
    """A profile you cannot diagnose from inside a session is a trap.

    remind_me_server_status is what reports the active profile, so it has to
    survive the narrowest one even though it is otherwise an ops tool.
    """
    assert "remind_me_server_status" in tool_profiles.CORE


def test_unknown_tools_default_to_the_most_restricted_tier() -> None:
    """A newly added tool must not smuggle itself into a narrowed surface."""
    assert "remind_me_totally_new_tool" not in tool_profiles.allowed_tools("standard")


def test_tiers_do_not_overlap() -> None:
    assert not (tool_profiles.CORE & tool_profiles.MAINTENANCE)


def test_tier_names_all_exist() -> None:
    """A typo in a tier set would silently drop a real tool from `standard`."""
    registered = _surface()
    unknown = (tool_profiles.CORE | tool_profiles.MAINTENANCE) - registered
    assert not unknown, f"tier names not registered as tools: {sorted(unknown)}"


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------


def test_full_profile_prunes_nothing(restore_surface) -> None:
    before = _surface()
    result = tool_profiles.apply_profile(mcp, "full")
    assert _surface() == before
    assert result["tools_hidden"] == 0


def test_standard_drops_admin_but_keeps_maintenance(restore_surface) -> None:
    tool_profiles.apply_profile(mcp, "standard")
    surface = _surface()
    assert "remind_me_search" in surface
    assert "remind_me_decompose_batch" in surface  # maintenance kept
    assert "remind_me_import_chat" not in surface  # admin dropped
    assert "remind_me_self_update" not in surface


def test_standard_keeps_the_maintenance_prompts_usable(restore_surface) -> None:
    """The prompts sequence maintenance tools, which `standard` still has."""
    tool_profiles.apply_profile(mcp, "standard")
    assert _prompts() >= tool_profiles.MAINTENANCE_PROMPTS


def test_core_hides_maintenance_tools_and_their_prompts(restore_surface) -> None:
    """A prompt that sequences tools the client cannot see is worse than absent."""
    tool_profiles.apply_profile(mcp, "core")
    surface = _surface()
    assert "remind_me_search" in surface
    assert "remind_me_decompose_batch" not in surface
    assert not (tool_profiles.MAINTENANCE_PROMPTS & _prompts())


def test_hidden_tools_are_uncallable_not_merely_unlisted(restore_surface) -> None:
    """Hidden must mean gone — a listable/callable split would be a trap."""
    tool_profiles.apply_profile(mcp, "core")
    assert mcp._tool_manager.get_tool("remind_me_import_chat") is None


def test_each_profile_actually_reduces_context(restore_surface) -> None:
    def cost() -> int:
        return sum(
            len(json.dumps(t.parameters, default=str)) + len(t.description or "")
            for t in mcp._tool_manager._tools.values()
        )

    full = cost()
    tool_profiles.apply_profile(mcp, "standard")
    standard = cost()
    tool_profiles.apply_profile(mcp, "core")
    core = cost()
    assert core < standard < full


def test_surface_cost_tracks_the_active_surface(restore_surface) -> None:
    n_full, tok_full = tool_profiles.surface_cost(mcp)
    tool_profiles.apply_profile(mcp, "core")
    n_core, tok_core = tool_profiles.surface_cost(mcp)
    assert n_core < n_full
    assert tok_core < tok_full


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def test_unknown_profile_value_falls_back_to_full(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo'd env var must not silently hide half the tools."""
    monkeypatch.setenv("REMIND_ME_TOOL_PROFILE", "kore")
    import importlib

    reloaded = importlib.reload(tool_profiles)
    try:
        assert reloaded.TOOL_PROFILE == "full"
        assert reloaded.allowed_tools() is None
    finally:
        monkeypatch.delenv("REMIND_ME_TOOL_PROFILE", raising=False)
        importlib.reload(tool_profiles)


# ---------------------------------------------------------------------------
# Search schema slimming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_schema_stays_lean() -> None:
    """remind_me_search is the hottest tool AND the most expensive, and no
    profile can narrow it — so its schema is the one place where every token
    is paid by every session regardless of configuration.
    """
    tools = await mcp.list_tools()
    search = next(t for t in tools if t.name == "remind_me_search")
    schema_tokens = len(json.dumps(search.inputSchema)) // 4
    assert schema_tokens < 950, f"search schema grew to ~{schema_tokens} tokens"


@pytest.mark.asyncio
async def test_search_flags_still_say_when_to_use_them() -> None:
    """Slimming trimmed response-shape narration, not call-decision guidance.

    The tool takes a single `params` object, so the real fields live under
    ``$defs``, not the top-level ``properties``.
    """
    tools = await mcp.list_tools()
    search = next(t for t in tools if t.name == "remind_me_search")
    props = search.inputSchema["$defs"]["MemorySearchInput"]["properties"]
    for flag in ("expand_entities", "include_neighbors", "expand_co_retrieval"):
        assert "Use " in props[flag]["description"], f"{flag} lost its guidance"
