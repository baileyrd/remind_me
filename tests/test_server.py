"""
Tests for remind_me_mcp.server — the FastMCP instance and application lifespan.

Focuses on _TracedFastMCP (Phase 7a): every real MCP tool call must pass
through an OTEL span wrapping remind_me_mcp.telemetry.maybe_span, without
touching each of the ~40 individually-decorated tool functions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import sqlite3


async def test_call_tool_wraps_dispatch_in_telemetry_span(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_TracedFastMCP.call_tool wraps the real dispatch entry point (not
    just individual tool functions) in a 'tool.<name>' span."""
    import remind_me_mcp.server as server_mod
    import remind_me_mcp.tools  # noqa: F401 -- registers tools on server_mod.mcp

    spans: list[str] = []
    real_maybe_span = server_mod.maybe_span

    def spy_maybe_span(name, **attrs):
        spans.append(name)
        return real_maybe_span(name, **attrs)

    monkeypatch.setattr(server_mod, "maybe_span", spy_maybe_span)

    await server_mod.mcp.call_tool("remind_me_stats", {"params": {}})

    assert spans == ["tool.remind_me_stats"]


async def test_call_tool_span_wraps_even_on_tool_error(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The span still wraps the call when the tool itself errors -- tracing
    must not depend on the tool succeeding."""
    import remind_me_mcp.server as server_mod
    import remind_me_mcp.tools  # noqa: F401

    spans: list[str] = []
    real_maybe_span = server_mod.maybe_span

    def spy_maybe_span(name, **attrs):
        spans.append(name)
        return real_maybe_span(name, **attrs)

    monkeypatch.setattr(server_mod, "maybe_span", spy_maybe_span)

    with pytest.raises(Exception):  # noqa: B017 -- unknown tool name, exact type is FastMCP-internal
        await server_mod.mcp.call_tool("this_tool_does_not_exist", {})

    assert spans == ["tool.this_tool_does_not_exist"]


def test_mcp_instance_is_traced_fastmcp() -> None:
    from remind_me_mcp.server import _TracedFastMCP, mcp

    assert isinstance(mcp, _TracedFastMCP)


async def test_call_tool_arms_and_disarms_the_slow_call_watchdog(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard for issue #128: call_tool is the watchdog's one choke
    point, matching the OTEL span above it -- every real tool invocation must
    arm on entry and disarm on exit, success or failure alike."""
    import remind_me_mcp.server as server_mod
    import remind_me_mcp.tools  # noqa: F401

    events: list[str] = []
    monkeypatch.setattr(server_mod.watchdog, "arm", lambda: events.append("arm"))
    monkeypatch.setattr(server_mod.watchdog, "disarm", lambda: events.append("disarm"))

    await server_mod.mcp.call_tool("remind_me_stats", {"params": {}})

    assert events == ["arm", "disarm"]


async def test_call_tool_disarms_the_watchdog_even_on_tool_error(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An errored call must still disarm -- otherwise one failing tool call
    would leave the watchdog permanently armed for the rest of the process."""
    import remind_me_mcp.server as server_mod
    import remind_me_mcp.tools  # noqa: F401

    events: list[str] = []
    monkeypatch.setattr(server_mod.watchdog, "arm", lambda: events.append("arm"))
    monkeypatch.setattr(server_mod.watchdog, "disarm", lambda: events.append("disarm"))

    with pytest.raises(Exception):  # noqa: B017
        await server_mod.mcp.call_tool("this_tool_does_not_exist", {})

    assert events == ["arm", "disarm"]
