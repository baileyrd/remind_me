"""
Tests for the MCP prompts (maintenance loops) and the server instructions.

Both exist to make remind_me's behaviour reach the model without the user
hand-pasting prose into each client, so what matters here is *registration and
wiring* — that the client actually receives them — plus the invariants that
would silently break a loop if a prompt were edited carelessly: naming the
right tools, and keeping the destructive second phase of the two-phase loops
gated behind their preview phase.
"""

from __future__ import annotations

import pytest

from remind_me_mcp.server import SERVER_INSTRUCTIONS, mcp

# Prompt name -> tool names the rendered text must reference. A prompt that
# stops naming its own tools is no longer a runnable loop, which is exactly
# the failure this file exists to catch.
LOOP_TOOLS = {
    "decompose_facts": ["remind_me_decompose_batch", "remind_me_decompose"],
    "normalize_imports": ["remind_me_normalize_batch", "remind_me_normalize_apply"],
    "backfill_graph": ["remind_me_extract_batch", "remind_me_annotate"],
    "classify_memories": ["remind_me_reclassify_batch", "remind_me_reclassify"],
    "compile_wiki": ["remind_me_wiki_compile", "remind_me_wiki_write"],
    "consolidate_duplicates": ["remind_me_consolidate"],
}


async def _render(name: str, args: dict[str, str] | None = None) -> str:
    result = await mcp.get_prompt(name, args or {})
    return "\n".join(
        m.content.text for m in result.messages if hasattr(m.content, "text")
    )


# ---------------------------------------------------------------------------
# Server instructions
# ---------------------------------------------------------------------------


def test_server_instructions_are_registered() -> None:
    """The instructions must reach the client via the FastMCP instance itself.

    Defining SERVER_INSTRUCTIONS but forgetting to pass it to the constructor
    would leave every client with no guidance and no error anywhere.
    """
    assert mcp.instructions == SERVER_INSTRUCTIONS
    assert SERVER_INSTRUCTIONS.strip()


def test_server_instructions_cover_retrieve_and_store() -> None:
    """Retrieval and capture are the two behaviours the instructions exist to drive."""
    assert "remind_me_search" in SERVER_INSTRUCTIONS
    assert "remind_me_add" in SERVER_INSTRUCTIONS


def test_server_instructions_stay_short() -> None:
    """Instructions cost context in every session, so length is a real budget."""
    assert len(SERVER_INSTRUCTIONS) < 4000


# ---------------------------------------------------------------------------
# Prompt registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_maintenance_loops_are_registered_as_prompts() -> None:
    registered = {p.name for p in await mcp.list_prompts()}
    assert set(LOOP_TOOLS) <= registered


@pytest.mark.asyncio
async def test_prompts_have_title_and_description() -> None:
    """Both are what a client renders in its prompt/slash-command picker."""
    for prompt in await mcp.list_prompts():
        if prompt.name not in LOOP_TOOLS:
            continue
        assert prompt.title, f"{prompt.name} has no title"
        assert prompt.description, f"{prompt.name} has no description"


@pytest.mark.asyncio
async def test_prompt_arguments_are_all_optional() -> None:
    """A required argument would make the prompt unusable as a bare slash command."""
    for prompt in await mcp.list_prompts():
        if prompt.name not in LOOP_TOOLS:
            continue
        for arg in prompt.arguments or []:
            assert not arg.required, f"{prompt.name}.{arg.name} is required"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "expected_tools"), LOOP_TOOLS.items())
async def test_prompt_names_the_tools_its_loop_drives(
    name: str, expected_tools: list[str]
) -> None:
    text = await _render(name)
    for tool in expected_tools:
        assert tool in text, f"{name} never mentions {tool}"


@pytest.mark.asyncio
async def test_prompts_render_with_default_arguments() -> None:
    """Invoked bare (no arguments), every prompt must still produce a full loop."""
    for name in LOOP_TOOLS:
        assert len(await _render(name)) > 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "arg", "value"),
    [
        ("decompose_facts", "batch_size", "77"),
        ("compile_wiki", "limit", "55"),
        ("consolidate_duplicates", "similarity_threshold", "0.93"),
    ],
)
async def test_client_supplied_arguments_reach_the_text(
    name: str, arg: str, value: str
) -> None:
    """MCP delivers prompt arguments as strings; they must survive into the text.

    Annotating these as int/float instead would make a client sending "77"
    either coerce surprisingly or fail outright.
    """
    assert value in await _render(name, {arg: value})


# ---------------------------------------------------------------------------
# Two-phase loop safety
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compile_wiki_gates_the_watermark_advance() -> None:
    """mark_integrated=true is destructive if run before the pages are written.

    It advances the watermark past sources whether or not they were actually
    folded into a page, so the prompt must present both phases in order.
    """
    text = await _render("compile_wiki")
    assert "mark_integrated=false" in text
    assert "mark_integrated=true" in text
    assert text.index("mark_integrated=false") < text.index("mark_integrated=true")


@pytest.mark.asyncio
async def test_consolidate_gates_the_merge_behind_a_dry_run() -> None:
    """Merging is not reversible through the tool, so the preview must come first."""
    text = await _render("consolidate_duplicates")
    assert "dry_run=true" in text
    assert "dry_run=false" in text
    assert text.index("dry_run=true") < text.index("dry_run=false")
