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

import contextlib
import hashlib
import json
import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any

from remind_me_mcp.config import DB_PATH, EMBEDDING_DIM
from remind_me_mcp.embeddings import _get_embedder

log = logging.getLogger("remind_me_mcp.db")

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

_db_connection: sqlite3.Connection | None = None


def _get_db() -> sqlite3.Connection:
    """Return the lazily initialized SQLite database singleton.

    The connection is configured with WAL journal mode for concurrent reader
    access, busy_timeout for graceful lock contention, and
    check_same_thread=False so it can be used from asyncio.to_thread workers.
    """
    global _db_connection
    if _db_connection is not None:
        return _db_connection

    db = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA foreign_keys=ON")

    # Load sqlite-vec extension if available
    try:
        import sqlite_vec
        db.enable_load_extension(True)
        try:
            sqlite_vec.load(db)
        except sqlite3.OperationalError as e:
            log.debug("sqlite-vec extension load failed: %s (vector search disabled)", e)
        db.enable_load_extension(False)
    except ImportError as e:
        log.debug("sqlite-vec not installed: %s (vector search disabled)", e)

    _ensure_schema(db)
    _db_connection = db
    return db


def _close_db() -> None:
    """Close the singleton database connection and reset it to None.

    Safe to call if the connection has not been opened yet (no-op).
    After this call, the next _get_db() invocation will open a fresh
    connection. Used during application shutdown and in tests to reset state.
    """
    global _db_connection
    if _db_connection is not None:
        _db_connection.close()
        _db_connection = None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def _ensure_schema(db: sqlite3.Connection) -> None:
    """Create tables, FTS virtual table, triggers, and indexes if absent.

    Idempotent — safe to call on an existing database. Creates the memories
    and chat_imports base tables, the FTS5 virtual table and its sync
    triggers, the memories_vec vector table (if sqlite-vec is loaded), and
    all required indexes. Calls _migrate_schema() at the end to apply any
    pending incremental migrations.

    Args:
        db: An open SQLite connection to configure.
    """
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
    except sqlite3.OperationalError as e:
        log.debug("sqlite-vec virtual table not available: %s", e)

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
    with contextlib.suppress(sqlite3.OperationalError):
        db.execute("ALTER TABLE memories ADD COLUMN capture_id TEXT DEFAULT NULL")

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
    """Generate embedding for content and store in the vector table.

    Looks up the memory's rowid, generates a float32 embedding vector via the
    ONNX engine, and upserts it into memories_vec. If the embedder is
    unavailable or the vector table is missing, returns False silently.

    Args:
        db: An open SQLite connection with the memories_vec virtual table.
        memory_id: The text primary key of the memory to embed.
        content: The text content to embed (truncated to 2000 chars).

    Returns:
        True if the embedding was stored successfully, False otherwise.
    """
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
    except sqlite3.OperationalError as e:
        log.warning("Database error storing embedding for %s: %s", memory_id, e)
        return False
    except (ValueError, TypeError) as e:
        log.warning("Embedding computation failed for %s: %s", memory_id, e)
        return False


def _semantic_search(
    db: sqlite3.Connection, query: str, limit: int = 20
) -> list[dict]:
    """Search memories by semantic similarity using the vector index.

    Embeds the query text and performs an approximate nearest-neighbour
    search against the memories_vec virtual table. Results include a
    'semantic_distance' key (lower = more similar). Returns an empty list
    if the embedder is unavailable or the vector table does not exist.

    Args:
        db: An open SQLite connection with the memories_vec virtual table.
        query: The search query text to embed and compare.
        limit: Maximum number of results to return.

    Returns:
        List of memory dicts (from _row_to_dict) with an added
        'semantic_distance' float field, sorted by distance ascending.
    """
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
               AND mv.k = ?
               ORDER BY mv.distance""",
            (query_bytes, limit),
        ).fetchall()
        results = []
        for r in rows:
            d = _row_to_dict(r)
            d["semantic_distance"] = d.pop("distance", None)
            results.append(d)
        return results
    except sqlite3.OperationalError as e:
        log.warning("Database error during semantic search: %s", e)
        return []
    except (ValueError, TypeError) as e:
        log.warning("Embedding error during semantic search: %s", e)
        return []


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string.

    Returns:
        ISO 8601 formatted datetime string with timezone offset, e.g.
        '2024-01-15T12:34:56.789012+00:00'.
    """
    return datetime.now(UTC).isoformat()


def _make_id(content: str) -> str:
    """Generate a unique short ID from content and current timestamp.

    NOT deterministic: calling with the same content at different times
    produces different IDs. This is intentional — it allows storing
    the same content multiple times (e.g., from different imports).

    For truly content-deterministic IDs, use a bare content hash instead.

    Args:
        content: The text content to incorporate into the hash.

    Returns:
        A 12-character hex string derived from SHA-256 of content + timestamp.
    """
    ts = _now_iso()
    return hashlib.sha256(f"{content}{ts}".encode()).hexdigest()[:12]


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a sqlite3.Row to a plain dict, deserializing JSON fields.

    Parses the 'tags', 'metadata', and 'stats' columns from their JSON string
    representations into Python objects. Malformed JSON fields are left as-is
    (logged at DEBUG level).

    Args:
        row: A sqlite3.Row from the memories or chat_imports table.

    Returns:
        A plain dict with all columns from the row, with JSON string fields
        parsed into their corresponding Python types.
    """
    d = dict(row)
    for key in ("tags", "metadata", "stats"):
        if key in d and isinstance(d[key], str):
            try:
                d[key] = json.loads(d[key])
            except json.JSONDecodeError:
                log.debug("Malformed JSON in field %s: %r", key, d[key])
    return d


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "_get_db",
    "_close_db",
    "_ensure_schema",
    "_migrate_schema",
    "_embed_and_store",
    "_semantic_search",
    "_now_iso",
    "_make_id",
    "_row_to_dict",
]
