"""
remind_me_mcp.mempalace_import — bulk importer for MemPalace drawers.

MemPalace only exposes drawer content one at a time through its own MCP
tools (`mempalace_get_drawer`), which is impractical at the scale of a real
palace (a single wing can hold tens of thousands of drawers). This module
instead opens MemPalace's persistent ChromaDB store directly, read-only, and
pulls content in bulk. Requires the optional ``mempalace`` extra
(``pip install remind-me-mcp[mempalace]``).

Drawers already matching remind_me's own memory frontmatter (as mined from a
prior remind_me export) have their original id/category/tags/source/created_at
restored; everything else is stored as one opaque memory per drawer, tagged
with its wing/room — MemPalace's AAAK dialect is designed to be read as-is,
so no special decoding is needed.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any

from remind_me_mcp.config import EMBED_BATCH_SIZE, MEMPALACE_PATH
from remind_me_mcp.db import _embed_and_store_rows, _get_db, _make_id, _now_iso

log = logging.getLogger("remind_me_mcp.mempalace_import")

_import_lock = threading.Lock()
# Max ids per IN (...) clause (SQLite's default bound-parameter limit is 999).
_ROWID_LOOKUP_BATCH = 500

COLLECTION_NAME = "mempalace_drawers"
SOURCE = "mempalace_import"

# Matches remind_me's own memory frontmatter, e.g.:
#   ---
#   id: 6bb2c33ed386
#   created: 2026-02-23T00:08:29.406417Z
#   category: fact
#   source: remind_me/manual
#   tags: work, deadline, migration
#   ---
#
#   <content>
_FRONTMATTER_RE = re.compile(
    r"\A---\n(?P<fields>(?:[a-zA-Z_]+:[^\n]*\n)+)---\n\n?(?P<body>.*)\Z",
    re.DOTALL,
)


def _parse_frontmatter(content: str) -> tuple[dict[str, str], str] | None:
    """Split remind_me-native frontmatter from a drawer's content, if present.

    Returns (fields, body) or None if content doesn't match the pattern.
    """
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return None
    fields: dict[str, str] = {}
    for line in m.group("fields").splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields, m.group("body")


def _open_collection() -> Any:
    """Open MemPalace's ChromaDB collection read-only.

    Raises ImportError if chromadb isn't installed, FileNotFoundError if no
    palace exists at MEMPALACE_PATH.
    """
    import chromadb  # optional dependency (extras: mempalace)

    if not MEMPALACE_PATH.exists():
        raise FileNotFoundError(f"No MemPalace store found at {MEMPALACE_PATH}")
    client = chromadb.PersistentClient(path=str(MEMPALACE_PATH))
    return client.get_collection(COLLECTION_NAME)


def pull_mempalace(
    wing: str = "",
    room: str = "",
    limit: int = 500,
    offset: int = 0,
    category: str = "",
    tags: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Pull a page of MemPalace drawers into remind_me memory.

    Drawers already imported (tracked in mempalace_imports by drawer_id) are
    skipped, so reruns are idempotent. Args mirror MempalaceImportInput.

    Returns:
        Summary dict: fetched, already_imported, to_import, native_format,
        opaque_format, imported, has_more.
    """
    where: dict[str, Any] | None = None
    if wing and room:
        where = {"$and": [{"wing": wing}, {"room": room}]}
    elif wing:
        where = {"wing": wing}
    elif room:
        where = {"room": room}

    collection = _open_collection()
    got = collection.get(where=where, limit=limit, offset=offset, include=["documents", "metadatas"])
    drawer_ids: list[str] = got["ids"]
    documents: list[str] = got["documents"]
    metadatas: list[dict[str, Any]] = got["metadatas"]

    db = _get_db()
    fetched = len(drawer_ids)
    already: set[str] = set()
    if drawer_ids:
        placeholders = ",".join("?" for _ in drawer_ids)
        rows = db.execute(
            f"SELECT drawer_id FROM mempalace_imports WHERE drawer_id IN ({placeholders})",
            drawer_ids,
        ).fetchall()
        already = {row["drawer_id"] for row in rows}

    to_import = [
        (did, doc or "", meta or {})
        for did, doc, meta in zip(drawer_ids, documents, metadatas, strict=True)
        if did not in already
    ]
    native_count = sum(1 for _, doc, _ in to_import if _parse_frontmatter(doc))

    result: dict[str, Any] = {
        "wing": wing or None,
        "room": room or None,
        "fetched": fetched,
        "already_imported": len(already),
        "to_import": len(to_import),
        "native_format": native_count,
        "opaque_format": len(to_import) - native_count,
        "offset": offset,
        "limit": limit,
        "has_more": fetched == limit,
    }
    if dry_run:
        result["imported"] = 0
        return result

    extra_tags = tags or []
    now = _now_iso()
    embed_entries: list[tuple[str, str]] = []

    with _import_lock:
        for drawer_id, doc, meta in to_import:
            parsed = _parse_frontmatter(doc)
            wing_val = meta.get("wing", wing) or ""
            room_val = meta.get("room", room) or ""
            if parsed:
                fields, body = parsed
                mem_category = fields.get("category") or category or "mempalace_import"
                native_tags = [t.strip() for t in fields.get("tags", "").split(",") if t.strip()]
                mem_tags = native_tags + extra_tags
                mem_source = f"mempalace:{fields.get('source', 'unknown')}"
                created_at = fields.get("created") or now
                content = body
            else:
                mem_category = category or "mempalace_import"
                mem_tags = [t for t in (wing_val, room_val) if t] + extra_tags
                mem_source = SOURCE
                created_at = now
                content = doc

            mem_id = _make_id(content)
            record_metadata = {"mempalace_drawer_id": drawer_id, "wing": wing_val, "room": room_val}
            db.execute(
                """INSERT OR IGNORE INTO memories
                   (id, content, category, tags, source, metadata, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    mem_id,
                    content,
                    mem_category,
                    json.dumps(mem_tags),
                    mem_source,
                    json.dumps(record_metadata),
                    created_at,
                    now,
                ),
            )
            db.execute(
                "INSERT OR IGNORE INTO mempalace_imports (drawer_id, memory_id, imported_at) VALUES (?, ?, ?)",
                (drawer_id, mem_id, now),
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
        for i in range(0, len(rows_to_embed), EMBED_BATCH_SIZE):
            _embed_and_store_rows(rows_to_embed[i : i + EMBED_BATCH_SIZE])

    result["imported"] = len(embed_entries)
    return result
