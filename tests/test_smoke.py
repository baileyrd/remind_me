"""
Smoke tests that validate every shared fixture in conftest.py works correctly.

All tests are synchronous (no async needed for fixture validation).
These run as part of every CI run to catch fixture regressions early.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np


# ---------------------------------------------------------------------------
# db_conn fixture
# ---------------------------------------------------------------------------


def test_db_conn_has_schema(db_conn: sqlite3.Connection) -> None:
    """In-memory database should have memories, chat_imports, and memories_fts tables."""
    # memories table
    db_conn.execute("SELECT COUNT(*) FROM memories").fetchone()

    # chat_imports table
    db_conn.execute("SELECT COUNT(*) FROM chat_imports").fetchone()

    # FTS5 virtual table
    db_conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()


def test_db_conn_isolation(db_conn: sqlite3.Connection, memory_factory) -> None:
    """Each test function receives a fresh in-memory database (function-scoped fixture)."""
    # This test creates a memory and merely confirms the db works in isolation.
    mem = memory_factory(content="Isolation test memory")
    rows = db_conn.execute(
        "SELECT * FROM memories WHERE id = ?", (mem["id"],)
    ).fetchall()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# memory_factory fixture
# ---------------------------------------------------------------------------


def test_memory_factory_creates_row(db_conn: sqlite3.Connection, memory_factory) -> None:
    """memory_factory should insert a valid row and return a dict with all fields."""
    result = memory_factory(content="Hello world", category="test")

    # Returned dict has all required keys
    expected_keys = {"id", "content", "category", "tags", "source", "metadata", "created_at", "updated_at"}
    assert expected_keys.issubset(result.keys()), f"Missing keys: {expected_keys - result.keys()}"

    assert result["content"] == "Hello world"
    assert result["category"] == "test"

    # Row persisted in database
    row = db_conn.execute(
        "SELECT * FROM memories WHERE id = ?", (result["id"],)
    ).fetchone()
    assert row is not None, "Row not found in database after factory insert"


def test_memory_factory_defaults(db_conn: sqlite3.Connection, memory_factory) -> None:
    """memory_factory() with no overrides should use sensible default values."""
    result = memory_factory()

    assert result["category"] == "general"
    assert result["source"] == "manual"
    assert result["tags"] == []


# ---------------------------------------------------------------------------
# mock_embedder fixture
# ---------------------------------------------------------------------------


def test_mock_embedder_returns_vectors(mock_embedder) -> None:
    """mock_embedder.embed() should return a (1, 384) float32 array, deterministically."""
    vec1 = mock_embedder.embed(["hello"])
    assert vec1.shape == (1, 384), f"Expected shape (1, 384), got {vec1.shape}"
    assert vec1.dtype == np.float32, f"Expected float32, got {vec1.dtype}"

    # Same text produces identical vector
    vec2 = mock_embedder.embed(["hello"])
    np.testing.assert_array_equal(vec1, vec2, err_msg="Embedder is not deterministic")


def test_mock_embedder_embed_one_returns_bytes(mock_embedder) -> None:
    """mock_embedder.embed_one() should return raw bytes of length 384 * 4."""
    result = mock_embedder.embed_one("test")
    assert isinstance(result, bytes), f"Expected bytes, got {type(result)}"
    assert len(result) == 384 * 4, f"Expected {384 * 4} bytes, got {len(result)}"


# ---------------------------------------------------------------------------
# sample_chat_json fixture
# ---------------------------------------------------------------------------


def test_sample_chat_json_exists(sample_chat_json: Path) -> None:
    """sample_chat_json fixture should create a valid JSON file with chat_messages."""
    assert sample_chat_json.exists(), f"File does not exist: {sample_chat_json}"

    data = json.loads(sample_chat_json.read_text())
    assert "chat_messages" in data, "Expected 'chat_messages' key in JSON"
    assert len(data["chat_messages"]) == 2, f"Expected 2 messages, got {len(data['chat_messages'])}"


# ---------------------------------------------------------------------------
# sample_chat_md fixture
# ---------------------------------------------------------------------------


def test_sample_chat_md_exists(sample_chat_md: Path) -> None:
    """sample_chat_md fixture should create a valid Markdown file with ## Human header."""
    assert sample_chat_md.exists(), f"File does not exist: {sample_chat_md}"

    contents = sample_chat_md.read_text()
    assert "## Human" in contents, "Expected '## Human' header in Markdown"
