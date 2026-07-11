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

FT-06: the entity graph (the ``entities`` and ``memory_entities`` tables) is
included by default, so a backup captures the full knowledge graph. Graph
records carry a ``record_type`` discriminator ('entity' / 'memory_entity') —
the same wire shape FT-04 sync uses; memory records carry no record_type. The
importer skips graph records in its chat-message parsing and restores them
through the entity helpers instead (see importer._restore_graph_records).
Restore caveat: links reference original memory ids, and a chat re-import
assigns NEW memory ids, so links only fully restore into a database that still
holds the referenced memories — dangling links are skipped and counted.
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


def collect_graph_records(
    memory_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Collect entity-graph rows as export records (FT-06).

    Entity records mirror the FT-04 sync wire format: every column of the
    ``entities`` table plus ``record_type='entity'`` (aliases deserialized to
    a list). Link records carry ``record_type='memory_entity'`` with
    memory_id/entity_id/created_at. Entities are emitted before links so a
    sequential restore can verify link endpoints exist.

    Args:
        memory_ids: When given (a filtered export), only links whose memory_id
            is in this set are included, and only entities referenced by those
            links. When None (a full backup), every entity — including ones
            with no links — and every link is exported.

    Returns:
        List of record dicts: entities ordered by (created_at, id), then
        links ordered by (created_at, memory_id, entity_id).
    """
    db = _get_db()
    entities = [
        _row_to_dict(r)
        for r in db.execute(
            "SELECT * FROM entities ORDER BY created_at, id"
        ).fetchall()
    ]
    links = [
        dict(r)
        for r in db.execute(
            """SELECT memory_id, entity_id, created_at FROM memory_entities
               ORDER BY created_at, memory_id, entity_id"""
        ).fetchall()
    ]
    if memory_ids is not None:
        links = [li for li in links if li["memory_id"] in memory_ids]
        linked_eids = {li["entity_id"] for li in links}
        entities = [e for e in entities if e["id"] in linked_eids]
    return [
        *({"record_type": "entity", **e} for e in entities),
        *({"record_type": "memory_entity", **li} for li in links),
    ]


def collect_export_records(
    category: str | None = None,
    tags: list[str] | None = None,
    include_graph: bool = True,
) -> list[dict[str, Any]]:
    """Collect memory (and entity-graph) rows as export records.

    Every column of the memories table is included so the export is a complete
    backup (embedding vectors live in separate tables and are excluded — they
    are rebuildable via remind_me_reindex). A ``role`` key is added to each
    record so the file is directly consumable by the importer's generic
    {role, content} message format for round-trip migration.

    With ``include_graph`` (the default — a backup should be complete) the
    entity graph follows the memory records as ``record_type``-tagged records
    (FT-06, see :func:`collect_graph_records`); memory records carry no
    record_type, so existing consumers are unaffected and the importer skips
    graph records in its chat-message parsing. Filtered exports scope the
    graph to the exported memories' links and the entities they reference.

    Args:
        category: If set, only export memories with this category.
        tags: If set, only export memories that have ALL of these tags.
        include_graph: Append entities/memory_entities records (default True).

    Returns:
        List of record dicts — memories ordered by (created_at, id), with the
        JSON columns (tags, metadata) deserialized into Python objects —
        followed by the graph records when ``include_graph`` is set.
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
    records = [{"role": "assistant", **_row_to_dict(r)} for r in rows]
    if include_graph:
        # Full backups export the whole graph; filtered exports scope it to
        # the exported memories (links by memory id, entities by reference).
        filtered = bool(category or tags)
        memory_ids = {str(r["id"]) for r in records} if filtered else None
        records.extend(collect_graph_records(memory_ids))
    return records


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
    include_graph: bool = True,
) -> dict[str, Any]:
    """Export memories (and the entity graph) to JSON or JSONL, inline or to a file.

    Args:
        format: 'json' or 'jsonl'.
        category: If set, only export memories with this category.
        tags: If set, only export memories that have ALL of these tags.
        file_path: Destination file. Callers are responsible for validating it
            against EXPORT_ROOTS (the MCP input model and HTTP route both do).
            When None, the payload is returned inline.
        inline_max: When set and file_path is None, refuse inline exports
            larger than this many records — memories plus graph records, the
            cap is about payload size (the caller should retry with a
            file_path instead).
        include_graph: Append entities/memory_entities records (FT-06,
            default True).

    Returns:
        A status dict. File write: {'status': 'ok', 'exported': int,
        'format': str, 'file': str, 'bytes': int}. Inline: {'status': 'ok',
        'exported': int, 'format': str, 'content': str}. Over the inline
        limit: {'status': 'error', 'error': str}. 'exported' counts memory
        records only; with include_graph, 'entities' and 'links' report the
        graph record counts.

    Raises:
        ValueError: If *format* is unsupported.
        OSError: If writing *file_path* fails.
    """
    records = collect_export_records(
        category=category, tags=tags, include_graph=include_graph
    )
    payload = render_export(records, format)
    n_entities = sum(1 for r in records if r.get("record_type") == "entity")
    n_links = sum(1 for r in records if r.get("record_type") == "memory_entity")
    n_memories = len(records) - n_entities - n_links
    counts: dict[str, Any] = {"exported": n_memories}
    if include_graph:
        counts["entities"] = n_entities
        counts["links"] = n_links

    if file_path is not None:
        path = Path(file_path)
        # write_bytes (not write_text) so no platform newline translation
        # happens — otherwise \n -> \r\n on Windows would make the on-disk
        # file bigger than the "bytes" count below, and a JSON/JSONL export
        # is meant to be byte-identical across platforms for diffing/hashing.
        path.write_bytes(payload.encode("utf-8"))
        log.info(
            "Exported %d memories (+%d entities, %d links) to %s (%s)",
            n_memories, n_entities, n_links, path, format,
        )
        return {
            "status": "ok",
            **counts,
            "format": format,
            "file": str(path),
            "bytes": len(payload.encode("utf-8")),
        }

    if inline_max is not None and len(records) > inline_max:
        return {
            "status": "error",
            "error": (
                f"Export of {len(records)} records exceeds the inline limit of "
                f"{inline_max}. Pass file_path to write the export to a file."
            ),
        }

    return {
        "status": "ok",
        **counts,
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
    "collect_graph_records",
    "render_export",
    "export_memories",
]
