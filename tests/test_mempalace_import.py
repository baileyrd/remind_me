"""Tests for remind_me_mcp.mempalace_import — the MemPalace ChromaDB bulk importer."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

import remind_me_mcp.mempalace_import as _mempalace_mod
from remind_me_mcp.mempalace_import import pull_mempalace

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

NATIVE_CONTENT = (
    "---\n"
    "id: 6bb2c33ed386\n"
    "created: 2026-02-23T00:08:29.406417Z\n"
    "category: fact\n"
    "source: remind_me/manual\n"
    "tags: work, deadline, migration\n"
    "---\n"
    "\n"
    "Microsoft Project Online migration deadline is September 2026."
)
OPAQUE_CONTENT = "Random note about the Zed editor's LSP config."


@pytest.fixture()
def fake_palace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A tiny real ChromaDB persistent store standing in for a MemPalace palace."""
    chromadb = pytest.importorskip("chromadb")

    client = chromadb.PersistentClient(path=str(tmp_path / "palace"))
    collection = client.create_collection(_mempalace_mod.COLLECTION_NAME)
    collection.add(
        ids=["drawer_native_1", "drawer_opaque_1"],
        documents=[NATIVE_CONTENT, OPAQUE_CONTENT],
        metadatas=[
            {"wing": "remind_me", "room": "general"},
            {"wing": "zed", "room": "general"},
        ],
    )
    monkeypatch.setattr(_mempalace_mod, "MEMPALACE_PATH", tmp_path / "palace")
    monkeypatch.setattr(_mempalace_mod, "_embed_and_store_rows", lambda rows: 0)
    return collection


def test_pull_mempalace_restores_native_frontmatter(db_conn: sqlite3.Connection, fake_palace) -> None:
    """A drawer matching remind_me's own frontmatter gets its original fields back."""
    result = pull_mempalace(wing="remind_me")

    assert result["fetched"] == 1
    assert result["imported"] == 1
    assert result["native_format"] == 1

    row = db_conn.execute("SELECT content, category, tags, source FROM memories").fetchone()
    assert row["content"] == "Microsoft Project Online migration deadline is September 2026."
    assert row["category"] == "fact"
    assert json.loads(row["tags"]) == ["work", "deadline", "migration"]
    assert row["source"] == "mempalace:remind_me/manual"


def test_pull_mempalace_stores_opaque_content_with_wing_room_tags(db_conn: sqlite3.Connection, fake_palace) -> None:
    """A drawer with no recognizable frontmatter is stored as-is, tagged by wing/room."""
    result = pull_mempalace(wing="zed")

    assert result["imported"] == 1
    assert result["opaque_format"] == 1

    row = db_conn.execute("SELECT content, category, tags, source FROM memories").fetchone()
    assert row["content"] == OPAQUE_CONTENT
    assert row["category"] == "mempalace_import"
    assert json.loads(row["tags"]) == ["zed", "general"]
    assert row["source"] == "mempalace_import"


def test_pull_mempalace_rerun_is_idempotent(db_conn: sqlite3.Connection, fake_palace) -> None:
    """Re-running an import over the same drawers skips them instead of duplicating."""
    first = pull_mempalace()
    assert first["imported"] == 2

    second = pull_mempalace()
    assert second["imported"] == 0
    assert second["already_imported"] == 2

    count = db_conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"]
    assert count == 2


def test_pull_mempalace_dry_run_writes_nothing(db_conn: sqlite3.Connection, fake_palace) -> None:
    """dry_run reports what would happen without touching the database."""
    result = pull_mempalace(dry_run=True)

    assert result["to_import"] == 2
    assert result["imported"] == 0
    count = db_conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"]
    assert count == 0
