"""
Unit tests for remind_me_mcp.formatting pure helper functions.

Tests cover _fmt_memory_md (single memory Markdown rendering) and
_fmt_memories (list rendering in both Markdown and JSON formats).
All tests are synchronous.
"""

from __future__ import annotations

import json

from remind_me_mcp.formatting import _fmt_memories, _fmt_memory_md
from remind_me_mcp.models import ResponseFormat

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_memory(
    *,
    id: str = "abc123def456",
    content: str = "Test memory content",
    category: str = "general",
    tags: list[str] | None = None,
    source: str = "manual",
    metadata: dict | None = None,
    created_at: str = "2026-01-01T00:00:00+00:00",
    updated_at: str = "2026-01-02T00:00:00+00:00",
) -> dict:
    """Build a minimal memory dict for use in formatting tests."""
    return {
        "id": id,
        "content": content,
        "category": category,
        "tags": tags if tags is not None else [],
        "source": source,
        "metadata": metadata if metadata is not None else {},
        "created_at": created_at,
        "updated_at": updated_at,
    }


# ---------------------------------------------------------------------------
# _fmt_memory_md
# ---------------------------------------------------------------------------


def test_fmt_memory_md_basic() -> None:
    """Basic memory dict produces Markdown with ID header, category, source, dates, content."""
    mem = _make_memory(content="Hello world", category="notes", source="manual")
    result = _fmt_memory_md(mem)
    assert "### Memory `abc123def456`" in result
    assert "**Category:** notes" in result
    assert "**Source:** manual" in result
    assert "2026-01-01T00:00:00+00:00" in result
    assert "2026-01-02T00:00:00+00:00" in result
    assert "Hello world" in result


def test_fmt_memory_md_no_tags() -> None:
    """Empty tags list renders as 'none'."""
    mem = _make_memory(tags=[])
    result = _fmt_memory_md(mem)
    assert "**Tags:** none" in result


def test_fmt_memory_md_with_metadata() -> None:
    """Metadata dict renders as 'key=value' pairs."""
    mem = _make_memory(metadata={"project": "remind-me", "env": "prod"})
    result = _fmt_memory_md(mem)
    assert "**Metadata:**" in result
    assert "project=remind-me" in result
    assert "env=prod" in result


def test_fmt_memory_md_truncates_long_content() -> None:
    """Content over 2000 characters is truncated with an ellipsis."""
    long_content = "X" * 2100
    mem = _make_memory(content=long_content)
    result = _fmt_memory_md(mem)
    # Original content is 2100 chars — should be truncated
    assert long_content not in result  # full content absent
    assert "…" in result
    assert "X" * 2000 in result  # first 2000 chars present


# ---------------------------------------------------------------------------
# _fmt_memories
# ---------------------------------------------------------------------------


def test_fmt_memories_json_format() -> None:
    """ResponseFormat.JSON returns valid JSON with 'count' and 'memories' keys."""
    mems = [_make_memory(id="id1"), _make_memory(id="id2")]
    result = _fmt_memories(mems, ResponseFormat.JSON)
    parsed = json.loads(result)
    assert parsed["count"] == 2
    assert len(parsed["memories"]) == 2
    assert "total" not in parsed


def test_fmt_memories_json_with_total() -> None:
    """JSON output includes 'total' key when total is provided."""
    mems = [_make_memory(id="id1")]
    result = _fmt_memories(mems, ResponseFormat.JSON, total=42)
    parsed = json.loads(result)
    assert parsed["total"] == 42
    assert parsed["count"] == 1


def test_fmt_memories_markdown_format() -> None:
    """ResponseFormat.MARKDOWN returns Markdown blocks separated by '---'."""
    mems = [_make_memory(id="id1"), _make_memory(id="id2")]
    result = _fmt_memories(mems, ResponseFormat.MARKDOWN)
    assert "---" in result
    assert "### Memory `id1`" in result
    assert "### Memory `id2`" in result


def test_fmt_memories_markdown_empty() -> None:
    """Empty list with MARKDOWN format returns '_No memories found._'."""
    result = _fmt_memories([], ResponseFormat.MARKDOWN)
    assert result == "_No memories found._"


def test_fmt_memories_markdown_with_total() -> None:
    """MARKDOWN output with total includes 'Showing X of Y' header."""
    mems = [_make_memory(id="id1")]
    result = _fmt_memories(mems, ResponseFormat.MARKDOWN, total=10)
    assert "Showing 1 of 10" in result
