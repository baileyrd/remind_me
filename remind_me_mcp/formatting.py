"""
remind_me_mcp.formatting — Memory formatting helpers.

Provides functions to render memory dicts as human-readable Markdown
or machine-readable JSON for MCP tool responses.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from remind_me_mcp.models import ResponseFormat

log = logging.getLogger("remind_me_mcp.formatting")

# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_memory_md(m: dict) -> str:
    """Render a single memory dict as a Markdown block.

    Produces a formatted section with the memory's ID, category, tags,
    source, metadata, timestamps, and content (truncated at 2000 chars).

    Args:
        m: A memory dict as returned by _row_to_dict, with keys id,
           category, tags, source, metadata, created_at, updated_at,
           and content.

    Returns:
        A multi-line Markdown string ending with a blank line.
    """
    tags = ", ".join(m.get("tags", [])) if m.get("tags") else "none"
    meta = m.get("metadata", {})
    meta_str = ", ".join(f"{k}={v}" for k, v in meta.items()) if meta else ""
    lines = [
        f"### Memory `{m['id']}`" + (" 🔒 _sensitive_" if m.get("sensitive") else ""),
        f"**Category:** {m['category']}  |  **Tags:** {tags}  |  **Source:** {m.get('source', '?')}",
    ]
    if meta_str:
        lines.append(f"**Metadata:** {meta_str}")
    lines.append(f"**Created:** {m['created_at']}  |  **Updated:** {m['updated_at']}")
    if m.get("remind_at"):
        lines.append(f"**Reminder:** {m['remind_at']}")
    lines.append("")
    lines.append(m["content"][:2000] + ("…" if len(m["content"]) > 2000 else ""))
    lines.append("")
    return "\n".join(lines)


def _fmt_memories(
    memories: list[dict], fmt: ResponseFormat, total: int | None = None
) -> str:
    """Render a list of memory dicts as Markdown or JSON.

    Args:
        memories: List of memory dicts (from _row_to_dict).
        fmt: Output format — ResponseFormat.MARKDOWN or ResponseFormat.JSON.
        total: Optional total count for pagination headers.

    Returns:
        Formatted string suitable for returning from an MCP tool handler.
    """
    if fmt == ResponseFormat.JSON:
        payload: dict[str, Any] = {"count": len(memories), "memories": memories}
        if total is not None:
            payload["total"] = total
        return json.dumps(payload, indent=2, default=str)
    if not memories:
        return "_No memories found._"
    parts = []
    if total is not None:
        parts.append(f"**Showing {len(memories)} of {total} memories**\n")
    for m in memories:
        parts.append(_fmt_memory_md(m))
    return "\n---\n".join(parts)


# ---------------------------------------------------------------------------
# Revision history formatting (issue #187)
# ---------------------------------------------------------------------------

# Short content preview length for a revision list entry — distinct from
# _fmt_memory_md's 2000-char full-content truncation, since a history listing
# shows several revisions at once and only needs enough text to recognize
# which one to revert to (mirrors scheduler._log_delivery's 200-char preview).
_REVISION_PREVIEW_CHARS = 200


def _fmt_revision_md(rev: dict) -> str:
    """Render a single memory_revisions row as a Markdown list item.

    Args:
        rev: A revision dict with keys id, edited_at, content, category,
             tags, metadata, revision_reason.

    Returns:
        A one-line Markdown bullet.
    """
    content = rev.get("content") or ""
    preview = content[:_REVISION_PREVIEW_CHARS]
    if len(content) > _REVISION_PREVIEW_CHARS:
        preview += "…"
    line = f"- **Revision `{rev['id']}`** ({rev['edited_at']}) — category: {rev.get('category', '?')} — {preview}"
    if rev.get("revision_reason"):
        line += f"  _{rev['revision_reason']}_"
    return line


def _fmt_revisions(memory_id: str, revisions: list[dict], fmt: ResponseFormat) -> str:
    """Render a memory's revision history as Markdown or JSON.

    Args:
        memory_id: The memory these revisions belong to (for the JSON envelope
            and the Markdown header).
        revisions: Revision dicts, expected newest-first.
        fmt: Output format — ResponseFormat.MARKDOWN or ResponseFormat.JSON.

    Returns:
        Formatted string suitable for returning from an MCP tool handler.
    """
    if fmt == ResponseFormat.JSON:
        return json.dumps(
            {"memory_id": memory_id, "count": len(revisions), "revisions": revisions},
            indent=2,
            default=str,
        )
    if not revisions:
        return f"_No revision history for memory `{memory_id}`._"
    lines = [f"**{len(revisions)} revision(s) for memory `{memory_id}`, newest first:**\n"]
    lines.extend(_fmt_revision_md(r) for r in revisions)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "_fmt_memory_md",
    "_fmt_memories",
    "_fmt_revision_md",
    "_fmt_revisions",
]
