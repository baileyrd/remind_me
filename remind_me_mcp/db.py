"""
remind_me_mcp.db — Database connection, schema management, and helpers.

All SQLite access goes through this module. The schema is created on first
connection (via _ensure_schema). Vector embeddings are stored in a separate
virtual table (memories_vec) when the sqlite-vec extension is available.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import struct
from datetime import datetime, timezone
from typing import Any

from remind_me_mcp.config import DB_PATH, EMBEDDING_DIM
from remind_me_mcp.embeddings import _get_embedder

log = logging.getLogger("remind_me_mcp.db")

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def _get_db() -> sqlite3.Connection:
    """Open (and lazily initialize) the SQLite database."""
    db = sqlite3.connect(str(DB_PATH), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")  # safe for concurrent readers
    db.execute("PRAGMA foreign_keys=ON")

    # Load sqlite-vec extension if available
    try:
        import sqlite_vec
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
    except (ImportError, Exception) as e:
        log.debug("sqlite-vec not available: %s (vector search disabled)", e)

    _ensure_schema(db)
    return db


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def _ensure_schema(db: sqlite3.Connection) -> None:
    """Create tables, FTS virtual table, triggers, and indexes if absent."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id          TEXT PRIMARY KEY,
            content     TEXT NOT NULL,
            category    TEXT NOT NULL DEFAULT 'general',
            tags        TEXT NOT NULL DEFAULT '[]',  -- JSON array
            source      TEXT NOT NULL DEFAULT 'manual',
            metadata    TEXT NOT NULL DEFAULT '{}',  -- JSON object
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chat_imports (
            import_id   TEXT PRIMARY KEY,
            filename    TEXT NOT NULL,
            hash        TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            stats       TEXT NOT NULL DEFAULT '{}'
        );

        -- FTS5 virtual table for full-text search
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            content, category, tags,
            content='memories',
            content_rowid='rowid'
        );

        -- Triggers to keep FTS in sync
        CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(rowid, content, category, tags)
            VALUES (new.rowid, new.content, new.category, new.tags);
        END;

        CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content, category, tags)
            VALUES ('delete', old.rowid, old.content, old.category, old.tags);
        END;

        CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content, category, tags)
            VALUES ('delete', old.rowid, old.content, old.category, old.tags);
            INSERT INTO memories_fts(rowid, content, category, tags)
            VALUES (new.rowid, new.content, new.category, new.tags);
        END;

        CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category);
        CREATE INDEX IF NOT EXISTS idx_memories_source ON memories(source);
        CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at);
    """)

    # Create sqlite-vec vector table if extension is loaded
    try:
        db.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec "
            f"USING vec0(embedding float[{EMBEDDING_DIM}])"
        )
    except Exception:
        pass  # sqlite-vec not available

    db.commit()


# ---------------------------------------------------------------------------
# Embedding helpers
# ---------------------------------------------------------------------------


def _embed_and_store(db: sqlite3.Connection, memory_id: str, content: str) -> bool:
    """Generate embedding for content and store in vector table. Returns True on success."""
    embedder = _get_embedder()
    if embedder is None:
        return False
    try:
        rowid = db.execute(
            "SELECT rowid FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if rowid is None:
            return False
        vec_bytes = embedder.embed_one(content[:2000])  # truncate very long content for embedding
        # Delete existing vector if any (for updates)
        db.execute("DELETE FROM memories_vec WHERE rowid = ?", (rowid[0],))
        db.execute(
            "INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)",
            (rowid[0], vec_bytes),
        )
        db.commit()
        return True
    except Exception as e:
        log.debug("Failed to embed memory %s: %s", memory_id, e)
        return False


def _semantic_search(
    db: sqlite3.Connection, query: str, limit: int = 20
) -> list[dict]:
    """Search memories by semantic similarity. Returns list of dicts with 'distance' added."""
    embedder = _get_embedder()
    if embedder is None:
        return []
    try:
        query_bytes = embedder.embed_one(query)
        rows = db.execute(
            """SELECT m.*, mv.distance
               FROM memories_vec mv
               JOIN memories m ON m.rowid = mv.rowid
               WHERE mv.embedding MATCH ?
               ORDER BY mv.distance
               LIMIT ?""",
            (query_bytes, limit),
        ).fetchall()
        results = []
        for r in rows:
            d = _row_to_dict(r)
            d["semantic_distance"] = d.pop("distance", None)
            results.append(d)
        return results
    except Exception as e:
        log.debug("Semantic search failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _make_id(content: str) -> str:
    """Deterministic short id from content hash + timestamp."""
    ts = _now_iso()
    return hashlib.sha256(f"{content}{ts}".encode()).hexdigest()[:12]


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a sqlite3.Row to a plain dict, deserializing JSON fields."""
    d = dict(row)
    for key in ("tags", "metadata", "stats"):
        if key in d and isinstance(d[key], str):
            try:
                d[key] = json.loads(d[key])
            except json.JSONDecodeError:
                pass
    return d


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "_get_db",
    "_ensure_schema",
    "_embed_and_store",
    "_semantic_search",
    "_now_iso",
    "_make_id",
    "_row_to_dict",
]
