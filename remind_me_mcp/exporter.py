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

Phase 3: typed entity-to-entity relations (``entity_relations``) are
included the same way, tagged ``record_type='entity_relation'``. A filtered
export scopes relations to those whose subject AND object are both among the
already-scoped entities (the same "only what's reachable from the exported
memories" rule links follow).
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
    """Collect entity-graph rows as export records (FT-06, Phase 3).

    Entity records mirror the FT-04 sync wire format: every column of the
    ``entities`` table plus ``record_type='entity'`` (aliases deserialized to
    a list). Link records carry ``record_type='memory_entity'`` with
    memory_id/entity_id/created_at. Relation records carry
    ``record_type='entity_relation'`` with every ``entity_relations`` column.
    Entities are emitted first so a sequential restore can verify link and
    relation endpoints exist.

    Args:
        memory_ids: When given (a filtered export), only links whose memory_id
            is in this set are included, only entities referenced by those
            links, and only relations whose subject AND object are both among
            those entities. When None (a full backup), every entity —
            including ones with no links — and every link/relation is
            exported.

    Returns:
        List of record dicts: entities ordered by (created_at, id), then
        links ordered by (created_at, memory_id, entity_id), then relations
        ordered by (created_at, id).
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
    relations = [
        _row_to_dict(r)
        for r in db.execute(
            "SELECT * FROM entity_relations ORDER BY created_at, id"
        ).fetchall()
    ]
    if memory_ids is not None:
        links = [li for li in links if li["memory_id"] in memory_ids]
        linked_eids = {li["entity_id"] for li in links}
        entities = [e for e in entities if e["id"] in linked_eids]
        relations = [
            r for r in relations
            if r["subject_entity_id"] in linked_eids
            and r["object_entity_id"] in linked_eids
        ]
    return [
        *({"record_type": "entity", **e} for e in entities),
        *({"record_type": "memory_entity", **li} for li in links),
        *({"record_type": "entity_relation", **r} for r in relations),
    ]


def collect_export_records(
    category: str | None = None,
    tags: list[str] | None = None,
    include_graph: bool = True,
    include_deleted: bool = False,
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

    Soft-deleted (``deleted_at``) and superseded (``superseded_by``) memories
    are excluded by default (issue #157): the generic importer has no
    concept of a tombstone or a superseded record -- it treats every
    exported record as a fresh live message, so re-importing an export that
    included them silently resurrected every memory the user had
    deliberately deleted, and every stale chunk the watcher had superseded,
    as brand-new live memories on the receiving side. Pass
    ``include_deleted=True`` for the genuine full-backup/audit case, where
    tombstones and history are exactly what you want preserved.

    Args:
        category: If set, only export memories with this category.
        tags: If set, only export memories that have ALL of these tags.
        include_graph: Append entities/memory_entities records (default True).
        include_deleted: Include soft-deleted and superseded memories
            (default False -- see above).

    Returns:
        List of record dicts — memories ordered by (created_at, id), with the
        JSON columns (tags, metadata) deserialized into Python objects —
        followed by the graph records when ``include_graph`` is set.
    """
    db = _get_db()
    conditions: list[str] = []
    bindings: list[Any] = []
    if not include_deleted:
        conditions.append("m.deleted_at IS NULL")
        conditions.append("m.superseded_by IS NULL")
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
        # Full backups export the whole graph -- including entities/links
        # not connected to any exported memory (e.g. a manually-created
        # entity with no mentions yet) -- so category/tag filtering is what
        # narrows the graph, not the deleted/superseded exclusion above.
        # A link that ends up pointing at an excluded memory_id degrades
        # the same way an already-supported case does: the importer skips
        # (and counts) dangling links on restore, so this isn't a new
        # failure mode, just the existing one reached a different way.
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
    include_deleted: bool = False,
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
        include_graph: Append entities/memory_entities/entity_relations
            records (FT-06/Phase 3, default True).
        include_deleted: Include soft-deleted and superseded memories
            (issue #157, default False). Re-importing an export that
            includes them resurrects them as fresh live memories, since the
            generic importer has no concept of a tombstone -- only set this
            for a genuine full-backup/audit export, not for portable
            migration between machines.

    Returns:
        A status dict. File write: {'status': 'ok', 'exported': int,
        'format': str, 'file': str, 'bytes': int}. Inline: {'status': 'ok',
        'exported': int, 'format': str, 'content': str}. Over the inline
        limit: {'status': 'error', 'error': str}. 'exported' counts memory
        records only; with include_graph, 'entities', 'links', and
        'relations' report the graph record counts.

    Raises:
        ValueError: If *format* is unsupported.
        OSError: If writing *file_path* fails.
    """
    records = collect_export_records(
        category=category,
        tags=tags,
        include_graph=include_graph,
        include_deleted=include_deleted,
    )
    payload = render_export(records, format)
    n_entities = sum(1 for r in records if r.get("record_type") == "entity")
    n_links = sum(1 for r in records if r.get("record_type") == "memory_entity")
    n_relations = sum(1 for r in records if r.get("record_type") == "entity_relation")
    n_memories = len(records) - n_entities - n_links - n_relations
    counts: dict[str, Any] = {"exported": n_memories}
    if include_graph:
        counts["entities"] = n_entities
        counts["links"] = n_links
        counts["relations"] = n_relations

    if file_path is not None:
        path = Path(file_path)
        # write_bytes (not write_text) so no platform newline translation
        # happens — otherwise \n -> \r\n on Windows would make the on-disk
        # file bigger than the "bytes" count below, and a JSON/JSONL export
        # is meant to be byte-identical across platforms for diffing/hashing.
        path.write_bytes(payload.encode("utf-8"))
        log.info(
            "Exported %d memories (+%d entities, %d links, %d relations) to %s (%s)",
            n_memories, n_entities, n_links, n_relations, path, format,
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
