"""
remind_me_mcp.db — Database connection, schema management, and helpers.

All SQLite access goes through this module. The schema is created on first
connection (via _ensure_schema). Vector embeddings are stored in a separate
virtual table (memories_vec) when the sqlite-vec extension is available.

Schema versioning uses PRAGMA user_version. Migrations are applied
incrementally by _migrate_schema(), which is called at the end of
_ensure_schema(). Each migration is idempotent and guarded by a version check.

Current schema versions:
  0 -> 1: Add capture_id column + index on memories table
  1 -> 2: Add memory_tags junction table, indexes, and sync triggers
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

    # Apply incremental migrations to evolve the schema safely.
    _migrate_schema(db)


# ---------------------------------------------------------------------------
# Schema migrations
# ---------------------------------------------------------------------------

# Current target schema version.  Increment when adding a new migration step.
_SCHEMA_VERSION = 2


def _migrate_schema(db: sqlite3.Connection) -> None:
    """Apply incremental schema migrations using PRAGMA user_version.

    Each migration step is guarded by a version check so that re-running this
    function on an already-migrated database is a safe no-op (idempotent).

    Migration history:
      v0 -> v1: capture_id TEXT column + index on memories; backfill from metadata JSON.
      v1 -> v2: memory_tags junction table, tag/memory indexes, sync triggers; backfill
                from existing JSON tags column.

    Args:
        db: An open SQLite connection with row_factory=sqlite3.Row set.
    """
    current_version: int = db.execute("PRAGMA user_version").fetchone()[0]

    if current_version < 1:
        _migrate_v0_to_v1(db)
        db.execute("PRAGMA user_version = 1")
        current_version = 1

    if current_version < 2:
        _migrate_v1_to_v2(db)
        db.execute("PRAGMA user_version = 2")
        current_version = 2

    db.commit()


def _migrate_v0_to_v1(db: sqlite3.Connection) -> None:
    """v0 -> v1: Add capture_id column and index; backfill from metadata JSON.

    Uses ADD COLUMN in a try/except to handle the case where the column
    already exists (making the operation idempotent).

    Args:
        db: An open SQLite connection.
    """
    # Add column — safe to re-run; SQLite raises OperationalError if it exists.
    try:
        db.execute("ALTER TABLE memories ADD COLUMN capture_id TEXT DEFAULT NULL")
    except sqlite3.OperationalError:
        pass  # Column already exists — skip silently.

    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_capture_id ON memories(capture_id)"
    )

    # Backfill capture_id from existing metadata JSON where the field is present.
    db.execute(
        """
        UPDATE memories
           SET capture_id = json_extract(metadata, '$.capture_id')
         WHERE json_extract(metadata, '$.capture_id') IS NOT NULL
           AND capture_id IS NULL
        """
    )


def _migrate_v1_to_v2(db: sqlite3.Connection) -> None:
    """v1 -> v2: Add memory_tags junction table, indexes, sync triggers; backfill.

    Creates the memory_tags table that maps memory IDs to individual tag strings,
    enabling efficient SQL-level tag filtering without JSON parsing. Three triggers
    keep the junction table in sync with the JSON tags column on INSERT, UPDATE,
    and DELETE.

    Args:
        db: An open SQLite connection.
    """
    db.executescript("""
        CREATE TABLE IF NOT EXISTS memory_tags (
            memory_id  TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            tag        TEXT NOT NULL,
            PRIMARY KEY (memory_id, tag)
        );

        CREATE INDEX IF NOT EXISTS idx_memory_tags_tag ON memory_tags(tag);

        -- Keep memory_tags in sync when a memory is inserted.
        -- json_valid() guard ensures malformed tags strings do not raise errors.
        CREATE TRIGGER IF NOT EXISTS memories_tags_ai AFTER INSERT ON memories
        BEGIN
            INSERT OR IGNORE INTO memory_tags (memory_id, tag)
            SELECT NEW.id, je.value
              FROM json_each(NEW.tags) AS je
             WHERE typeof(je.value) = 'text'
               AND json_valid(NEW.tags);
        END;

        -- Keep memory_tags in sync when the tags column is updated.
        CREATE TRIGGER IF NOT EXISTS memories_tags_au AFTER UPDATE OF tags ON memories
        BEGIN
            DELETE FROM memory_tags WHERE memory_id = OLD.id;
            INSERT OR IGNORE INTO memory_tags (memory_id, tag)
            SELECT NEW.id, je.value
              FROM json_each(NEW.tags) AS je
             WHERE typeof(je.value) = 'text'
               AND json_valid(NEW.tags);
        END;

        -- Remove junction rows when a memory is deleted.
        CREATE TRIGGER IF NOT EXISTS memories_tags_ad AFTER DELETE ON memories
        BEGIN
            DELETE FROM memory_tags WHERE memory_id = OLD.id;
        END;
    """)

    # Backfill from existing JSON tags column for memories that pre-date the trigger.
    rows = db.execute(
        "SELECT id, tags FROM memories WHERE tags IS NOT NULL AND tags != '[]'"
    ).fetchall()
    for row in rows:
        raw_tags = row["tags"]
        try:
            tags = json.loads(raw_tags) if isinstance(raw_tags, str) else raw_tags
        except (json.JSONDecodeError, TypeError):
            continue
        for tag in tags:
            if isinstance(tag, str):
                db.execute(
                    "INSERT OR IGNORE INTO memory_tags (memory_id, tag) VALUES (?, ?)",
                    (row["id"], tag),
                )


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
    "_migrate_schema",
    "_embed_and_store",
    "_semantic_search",
    "_now_iso",
    "_make_id",
    "_row_to_dict",
]
