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
  7 -> 8: Add vec_chunks map for multi-vector (sliding-window) embeddings
  8 -> 9: Sync hardening — per-remote send tracking (sync_sends), keyset pull
          cursor (sync_log.last_pull_id), sync_flags gate so outbox triggers
          only fire when sync is enabled, canonical ISO-8601 UTC timestamps in
          the outbox triggers, and an index on memories(updated_at)
  9 -> 10: FT-04 entity graph — entities + memory_entities tables with
           deterministic entity ids (sha256 of the normalized canonical name)
           and outbox triggers so the graph syncs between peers
  10 -> 11: FT-08 LLM Wiki — wiki_pages (+ external-content wiki_fts),
            wiki_links (backlink graph), and wiki_meta (compile watermark)
            tables. The wiki's source of truth is markdown files on disk; these
            tables are a search/index cache only, so there are deliberately NO
            sync outbox triggers (the file layer is what would be synced).
  11 -> 12: MemPalace importer — mempalace_imports dedup table (drawer_id ->
            memory_id), mirroring chat_imports for the file-based importer.
  14 -> 15: dbs importer — dbs_imports dedup/update-tracking table, keyed by
            (dbs_source, external_id) rather than a single id column since
            that's dbs's own item identity; also stores content_hash so a
            rerun can tell an edited item from an unchanged one.
  15 -> 16: deleted_at tombstone column (gap #11) so deletion propagates over
            sync — a soft-delete UPDATE rides the existing memories_outbox_au
            trigger instead of a hard DELETE producing no outbox row at all.
  17 -> 18: embedding_meta table (issue #18) recording which embedding
            model/dimension/backend the stored vectors were actually computed
            with, so a changed EMBEDDING_MODEL/EMBEDDING_DIM/EMBEDDING_BACKEND
            can be detected at startup and stale vectors cleared instead of
            silently serving garbage nearest-neighbor results.
  18 -> 19: memory_associations table (issue #9) for co-retrieval
            reinforcement -- a bounded, undecayed weight per memory pair
            that appeared together in a search result set, surfaced only as
            an opt-in expand_co_retrieval search section (never feeding
            back into ranking).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from remind_me_mcp import ann_index
from remind_me_mcp.config import (
    ANN_MIN_CHUNKS,
    DB_PATH,
    EMBED_BATCH_SIZE,
    EMBEDDING_BACKEND,
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
)
from remind_me_mcp.embeddings import _get_embedder, chunk_text

# Over-fetch factor for chunked KNN: a single memory may own several chunk
# vectors, so we ask sqlite-vec for limit * this many chunk hits and then dedupe
# to distinct parent memories before truncating to the caller's limit.
_CHUNK_KNN_FANOUT = 4

log = logging.getLogger("remind_me_mcp.db")

# ---------------------------------------------------------------------------
# Connection — per-thread with registry for shutdown cleanup
# ---------------------------------------------------------------------------

_local = threading.local()
_all_connections: list[sqlite3.Connection] = []
_connections_lock = threading.Lock()
_schema_ready = False
# Incremented by _close_db() so threads holding a reference to a closed
# connection in their threading.local detect staleness and reconnect (SE-07).
_db_generation = 0


def _get_db() -> sqlite3.Connection:
    """Return a per-thread SQLite connection, creating one if needed.

    Each thread gets its own connection configured with WAL journal mode for
    concurrent access, busy_timeout for graceful lock contention, and foreign
    key enforcement. The sqlite-vec extension is loaded per-connection when
    available. Schema initialisation runs once (guarded by a lock) on the
    first connection created.

    Connections are created with ``check_same_thread=False`` so the lifespan
    thread can actually close every tracked connection at shutdown (SE-07);
    thread isolation is still provided by the per-thread ``threading.local``
    registry. All connections are tracked in ``_all_connections`` so
    ``_close_db()`` can shut them down at application exit.
    """
    global _schema_ready

    conn = getattr(_local, "connection", None)
    if conn is not None and getattr(_local, "generation", None) == _db_generation:
        return conn

    db = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
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
    _local.generation = _db_generation
    return db


def vec_search_available() -> bool:
    """True when ``memories_vec`` actually exists and is queryable.

    Deliberately distinct from "the ``sqlite-vec`` package is installed" or
    "the ONNX embedder loaded" (:func:`remind_me_mcp.embeddings._get_embedder`)
    -- the native ``sqlite-vec`` extension can fail to load via
    ``enable_load_extension``/``sqlite_vec.load`` even when the Python
    package imports fine (e.g. a ``sqlite3`` build without loadable-extension
    support), in which case ``memories_vec`` is never created and semantic
    search has nothing to query even though an embedding could still be
    computed. Callers deciding whether to route/weight semantic results
    (:func:`remind_me_mcp.retrieval.choose_rrf_weights`'s ``has_semantic``)
    need this, not just embedder availability.
    """
    db = _get_db()
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories_vec'"
    ).fetchone()
    return row is not None


def _close_db() -> None:
    """Close all tracked database connections (any thread's) and reset state.

    Safe to call even if no connections have been opened (no-op).
    Connections are created with ``check_same_thread=False`` (SE-07), so the
    calling thread can genuinely close every tracked connection — releasing
    file descriptors and letting SQLite checkpoint the WAL on the last close.
    The generation counter is bumped so threads still holding a reference to
    a closed connection in their ``threading.local`` reconnect on the next
    ``_get_db()`` call.
    """
    global _schema_ready, _db_generation
    with _connections_lock:
        for conn in _all_connections:
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover — defensive; close is expected to succeed
                log.warning("Failed to close a tracked DB connection", exc_info=True)
        _all_connections.clear()
        _schema_ready = False
        _db_generation += 1
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
_SCHEMA_VERSION = 19



def _maybe_snapshot_before_migration(db: sqlite3.Connection, current_version: int) -> None:
    """Snapshot the DB file before running a pending migration (issue #17).

    Skipped for a brand-new, empty database (nothing to protect) -- detected
    by checking whether the memories table already has any rows. A failed or
    semantically-wrong migration then has a restorable pre-migration copy
    instead of no safety net at all. Snapshot failure is logged and swallowed
    rather than raised: it must never block startup or the migration itself.

    Args:
        db: An open SQLite connection.
        current_version: The schema version read before migrations run, used
            to label the snapshot with what it's a backup *of*.
    """
    try:
        has_data = bool(
            db.execute("SELECT EXISTS(SELECT 1 FROM memories LIMIT 1)").fetchone()[0]
        )
    except sqlite3.OperationalError:
        has_data = False  # memories table doesn't exist yet -- truly fresh db

    if not has_data:
        return

    try:
        from remind_me_mcp.backup import create_backup

        path = create_backup(db, label=f"pre-migration-v{current_version}")
        log.info("Pre-migration snapshot created: %s", path)
    except OSError as e:
        log.warning("Pre-migration snapshot failed (continuing without one): %s", e)


def _migrate_schema(db: sqlite3.Connection) -> None:
    """Apply incremental schema migrations using PRAGMA user_version.

    Each migration step is guarded by a version check so that re-running this
    function on an already-migrated database is a safe no-op (idempotent). A
    snapshot of the DB file is taken before any pending migration runs (issue
    #17), so a failed or buggy migration can be rolled back by restoring it.

    Migration history:
      v0 -> v1: capture_id TEXT column + index on memories; backfill from metadata JSON.
      v1 -> v2: memory_tags junction table, tag/memory indexes, sync triggers; backfill
                from existing JSON tags column.

    Args:
        db: An open SQLite connection with row_factory=sqlite3.Row set.
    """
    current_version: int = db.execute("PRAGMA user_version").fetchone()[0]

    if current_version < _SCHEMA_VERSION:
        _maybe_snapshot_before_migration(db, current_version)

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

    if current_version < 8:
        _migrate_v7_to_v8(db)
        db.execute("PRAGMA user_version = 8")
        current_version = 8

    if current_version < 9:
        _migrate_v8_to_v9(db)
        db.execute("PRAGMA user_version = 9")
        current_version = 9

    if current_version < 10:
        _migrate_v9_to_v10(db)
        db.execute("PRAGMA user_version = 10")
        current_version = 10

    if current_version < 11:
        _migrate_v10_to_v11(db)
        db.execute("PRAGMA user_version = 11")
        current_version = 11

    if current_version < 12:
        _migrate_v11_to_v12(db)
        db.execute("PRAGMA user_version = 12")
        current_version = 12

    if current_version < 13:
        _migrate_v12_to_v13(db)
        db.execute("PRAGMA user_version = 13")
        current_version = 13

    if current_version < 14:
        _migrate_v13_to_v14(db)
        db.execute("PRAGMA user_version = 14")
        current_version = 14

    if current_version < 15:
        _migrate_v14_to_v15(db)
        db.execute("PRAGMA user_version = 15")
        current_version = 15

    if current_version < 16:
        _migrate_v15_to_v16(db)
        db.execute("PRAGMA user_version = 16")
        current_version = 16

    if current_version < 17:
        _migrate_v16_to_v17(db)
        db.execute("PRAGMA user_version = 17")
        current_version = 17

    if current_version < 18:
        _migrate_v17_to_v18(db)
        db.execute("PRAGMA user_version = 18")
        current_version = 18

    if current_version < 19:
        _migrate_v18_to_v19(db)
        db.execute("PRAGMA user_version = 19")
        current_version = 19

    db.commit()

    # Align the sync_flags gate (and outbox contents) with the current
    # SYNC_ENABLED configuration on every startup.
    _reconcile_sync_enabled_flag(db)

    # Detect an embedding-model/dimension change and clear now-invalid
    # vectors (issue #18) on every startup.
    _reconcile_embedding_meta(db)


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


def _migrate_v7_to_v8(db: sqlite3.Connection) -> None:
    """v7 -> v8: Add the vec_chunks map for multi-vector (chunked) embeddings.

    Previously each memory stored exactly one vector in ``memories_vec`` keyed by
    the memory's own rowid (1:1). To embed long content as several overlapping
    sliding windows, ``memories_vec`` now holds one row per *chunk* (its own
    auto-assigned rowid) and ``vec_chunks`` maps each chunk vector back to its
    parent memory. Existing single vectors are backfilled as ``chunk_ix = 0`` so
    they keep working unchanged until the memory is re-embedded (or reindexed).

    Args:
        db: An open SQLite connection.
    """
    db.execute(
        """CREATE TABLE IF NOT EXISTS vec_chunks (
               vec_rowid    INTEGER PRIMARY KEY,
               memory_rowid INTEGER NOT NULL,
               chunk_ix     INTEGER NOT NULL
           )"""
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_vec_chunks_memory ON vec_chunks(memory_rowid)"
    )
    # Backfill existing 1:1 vectors (vec rowid == memory rowid) as chunk 0. Guard
    # for deployments where sqlite-vec isn't loaded (memories_vec absent).
    with contextlib.suppress(sqlite3.OperationalError):
        db.execute(
            """INSERT OR IGNORE INTO vec_chunks(vec_rowid, memory_rowid, chunk_ix)
               SELECT rowid, rowid, 0 FROM memories_vec"""
        )


# All memory columns mirrored into sync_outbox payloads. Single source of
# truth for the v9 triggers and the enable-time backfill.
_OUTBOX_PAYLOAD_COLUMNS = (
    "id", "content", "category", "tags", "source", "metadata",
    "created_at", "updated_at", "capture_id", "node_id", "client",
    "accessed_at", "access_count", "decay_rate", "vitality", "base_weight",
    "status", "memory_type", "source_capture_id",
    "subject", "predicate", "object", "superseded_by",
    "doc_id", "chunk_index", "deleted_at",
)

# Entity columns mirrored into sync_outbox payloads (FT-04). Memory records
# carry no record_type (older peers must keep accepting them unchanged);
# entity and link payloads are tagged with record_type so receivers dispatch.
_ENTITY_OUTBOX_COLUMNS = (
    "id", "name", "kind", "aliases", "created_at", "updated_at", "node_id",
)

# entity_relations columns mirrored into sync_outbox payloads. Relations are
# immutable (insert-or-ignore, like memory_entities links) but -- unlike
# links -- already carry a real deterministic id, so no synthetic wire id is
# needed.
_ENTITY_RELATION_OUTBOX_COLUMNS = (
    "id", "subject_entity_id", "relation", "object_entity_id",
    "created_at", "updated_at", "node_id",
)

# Canonical UTC ISO-8601 timestamp in SQL, string-comparable with Python's
# datetime.now(UTC).isoformat(). Replaces the previous datetime('now','utc'),
# which is documented-incorrect SQLite usage ('now' is already UTC) and
# produced a different, non-ISO format ('YYYY-MM-DD HH:MM:SS').
_SQL_NOW_ISO = "strftime('%Y-%m-%dT%H:%M:%f000', 'now') || '+00:00'"


def _outbox_payload_sql(
    prefix: str,
    columns: tuple[str, ...] = _OUTBOX_PAYLOAD_COLUMNS,
    record_type: str | None = None,
) -> str:
    """Build the json_object(...) expression for an outbox payload.

    Args:
        prefix: Column reference prefix, e.g. ``"NEW."`` inside a trigger or
            ``""`` for a plain SELECT backfill.
        columns: The columns to mirror into the payload (HY-03: a single
            column list generates both the triggers and the backfill SQL).
        record_type: Optional record-kind discriminator added to the payload
            (``'entity'`` / ``'memory_entity'``). Memory payloads omit it so
            the wire format pre-FT-04 peers expect is unchanged.
    """
    pairs = ", ".join(f"'{col}', {prefix}{col}" for col in columns)
    if record_type is not None:
        pairs = f"'record_type', '{record_type}', " + pairs
    return f"json_object({pairs})"


# Link payload: the synthetic 'id' (memory_id|entity_id) lets the push
# protocol's processed_ids matching mark link rows sent, like any record.
_LINK_PAYLOAD_SQL = (
    "json_object("
    "'record_type', 'memory_entity', "
    "'id', {p}memory_id || '|' || {p}entity_id, "
    "'memory_id', {p}memory_id, "
    "'entity_id', {p}entity_id, "
    "'created_at', {p}created_at)"
)


def _migrate_v8_to_v9(db: sqlite3.Connection) -> None:
    """v8 -> v9: Sync hardening support tables, triggers, and indexes.

    - ``sync_sends(remote_id, outbox_id, sent_at)``: per-remote outbox send
      tracking, so every configured hub/peer receives every row (SY-02).
    - ``sync_log.last_pull_id``: id half of the keyset pull cursor
      ``(updated_at, id)`` so page-boundary timestamp ties are never lost (SY-04).
    - ``sync_flags``: small key/value table; the outbox triggers are gated on
      ``sync_enabled = '1'`` so the outbox does not accumulate when sync is
      disabled (SY-07). The flag is reconciled with config at every startup by
      :func:`_reconcile_sync_enabled_flag`.
    - Outbox triggers recreated with a canonical ISO-8601 UTC ``created_at``
      instead of the incorrect ``datetime('now','utc')`` (SY-08); legacy
      outbox timestamps are normalized in place.
    - ``idx_memories_updated_at`` so peer pull pagination does not scan the
      whole table (SY-09), plus an index on ``sync_outbox(created_at)`` for
      retention pruning (SY-07).

    Args:
        db: An open SQLite connection.
    """
    db.executescript("""
        CREATE TABLE IF NOT EXISTS sync_sends (
            remote_id  TEXT NOT NULL,
            outbox_id  INTEGER NOT NULL,
            sent_at    TEXT NOT NULL,
            PRIMARY KEY (remote_id, outbox_id)
        );

        CREATE TABLE IF NOT EXISTS sync_flags (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_memories_updated_at
            ON memories(updated_at);

        CREATE INDEX IF NOT EXISTS idx_outbox_created_at
            ON sync_outbox(created_at);
    """)

    with contextlib.suppress(sqlite3.OperationalError):
        db.execute(
            "ALTER TABLE sync_log ADD COLUMN last_pull_id TEXT NOT NULL DEFAULT ''"
        )

    # Normalize legacy outbox timestamps ('YYYY-MM-DD HH:MM:SS' from the old
    # triggers) into the canonical ISO format so string comparisons (pruning,
    # ordering) stay coherent.
    db.execute("""
        UPDATE sync_outbox
           SET created_at = replace(created_at, ' ', 'T') || '+00:00'
         WHERE created_at LIKE '____-__-__ __:__:__'
    """)

    payload = _outbox_payload_sql("NEW.")
    db.executescript(f"""
        DROP TRIGGER IF EXISTS memories_outbox_ai;
        DROP TRIGGER IF EXISTS memories_outbox_au;

        CREATE TRIGGER IF NOT EXISTS memories_outbox_ai
        AFTER INSERT ON memories
        WHEN COALESCE((SELECT value FROM sync_flags WHERE key = 'sync_enabled'), '0') = '1'
        BEGIN
            INSERT INTO sync_outbox (memory_id, operation, payload, created_at)
            VALUES (NEW.id, 'insert', {payload}, {_SQL_NOW_ISO});
        END;

        CREATE TRIGGER IF NOT EXISTS memories_outbox_au
        AFTER UPDATE ON memories
        WHEN COALESCE((SELECT value FROM sync_flags WHERE key = 'sync_enabled'), '0') = '1'
        BEGIN
            INSERT INTO sync_outbox (memory_id, operation, payload, created_at)
            VALUES (NEW.id, 'update', {payload}, {_SQL_NOW_ISO});
        END;
    """)


def _migrate_v9_to_v10(db: sqlite3.Connection) -> None:
    """v9 -> v10: FT-04 entity graph — entities + memory_entities tables.

    ``entities`` is a fully-synced table of canonical named things (people,
    projects, tools, ...). Entity ids are DETERMINISTIC — derived from the
    normalized canonical name (see :func:`_entity_id`) — so the same entity
    created independently on two machines converges to the same row instead
    of conflicting. ``memory_entities`` links memories to the entities they
    mention (immutable insert-or-ignore rows).

    Outbox triggers mirror the SY-07/SY-08 memory triggers: gated on the
    ``sync_enabled`` flag, canonical ISO-8601 UTC ``created_at``, payloads
    generated from a single column list (HY-03). There are no delete
    triggers — memory deletes do not sync either (no tombstones), so the
    entity graph mirrors that behavior.

    Args:
        db: An open SQLite connection.
    """
    db.executescript("""
        CREATE TABLE IF NOT EXISTS entities (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            kind        TEXT DEFAULT NULL,
            aliases     TEXT NOT NULL DEFAULT '[]',  -- JSON array
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            node_id     TEXT DEFAULT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
        CREATE INDEX IF NOT EXISTS idx_entities_kind ON entities(kind);
        CREATE INDEX IF NOT EXISTS idx_entities_updated_at ON entities(updated_at);

        -- No FK on memory_id/entity_id: sync may deliver a link before its
        -- memory or entity arrives. memory_delete cleans up links explicitly.
        CREATE TABLE IF NOT EXISTS memory_entities (
            memory_id   TEXT NOT NULL,
            entity_id   TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            PRIMARY KEY (memory_id, entity_id)
        );

        CREATE INDEX IF NOT EXISTS idx_memory_entities_entity
            ON memory_entities(entity_id);
        CREATE INDEX IF NOT EXISTS idx_memory_entities_created_at
            ON memory_entities(created_at);
    """)

    entity_payload = _outbox_payload_sql(
        "NEW.", _ENTITY_OUTBOX_COLUMNS, record_type="entity"
    )
    link_payload = _LINK_PAYLOAD_SQL.format(p="NEW.")
    db.executescript(f"""
        DROP TRIGGER IF EXISTS entities_outbox_ai;
        DROP TRIGGER IF EXISTS entities_outbox_au;
        DROP TRIGGER IF EXISTS memory_entities_outbox_ai;

        CREATE TRIGGER IF NOT EXISTS entities_outbox_ai
        AFTER INSERT ON entities
        WHEN COALESCE((SELECT value FROM sync_flags WHERE key = 'sync_enabled'), '0') = '1'
        BEGIN
            INSERT INTO sync_outbox (memory_id, operation, payload, created_at)
            VALUES (NEW.id, 'insert', {entity_payload}, {_SQL_NOW_ISO});
        END;

        CREATE TRIGGER IF NOT EXISTS entities_outbox_au
        AFTER UPDATE ON entities
        WHEN COALESCE((SELECT value FROM sync_flags WHERE key = 'sync_enabled'), '0') = '1'
        BEGIN
            INSERT INTO sync_outbox (memory_id, operation, payload, created_at)
            VALUES (NEW.id, 'update', {entity_payload}, {_SQL_NOW_ISO});
        END;

        CREATE TRIGGER IF NOT EXISTS memory_entities_outbox_ai
        AFTER INSERT ON memory_entities
        WHEN COALESCE((SELECT value FROM sync_flags WHERE key = 'sync_enabled'), '0') = '1'
        BEGIN
            INSERT INTO sync_outbox (memory_id, operation, payload, created_at)
            VALUES (NEW.memory_id, 'insert', {link_payload}, {_SQL_NOW_ISO});
        END;
    """)


def _migrate_v10_to_v11(db: sqlite3.Connection) -> None:
    """v10 -> v11: FT-08 LLM Wiki index tables.

    The wiki's source of truth is plain markdown files on disk (see
    :mod:`remind_me_mcp.wiki`); these tables are a rebuildable search/index
    cache reconciled from the files, so they carry NO sync outbox triggers
    (unlike memories/entities). ``wiki_fts`` is an external-content FTS5 table
    mirroring ``wiki_pages`` via the same ai/ad/au trigger pattern as
    ``memories_fts``. ``wiki_links`` records the ``[[wikilink]]`` graph for
    backlink lookups; ``wiki_meta`` holds small key/value state such as the
    compile watermark.

    Args:
        db: An open SQLite connection.
    """
    db.executescript("""
        CREATE TABLE IF NOT EXISTS wiki_pages (
            slug        TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            content     TEXT NOT NULL,
            summary     TEXT NOT NULL DEFAULT '',
            mtime       REAL NOT NULL DEFAULT 0,
            updated_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS wiki_links (
            src_slug   TEXT NOT NULL,
            dst_slug   TEXT NOT NULL,
            dst_title  TEXT NOT NULL,
            PRIMARY KEY (src_slug, dst_slug)
        );

        CREATE INDEX IF NOT EXISTS idx_wiki_links_dst ON wiki_links(dst_slug);

        CREATE TABLE IF NOT EXISTS wiki_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        -- External-content FTS5 over wiki_pages (mirrors memories_fts).
        CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(
            title, content,
            content='wiki_pages',
            content_rowid='rowid'
        );

        CREATE TRIGGER IF NOT EXISTS wiki_pages_ai AFTER INSERT ON wiki_pages BEGIN
            INSERT INTO wiki_fts(rowid, title, content)
            VALUES (new.rowid, new.title, new.content);
        END;

        CREATE TRIGGER IF NOT EXISTS wiki_pages_ad AFTER DELETE ON wiki_pages BEGIN
            INSERT INTO wiki_fts(wiki_fts, rowid, title, content)
            VALUES ('delete', old.rowid, old.title, old.content);
        END;

        CREATE TRIGGER IF NOT EXISTS wiki_pages_au AFTER UPDATE ON wiki_pages BEGIN
            INSERT INTO wiki_fts(wiki_fts, rowid, title, content)
            VALUES ('delete', old.rowid, old.title, old.content);
            INSERT INTO wiki_fts(rowid, title, content)
            VALUES (new.rowid, new.title, new.content);
        END;
    """)


def _migrate_v11_to_v12(db: sqlite3.Connection) -> None:
    """v11 -> v12: mempalace_imports dedup table for the MemPalace importer.

    Mirrors chat_imports (dedup-by-hash for file imports): tracks which
    MemPalace drawers have already been pulled in, keyed by drawer_id, so
    re-running an import is a safe no-op for drawers already stored.
    """
    db.executescript("""
        CREATE TABLE IF NOT EXISTS mempalace_imports (
            drawer_id   TEXT PRIMARY KEY,
            memory_id   TEXT NOT NULL,
            imported_at TEXT NOT NULL
        );
    """)


def _migrate_v12_to_v13(db: sqlite3.Connection) -> None:
    """v12 -> v13: doc_id/chunk_index columns for neighbor-aware chunk retrieval.

    Promotes the per-file import grouping -- previously only reconstructable
    from ``metadata.import_id``, an unindexed JSON field -- to first-class
    ``doc_id``/``chunk_index`` columns on ``memories``, so a search hit's
    sibling chunks from the same source document/message can be looked up
    directly instead of re-parsing metadata.

    Rides the existing ``memories_outbox_ai``/``_au`` triggers (HY-03): both
    columns are added to ``_OUTBOX_PAYLOAD_COLUMNS`` and the triggers are
    dropped and recreated so their baked-in payload SQL picks up the new
    columns. SQLite defers column resolution inside a trigger body to
    fire-time rather than CREATE TRIGGER time, so this is safe to run before
    any row has doc_id/chunk_index populated.

    Args:
        db: An open SQLite connection.
    """
    with contextlib.suppress(sqlite3.OperationalError):
        db.execute("ALTER TABLE memories ADD COLUMN doc_id TEXT DEFAULT NULL")
    with contextlib.suppress(sqlite3.OperationalError):
        db.execute("ALTER TABLE memories ADD COLUMN chunk_index INTEGER DEFAULT NULL")

    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_doc_chunk ON memories(doc_id, chunk_index)"
    )

    payload = _outbox_payload_sql("NEW.")
    db.executescript(f"""
        DROP TRIGGER IF EXISTS memories_outbox_ai;
        DROP TRIGGER IF EXISTS memories_outbox_au;

        CREATE TRIGGER IF NOT EXISTS memories_outbox_ai
        AFTER INSERT ON memories
        WHEN COALESCE((SELECT value FROM sync_flags WHERE key = 'sync_enabled'), '0') = '1'
        BEGIN
            INSERT INTO sync_outbox (memory_id, operation, payload, created_at)
            VALUES (NEW.id, 'insert', {payload}, {_SQL_NOW_ISO});
        END;

        CREATE TRIGGER IF NOT EXISTS memories_outbox_au
        AFTER UPDATE ON memories
        WHEN COALESCE((SELECT value FROM sync_flags WHERE key = 'sync_enabled'), '0') = '1'
        BEGIN
            INSERT INTO sync_outbox (memory_id, operation, payload, created_at)
            VALUES (NEW.id, 'update', {payload}, {_SQL_NOW_ISO});
        END;
    """)


def _migrate_v13_to_v14(db: sqlite3.Connection) -> None:
    """v13 -> v14: entity_relations -- typed entity-to-entity edges + multi-hop traversal.

    ``entities``/``memory_entities`` (v9->v10) only encode "entity X is
    mentioned in memory Y" -- a memory<->entity bipartite graph. This adds a
    genuine entity<->entity typed edge: (subject_entity_id, relation,
    object_entity_id), e.g. "Bailey --works_with--> Alex". Deterministic id
    (sha256 of the triple, see :func:`_entity_relation_id`) so the same
    relation recorded independently on two machines converges to the same
    row; immutable insert-or-ignore semantics (see
    :func:`_upsert_entity_relation`), no FKs (same sync-order-tolerance
    rationale as ``memory_entities`` -- a relation may arrive before either
    entity does). Outbox trigger mirrors the v9/v10 entity/link triggers
    (insert-only, since relations never change once recorded), gated on
    ``sync_enabled``, payload tagged ``record_type='entity_relation'``.

    Args:
        db: An open SQLite connection.
    """
    db.executescript("""
        CREATE TABLE IF NOT EXISTS entity_relations (
            id                TEXT PRIMARY KEY,
            subject_entity_id TEXT NOT NULL,
            relation          TEXT NOT NULL,
            object_entity_id  TEXT NOT NULL,
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL,
            node_id           TEXT DEFAULT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_entity_relations_subject
            ON entity_relations(subject_entity_id);
        CREATE INDEX IF NOT EXISTS idx_entity_relations_object
            ON entity_relations(object_entity_id);
        CREATE INDEX IF NOT EXISTS idx_entity_relations_created_at
            ON entity_relations(created_at);
    """)

    relation_payload = _outbox_payload_sql(
        "NEW.", _ENTITY_RELATION_OUTBOX_COLUMNS, record_type="entity_relation"
    )
    db.executescript(f"""
        DROP TRIGGER IF EXISTS entity_relations_outbox_ai;

        CREATE TRIGGER IF NOT EXISTS entity_relations_outbox_ai
        AFTER INSERT ON entity_relations
        WHEN COALESCE((SELECT value FROM sync_flags WHERE key = 'sync_enabled'), '0') = '1'
        BEGIN
            INSERT INTO sync_outbox (memory_id, operation, payload, created_at)
            VALUES (NEW.id, 'insert', {relation_payload}, {_SQL_NOW_ISO});
        END;
    """)


def _migrate_v14_to_v15(db: sqlite3.Connection) -> None:
    """v14 -> v15: dbs_imports dedup/update-tracking table for the dbs importer.

    Mirrors mempalace_imports (dedup-by-id for the MemPalace importer), but
    keyed by (dbs_source, external_id) -- dbs's own item identity -- rather
    than a single id column, and storing content_hash so a rerun can tell an
    edited dbs item (content_hash changed) from an unchanged one (skip) or a
    brand new one (import). No sync outbox trigger: purely local bookkeeping
    for the importer, same as mempalace_imports.
    """
    db.executescript("""
        CREATE TABLE IF NOT EXISTS dbs_imports (
            dbs_source   TEXT NOT NULL,
            external_id  TEXT NOT NULL,
            memory_id    TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            imported_at  TEXT NOT NULL,
            PRIMARY KEY (dbs_source, external_id)
        );
    """)


def _migrate_v15_to_v16(db: sqlite3.Connection) -> None:
    """v15 -> v16: deleted_at tombstone column for delete/sync propagation (gap #11).

    Deletion previously had no sync representation at all: ``memory_delete``
    hard-DELETEd the row, which produces no ``sync_outbox`` entry (the
    triggers only fire on INSERT/UPDATE), so a memory deleted on one device
    silently resurrected on the next pull from another. This adds a
    ``deleted_at`` tombstone column instead: "deleting" a memory becomes an
    UPDATE (setting ``deleted_at``/``updated_at``), which rides the
    *existing* ``memories_outbox_au`` trigger for free once the column is
    added to ``_OUTBOX_PAYLOAD_COLUMNS`` -- no new outbox operation type or
    trigger is needed, and LWW conflict resolution (compare ``updated_at``)
    already applies to a tombstone exactly like any other update.

    Every normal read path (search, list, get, entity profile, ...) is
    updated elsewhere to filter ``deleted_at IS NULL``, same as the existing
    ``superseded_by IS NULL`` convention -- the row stays in the database
    (so the tombstone itself can propagate) but is invisible to normal use.
    Sync's pull/push wire paths and ``exporter.py``'s full-backup export
    deliberately do NOT filter it, since they need to carry tombstones
    across nodes / preserve them in a restorable backup.

    A separate periodic compaction pass (``sync._compact_tombstones``) hard-
    deletes tombstones once they're old enough that every reachable peer/hub
    has almost certainly already observed them, so the table doesn't grow
    forever.

    Rides the existing ``memories_outbox_ai``/``_au`` triggers (HY-03), same
    pattern as v12->v13's doc_id/chunk_index addition.

    Args:
        db: An open SQLite connection.
    """
    with contextlib.suppress(sqlite3.OperationalError):
        db.execute("ALTER TABLE memories ADD COLUMN deleted_at TEXT DEFAULT NULL")

    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_deleted_at ON memories(deleted_at)"
    )

    payload = _outbox_payload_sql("NEW.")
    db.executescript(f"""
        DROP TRIGGER IF EXISTS memories_outbox_ai;
        DROP TRIGGER IF EXISTS memories_outbox_au;

        CREATE TRIGGER IF NOT EXISTS memories_outbox_ai
        AFTER INSERT ON memories
        WHEN COALESCE((SELECT value FROM sync_flags WHERE key = 'sync_enabled'), '0') = '1'
        BEGIN
            INSERT INTO sync_outbox (memory_id, operation, payload, created_at)
            VALUES (NEW.id, 'insert', {payload}, {_SQL_NOW_ISO});
        END;

        CREATE TRIGGER IF NOT EXISTS memories_outbox_au
        AFTER UPDATE ON memories
        WHEN COALESCE((SELECT value FROM sync_flags WHERE key = 'sync_enabled'), '0') = '1'
        BEGIN
            INSERT INTO sync_outbox (memory_id, operation, payload, created_at)
            VALUES (NEW.id, 'update', {payload}, {_SQL_NOW_ISO});
        END;
    """)


def _migrate_v16_to_v17(db: sqlite3.Connection) -> None:
    """v16 -> v17: memory_feedback table for query-contextual feedback (gap #6).

    ``record_feedback`` previously always mutated ``base_weight`` globally --
    a memory marked unhelpful for one query got demoted for *every* future
    query, even though ``FeedbackInput`` already carried a ``query`` field
    that was silently discarded. This adds a table to log each feedback
    event with its query context: ``query_tokens`` is a normalized,
    space-joined token set (lowercased, deduplicated) used for a coarse
    Jaccard-similarity comparison against a future query at ranking time
    (``vitality.apply_feedback_adjustment``) -- no embedder dependency, so
    this works identically whether or not semantic search is configured.

    When feedback is given *with* a query, ``record_feedback`` now logs a
    row here instead of touching ``base_weight`` (query-contextual only);
    without a query, the original global mutation is unchanged, preserving
    exact backward compatibility for any caller that doesn't supply one.

    No sync outbox trigger: purely local bookkeeping, same as
    ``dbs_imports``/``mempalace_imports`` -- feedback given on one device
    doesn't (yet) propagate to others. An explicit, flagged scope decision,
    not an oversight: full cross-device sync would need its own outbox
    triggers, a ``sync.py`` dispatch branch, and hub route parity (the same
    lift ``entity_relations`` required), disproportionate to this gap.

    Args:
        db: An open SQLite connection.
    """
    db.executescript("""
        CREATE TABLE IF NOT EXISTS memory_feedback (
            id           TEXT PRIMARY KEY,
            memory_id    TEXT NOT NULL,
            query        TEXT NOT NULL,
            query_tokens TEXT NOT NULL,
            signal       TEXT NOT NULL CHECK (signal IN ('helpful', 'unhelpful')),
            magnitude    REAL NOT NULL,
            created_at   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_memory_feedback_memory_id
            ON memory_feedback(memory_id);
    """)


def _migrate_v17_to_v18(db: sqlite3.Connection) -> None:
    """v17 -> v18: embedding_meta table for embedding-model versioning (issue #18).

    Records which embedding model/dimension/backend the vectors currently
    stored in ``memories_vec``/``vec_chunks`` were actually computed with --
    written by :func:`_mark_embedding_meta_current` after a batch of vectors
    is (re-)written, not merely inferred from the running config, so a
    mismatch check stays accurate even mid-reindex.

    No sync outbox trigger: this describes which model produced *this
    node's* local vectors (vectors themselves are never synced -- see
    sync.py's ``_embed_and_store_rows`` call after pull), not something
    meaningful to replicate to a peer that might run a different backend.

    Args:
        db: An open SQLite connection.
    """
    db.executescript("""
        CREATE TABLE IF NOT EXISTS embedding_meta (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """)


def _migrate_v18_to_v19(db: sqlite3.Connection) -> None:
    """v18 -> v19: memory_associations table for co-retrieval reinforcement (issue #9).

    True ACT-R-style memory reinforces associations *between* items
    retrieved together, not just each item independently -- ``memory_entities``
    links a memory to entities it mentions, but nothing captured "these two
    memories tend to be useful together" from actual search co-occurrence.
    ``memory_id_a``/``memory_id_b`` are stored in canonical (sorted) order so
    a pair only ever has one row regardless of retrieval order; ``weight`` is
    a simple bounded counter (see ``vitality.CO_RETRIEVAL_MAX_WEIGHT``) --
    deliberately no time-decay in this pass (an explicitly flagged, unresolved
    design question in the issue -- "a project of its own," not a quick add).

    No sync outbox trigger: purely local usage-pattern bookkeeping, same
    scope decision as ``memory_feedback`` (v16->v17) -- association strength
    observed on one device doesn't (yet) propagate to others.

    Args:
        db: An open SQLite connection.
    """
    db.executescript("""
        CREATE TABLE IF NOT EXISTS memory_associations (
            memory_id_a TEXT NOT NULL,
            memory_id_b TEXT NOT NULL,
            weight      INTEGER NOT NULL DEFAULT 1,
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (memory_id_a, memory_id_b)
        );
        CREATE INDEX IF NOT EXISTS idx_memory_associations_a
            ON memory_associations(memory_id_a);
        CREATE INDEX IF NOT EXISTS idx_memory_associations_b
            ON memory_associations(memory_id_b);
    """)


def embedding_mismatch_info(db: sqlite3.Connection) -> dict[str, str] | None:
    """Read-only check: do the stored vectors' model/dim/backend differ from
    the currently configured ``EMBEDDING_MODEL``/``EMBEDDING_DIM``/
    ``EMBEDDING_BACKEND`` (issue #18)?

    Returns None when nothing is recorded yet (a fresh store, or one that
    predates this feature -- nothing to compare against) or when the
    recorded values match. Otherwise returns the stored vs. current values
    for display (:func:`remind_me_mcp.tools.admin.remind_me_server_status`)
    or action (:func:`_reconcile_embedding_meta`).

    Args:
        db: An open SQLite connection.
    """
    try:
        rows = db.execute("SELECT key, value FROM embedding_meta").fetchall()
    except sqlite3.OperationalError:
        return None  # pre-v18 database; migrations haven't run yet
    stored = {r[0]: r[1] for r in rows}
    if not stored:
        return None
    if (
        stored.get("model") == EMBEDDING_MODEL
        and stored.get("dim") == str(EMBEDDING_DIM)
        and stored.get("backend") == EMBEDDING_BACKEND
    ):
        return None
    return {
        "stored_model": stored.get("model", "?"),
        "stored_dim": stored.get("dim", "?"),
        "stored_backend": stored.get("backend", "?"),
        "current_model": EMBEDDING_MODEL,
        "current_dim": str(EMBEDDING_DIM),
        "current_backend": EMBEDDING_BACKEND,
    }


def _reconcile_embedding_meta(db: sqlite3.Connection) -> None:
    """Clear stale vectors when the embedding model/dimension has changed (issue #18).

    Existing vectors computed by a different model (or a different
    dimension) are not just outdated -- they're actively wrong: KNN against
    them would silently return garbage nearest-neighbor results rather than
    erroring. ``memories_vec``/``vec_chunks`` are cleared entirely (and
    ``memories_vec`` recreated at the new dimension if it changed) so every
    memory falls through to the existing "missing embeddings" path that
    ``remind_me_reindex`` and ``remind_me_server_status`` already surface --
    this is the "clear, actionable warning" the issue asks for, reusing
    machinery that already exists rather than adding a parallel one.

    Deliberately does NOT update ``embedding_meta`` here -- that only
    happens once vectors are actually rewritten
    (:func:`_mark_embedding_meta_current`), so the mismatch stays flagged
    across every connection/startup until a real reindex happens, not just
    the one connection that first detected it.

    An automatic background re-embed (spawning a reindex thread at startup)
    was considered and rejected: it would run unconditionally on every
    server start with a pending mismatch, including inside tests and quick
    CLI invocations, for a potentially expensive operation the existing
    ``remind_me_reindex`` tool already does deliberately, on request.

    Args:
        db: An open SQLite connection with row_factory=sqlite3.Row set.
    """
    info = embedding_mismatch_info(db)
    if info is None:
        return

    log.warning(
        "Embedding model changed (%s/%s dim=%s -> %s/%s dim=%s); clearing "
        "stale vectors. Run remind_me_reindex to rebuild them.",
        info["stored_backend"],
        info["stored_model"],
        info["stored_dim"],
        info["current_backend"],
        info["current_model"],
        info["current_dim"],
    )

    with contextlib.suppress(sqlite3.OperationalError):
        db.execute("DELETE FROM vec_chunks")
    with contextlib.suppress(sqlite3.OperationalError):
        db.execute("DROP TABLE IF EXISTS memories_vec")
    try:
        db.execute(
            f"CREATE VIRTUAL TABLE memories_vec USING vec0(embedding float[{EMBEDDING_DIM}])"
        )
    except sqlite3.OperationalError as e:
        log.debug("sqlite-vec not available while recreating memories_vec: %s", e)
    db.commit()

    ann_index.invalidate_index(db)


def _mark_embedding_meta_current(db: sqlite3.Connection) -> None:
    """Record that stored vectors now match the configured embedding model (issue #18).

    Called after a batch of vectors is successfully (re-)written
    (:func:`_embed_and_store_batch`). Safe to call repeatedly/mid-reindex:
    any vector present at all was written under the current config (a
    mismatch clears every old vector first via
    :func:`_reconcile_embedding_meta`), so marking "current" as soon as any
    batch succeeds is accurate, not just once a full reindex finishes.

    Best-effort -- a failure here is bookkeeping only and must never surface
    as an embedding failure to the caller.

    Args:
        db: An open SQLite connection.
    """
    now = _now_iso()
    with contextlib.suppress(sqlite3.Error):
        for key, value in (
            ("model", EMBEDDING_MODEL),
            ("dim", str(EMBEDDING_DIM)),
            ("backend", EMBEDDING_BACKEND),
        ):
            db.execute(
                "INSERT INTO embedding_meta (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, value, now),
            )
        db.commit()


def _reconcile_sync_enabled_flag(db: sqlite3.Connection) -> None:
    """Align the sync_flags gate with config.SYNC_ENABLED at startup (SY-07).

    The outbox triggers only fire while ``sync_flags['sync_enabled'] = '1'``.
    On every startup this function compares the stored flag with the current
    configuration:

    - enabled -> disabled: the outbox (and per-remote send log) is truncated —
      nothing will consume it.
    - disabled -> enabled: the outbox is backfilled from all current memories
      so changes made while sync was off still reach the remotes.
    - unset (pre-v9 database or fresh schema): no backfill is needed — pre-v9
      triggers were unconditional, so the outbox is already complete.

    Args:
        db: An open SQLite connection.
    """
    from remind_me_mcp import config as _config

    desired = "1" if _config.SYNC_ENABLED else "0"
    row = db.execute(
        "SELECT value FROM sync_flags WHERE key = 'sync_enabled'"
    ).fetchone()
    stored = row[0] if row is not None else None
    if stored == desired:
        return

    if desired == "1":
        if stored == "0":
            # Triggers were off while sync was disabled — backfill so every
            # memory (and entity-graph row, FT-04) reaches the remotes.
            payload = _outbox_payload_sql("")
            db.execute(f"""
                INSERT INTO sync_outbox (memory_id, operation, payload, created_at)
                SELECT id, 'insert', {payload}, {_SQL_NOW_ISO}
                FROM memories
            """)
            entity_payload = _outbox_payload_sql(
                "", _ENTITY_OUTBOX_COLUMNS, record_type="entity"
            )
            db.execute(f"""
                INSERT INTO sync_outbox (memory_id, operation, payload, created_at)
                SELECT id, 'insert', {entity_payload}, {_SQL_NOW_ISO}
                FROM entities
            """)
            link_payload = _LINK_PAYLOAD_SQL.format(p="")
            db.execute(f"""
                INSERT INTO sync_outbox (memory_id, operation, payload, created_at)
                SELECT memory_id, 'insert', {link_payload}, {_SQL_NOW_ISO}
                FROM memory_entities
            """)
    else:
        db.execute("DELETE FROM sync_outbox")
        db.execute("DELETE FROM sync_sends")

    db.execute(
        """INSERT INTO sync_flags (key, value) VALUES ('sync_enabled', ?)
           ON CONFLICT (key) DO UPDATE SET value = excluded.value""",
        (desired,),
    )
    db.commit()


# ---------------------------------------------------------------------------
# Embedding helpers
# ---------------------------------------------------------------------------


def _delete_chunks(db: sqlite3.Connection, memory_rowid: int) -> list[int]:
    """Remove all chunk vectors (and map rows) belonging to one memory.

    Returns the vec_rowids that were removed, so a caller also maintaining
    the optional ANN index (ann_index.py) can mirror the removal there —
    but only *after* its own transaction commits, since ANN mutations
    aren't part of the SQL transaction and can't be rolled back with it.
    """
    old = db.execute(
        "SELECT vec_rowid FROM vec_chunks WHERE memory_rowid = ?", (memory_rowid,)
    ).fetchall()
    removed = [vec_rowid for (vec_rowid,) in old]
    for vec_rowid in removed:
        db.execute("DELETE FROM memories_vec WHERE rowid = ?", (vec_rowid,))
    db.execute("DELETE FROM vec_chunks WHERE memory_rowid = ?", (memory_rowid,))
    return removed


def _prune_orphan_chunks(db: sqlite3.Connection) -> int:
    """Remove chunk vectors whose parent memory no longer exists.

    SQLite reuses freed rowids, so an orphaned ``vec_chunks`` row would make a
    new memory inherit a deleted memory's embedding (and reindex would skip it
    because the rowid already looks embedded). Called by reindex to heal
    databases written before deletes cleaned up chunk vectors.

    Args:
        db: An open SQLite connection.

    Returns:
        The number of orphaned chunk rows removed.
    """
    orphans = db.execute(
        """SELECT vec_rowid FROM vec_chunks
           WHERE memory_rowid NOT IN (SELECT rowid FROM memories)"""
    ).fetchall()
    if not orphans:
        return 0
    vec_rowids = [vec_rowid for (vec_rowid,) in orphans]
    for vec_rowid in vec_rowids:
        db.execute("DELETE FROM memories_vec WHERE rowid = ?", (vec_rowid,))
        db.execute("DELETE FROM vec_chunks WHERE vec_rowid = ?", (vec_rowid,))
    db.commit()
    # ANN mutations only after the commit succeeds — see _delete_chunks.
    for vec_rowid in vec_rowids:
        ann_index.remove_vector(db, vec_rowid)
    return len(vec_rowids)


def _embed_and_store_rows(rows: list[tuple[int, str]]) -> int:
    """Embed and store sliding-window chunk vectors for several memories at once.

    Internally batches into groups of at most ``EMBED_BATCH_SIZE`` memories
    per actual ``embedder.embed()`` call and DB transaction — regardless of
    how many rows the caller passes in one call. This is the single source
    of truth for that invariant: every caller (reindex, file import,
    mempalace/dbs import, sync's pulled-record embedding) gets it for free
    instead of each having to remember to pre-slice its own input. Defense
    in depth alongside the hard ``EMBED_FORWARD_BATCH`` ceiling inside
    ``embedder.embed()`` itself (config.py) — that bounds peak *forward-pass*
    memory for any caller, but without this, a caller handing over an
    unbounded number of rows in one call still built one unbounded
    ``flat_chunks`` list and one unbounded transaction before ever reaching
    that cap.

    Args:
        rows: ``(memory_rowid, content)`` pairs to embed.

    Returns:
        The number of memories that had at least one chunk stored.
    """
    embedder = _get_embedder()
    if embedder is None:
        return 0
    stored = 0
    for batch_start in range(0, len(rows), EMBED_BATCH_SIZE):
        batch = rows[batch_start : batch_start + EMBED_BATCH_SIZE]
        stored += _embed_and_store_batch(embedder, batch)
    return stored


def _embed_and_store_batch(embedder: Any, rows: list[tuple[int, str]]) -> int:
    """Embed and store one already-size-bounded batch of memories.

    The single-transaction body previously inlined in
    :func:`_embed_and_store_rows` — split out so that function's batching
    loop can call it repeatedly without nesting a try/except per iteration
    at the call site.

    Args:
        embedder: An already-resolved, available embedder.
        rows: ``(memory_rowid, content)`` pairs to embed — assumed to already
            be at most ``EMBED_BATCH_SIZE`` long.

    Returns:
        The number of memories in this batch that had at least one chunk stored.
    """
    # Split every memory into chunks, flattening into one batch for embedding.
    plan: list[tuple[int, int, int]] = []  # (memory_rowid, offset, count)
    flat_chunks: list[str] = []
    for memory_rowid, content in rows:
        chunks = chunk_text(content or "")
        if not chunks:
            continue
        plan.append((memory_rowid, len(flat_chunks), len(chunks)))
        flat_chunks.extend(chunks)
    if not flat_chunks:
        return 0
    db = _get_db()
    try:
        vecs = embedder.embed(flat_chunks, role="passage")
        stored = 0
        removed_vec_rowids: list[int] = []
        added_vecs: list[tuple[int, bytes]] = []
        for memory_rowid, offset, count in plan:
            removed_vec_rowids.extend(_delete_chunks(db, memory_rowid))
            for ci in range(count):
                vec_bytes = vecs[offset + ci].tobytes()
                cur = db.execute(
                    "INSERT INTO memories_vec(embedding) VALUES (?)",
                    (vec_bytes,),
                )
                vec_rowid = cur.lastrowid
                assert vec_rowid is not None  # guaranteed by the INSERT above
                db.execute(
                    "INSERT INTO vec_chunks(vec_rowid, memory_rowid, chunk_ix) "
                    "VALUES (?, ?, ?)",
                    (vec_rowid, memory_rowid, ci),
                )
                added_vecs.append((vec_rowid, vec_bytes))
            stored += 1
        db.commit()
        # ANN mutations only after the commit succeeds, so a rollback in the
        # except clause below never leaves the ANN index out of sync with
        # memories_vec (ANN isn't part of the SQL transaction).
        for vec_rowid in removed_vec_rowids:
            ann_index.remove_vector(db, vec_rowid)
        for vec_rowid, vec_bytes in added_vecs:
            ann_index.add_vector(db, vec_rowid, vec_bytes)
        if stored:
            _mark_embedding_meta_current(db)
        return stored
    except (sqlite3.DatabaseError, sqlite3.InterfaceError) as e:
        # InterfaceError is a sibling of DatabaseError in Python's sqlite3
        # hierarchy (not a subclass), but "bad parameter or other API misuse"
        # is reachable here too (e.g. a connection used concurrently from
        # another thread) and is just as self-healing via remind_me_reindex.
        log.warning("Database error storing chunk embeddings: %s", e)
        # PF-05: undo any uncommitted chunk DELETEs/INSERTs, otherwise they
        # would silently ride along with the next unrelated commit on this
        # connection — deleting a memory's existing embeddings.
        with contextlib.suppress(sqlite3.Error):
            db.rollback()
        return 0
    except (ValueError, TypeError) as e:
        log.warning("Embedding computation failed: %s", e)
        with contextlib.suppress(sqlite3.Error):
            db.rollback()  # PF-05: see above
        return 0


def _embed_and_store(memory_id: str, content: str) -> bool:
    """Generate sliding-window embeddings for one memory and store them.

    Looks up the memory's rowid, splits ``content`` into overlapping chunks,
    embeds each, and stores them as chunk vectors linked to the memory (see
    :func:`_embed_and_store_rows`). Returns False silently when the embedder is
    unavailable, the memory is unknown, or the vector table is missing.

    Args:
        memory_id: The text primary key of the memory to embed.
        content: The full text content to embed (chunked, not truncated).

    Returns:
        True if at least one chunk vector was stored, False otherwise.
    """
    db = _get_db()
    row = db.execute(
        "SELECT rowid FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    if row is None:
        return False
    return _embed_and_store_rows([(row[0], content)]) > 0


def _fuse_query_embedding(embedder, texts: list[str]) -> bytes:
    """Embed *texts* and average them into one L2-normalised search vector.

    With a single text this is exactly ``embed_one``. With several (e.g. the
    query plus a HyDE passage), the mean vector blends question-space and
    document-space so candidates near either phrasing rank well.

    ``texts[0]`` is always the literal search query and is embedded with
    ``role="query"``; any remaining texts (e.g. a HyDE passage) are
    document-like synthetic text rather than a query, so they're embedded
    with ``role="passage"`` (query/document embedding prefix asymmetry) —
    otherwise a query-prefixed model would apply the wrong instruction to
    half the fused vector's inputs.

    Args:
        embedder: Any embedder exposing ``embed(list[str]) -> np.ndarray``.
        texts: One or more texts to fuse (must be non-empty); the first is
            the query, the rest (if any) are passage-like expansion text.

    Returns:
        Raw float32 bytes of the fused vector for sqlite-vec.
    """
    import numpy as np

    query_text, *extra_texts = texts
    vecs = embedder.embed([query_text], role="query")
    if extra_texts:
        vecs = np.vstack([vecs, embedder.embed(extra_texts, role="passage")])
    fused = vecs.mean(axis=0)
    norm = float(np.linalg.norm(fused))
    if norm > 1e-9:
        fused = fused / norm
    return fused.astype(np.float32).tobytes()


def _hydrate_ann_hits(
    db: sqlite3.Connection,
    hits: list[tuple[int, float]],
    limit: int,
    category: str | None,
    tags: list[str] | None,
) -> list[dict]:
    """Turn ANN ``(vec_rowid, distance)`` pairs into full memory dicts.

    Mirrors the brute-force SQL path's semantics exactly: dedupe to each
    memory's best (smallest-distance) chunk, exclude superseded memories,
    apply category/tag filters, sort by distance, and truncate to *limit*.
    A ``vec_rowid`` with no matching ``vec_chunks`` row (a stale ANN entry —
    e.g. a chunk removed between an in-flight search and an unrelated
    delete) is silently skipped rather than treated as an error.
    """
    if not hits:
        return []
    vec_rowids = [vr for vr, _ in hits]
    placeholders = ",".join("?" * len(vec_rowids))
    chunk_rows = db.execute(
        f"SELECT vec_rowid, memory_rowid FROM vec_chunks WHERE vec_rowid IN ({placeholders})",
        vec_rowids,
    ).fetchall()
    vec_to_memory = {r["vec_rowid"]: r["memory_rowid"] for r in chunk_rows}

    best_by_memory: dict[int, float] = {}
    for vec_rowid, distance in hits:
        memory_rowid = vec_to_memory.get(vec_rowid)
        if memory_rowid is None:
            continue
        prev = best_by_memory.get(memory_rowid)
        if prev is None or distance < prev:
            best_by_memory[memory_rowid] = distance
    if not best_by_memory:
        return []

    conditions = ""
    bindings: list = []
    if category:
        conditions += " AND m.category = ?"
        bindings.append(category)
    for i, tag in enumerate(tags or []):
        alias = f"mt{i}"
        conditions += (
            f" AND EXISTS (SELECT 1 FROM memory_tags {alias}"
            f" WHERE {alias}.memory_id = m.id AND {alias}.tag = ?)"
        )
        bindings.append(tag)

    memory_rowids = list(best_by_memory)
    m_placeholders = ",".join("?" * len(memory_rowids))
    rows = db.execute(
        f"""SELECT m.*, m.rowid AS _ann_rowid FROM memories m
           WHERE m.rowid IN ({m_placeholders}) AND m.superseded_by IS NULL
           AND m.deleted_at IS NULL{conditions}""",
        [*memory_rowids, *bindings],
    ).fetchall()

    results = []
    for r in rows:
        d = _row_to_dict(r)
        memory_rowid = d.pop("_ann_rowid")
        d["semantic_distance"] = best_by_memory[memory_rowid]
        results.append(d)
    results.sort(key=lambda d: d["semantic_distance"])
    return results[:limit]


def _semantic_search(
    query: str,
    limit: int = 20,
    extra_texts: list[str] | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
) -> list[dict]:
    """Search memories by semantic similarity using the chunked vector index.

    Embeds the query, runs KNN over the per-chunk vectors in ``memories_vec``,
    then deduplicates to distinct parent memories — keeping each memory's best
    (smallest-distance) chunk. Because one memory may own several chunks, the
    KNN over-fetches ``limit * _CHUNK_KNN_FANOUT`` chunk hits so enough distinct
    memories survive the dedupe (a further 4x when category/tag filters prune
    candidates). Results carry a 'semantic_distance' key (lower = more similar).
    Returns [] when the embedder or vector table is unavailable.

    Once the corpus grows past ``config.ANN_MIN_CHUNKS`` chunk vectors, the
    KNN is served by the optional HNSW ANN index (ann_index.py) instead of
    sqlite-vec's exact brute-force scan — same output shape either way. Below
    that threshold, or whenever the optional ``usearch`` package isn't
    installed, or if the ANN path itself fails, this transparently falls back
    to the brute-force scan below.

    Args:
        query: The search query text to embed and compare.
        limit: Maximum number of distinct memories to return.
        extra_texts: Optional expansion texts (e.g. a HyDE passage) whose
            embeddings are averaged with the query's before the KNN.
        category: If set, only return memories with this category.
        tags: If set, only return memories that have ALL of these tags.

    Returns:
        List of memory dicts (from _row_to_dict) with an added
        'semantic_distance' float field, sorted by distance ascending.
    """
    embedder = _get_embedder()
    if embedder is None:
        return []
    try:
        db = _get_db()
        if extra_texts:
            query_bytes = _fuse_query_embedding(embedder, [query, *extra_texts])
        else:
            query_bytes = embedder.embed_one(query, role="query")
        # Filters are applied after the KNN, so over-fetch harder when they
        # can prune candidates (DI-03).
        fanout = _CHUNK_KNN_FANOUT * (4 if (category or tags) else 1)
        knn_k = max(limit, limit * fanout)

        try:
            (chunk_count,) = db.execute("SELECT COUNT(*) FROM memories_vec").fetchone()
        except sqlite3.OperationalError:
            chunk_count = 0
        if chunk_count >= ANN_MIN_CHUNKS:
            ann_hits = ann_index.search(db, query_bytes, knn_k)
            if ann_hits is not None:
                return _hydrate_ann_hits(db, ann_hits, limit, category, tags)

        conditions = ""
        bindings: list = [query_bytes, knn_k]
        if category:
            conditions += " AND m.category = ?"
            bindings.append(category)
        for i, tag in enumerate(tags or []):
            alias = f"mt{i}"
            conditions += (
                f" AND EXISTS (SELECT 1 FROM memory_tags {alias}"
                f" WHERE {alias}.memory_id = m.id AND {alias}.tag = ?)"
            )
            bindings.append(tag)
        rows = db.execute(
            f"""SELECT m.*, MIN(mv.distance) AS distance
               FROM memories_vec mv
               JOIN vec_chunks vc ON vc.vec_rowid = mv.rowid
               JOIN memories m ON m.rowid = vc.memory_rowid
               WHERE mv.embedding MATCH ?
               AND mv.k = ?
               AND m.superseded_by IS NULL
               AND m.deleted_at IS NULL{conditions}
               GROUP BY m.rowid
               ORDER BY distance""",
            bindings,
        ).fetchall()
        results = []
        for r in rows[:limit]:
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


_last_now_lock = threading.Lock()
_last_now: datetime | None = None


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string, strictly monotonic.

    Two calls in immediate succession (e.g. an insert followed by an update
    a few microseconds later) can otherwise land in the same clock tick and
    produce an identical string, which is more than a cosmetic issue here:
    hub sync uses last-write-wins on `updated_at` (see Multi-Machine Sync in
    the README), so a tie between two same-node writes would make their
    relative order ambiguous once synced against a concurrent remote write.
    Nudging forward by a microsecond when the clock hasn't visibly advanced
    keeps successive calls strictly ordered without lying about real time by
    more than a tick.

    Returns:
        ISO 8601 formatted datetime string with timezone offset, e.g.
        '2024-01-15T12:34:56.789012+00:00'.
    """
    global _last_now
    with _last_now_lock:
        now = datetime.now(UTC)
        if _last_now is not None and now <= _last_now:
            now = _last_now + timedelta(microseconds=1)
        _last_now = now
        return now.isoformat()


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


# ---------------------------------------------------------------------------
# Entity graph helpers (FT-04)
# ---------------------------------------------------------------------------


def _normalize_entity_name(name: str) -> str:
    """Normalize an entity name for deterministic identity.

    Lowercases and collapses all internal/surrounding whitespace, so
    'Bailey  Robertson ' and 'bailey robertson' identify the same entity.
    Shared by every write path (decompose, add, annotate, sync) — entity ids
    are derived from this normalized form, so two machines independently
    creating the same-named entity converge to the same row.

    Args:
        name: The raw entity name.

    Returns:
        The normalized name (lowercased, whitespace-collapsed).
    """
    return " ".join(name.split()).lower()


def _entity_id(name: str) -> str:
    """Derive the DETERMINISTIC id for an entity from its name.

    Unlike :func:`_make_id`, this is a pure content hash (no timestamp):
    sha256 of the normalized name, truncated to the same 12-hex-char length
    as memory ids. Determinism is what makes fully-synced entities converge
    across peers instead of conflicting.

    Args:
        name: The entity name (any casing/whitespace).

    Returns:
        A 12-character hex string.
    """
    return hashlib.sha256(_normalize_entity_name(name).encode()).hexdigest()[:12]


def _upsert_entity(
    db: sqlite3.Connection,
    name: str,
    kind: str | None = None,
    aliases: list[str] | None = None,
    *,
    node_id: str | None = None,
    now: str | None = None,
) -> str:
    """Insert or update an entity by its deterministic id (local write path).

    A new name creates a new entity row (the display name keeps the caller's
    casing, whitespace-collapsed). If the entity already exists, the provided
    aliases are union-merged (dedup, order-preserving: existing first) and a
    missing kind is filled in — the canonical name is never auto-merged with
    a different mention name; alias merging is explicit via *aliases*.
    ``updated_at`` is bumped only when something actually changed, so sync
    propagates exactly the real edits. Does NOT commit.

    Args:
        db: An open SQLite connection.
        name: The entity's canonical name as mentioned.
        kind: Optional entity kind (e.g. 'person', 'project', 'tool').
        aliases: Optional explicit alias names to merge in.
        node_id: This node's id, stamped on newly created rows.
        now: Timestamp override (defaults to now).

    Returns:
        The entity's deterministic id.
    """
    eid = _entity_id(name)
    ts = now or _now_iso()
    clean_aliases = list(dict.fromkeys(
        a.strip() for a in (aliases or []) if isinstance(a, str) and a.strip()
    ))

    row = db.execute(
        "SELECT kind, aliases FROM entities WHERE id = ?", (eid,)
    ).fetchone()
    if row is None:
        db.execute(
            """INSERT INTO entities (id, name, kind, aliases, created_at, updated_at, node_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (eid, " ".join(name.split()), kind, json.dumps(clean_aliases),
             ts, ts, node_id),
        )
        return eid

    try:
        existing = json.loads(row["aliases"]) if isinstance(row["aliases"], str) else []
    except json.JSONDecodeError:
        existing = []
    if not isinstance(existing, list):
        existing = []
    merged = list(dict.fromkeys([*existing, *clean_aliases]))
    new_kind = row["kind"] or kind
    if merged != existing or new_kind != row["kind"]:
        db.execute(
            "UPDATE entities SET kind = ?, aliases = ?, updated_at = ? WHERE id = ?",
            (new_kind, json.dumps(merged), ts, eid),
        )
    return eid


def _link_memory_entity(
    db: sqlite3.Connection,
    memory_id: str,
    entity_id: str,
    now: str | None = None,
) -> bool:
    """Record that a memory mentions an entity (immutable, insert-or-ignore).

    Args:
        db: An open SQLite connection.
        memory_id: The mentioning memory's id.
        entity_id: The mentioned entity's id.
        now: Timestamp override (defaults to now).

    Returns:
        True if a new link row was created, False if it already existed.
        Does NOT commit.
    """
    cur = db.execute(
        """INSERT OR IGNORE INTO memory_entities (memory_id, entity_id, created_at)
           VALUES (?, ?, ?)""",
        (memory_id, entity_id, now or _now_iso()),
    )
    return cur.rowcount > 0


def _entity_relation_id(subject_entity_id: str, relation: str, object_entity_id: str) -> str:
    """Derive the DETERMINISTIC id for an entity-to-entity relation triple.

    sha256 of ``"subject_id|normalized_relation|object_id"``, truncated to
    the same 12-hex-char length as :func:`_entity_id`. The relation label is
    normalized (:func:`_normalize_entity_name`) for the same reason entity
    names are: so "works_with" and " Works_With " hash to the same edge,
    and the same triple recorded independently on two machines converges to
    the same row.

    Args:
        subject_entity_id: The subject entity's id.
        relation: The relation label (any casing/whitespace).
        object_entity_id: The object entity's id.

    Returns:
        A 12-character hex string.
    """
    key = f"{subject_entity_id}|{_normalize_entity_name(relation)}|{object_entity_id}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _upsert_entity_relation(
    db: sqlite3.Connection,
    subject_entity_id: str,
    relation: str,
    object_entity_id: str,
    *,
    node_id: str | None = None,
    now: str | None = None,
) -> str:
    """Insert an entity-to-entity relation if it doesn't already exist.

    Unlike :func:`_upsert_entity` (which merges aliases/kind into an
    existing row), a relation triple's identity IS its content -- there is
    nothing to merge, so re-recording the same (subject, relation, object)
    is a no-op. Mirrors :func:`_link_memory_entity`'s immutable
    insert-or-ignore semantics. Does NOT commit.

    Args:
        db: An open SQLite connection.
        subject_entity_id: The subject entity's id.
        relation: The relation label (e.g. "works_with", "reports_to").
        object_entity_id: The object entity's id.
        node_id: This node's id, stamped on newly created rows.
        now: Timestamp override (defaults to now).

    Returns:
        The relation's deterministic id (whether newly created or already
        existing).
    """
    rid = _entity_relation_id(subject_entity_id, relation, object_entity_id)
    ts = now or _now_iso()
    db.execute(
        """INSERT OR IGNORE INTO entity_relations
           (id, subject_entity_id, relation, object_entity_id, created_at, updated_at, node_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (rid, subject_entity_id, " ".join(relation.split()), object_entity_id, ts, ts, node_id),
    )
    return rid


def _resolve_entity(db: sqlite3.Connection, query: str) -> dict[str, Any] | None:
    """Resolve a name or alias to its canonical entity row (FT-04 part 2).

    Resolution order:

      1. Deterministic-id lookup: ``_entity_id(query)`` — an indexed primary
         key hit whenever the query is the entity's canonical name (identity
         is case/whitespace-insensitive by construction).
      2. Fallback scan over all entities: case-insensitive canonical-name
         match first (defensive — ids are derived from names, so this only
         fires for rows whose stored name diverged from its id), then a
         match against the JSON ``aliases`` array.

    Args:
        db: An open SQLite connection.
        query: An entity name or alias (any casing/whitespace).

    Returns:
        The entity row as a dict (aliases deserialized), or None when nothing
        matches.
    """
    row = db.execute(
        "SELECT * FROM entities WHERE id = ?", (_entity_id(query),)
    ).fetchone()
    if row is not None:
        return _row_to_dict(row)

    norm = _normalize_entity_name(query)
    if not norm:
        return None
    alias_hit: dict[str, Any] | None = None
    for r in db.execute("SELECT * FROM entities").fetchall():
        d = _row_to_dict(r)
        if _normalize_entity_name(str(d["name"])) == norm:
            return d
        if alias_hit is None:
            aliases = d.get("aliases")
            if isinstance(aliases, list) and any(
                isinstance(a, str) and _normalize_entity_name(a) == norm
                for a in aliases
            ):
                alias_hit = d
    return alias_hit


def _entity_profile(
    db: sqlite3.Connection, query: str, limit: int = 20
) -> dict[str, Any] | None:
    """Build the full lookup payload for an entity: row + facts + memories.

    Shared by the ``remind_me_entity`` MCP tool and ``GET /api/entity``.
    Facts are non-superseded memories whose SPO subject or object equals the
    entity's canonical name (part 1 writes SPO values verbatim from the
    caller, so the comparison is case-insensitive — ``lower()`` on both
    sides; the canonical name is already whitespace-collapsed). Linked
    memories come from ``memory_entities`` via INNER joins, so dangling
    links (sync may deliver a link before its endpoints) are invisible.
    Superseded memories are excluded everywhere (DI-02).

    Args:
        db: An open SQLite connection.
        query: An entity name or alias (resolved via :func:`_resolve_entity`).
        limit: Maximum facts and maximum linked memories to return.

    Returns:
        Dict with ``entity``, ``facts``, ``memories``, and
        ``total_linked_memories`` keys, or None when the entity is unknown.
    """
    ent = _resolve_entity(db, query)
    if ent is None:
        return None

    canon = _normalize_entity_name(str(ent["name"]))
    fact_rows = db.execute(
        """SELECT id, content, subject, predicate, object, category, created_at
           FROM memories
           WHERE superseded_by IS NULL AND deleted_at IS NULL
             AND (lower(subject) = ? OR lower(object) = ?)
           ORDER BY created_at DESC
           LIMIT ?""",
        (canon, canon, limit),
    ).fetchall()

    memory_rows = db.execute(
        """SELECT m.id, substr(m.content, 1, 300) AS content_snippet,
                  m.category, m.created_at
           FROM memory_entities me
           JOIN memories m ON m.id = me.memory_id
           WHERE me.entity_id = ? AND m.superseded_by IS NULL AND m.deleted_at IS NULL
           ORDER BY m.created_at DESC
           LIMIT ?""",
        (ent["id"], limit),
    ).fetchall()
    total_linked = db.execute(
        """SELECT COUNT(*) AS cnt
           FROM memory_entities me
           JOIN memories m ON m.id = me.memory_id
           WHERE me.entity_id = ? AND m.superseded_by IS NULL AND m.deleted_at IS NULL""",
        (ent["id"],),
    ).fetchone()["cnt"]

    return {
        "entity": {
            k: ent.get(k)
            for k in ("id", "name", "kind", "aliases", "created_at", "updated_at")
        },
        "facts": [dict(r) for r in fact_rows],
        "memories": [dict(r) for r in memory_rows],
        "total_linked_memories": total_linked,
    }


def _list_entities(
    db: sqlite3.Connection, *, limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    """List entities, most-mentioned first, for the dashboard entity browser (issue #15).

    There's no equivalent MCP tool for this — ``remind_me_entity`` is a
    lookup-by-name/alias tool, browsing everything by list is a
    dashboard-only need — so this is used only by ``GET /api/entities``.

    Args:
        db: An open SQLite connection.
        limit: Maximum entities to return.
        offset: Pagination offset.

    Returns:
        Dict with the standard pagination envelope (``total``/``count``/
        ``offset``/``limit``/``has_more``, matching ``GET /api/memories``)
        plus ``entities`` — each with ``id``/``name``/``kind``/``aliases``/
        ``updated_at``/``mention_count`` (linked-memory count via
        ``memory_entities``, used to surface the most-referenced entities
        first rather than an arbitrary or purely alphabetical order).
    """
    total = db.execute("SELECT COUNT(*) AS cnt FROM entities").fetchone()["cnt"]
    rows = db.execute(
        """SELECT e.id, e.name, e.kind, e.aliases, e.updated_at,
                  COUNT(me.memory_id) AS mention_count
           FROM entities e
           LEFT JOIN memory_entities me ON me.entity_id = e.id
           GROUP BY e.id
           ORDER BY mention_count DESC, e.name ASC
           LIMIT ? OFFSET ?""",
        (limit, offset),
    ).fetchall()
    entities = [_row_to_dict(r) for r in rows]
    return {
        "total": total,
        "count": len(entities),
        "offset": offset,
        "limit": limit,
        "has_more": total > offset + limit,
        "entities": entities,
    }


# Default cap on relation edges returned by a traversal. Unlike the search
# expansion caps (which bound response cost against a token budget), this
# also bounds worst-case query volume across hops -- see EntityTraverseInput.
_RELATION_TRAVERSAL_CAP = 20


def _expand_via_entity_relations(
    db: sqlite3.Connection,
    seed_entity_ids: list[str],
    hops: int = 1,
    relation: str | None = None,
    cap: int = _RELATION_TRAVERSAL_CAP,
) -> list[dict[str, Any]]:
    """Breadth-first traversal of the typed entity-relation graph.

    Follows ``entity_relations`` edges in both directions (subject->object
    and object->subject) up to *hops* steps, so a traversal from "Bailey"
    surfaces both relations Bailey is the subject of and relations naming
    Bailey as the object. Each hop only queries the *newly* discovered
    entities from the previous hop (the seed-set stays out of later
    frontiers), so an edge is never refetched once both its endpoints have
    already been visited -- this is what makes the walk terminate on cycles
    without an explicit depth-first "seen" check per edge.

    Shared by the ``remind_me_entity_traverse`` MCP tool
    (tools/entity.py) and ``GET /api/entity/traverse`` (issue #15).

    Args:
        db: An open SQLite connection.
        seed_entity_ids: Entity ids to start the traversal from.
        hops: Maximum traversal depth (1-3 recommended; larger values are
            still safe -- bounded by *cap* and the shrinking frontier).
        relation: Optional exact-match filter on the relation label.
        cap: Maximum number of edges to return, across all hops.

    Returns:
        List of {subject_entity_id, subject_name, subject_kind, relation,
        object_entity_id, object_name, object_kind, hop} dicts, in
        breadth-first order (hop 1 edges first).
    """
    seen_entities: set[str] = set(seed_entity_ids)
    frontier: set[str] = set(seed_entity_ids)
    edges: list[dict[str, Any]] = []
    seen_edge_ids: set[str] = set()

    for hop in range(1, hops + 1):
        if not frontier or len(edges) >= cap:
            break
        placeholders = ",".join("?" * len(frontier))
        bindings: list[Any] = [*frontier, *frontier]
        relation_clause = ""
        if relation:
            relation_clause = " AND r.relation = ?"
            bindings.append(relation)

        rows = db.execute(
            f"""SELECT r.id, r.subject_entity_id, r.relation, r.object_entity_id,
                       s.name AS subject_name, s.kind AS subject_kind,
                       o.name AS object_name, o.kind AS object_kind
                FROM entity_relations r
                JOIN entities s ON s.id = r.subject_entity_id
                JOIN entities o ON o.id = r.object_entity_id
                WHERE (r.subject_entity_id IN ({placeholders})
                       OR r.object_entity_id IN ({placeholders})){relation_clause}
                ORDER BY r.created_at""",
            bindings,
        ).fetchall()

        next_frontier: set[str] = set()
        for r in rows:
            if r["id"] in seen_edge_ids:
                continue
            if len(edges) >= cap:
                break
            seen_edge_ids.add(r["id"])
            edges.append({
                "subject_entity_id": r["subject_entity_id"],
                "subject_name": r["subject_name"],
                "subject_kind": r["subject_kind"],
                "relation": r["relation"],
                "object_entity_id": r["object_entity_id"],
                "object_name": r["object_name"],
                "object_kind": r["object_kind"],
                "hop": hop,
            })
            for nbr in (r["subject_entity_id"], r["object_entity_id"]):
                if nbr not in seen_entities:
                    seen_entities.add(nbr)
                    next_frontier.add(nbr)
        frontier = next_frontier

    return edges


def _supersede_contradicting_facts(
    db: sqlite3.Connection,
    memory_id: str,
    subject: str | None,
    predicate: str | None,
    obj: str | None,
    now: str,
) -> list[str]:
    """Supersede facts a new/updated SPO triple contradicts (gap #5).

    Supersession previously only happened via similarity-merge
    (``remind_me_consolidate``) — near-duplicate memories get merged. Nothing
    let a genuinely contradictory update replace an old fact: "I moved to
    Boston" doesn't textually resemble "I live in Seattle" even though
    they're in direct conflict. This closes that gap deterministically using
    the SPO columns that already exist: another non-superseded, non-deleted
    memory that shares this triple's (subject, predicate) but has a
    *different* object is a contradiction and gets superseded by
    *memory_id* — the same ``superseded_by`` mechanism similarity-merge
    already uses, so every existing superseded-exclusion read path picks
    this up for free.

    Comparison uses :func:`_normalize_entity_name` (lowercase + whitespace-
    collapse, the same normalization the entity graph uses for identity) on
    all three fields in Python — not predicate-inference: "I live in
    Seattle" (predicate ``lives_in``) does NOT contradict "I visited
    Seattle" (predicate ``visited``) — the two facts simply don't share a
    predicate, so a differently-worded predicate for a related-but-distinct
    claim is a false-positive risk the caller (an LLM choosing predicate
    names) controls, not something this function tries to resolve.

    Args:
        db: An open SQLite connection. Does not commit.
        memory_id: The id of the new/updated memory whose triple is being
            checked — excluded from its own candidate search, and used as
            the new ``superseded_by`` target for anything it contradicts.
        subject: The triple's subject, or None/empty to skip (no-op).
        predicate: The triple's predicate, or None/empty to skip.
        obj: The triple's object, or None/empty to skip.
        now: Canonical timestamp written to a superseded row's ``updated_at``
            (so the change syncs, same as any other supersession).

    Returns:
        The ids of memories that were superseded by this call (0 or more).
    """
    if not subject or not predicate or not obj:
        return []
    subj_canon = _normalize_entity_name(subject)
    pred_canon = _normalize_entity_name(predicate)
    obj_canon = _normalize_entity_name(obj)

    # A plain SQL lower()/= comparison would miss internal-whitespace
    # variants (_normalize_entity_name also collapses those), so this only
    # narrows to non-null-triple candidates in SQL and does the exact
    # subject/predicate/object comparison in Python.
    rows = db.execute(
        """SELECT id, subject, predicate, object FROM memories
           WHERE id != ?
             AND superseded_by IS NULL AND deleted_at IS NULL
             AND subject IS NOT NULL AND predicate IS NOT NULL AND object IS NOT NULL""",
        (memory_id,),
    ).fetchall()

    superseded_ids: list[str] = []
    for row in rows:
        if (
            _normalize_entity_name(str(row["subject"])) != subj_canon
            or _normalize_entity_name(str(row["predicate"])) != pred_canon
        ):
            continue
        if _normalize_entity_name(str(row["object"])) == obj_canon:
            continue  # same fact restated verbatim, not a contradiction
        db.execute(
            "UPDATE memories SET superseded_by = ?, updated_at = ? WHERE id = ?",
            (memory_id, now, row["id"]),
        )
        superseded_ids.append(row["id"])
    return superseded_ids


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
    for key in ("tags", "metadata", "stats", "aliases"):
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
    "_delete_chunks",
    "_embed_and_store",
    "_embed_and_store_rows",
    "_embed_and_store_batch",
    "_prune_orphan_chunks",
    "_fuse_query_embedding",
    "_semantic_search",
    "_hydrate_ann_hits",
    "_now_iso",
    "_make_id",
    "_normalize_entity_name",
    "_entity_id",
    "_upsert_entity",
    "_link_memory_entity",
    "_entity_relation_id",
    "_upsert_entity_relation",
    "_resolve_entity",
    "_entity_profile",
    "_list_entities",
    "_expand_via_entity_relations",
    "_supersede_contradicting_facts",
    "_row_to_dict",
]
