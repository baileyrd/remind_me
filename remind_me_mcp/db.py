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
  4 -> 5: Add decay, vitality, and classification columns
  5 -> 6: Add source_capture_id column for atomic decomposition
  6 -> 7: Add subject, predicate, object, superseded_by columns for structured memory
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime
from typing import Any

from remind_me_mcp.config import DB_PATH, EMBEDDING_DIM
from remind_me_mcp.embeddings import _get_embedder

log = logging.getLogger("remind_me_mcp.db")

# ---------------------------------------------------------------------------
# Connection — per-thread with registry for shutdown cleanup
# ---------------------------------------------------------------------------

_local = threading.local()
_all_connections: list[sqlite3.Connection] = []
_connections_lock = threading.Lock()
_schema_ready = False


def _get_db() -> sqlite3.Connection:
    """Return a per-thread SQLite connection, creating one if needed.

    Each thread gets its own connection configured with WAL journal mode for
    concurrent access, busy_timeout for graceful lock contention, and foreign
    key enforcement. The sqlite-vec extension is loaded per-connection when
    available. Schema initialisation runs once (guarded by a lock) on the
    first connection created.

    All connections are tracked in ``_all_connections`` so ``_close_db()``
    can shut them down at application exit.
    """
    global _schema_ready

    conn = getattr(_local, "connection", None)
    if conn is not None:
        return conn

    db = sqlite3.connect(str(DB_PATH), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA foreign_keys=ON")

    # Load sqlite-vec extension if available (per-connection)
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

    # Run schema init/migration once across all threads
    with _connections_lock:
        if not _schema_ready:
            _ensure_schema(db)
            _schema_ready = True
        _all_connections.append(db)

    _local.connection = db
    return db


def _close_db() -> None:
    """Close all per-thread database connections and reset state.

    Safe to call even if no connections have been opened (no-op).
    After this call, the next ``_get_db()`` invocation on any thread will
    open a fresh connection. Used during application shutdown and in tests
    to reset state.
    """
    global _schema_ready
    with _connections_lock:
        for conn in _all_connections:
            try:
                conn.close()
            except Exception:
                pass
        _all_connections.clear()
        _schema_ready = False
    # Clear the calling thread's local reference
    _local.connection = None


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
_SCHEMA_VERSION = 7



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
    
    if current_version < 3:
        _migrate_v2_to_v3(db)
        db.execute("PRAGMA user_version = 3")
        current_version = 3
    
    if current_version < 4:
        _migrate_v3_to_v4(db)
        db.execute("PRAGMA user_version = 4")
        current_version = 4

    if current_version < 5:
        _migrate_v4_to_v5(db)
        db.execute("PRAGMA user_version = 5")
        current_version = 5

    if current_version < 6:
        _migrate_v5_to_v6(db)
        db.execute("PRAGMA user_version = 6")
        current_version = 6

    if current_version < 7:
        _migrate_v6_to_v7(db)
        db.execute("PRAGMA user_version = 7")
        current_version = 7

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


def _migrate_v2_to_v3(db: sqlite3.Connection) -> None:
    """v2 -> v3: Add node_id, sync_log, sync_outbox, outbox triggers."""

    with contextlib.suppress(sqlite3.OperationalError):
        db.execute("ALTER TABLE memories ADD COLUMN node_id TEXT DEFAULT NULL")

    db.executescript("""
        CREATE TABLE IF NOT EXISTS sync_log (
            remote_id   TEXT NOT NULL,
            last_pull   TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00',
            last_push   TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00',
            PRIMARY KEY (remote_id)
        );

        CREATE TABLE IF NOT EXISTS sync_outbox (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id   TEXT NOT NULL,
            operation   TEXT NOT NULL,
            payload     TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            sent_at     TEXT DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_outbox_unsent
            ON sync_outbox(sent_at) WHERE sent_at = '';

        CREATE INDEX IF NOT EXISTS idx_outbox_memory_id
            ON sync_outbox(memory_id);

        CREATE TRIGGER IF NOT EXISTS memories_outbox_ai
        AFTER INSERT ON memories BEGIN
            INSERT INTO sync_outbox (memory_id, operation, payload, created_at)
            VALUES (
                NEW.id, 'insert',
                json_object(
                    'id',         NEW.id,
                    'content',    NEW.content,
                    'category',   NEW.category,
                    'tags',       NEW.tags,
                    'source',     NEW.source,
                    'metadata',   NEW.metadata,
                    'created_at', NEW.created_at,
                    'updated_at', NEW.updated_at,
                    'capture_id', NEW.capture_id,
                    'node_id',    NEW.node_id
                ),
                datetime('now', 'utc')
            );
        END;

        CREATE TRIGGER IF NOT EXISTS memories_outbox_au
        AFTER UPDATE ON memories BEGIN
            INSERT INTO sync_outbox (memory_id, operation, payload, created_at)
            VALUES (
                NEW.id, 'update',
                json_object(
                    'id',         NEW.id,
                    'content',    NEW.content,
                    'category',   NEW.category,
                    'tags',       NEW.tags,
                    'source',     NEW.source,
                    'metadata',   NEW.metadata,
                    'created_at', NEW.created_at,
                    'updated_at', NEW.updated_at,
                    'capture_id', NEW.capture_id,
                    'node_id',    NEW.node_id
                ),
                datetime('now', 'utc')
            );
        END;
    """)

    # Backfill existing memories into outbox so first sync pushes everything
    db.execute("""
        INSERT INTO sync_outbox (memory_id, operation, payload, created_at)
        SELECT
            id, 'insert',
            json_object(
                'id',         id,
                'content',    content,
                'category',   category,
                'tags',       tags,
                'source',     source,
                'metadata',   metadata,
                'created_at', created_at,
                'updated_at', updated_at,
                'capture_id', capture_id,
                'node_id',    node_id
            ),
            datetime('now', 'utc')
        FROM memories
    """)


def _migrate_v3_to_v4(db: sqlite3.Connection) -> None:
    """v3 -> v4: Add client column to memories; backfill existing records with 'unknown'.

    The client column identifies what tool created the memory:
    'claude-desktop', 'claude-code', 'unknown' (pre-existing records).

    Args:
        db: An open SQLite connection.
    """
    with contextlib.suppress(sqlite3.OperationalError):
        db.execute(
            "ALTER TABLE memories ADD COLUMN client TEXT NOT NULL DEFAULT 'unknown'"
        )

    db.execute("CREATE INDEX IF NOT EXISTS idx_memories_client ON memories(client)")

    # Backfill existing records
    db.execute("UPDATE memories SET client = 'unknown' WHERE client IS NULL OR client = ''")

    # Drop and recreate outbox triggers to include client field
    db.executescript("""
        DROP TRIGGER IF EXISTS memories_outbox_ai;
        DROP TRIGGER IF EXISTS memories_outbox_au;

        CREATE TRIGGER IF NOT EXISTS memories_outbox_ai
        AFTER INSERT ON memories BEGIN
            INSERT INTO sync_outbox (memory_id, operation, payload, created_at)
            VALUES (
                NEW.id, 'insert',
                json_object(
                    'id',         NEW.id,
                    'content',    NEW.content,
                    'category',   NEW.category,
                    'tags',       NEW.tags,
                    'source',     NEW.source,
                    'metadata',   NEW.metadata,
                    'created_at', NEW.created_at,
                    'updated_at', NEW.updated_at,
                    'capture_id', NEW.capture_id,
                    'node_id',    NEW.node_id,
                    'client',     NEW.client
                ),
                datetime('now', 'utc')
            );
        END;

        CREATE TRIGGER IF NOT EXISTS memories_outbox_au
        AFTER UPDATE ON memories BEGIN
            INSERT INTO sync_outbox (memory_id, operation, payload, created_at)
            VALUES (
                NEW.id, 'update',
                json_object(
                    'id',         NEW.id,
                    'content',    NEW.content,
                    'category',   NEW.category,
                    'tags',       NEW.tags,
                    'source',     NEW.source,
                    'metadata',   NEW.metadata,
                    'created_at', NEW.created_at,
                    'updated_at', NEW.updated_at,
                    'capture_id', NEW.capture_id,
                    'node_id',    NEW.node_id,
                    'client',     NEW.client
                ),
                datetime('now', 'utc')
            );
        END;
    """)


def _migrate_v4_to_v5(db: sqlite3.Connection) -> None:
    """v4 -> v5: Add decay, vitality, and classification columns to memories.

    Adds seven new columns for the ACT-R vitality model and memory classification:
      - accessed_at: timestamp of last access (backfilled from created_at)
      - access_count: number of times the memory has been accessed
      - decay_rate: per-memory decay rate (set by memory_type)
      - vitality: current vitality score (ACT-R formula output)
      - base_weight: base importance weight for vitality computation
      - status: 'active' or 'dormant' based on vitality threshold
      - memory_type: classification label (e.g., 'fact', 'decision', 'preference')

    Also creates indexes on status, memory_type, and vitality for efficient filtering,
    and updates outbox triggers to include all new fields in the JSON payload.

    Args:
        db: An open SQLite connection.
    """
    # Add new columns (idempotent via suppress)
    with contextlib.suppress(sqlite3.OperationalError):
        db.execute("ALTER TABLE memories ADD COLUMN accessed_at TEXT DEFAULT NULL")
    with contextlib.suppress(sqlite3.OperationalError):
        db.execute("ALTER TABLE memories ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0")
    with contextlib.suppress(sqlite3.OperationalError):
        db.execute("ALTER TABLE memories ADD COLUMN decay_rate REAL NOT NULL DEFAULT 0.1")
    with contextlib.suppress(sqlite3.OperationalError):
        db.execute("ALTER TABLE memories ADD COLUMN vitality REAL NOT NULL DEFAULT 1.0")
    with contextlib.suppress(sqlite3.OperationalError):
        db.execute("ALTER TABLE memories ADD COLUMN base_weight REAL NOT NULL DEFAULT 1.0")
    with contextlib.suppress(sqlite3.OperationalError):
        db.execute("ALTER TABLE memories ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
    with contextlib.suppress(sqlite3.OperationalError):
        db.execute("ALTER TABLE memories ADD COLUMN memory_type TEXT NOT NULL DEFAULT 'unclassified'")

    # Create indexes for efficient filtering
    db.execute("CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_memories_memory_type ON memories(memory_type)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_memories_vitality ON memories(vitality)")

    # Backfill accessed_at from created_at for existing records
    db.execute("UPDATE memories SET accessed_at = created_at WHERE accessed_at IS NULL")

    # Drop and recreate outbox triggers to include new fields
    db.executescript("""
        DROP TRIGGER IF EXISTS memories_outbox_ai;
        DROP TRIGGER IF EXISTS memories_outbox_au;

        CREATE TRIGGER IF NOT EXISTS memories_outbox_ai
        AFTER INSERT ON memories BEGIN
            INSERT INTO sync_outbox (memory_id, operation, payload, created_at)
            VALUES (
                NEW.id, 'insert',
                json_object(
                    'id',           NEW.id,
                    'content',      NEW.content,
                    'category',     NEW.category,
                    'tags',         NEW.tags,
                    'source',       NEW.source,
                    'metadata',     NEW.metadata,
                    'created_at',   NEW.created_at,
                    'updated_at',   NEW.updated_at,
                    'capture_id',   NEW.capture_id,
                    'node_id',      NEW.node_id,
                    'client',       NEW.client,
                    'accessed_at',  NEW.accessed_at,
                    'access_count', NEW.access_count,
                    'decay_rate',   NEW.decay_rate,
                    'vitality',     NEW.vitality,
                    'base_weight',  NEW.base_weight,
                    'status',       NEW.status,
                    'memory_type',  NEW.memory_type
                ),
                datetime('now', 'utc')
            );
        END;

        CREATE TRIGGER IF NOT EXISTS memories_outbox_au
        AFTER UPDATE ON memories BEGIN
            INSERT INTO sync_outbox (memory_id, operation, payload, created_at)
            VALUES (
                NEW.id, 'update',
                json_object(
                    'id',           NEW.id,
                    'content',      NEW.content,
                    'category',     NEW.category,
                    'tags',         NEW.tags,
                    'source',       NEW.source,
                    'metadata',     NEW.metadata,
                    'created_at',   NEW.created_at,
                    'updated_at',   NEW.updated_at,
                    'capture_id',   NEW.capture_id,
                    'node_id',      NEW.node_id,
                    'client',       NEW.client,
                    'accessed_at',  NEW.accessed_at,
                    'access_count', NEW.access_count,
                    'decay_rate',   NEW.decay_rate,
                    'vitality',     NEW.vitality,
                    'base_weight',  NEW.base_weight,
                    'status',       NEW.status,
                    'memory_type',  NEW.memory_type
                ),
                datetime('now', 'utc')
            );
        END;
    """)

def _migrate_v5_to_v6(db: sqlite3.Connection) -> None:
    """v5 -> v6: Add source_capture_id column for atomic decomposition linkage.

    Adds source_capture_id TEXT column linking decomposed facts back to
    their parent capture. Creates an index for efficient lookup of children
    by parent capture_id. Updates outbox triggers to include the new field.

    Args:
        db: An open SQLite connection.
    """
    with contextlib.suppress(sqlite3.OperationalError):
        db.execute(
            "ALTER TABLE memories ADD COLUMN source_capture_id TEXT DEFAULT NULL"
        )

    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_source_capture_id "
        "ON memories(source_capture_id)"
    )

    # Drop and recreate outbox triggers to include source_capture_id
    db.executescript("""
        DROP TRIGGER IF EXISTS memories_outbox_ai;
        DROP TRIGGER IF EXISTS memories_outbox_au;

        CREATE TRIGGER IF NOT EXISTS memories_outbox_ai
        AFTER INSERT ON memories BEGIN
            INSERT INTO sync_outbox (memory_id, operation, payload, created_at)
            VALUES (
                NEW.id, 'insert',
                json_object(
                    'id',                 NEW.id,
                    'content',            NEW.content,
                    'category',           NEW.category,
                    'tags',               NEW.tags,
                    'source',             NEW.source,
                    'metadata',           NEW.metadata,
                    'created_at',         NEW.created_at,
                    'updated_at',         NEW.updated_at,
                    'capture_id',         NEW.capture_id,
                    'node_id',            NEW.node_id,
                    'client',             NEW.client,
                    'accessed_at',        NEW.accessed_at,
                    'access_count',       NEW.access_count,
                    'decay_rate',         NEW.decay_rate,
                    'vitality',           NEW.vitality,
                    'base_weight',        NEW.base_weight,
                    'status',             NEW.status,
                    'memory_type',        NEW.memory_type,
                    'source_capture_id',  NEW.source_capture_id
                ),
                datetime('now', 'utc')
            );
        END;

        CREATE TRIGGER IF NOT EXISTS memories_outbox_au
        AFTER UPDATE ON memories BEGIN
            INSERT INTO sync_outbox (memory_id, operation, payload, created_at)
            VALUES (
                NEW.id, 'update',
                json_object(
                    'id',                 NEW.id,
                    'content',            NEW.content,
                    'category',           NEW.category,
                    'tags',               NEW.tags,
                    'source',             NEW.source,
                    'metadata',           NEW.metadata,
                    'created_at',         NEW.created_at,
                    'updated_at',         NEW.updated_at,
                    'capture_id',         NEW.capture_id,
                    'node_id',            NEW.node_id,
                    'client',             NEW.client,
                    'accessed_at',        NEW.accessed_at,
                    'access_count',       NEW.access_count,
                    'decay_rate',         NEW.decay_rate,
                    'vitality',           NEW.vitality,
                    'base_weight',        NEW.base_weight,
                    'status',             NEW.status,
                    'memory_type',        NEW.memory_type,
                    'source_capture_id',  NEW.source_capture_id
                ),
                datetime('now', 'utc')
            );
        END;
    """)


def _migrate_v6_to_v7(db: sqlite3.Connection) -> None:
    """v6 -> v7: Add subject/predicate/object/superseded_by columns for structured memory.

    Adds four nullable TEXT columns to support structured fact triples
    (subject, predicate, object) and fact supersession tracking (superseded_by).
    Creates an index on subject for fast structured lookups. Updates outbox
    triggers to include the new fields in JSON payloads.

    Args:
        db: An open SQLite connection.
    """
    with contextlib.suppress(sqlite3.OperationalError):
        db.execute("ALTER TABLE memories ADD COLUMN subject TEXT DEFAULT NULL")
    with contextlib.suppress(sqlite3.OperationalError):
        db.execute("ALTER TABLE memories ADD COLUMN predicate TEXT DEFAULT NULL")
    with contextlib.suppress(sqlite3.OperationalError):
        db.execute("ALTER TABLE memories ADD COLUMN object TEXT DEFAULT NULL")
    with contextlib.suppress(sqlite3.OperationalError):
        db.execute("ALTER TABLE memories ADD COLUMN superseded_by TEXT DEFAULT NULL")

    db.execute("CREATE INDEX IF NOT EXISTS idx_memories_subject ON memories(subject)")

    # Drop and recreate outbox triggers to include new columns
    db.executescript("""
        DROP TRIGGER IF EXISTS memories_outbox_ai;
        DROP TRIGGER IF EXISTS memories_outbox_au;

        CREATE TRIGGER IF NOT EXISTS memories_outbox_ai
        AFTER INSERT ON memories BEGIN
            INSERT INTO sync_outbox (memory_id, operation, payload, created_at)
            VALUES (
                NEW.id, 'insert',
                json_object(
                    'id',                 NEW.id,
                    'content',            NEW.content,
                    'category',           NEW.category,
                    'tags',               NEW.tags,
                    'source',             NEW.source,
                    'metadata',           NEW.metadata,
                    'created_at',         NEW.created_at,
                    'updated_at',         NEW.updated_at,
                    'capture_id',         NEW.capture_id,
                    'node_id',            NEW.node_id,
                    'client',             NEW.client,
                    'accessed_at',        NEW.accessed_at,
                    'access_count',       NEW.access_count,
                    'decay_rate',         NEW.decay_rate,
                    'vitality',           NEW.vitality,
                    'base_weight',        NEW.base_weight,
                    'status',             NEW.status,
                    'memory_type',        NEW.memory_type,
                    'source_capture_id',  NEW.source_capture_id,
                    'subject',            NEW.subject,
                    'predicate',          NEW.predicate,
                    'object',             NEW.object,
                    'superseded_by',      NEW.superseded_by
                ),
                datetime('now', 'utc')
            );
        END;

        CREATE TRIGGER IF NOT EXISTS memories_outbox_au
        AFTER UPDATE ON memories BEGIN
            INSERT INTO sync_outbox (memory_id, operation, payload, created_at)
            VALUES (
                NEW.id, 'update',
                json_object(
                    'id',                 NEW.id,
                    'content',            NEW.content,
                    'category',           NEW.category,
                    'tags',               NEW.tags,
                    'source',             NEW.source,
                    'metadata',           NEW.metadata,
                    'created_at',         NEW.created_at,
                    'updated_at',         NEW.updated_at,
                    'capture_id',         NEW.capture_id,
                    'node_id',            NEW.node_id,
                    'client',             NEW.client,
                    'accessed_at',        NEW.accessed_at,
                    'access_count',       NEW.access_count,
                    'decay_rate',         NEW.decay_rate,
                    'vitality',           NEW.vitality,
                    'base_weight',        NEW.base_weight,
                    'status',             NEW.status,
                    'memory_type',        NEW.memory_type,
                    'source_capture_id',  NEW.source_capture_id,
                    'subject',            NEW.subject,
                    'predicate',          NEW.predicate,
                    'object',             NEW.object,
                    'superseded_by',      NEW.superseded_by
                ),
                datetime('now', 'utc')
            );
        END;
    """)


# ---------------------------------------------------------------------------
# Embedding helpers
# ---------------------------------------------------------------------------


def _embed_and_store(memory_id: str, content: str) -> bool:
    """Generate embedding for content and store in the vector table.

    Acquires its own per-thread database connection via ``_get_db()``, looks
    up the memory's rowid, generates a float32 embedding vector via the ONNX
    engine, and upserts it into memories_vec. If the embedder is unavailable
    or the vector table is missing, returns False silently.

    Args:
        memory_id: The text primary key of the memory to embed.
        content: The text content to embed (truncated to 2000 chars).

    Returns:
        True if the embedding was stored successfully, False otherwise.
    """
    embedder = _get_embedder()
    if embedder is None:
        return False
    try:
        db = _get_db()
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
    except sqlite3.DatabaseError as e:
        log.warning("Database error storing embedding for %s: %s", memory_id, e)
        return False
    except (ValueError, TypeError) as e:
        log.warning("Embedding computation failed for %s: %s", memory_id, e)
        return False


def _semantic_search(query: str, limit: int = 20) -> list[dict]:
    """Search memories by semantic similarity using the vector index.

    Acquires its own per-thread database connection via ``_get_db()``, embeds
    the query text, and performs an approximate nearest-neighbour search
    against the memories_vec virtual table. Results include a
    'semantic_distance' key (lower = more similar). Returns an empty list
    if the embedder is unavailable or the vector table does not exist.

    Args:
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
        db = _get_db()
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
