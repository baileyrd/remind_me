"""
remind_me_mcp.exporter — Memory export engine (FT-01).

Dumps memories to JSON or JSONL in a format that is both a complete logical
backup (every column of the memories table — id, content, category, tags,
source, metadata, timestamps, and lifecycle fields like vitality and
superseded_by) and consumable by the chat import engine in importer.py: each
record carries a ``role``/``content`` pair, the generic message format the
importer accepts, enabling round-trip migration between machines.

Round-trip note: re-importing an export preserves memory *content* verbatim,
but the importer re-chunks long content and assigns fresh ids, category, tags,
and source — the original values are still present in the export file for
manual restoration. Embedding vectors are intentionally excluded: they are
derived data and can be rebuilt on the target machine with remind_me_reindex.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from remind_me_mcp.db import _get_db, _row_to_dict

log = logging.getLogger("remind_me_mcp.exporter")

EXPORT_FORMATS = ("json", "jsonl")
"""Supported export serialization formats."""

EXPORT_INLINE_MAX = 200
"""Maximum number of memories the MCP tool returns inline (no file_path)."""

# ---------------------------------------------------------------------------
# Record collection and rendering
# ---------------------------------------------------------------------------


def collect_export_records(
    category: str | None = None,
    tags: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Collect memory rows as export records, optionally filtered.

    Every column of the memories table is included so the export is a complete
    backup (embedding vectors live in separate tables and are excluded — they
    are rebuildable via remind_me_reindex). A ``role`` key is added to each
    record so the file is directly consumable by the importer's generic
    {role, content} message format for round-trip migration.

    Args:
        category: If set, only export memories with this category.
        tags: If set, only export memories that have ALL of these tags.

    Returns:
        List of record dicts ordered by (created_at, id), with the JSON
        columns (tags, metadata) deserialized into Python objects.
    """
    db = _get_db()
    conditions: list[str] = []
    bindings: list[Any] = []
    if category:
        conditions.append("m.category = ?")
        bindings.append(category)
    # Tag filtering via the memory_tags junction table (same ALL-of semantics
    # and SQL shape as api_list / _semantic_search).
    for i, tag in enumerate(tags or []):
        alias = f"mt{i}"
        conditions.append(
            f"EXISTS (SELECT 1 FROM memory_tags {alias}"
            f" WHERE {alias}.memory_id = m.id AND {alias}.tag = ?)"
        )
        bindings.append(tag)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = db.execute(
        f"SELECT m.* FROM memories m {where} ORDER BY m.created_at, m.id",
        bindings,
    ).fetchall()
    # 'role' exists solely for importer compatibility: the default
    # extract_mode ('assistant_messages') keeps role 'assistant' content
    # verbatim, so a re-import preserves memory content losslessly.
    return [{"role": "assistant", **_row_to_dict(r)} for r in rows]


def render_export(records: list[dict[str, Any]], format: str) -> str:
    """Serialize export records to the requested format.

    Args:
        records: Export records from :func:`collect_export_records`.
        format: 'json' (single indented array) or 'jsonl' (one record per line).

    Returns:
        The serialized export payload.

    Raises:
        ValueError: If *format* is not one of EXPORT_FORMATS.
    """
    if format == "json":
        return json.dumps(records, indent=2, ensure_ascii=False, default=str)
    if format == "jsonl":
        return "".join(
            json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in records
        )
    raise ValueError(f"Unsupported export format: {format!r} (use 'json' or 'jsonl')")


# ---------------------------------------------------------------------------
# Public export function
# ---------------------------------------------------------------------------


def export_memories(
    format: str = "json",
    category: str | None = None,
    tags: list[str] | None = None,
    file_path: str | None = None,
    inline_max: int | None = None,
) -> dict[str, Any]:
    """Export memories to JSON or JSONL, inline or to a file.

    Args:
        format: 'json' or 'jsonl'.
        category: If set, only export memories with this category.
        tags: If set, only export memories that have ALL of these tags.
        file_path: Destination file. Callers are responsible for validating it
            against EXPORT_ROOTS (the MCP input model and HTTP route both do).
            When None, the payload is returned inline.
        inline_max: When set and file_path is None, refuse inline exports
            larger than this many memories (the caller should retry with a
            file_path instead).

    Returns:
        A status dict. File write: {'status': 'ok', 'exported': int,
        'format': str, 'file': str, 'bytes': int}. Inline: {'status': 'ok',
        'exported': int, 'format': str, 'content': str}. Over the inline
        limit: {'status': 'error', 'error': str}.

    Raises:
        ValueError: If *format* is unsupported.
        OSError: If writing *file_path* fails.
    """
    records = collect_export_records(category=category, tags=tags)
    payload = render_export(records, format)

    if file_path is not None:
        path = Path(file_path)
        path.write_text(payload, encoding="utf-8")
        log.info("Exported %d memories to %s (%s)", len(records), path, format)
        return {
            "status": "ok",
            "exported": len(records),
            "format": format,
            "file": str(path),
            "bytes": len(payload.encode("utf-8")),
        }

    if inline_max is not None and len(records) > inline_max:
        return {
            "status": "error",
            "error": (
                f"Export of {len(records)} memories exceeds the inline limit of "
                f"{inline_max}. Pass file_path to write the export to a file."
            ),
        }

    return {
        "status": "ok",
        "exported": len(records),
        "format": format,
        "content": payload,
    }


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "EXPORT_FORMATS",
    "EXPORT_INLINE_MAX",
    "collect_export_records",
    "render_export",
    "export_memories",
]
