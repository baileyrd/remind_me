"""
Unit tests for remind_me_mcp.db utility functions and schema verification.

Uses the db_conn fixture from conftest.py which provides an in-memory
SQLite connection with the full schema (tables, FTS5, triggers, indexes).

FTS5 trigger tests use real in-memory SQLite — no mocks (TEST-06 requirement).
"""

from __future__ import annotations

import json
import re
import sqlite3

import pytest

from remind_me_mcp.db import _ensure_schema, _make_id, _now_iso, _row_to_dict


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
