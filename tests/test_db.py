"""
Unit tests for remind_me_mcp.db utility functions and schema verification.

Uses the db_conn fixture from conftest.py which provides an in-memory
SQLite connection with the full schema (tables, FTS5, triggers, indexes).

FTS5 trigger tests use real in-memory SQLite — no mocks (TEST-06 requirement).
Migration tests also use real in-memory SQLite to validate PRAGMA user_version
handling, junction table creation, tag sync triggers, and idempotent re-runs.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from remind_me_mcp.db import _ensure_schema, _make_id, _migrate_schema, _now_iso, _row_to_dict

if TYPE_CHECKING:
    import sqlite3

# ---------------------------------------------------------------------------
# _now_iso
# ---------------------------------------------------------------------------


def test_now_iso_format() -> None:
    """_now_iso returns an ISO 8601 string containing 'T' and a timezone offset."""
    result = _now_iso()
    assert isinstance(result, str)
    assert "T" in result
    # Must have UTC timezone: either '+00:00' or 'Z' or equivalent
    assert "+" in result or result.endswith("Z")


def test_now_iso_utc() -> None:
    """_now_iso always returns UTC timezone (ends with '+00:00' or 'Z')."""
    result = _now_iso()
    # Python's datetime.now(timezone.utc).isoformat() produces '+00:00'
    assert result.endswith("+00:00") or result.endswith("Z")


# ---------------------------------------------------------------------------
# _make_id
# ---------------------------------------------------------------------------


def test_make_id_returns_12_chars() -> None:
    """_make_id returns exactly 12 characters."""
    result = _make_id("some content")
    assert len(result) == 12


def test_make_id_is_hex() -> None:
    """All characters in the returned ID are valid lowercase hex digits."""
    result = _make_id("some content")
    assert re.fullmatch(r"[0-9a-f]{12}", result) is not None


def test_make_id_different_content_different_id() -> None:
    """Two calls with different content produce different IDs.

    Note: _make_id includes a timestamp component so even the same content
    input at different times produces different IDs.  Here we rely on
    different content strings to maximise the chance of differing results,
    and accept that the timestamp alone would also cause divergence.
    """
    id1 = _make_id("content alpha")
    id2 = _make_id("content beta")
    # Statistically virtually impossible for these to collide
    assert id1 != id2


# ---------------------------------------------------------------------------
# _row_to_dict
# ---------------------------------------------------------------------------


def test_row_to_dict_deserializes_json_tags(db_conn: sqlite3.Connection) -> None:
    """Tags stored as a JSON string are deserialized to a Python list."""
    now = _now_iso()
    mem_id = _make_id("tags-test")
    db_conn.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (mem_id, "Tag test content", "general", '["python","test"]', "manual", "{}", now, now),
    )
    db_conn.commit()
    row = db_conn.execute("SELECT * FROM memories WHERE id = ?", (mem_id,)).fetchone()
    result = _row_to_dict(row)
    assert isinstance(result["tags"], list)
    assert result["tags"] == ["python", "test"]


def test_row_to_dict_deserializes_json_metadata(db_conn: sqlite3.Connection) -> None:
    """Metadata stored as a JSON string is deserialized to a Python dict."""
    now = _now_iso()
    mem_id = _make_id("metadata-test")
    db_conn.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (mem_id, "Meta test content", "general", "[]", "manual", '{"key":"val"}', now, now),
    )
    db_conn.commit()
    row = db_conn.execute("SELECT * FROM memories WHERE id = ?", (mem_id,)).fetchone()
    result = _row_to_dict(row)
    assert isinstance(result["metadata"], dict)
    assert result["metadata"] == {"key": "val"}


def test_row_to_dict_handles_invalid_json(db_conn: sqlite3.Connection) -> None:
    """Invalid JSON in tags field is left as-is (no crash)."""
    now = _now_iso()
    mem_id = _make_id("invalid-json-test")
    db_conn.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (mem_id, "Invalid JSON content", "general", "not json", "manual", "{}", now, now),
    )
    db_conn.commit()
    row = db_conn.execute("SELECT * FROM memories WHERE id = ?", (mem_id,)).fetchone()
    result = _row_to_dict(row)
    # Should not raise; tags stays as the raw string
    assert result["tags"] == "not json"


# ---------------------------------------------------------------------------
# _ensure_schema — table and index existence
# ---------------------------------------------------------------------------


def test_schema_creates_memories_table(db_conn: sqlite3.Connection) -> None:
    """The memories table exists and supports INSERT + SELECT."""
    now = _now_iso()
    mem_id = _make_id("schema-check")
    db_conn.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (mem_id, "Schema check", "general", "[]", "manual", "{}", now, now),
    )
    db_conn.commit()
    row = db_conn.execute("SELECT id FROM memories WHERE id = ?", (mem_id,)).fetchone()
    assert row is not None
    assert row["id"] == mem_id


def test_schema_creates_chat_imports_table(db_conn: sqlite3.Connection) -> None:
    """The chat_imports table exists and supports INSERT + SELECT."""
    now = _now_iso()
    imp_id = _make_id("import-check")
    db_conn.execute(
        """INSERT INTO chat_imports (import_id, filename, hash, imported_at, stats)
           VALUES (?, ?, ?, ?, ?)""",
        (imp_id, "test_file.json", "abc123", now, "{}"),
    )
    db_conn.commit()
    row = db_conn.execute("SELECT import_id FROM chat_imports WHERE import_id = ?", (imp_id,)).fetchone()
    assert row is not None


def test_schema_creates_fts_table(db_conn: sqlite3.Connection) -> None:
    """The memories_fts virtual table exists and is queryable."""
    # An FTS5 query that returns nothing is fine — we just verify it doesn't error
    rows = db_conn.execute(
        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'nonexistent_word_xyz'"
    ).fetchall()
    assert isinstance(rows, list)


def test_fts_trigger_on_insert(db_conn: sqlite3.Connection) -> None:
    """Inserting a memory makes it queryable via FTS5 MATCH."""
    now = _now_iso()
    mem_id = _make_id("fts-insert-test")
    db_conn.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (mem_id, "The quick brown fox", "general", "[]", "manual", "{}", now, now),
    )
    db_conn.commit()
    rows = db_conn.execute(
        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'quick'"
    ).fetchall()
    assert len(rows) >= 1


def test_fts_trigger_on_delete(db_conn: sqlite3.Connection) -> None:
    """Deleting a memory removes it from FTS5 results."""
    now = _now_iso()
    mem_id = _make_id("fts-delete-test")
    db_conn.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (mem_id, "Uniquewordfordeletetest", "general", "[]", "manual", "{}", now, now),
    )
    db_conn.commit()

    # Confirm it's findable before deletion
    before = db_conn.execute(
        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'Uniquewordfordeletetest'"
    ).fetchall()
    assert len(before) >= 1

    # Delete and confirm it's gone
    db_conn.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
    db_conn.commit()

    after = db_conn.execute(
        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'Uniquewordfordeletetest'"
    ).fetchall()
    assert len(after) == 0


def test_fts_trigger_on_update(db_conn: sqlite3.Connection) -> None:
    """Updating memory content updates FTS5: new content is findable, old is not."""
    now = _now_iso()
    mem_id = _make_id("fts-update-test")
    db_conn.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (mem_id, "Oldcontentxyz123", "general", "[]", "manual", "{}", now, now),
    )
    db_conn.commit()

    # Update the content
    db_conn.execute(
        "UPDATE memories SET content = ?, updated_at = ? WHERE id = ?",
        ("Newcontentabc456", _now_iso(), mem_id),
    )
    db_conn.commit()

    # Old word should no longer be in FTS
    old_rows = db_conn.execute(
        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'Oldcontentxyz123'"
    ).fetchall()
    assert len(old_rows) == 0

    # New word should be in FTS
    new_rows = db_conn.execute(
        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'Newcontentabc456'"
    ).fetchall()
    assert len(new_rows) >= 1


def test_schema_creates_indexes(db_conn: sqlite3.Connection) -> None:
    """Category, source, and created_at indexes exist on the memories table."""
    index_rows = db_conn.execute("PRAGMA index_list(memories)").fetchall()
    index_names = {row[1] for row in index_rows}  # index name is column 1
    assert "idx_memories_category" in index_names
    assert "idx_memories_source" in index_names
    assert "idx_memories_created" in index_names


# ---------------------------------------------------------------------------
# Migration system — PRAGMA user_version, capture_id, memory_tags
# ---------------------------------------------------------------------------


def test_migrate_schema_sets_user_version(db_conn: sqlite3.Connection) -> None:
    """_ensure_schema sets PRAGMA user_version to the current schema version."""
    from remind_me_mcp.db import _SCHEMA_VERSION
    version = db_conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == _SCHEMA_VERSION, f"Expected user_version {_SCHEMA_VERSION}, got {version}"


def test_capture_id_column_exists(db_conn: sqlite3.Connection) -> None:
    """The memories table has a capture_id column after schema creation."""
    columns = [row[1] for row in db_conn.execute("PRAGMA table_info(memories)").fetchall()]
    assert "capture_id" in columns, "capture_id column is missing from memories table"


def test_capture_id_index_exists(db_conn: sqlite3.Connection) -> None:
    """An index named idx_memories_capture_id exists on the memories table."""
    indexes = [
        row[0]
        for row in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    ]
    assert "idx_memories_capture_id" in indexes, "idx_memories_capture_id index is missing"


def test_memory_tags_table_exists(db_conn: sqlite3.Connection) -> None:
    """The memory_tags junction table exists with memory_id and tag columns."""
    tables = [
        row[0]
        for row in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    assert "memory_tags" in tables, "memory_tags table is missing"

    tag_columns = [
        row[1] for row in db_conn.execute("PRAGMA table_info(memory_tags)").fetchall()
    ]
    assert "memory_id" in tag_columns, "memory_id column missing from memory_tags"
    assert "tag" in tag_columns, "tag column missing from memory_tags"


def test_memory_tags_populated_on_insert(db_conn: sqlite3.Connection) -> None:
    """Inserting a memory with JSON tags populates memory_tags via the after-insert trigger."""
    now = _now_iso()
    mem_id = _make_id("tags-junction-insert")
    db_conn.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (mem_id, "Tag junction insert test", "general", '["python","async"]', "manual", "{}", now, now),
    )
    db_conn.commit()

    rows = db_conn.execute(
        "SELECT tag FROM memory_tags WHERE memory_id = ? ORDER BY tag", (mem_id,)
    ).fetchall()
    tags_found = {row[0] for row in rows}
    assert tags_found == {"python", "async"}, f"Expected python and async, got {tags_found}"


def test_memory_tags_updated_on_tag_change(db_conn: sqlite3.Connection) -> None:
    """Updating the tags column replaces memory_tags rows for that memory."""
    now = _now_iso()
    mem_id = _make_id("tags-junction-update")
    db_conn.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (mem_id, "Tag junction update test", "general", '["a","b"]', "manual", "{}", now, now),
    )
    db_conn.commit()

    db_conn.execute(
        "UPDATE memories SET tags = ?, updated_at = ? WHERE id = ?",
        ('["b","c"]', _now_iso(), mem_id),
    )
    db_conn.commit()

    rows = db_conn.execute(
        "SELECT tag FROM memory_tags WHERE memory_id = ? ORDER BY tag", (mem_id,)
    ).fetchall()
    tags_found = {row[0] for row in rows}
    assert tags_found == {"b", "c"}, f"Expected b and c after update, got {tags_found}"


def test_memory_tags_deleted_on_memory_delete(db_conn: sqlite3.Connection) -> None:
    """Deleting a memory removes its rows from memory_tags via the after-delete trigger."""
    now = _now_iso()
    mem_id = _make_id("tags-junction-delete")
    db_conn.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (mem_id, "Tag junction delete test", "general", '["x","y"]', "manual", "{}", now, now),
    )
    db_conn.commit()

    # Confirm rows exist before deletion
    before = db_conn.execute(
        "SELECT tag FROM memory_tags WHERE memory_id = ?", (mem_id,)
    ).fetchall()
    assert len(before) == 2, f"Expected 2 tag rows before delete, got {len(before)}"

    db_conn.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
    db_conn.commit()

    after = db_conn.execute(
        "SELECT tag FROM memory_tags WHERE memory_id = ?", (mem_id,)
    ).fetchall()
    assert len(after) == 0, f"Expected 0 tag rows after delete, got {len(after)}"


def test_capture_id_backfill_from_metadata(db_conn: sqlite3.Connection) -> None:
    """Running _migrate_schema on a db with metadata capture_id backfills the column.

    Simulates an upgrade scenario: insert a row with capture_id=NULL and
    capture_id stored in the metadata JSON, then call _migrate_schema again.
    The column should be populated from the JSON metadata.
    """
    now = _now_iso()
    mem_id = _make_id("backfill-capture-id")

    # Insert with capture_id NULL and capture_id inside metadata JSON.
    db_conn.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at, capture_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
        (
            mem_id,
            "Backfill test content",
            "general",
            "[]",
            "manual",
            '{"capture_id": "abc123"}',
            now,
            now,
        ),
    )
    db_conn.commit()

    # Force user_version back to 0 to re-run v0->v1 migration (backfill step).
    db_conn.execute("PRAGMA user_version = 0")
    db_conn.commit()
    _migrate_schema(db_conn)

    row = db_conn.execute(
        "SELECT capture_id FROM memories WHERE id = ?", (mem_id,)
    ).fetchone()
    assert row is not None, "Memory row not found after migration"
    assert row[0] == "abc123", f"Expected capture_id='abc123', got {row[0]!r}"


def test_migration_idempotent(db_conn: sqlite3.Connection) -> None:
    """Running _ensure_schema twice on the same database does not raise and keeps user_version stable."""
    from remind_me_mcp.db import _SCHEMA_VERSION
    # db_conn fixture has already run _ensure_schema once via the fixture setup.
    # Run it again to verify idempotency.
    _ensure_schema(db_conn)

    version = db_conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == _SCHEMA_VERSION, f"user_version should still be {_SCHEMA_VERSION} after re-run, got {version}"


# ---------------------------------------------------------------------------
# Migration v4 -> v5 — decay, vitality, and classification columns
# ---------------------------------------------------------------------------


def test_v4_to_v5_schema_version_is_5(db_conn: sqlite3.Connection) -> None:
    """After migration, _SCHEMA_VERSION is at least 5 (later migrations may bump it further)."""
    from remind_me_mcp.db import _SCHEMA_VERSION
    assert _SCHEMA_VERSION >= 5
    version = db_conn.execute("PRAGMA user_version").fetchone()[0]
    assert version >= 5, f"Expected user_version >= 5, got {version}"


def test_v4_to_v5_new_columns_exist(db_conn: sqlite3.Connection) -> None:
    """Fresh database has all 7 new columns from v4->v5 migration."""
    columns = [row[1] for row in db_conn.execute("PRAGMA table_info(memories)").fetchall()]
    for col in ("accessed_at", "access_count", "decay_rate", "vitality", "base_weight", "status", "memory_type"):
        assert col in columns, f"Column {col} missing from memories table"


def test_v4_to_v5_defaults(db_conn: sqlite3.Connection) -> None:
    """Migration from v4 sets sensible defaults for new columns."""
    now = _now_iso()
    mem_id = _make_id("v5-defaults-test")
    db_conn.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (mem_id, "Defaults test", "general", "[]", "manual", "{}", now, now),
    )
    db_conn.commit()
    row = db_conn.execute(
        "SELECT vitality, base_weight, decay_rate, access_count, status, memory_type FROM memories WHERE id = ?",
        (mem_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == 1.0, f"Expected vitality=1.0, got {row[0]}"
    assert row[1] == 1.0, f"Expected base_weight=1.0, got {row[1]}"
    assert row[2] == 0.1, f"Expected decay_rate=0.1, got {row[2]}"
    assert row[3] == 0, f"Expected access_count=0, got {row[3]}"
    assert row[4] == "active", f"Expected status='active', got {row[4]}"
    assert row[5] == "unclassified", f"Expected memory_type='unclassified', got {row[5]}"


def test_v4_to_v5_accessed_at_backfill(db_conn: sqlite3.Connection) -> None:
    """accessed_at is backfilled from created_at for existing records."""
    now = _now_iso()
    mem_id = _make_id("v5-backfill-test")
    db_conn.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (mem_id, "Backfill test", "general", "[]", "manual", "{}", now, now),
    )
    db_conn.commit()

    # Force re-run of migration to simulate upgrade from v4
    db_conn.execute("PRAGMA user_version = 4")
    db_conn.commit()
    # Set accessed_at to NULL to simulate pre-v5 record
    db_conn.execute("UPDATE memories SET accessed_at = NULL WHERE id = ?", (mem_id,))
    db_conn.commit()

    _migrate_schema(db_conn)

    row = db_conn.execute(
        "SELECT accessed_at, created_at FROM memories WHERE id = ?", (mem_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == row[1], f"accessed_at should equal created_at after backfill, got {row[0]!r} vs {row[1]!r}"


def test_v4_to_v5_indexes_exist(db_conn: sqlite3.Connection) -> None:
    """Index exists on status, memory_type, and vitality columns."""
    indexes = [
        row[0]
        for row in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    ]
    assert "idx_memories_status" in indexes, "idx_memories_status index is missing"
    assert "idx_memories_memory_type" in indexes, "idx_memories_memory_type index is missing"
    assert "idx_memories_vitality" in indexes, "idx_memories_vitality index is missing"


# ---------------------------------------------------------------------------
# Migration v6 -> v7 — subject/predicate/object/superseded_by columns
# ---------------------------------------------------------------------------


def test_schema_version_is_current(db_conn: sqlite3.Connection) -> None:
    """After migration, _SCHEMA_VERSION and PRAGMA user_version match the latest."""
    from remind_me_mcp.db import _SCHEMA_VERSION
    assert _SCHEMA_VERSION == 8
    version = db_conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 8, f"Expected user_version 8, got {version}"


def test_v6_to_v7_new_columns_exist(db_conn: sqlite3.Connection) -> None:
    """Fresh database has subject, predicate, object, superseded_by columns from v6->v7."""
    columns = [row[1] for row in db_conn.execute("PRAGMA table_info(memories)").fetchall()]
    for col in ("subject", "predicate", "object", "superseded_by"):
        assert col in columns, f"Column {col} missing from memories table"


def test_v6_to_v7_columns_default_null(db_conn: sqlite3.Connection) -> None:
    """New columns default to NULL for existing memories."""
    now = _now_iso()
    mem_id = _make_id("v7-defaults-test")
    db_conn.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (mem_id, "V7 defaults test", "general", "[]", "manual", "{}", now, now),
    )
    db_conn.commit()
    row = db_conn.execute(
        "SELECT subject, predicate, object, superseded_by FROM memories WHERE id = ?",
        (mem_id,),
    ).fetchone()
    assert row is not None
    assert row[0] is None, f"Expected subject=NULL, got {row[0]}"
    assert row[1] is None, f"Expected predicate=NULL, got {row[1]}"
    assert row[2] is None, f"Expected object=NULL, got {row[2]}"
    assert row[3] is None, f"Expected superseded_by=NULL, got {row[3]}"


def test_v6_to_v7_subject_index_exists(db_conn: sqlite3.Connection) -> None:
    """Index idx_memories_subject exists on the subject column."""
    indexes = [
        row[0]
        for row in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    ]
    assert "idx_memories_subject" in indexes, "idx_memories_subject index is missing"


def test_v6_to_v7_memory_type_index_still_present(db_conn: sqlite3.Connection) -> None:
    """Index idx_memories_memory_type from v5 is still present after v7 migration."""
    indexes = [
        row[0]
        for row in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    ]
    assert "idx_memories_memory_type" in indexes, "idx_memories_memory_type index missing after v7"


def test_v6_to_v7_insert_with_subject_predicate_object(db_conn: sqlite3.Connection) -> None:
    """Can INSERT a memory with subject/predicate/object triple values."""
    now = _now_iso()
    mem_id = _make_id("v7-spo-test")
    db_conn.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at,
                                 subject, predicate, object)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (mem_id, "Bailey prefers dark mode", "preference", "[]", "decomposition", "{}",
         now, now, "Bailey", "prefers", "dark mode"),
    )
    db_conn.commit()
    row = db_conn.execute(
        "SELECT subject, predicate, object FROM memories WHERE id = ?", (mem_id,)
    ).fetchone()
    assert row[0] == "Bailey", f"Expected subject='Bailey', got {row[0]}"
    assert row[1] == "prefers", f"Expected predicate='prefers', got {row[1]}"
    assert row[2] == "dark mode", f"Expected object='dark mode', got {row[2]}"


def test_v6_to_v7_update_superseded_by(db_conn: sqlite3.Connection) -> None:
    """Can UPDATE superseded_by to point to a newer memory ID."""
    now = _now_iso()
    old_id = _make_id("v7-old-fact")
    new_id = _make_id("v7-new-fact")

    for mid, content in [(old_id, "Old fact"), (new_id, "New fact")]:
        db_conn.execute(
            """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at,
                                     subject, predicate, object)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (mid, content, "fact", "[]", "decomposition", "{}", now, now, "Bailey", "prefers", "dark mode"),
        )
    db_conn.commit()

    db_conn.execute("UPDATE memories SET superseded_by = ? WHERE id = ?", (new_id, old_id))
    db_conn.commit()

    row = db_conn.execute("SELECT superseded_by FROM memories WHERE id = ?", (old_id,)).fetchone()
    assert row[0] == new_id, f"Expected superseded_by='{new_id}', got {row[0]}"


def test_v6_to_v7_outbox_triggers_include_new_columns(db_conn: sqlite3.Connection) -> None:
    """Outbox triggers include subject, predicate, object, superseded_by in JSON payload."""
    now = _now_iso()
    mem_id = _make_id("v7-outbox-test")
    db_conn.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at,
                                 subject, predicate, object, superseded_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (mem_id, "Outbox test", "general", "[]", "manual", "{}", now, now,
         "Alice", "likes", "Python", None),
    )
    db_conn.commit()

    outbox_row = db_conn.execute(
        "SELECT payload FROM sync_outbox WHERE memory_id = ? ORDER BY id DESC LIMIT 1",
        (mem_id,),
    ).fetchone()
    assert outbox_row is not None, "No outbox row found for insert"
    import json
    payload = json.loads(outbox_row[0])
    assert "subject" in payload, "subject missing from outbox payload"
    assert "predicate" in payload, "predicate missing from outbox payload"
    assert "object" in payload, "object missing from outbox payload"
    assert "superseded_by" in payload, "superseded_by missing from outbox payload"
    assert payload["subject"] == "Alice"
    assert payload["predicate"] == "likes"
    assert payload["object"] == "Python"
