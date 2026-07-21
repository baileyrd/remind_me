"""
remind_me_mcp.dbs_import — bulk importer for a dbs (daily-backup-system) store.

dbs (https://github.com/baileyrd/daily-backup-system) archives a user's data
from many external sources (Reddit, YouTube, Raindrop, GitHub stars, ...)
into one SQLite database with a uniform ``items``/``sources`` schema. This
module opens that database directly, read-only, and imports each live item
as a memory — preserving dbs's source/tags as first-class knowledge-graph
entities (FT-04) instead of collapsing them into note prose, which is what
the file-export pipeline (``dbs export-notes`` + the folder watcher) has to
do instead. (``item_kind`` becomes the memory's category/metadata, not an
entity — there's no established "kind" entity type elsewhere in this
codebase's entity graph to reuse.) This is "option 3" of the dbs/remind_me
integration review — see
docs/dbs-integration-review-2026-07-21.md.

No dependency on the ``dbs`` package itself: dbs's SQLite schema (items,
sources) is stable, documented, and read directly with plain SQL, the same
way MemPalace's ChromaDB store is read directly rather than through its own
MCP tools.

Tracks its own dedup/update table (``dbs_imports``, keyed by
``(dbs_source, external_id)`` — dbs's own item identity) so reruns only
import new items and re-import edited ones. An edited item (content_hash
changed since the last import) gets a *fresh* memory, with the old one
marked ``superseded_by`` the new id (mirrors the folder watcher's
changed-file supersession) — this is what lets option 3 avoid the
``item_created_at``-only staleness gap that option 1 (``dbs export-notes``)
has to live with (see dbs's own docs/BACKLOG.md #4): comparing every item's
actual content_hash on every pull catches edits regardless of which
timestamp dbs recorded them under.

Phase 4: a lightweight ``Connector`` is also registered under the ``"dbs"``
kind, purely for discovery (``remind_me_list_connectors``) — like
``mempalace``, the real ingestion path (:func:`pull_dbs`) keeps its own
bespoke per-item dedup loop and never actually flows through
``import_chat_file``'s registry dispatch.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

from remind_me_mcp.db import (
    _embed_and_store_rows,
    _get_db,
    _link_memory_entity,
    _now_iso,
    _upsert_entity,
)
from remind_me_mcp.importer import register_connector

log = logging.getLogger("remind_me_mcp.dbs_import")

_import_lock = threading.Lock()
# Max ids per IN (...) clause (SQLite's default bound-parameter limit is 999).
_ROWID_LOOKUP_BATCH = 500

SOURCE_ENTITY_KIND = "dbs_source"
TAG_ENTITY_KIND = "tag"


def _dbs_connector(raw: str, meta: dict[str, Any]) -> tuple[list[tuple[str, dict[str, Any]]], int]:
    """Connector-protocol facade, registered for discovery only (see module docstring)."""
    return [(raw, {})], 1


register_connector("dbs", _dbs_connector)


def _open_dbs_db(db_path: str) -> sqlite3.Connection:
    """Open a dbs SQLite database read-only.

    Raises FileNotFoundError if no database exists at the given path, or
    sqlite3.DatabaseError if it exists but isn't a valid SQLite file (the
    file signature is checked immediately below via a real read, since
    sqlite3.connect() itself doesn't validate anything until first use).
    """
    path = Path(db_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"No dbs database found at {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
    except sqlite3.DatabaseError:
        conn.close()
        raise
    return conn


def _dbs_memory_id(dbs_source: str, external_id: str, content_hash: str) -> str:
    """Deterministic memory id for one dbs item version (SEC-10).

    Unlike _make_id (content + wall-clock timestamp, deliberately NOT
    deterministic -- see its docstring -- so remind_me_add can store the
    same content again as a distinct memory), this is a pure hash of
    (dbs_source, external_id, content_hash): two concurrent or retried
    pull_dbs calls processing the same item version always compute the
    SAME id, so INSERT OR IGNORE correctly collapses them into one row
    instead of leaving an orphan duplicate that dbs_imports' (dbs_source,
    external_id) uniqueness never catches (that table only tracks the id
    of whichever call wrote *last*). A genuine content edit changes
    content_hash, which changes the id, which is exactly what's supposed
    to trigger the supersession path below.
    """
    return hashlib.sha256(f"dbs:{dbs_source}:{external_id}:{content_hash}".encode()).hexdigest()[:12]


def _memory_content(title: str | None, body: str | None, url: str | None, external_id: str) -> str:
    """Compose memory content from a dbs item's title/body, falling back to url/id."""
    title = (title or "").strip()
    body = (body or "").strip()
    if title and body:
        return f"{title}\n\n{body}"
    return title or body or url or external_id


def pull_dbs(
    db_path: str,
    source: str = "",
    item_type: str = "",
    limit: int = 500,
    offset: int = 0,
    tags: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Pull a page of dbs items into remind_me memory + knowledge graph.

    Items already imported with an unchanged content_hash (tracked in
    dbs_imports) are skipped. An item whose content_hash has changed since
    its last import gets a fresh memory (the old one is marked
    superseded_by the new id), so reruns pick up edits as well as new items.
    Args mirror DbsImportInput.

    Returns:
        Summary dict: fetched, already_imported, to_import, created,
        updated, imported (created + updated), has_more.
    """
    dbs_db = _open_dbs_db(db_path)
    try:
        where = ["i.deleted = 0"]
        params: list[Any] = []
        if source:
            where.append("s.name = ?")
            params.append(source)
        if item_type:
            where.append("i.item_kind = ?")
            params.append(item_type)
        where_sql = " AND ".join(where)
        rows = dbs_db.execute(
            f"""SELECT i.external_id, i.item_kind, i.title, i.url, i.body,
                       i.tags_json, i.item_created_at, i.item_updated_at,
                       i.content_hash, s.name AS source_name
                FROM items i JOIN sources s ON i.source_id = s.id
                WHERE {where_sql}
                ORDER BY i.item_created_at, i.external_id
                LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
    finally:
        dbs_db.close()

    fetched = len(rows)
    db = _get_db()
    extra_tags = tags or []
    now = _now_iso()
    created = 0
    updated = 0
    embed_entries: list[tuple[str, str]] = []

    # SEC-10: the tracked-state lookup and the created/updated writes below
    # share one lock acquisition. Splitting them (read outside, write
    # inside) let two concurrent/retried calls both read "not yet
    # imported" for the same item before either had written it -- with
    # _make_id's wall-clock-salted ids, each call minted a *different* id
    # and both inserts succeeded, leaving a permanent orphan duplicate that
    # dbs_imports' (dbs_source, external_id) row (last-writer-wins) never
    # caught. _dbs_memory_id's determinism closes half of that (duplicate
    # calls now compute the identical id, so INSERT OR IGNORE collapses
    # them); locking the read+decide+write as one unit closes the other
    # half (an accurate already_imported/to_import count and no wasted
    # duplicate work).
    with _import_lock:
        tracked: dict[tuple[str, str], sqlite3.Row] = {}
        if rows:
            by_source: dict[str, list[str]] = {}
            for row in rows:
                by_source.setdefault(row["source_name"], []).append(row["external_id"])
            for src, ext_ids in by_source.items():
                placeholders = ",".join("?" for _ in ext_ids)
                for tr in db.execute(
                    f"""SELECT dbs_source, external_id, memory_id, content_hash
                        FROM dbs_imports
                        WHERE dbs_source = ? AND external_id IN ({placeholders})""",
                    (src, *ext_ids),
                ).fetchall():
                    tracked[(tr["dbs_source"], tr["external_id"])] = tr

        already = 0
        to_import: list[sqlite3.Row] = []
        for row in rows:
            key = (row["source_name"], row["external_id"])
            prior = tracked.get(key)
            if prior is not None and prior["content_hash"] == row["content_hash"]:
                already += 1
            else:
                to_import.append(row)

        result: dict[str, Any] = {
            "source": source or None,
            "item_type": item_type or None,
            "fetched": fetched,
            "already_imported": already,
            "to_import": len(to_import),
            "offset": offset,
            "limit": limit,
            "has_more": fetched == limit,
        }
        if dry_run:
            result["created"] = 0
            result["updated"] = 0
            result["imported"] = 0
            return result

        for row in to_import:
            key = (row["source_name"], row["external_id"])
            prior = tracked.get(key)
            item_tags = [t for t in json.loads(row["tags_json"] or "[]") if t] + extra_tags
            content = _memory_content(row["title"], row["body"], row["url"], row["external_id"])
            mem_id = _dbs_memory_id(row["source_name"], row["external_id"], row["content_hash"])
            metadata = {
                "dbs_source": row["source_name"],
                "dbs_external_id": row["external_id"],
                "dbs_item_kind": row["item_kind"],
                "dbs_url": row["url"],
                "dbs_content_hash": row["content_hash"],
            }
            db.execute(
                """INSERT OR IGNORE INTO memories
                   (id, content, category, tags, source, metadata, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    mem_id,
                    content,
                    row["item_kind"] or "dbs_import",
                    json.dumps(item_tags),
                    f"dbs:{row['source_name']}",
                    json.dumps(metadata),
                    row["item_created_at"] or now,
                    now,
                ),
            )

            source_entity_id = _upsert_entity(db, row["source_name"], kind=SOURCE_ENTITY_KIND, now=now)
            _link_memory_entity(db, mem_id, source_entity_id, now=now)
            for tag in item_tags:
                if not tag.strip():
                    continue
                tag_entity_id = _upsert_entity(db, tag, kind=TAG_ENTITY_KIND, now=now)
                _link_memory_entity(db, mem_id, tag_entity_id, now=now)

            if prior is not None:
                db.execute(
                    "UPDATE memories SET superseded_by = ? WHERE id = ?",
                    (mem_id, prior["memory_id"]),
                )
                updated += 1
            else:
                created += 1

            db.execute(
                """INSERT INTO dbs_imports (dbs_source, external_id, memory_id, content_hash, imported_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(dbs_source, external_id)
                   DO UPDATE SET memory_id = excluded.memory_id,
                                 content_hash = excluded.content_hash,
                                 imported_at = excluded.imported_at""",
                (row["source_name"], row["external_id"], mem_id, row["content_hash"], now),
            )
            embed_entries.append((mem_id, content))
        db.commit()

    if embed_entries:
        ids = [mem_id for mem_id, _ in embed_entries]
        content_by_id = dict(embed_entries)
        rows_to_embed: list[tuple[int, str]] = []
        with _import_lock:
            for i in range(0, len(ids), _ROWID_LOOKUP_BATCH):
                batch_ids = ids[i : i + _ROWID_LOOKUP_BATCH]
                placeholders = ",".join("?" for _ in batch_ids)
                for row in db.execute(
                    f"SELECT id, rowid FROM memories WHERE id IN ({placeholders})",
                    batch_ids,
                ).fetchall():
                    rows_to_embed.append((row["rowid"], content_by_id[row["id"]]))
        _embed_and_store_rows(rows_to_embed)

    result["created"] = created
    result["updated"] = updated
    result["imported"] = created + updated
    return result


__all__ = ["pull_dbs", "SOURCE_ENTITY_KIND", "TAG_ENTITY_KIND"]
