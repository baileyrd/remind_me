"""Tests for remind_me_mcp.dbs_import — the dbs (daily-backup-system) bulk importer."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

import pytest

import remind_me_mcp.dbs_import as _dbs_mod
from remind_me_mcp.dbs_import import pull_dbs

if TYPE_CHECKING:
    from pathlib import Path


def _make_dbs_db(db_path, items: list[dict]) -> None:
    """Create a tiny real SQLite database matching dbs's items/sources schema."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE sources (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE items (
            id              INTEGER PRIMARY KEY,
            source_id       INTEGER NOT NULL,
            external_id     TEXT NOT NULL,
            item_kind       TEXT NOT NULL,
            title           TEXT,
            url             TEXT,
            body            TEXT,
            tags_json       TEXT NOT NULL DEFAULT '[]',
            item_created_at TEXT,
            item_updated_at TEXT,
            content_hash    TEXT NOT NULL,
            deleted         INTEGER NOT NULL DEFAULT 0
        );
    """)
    sources = {item["source"] for item in items}
    for i, name in enumerate(sorted(sources), start=1):
        conn.execute("INSERT INTO sources (id, name) VALUES (?, ?)", (i, name))
    source_ids = {name: i for i, name in enumerate(sorted(sources), start=1)}
    for i, item in enumerate(items, start=1):
        conn.execute(
            """INSERT INTO items
               (id, source_id, external_id, item_kind, title, url, body, tags_json,
                item_created_at, item_updated_at, content_hash, deleted)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                i,
                source_ids[item["source"]],
                item["external_id"],
                item.get("item_kind", "link"),
                item.get("title"),
                item.get("url"),
                item.get("body"),
                json.dumps(item.get("tags", [])),
                item.get("created_at", "2026-01-01T00:00:00Z"),
                item.get("updated_at", "2026-01-01T00:00:00Z"),
                item["content_hash"],
                int(item.get("deleted", False)),
            ),
        )
    conn.commit()
    conn.close()


@pytest.fixture()
def fake_dbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tiny real dbs-shaped SQLite database with one live and one deleted item."""
    db_path = tmp_path / "dbs.sqlite3"
    _make_dbs_db(
        db_path,
        [
            {
                "source": "raindrop",
                "external_id": "1",
                "item_kind": "link",
                "title": "Cool Article",
                "url": "https://example.com/a",
                "body": "Some notes about it",
                "tags": ["ai", "reading"],
                "content_hash": "h1",
            },
            {
                "source": "raindrop",
                "external_id": "2",
                "item_kind": "link",
                "title": "Gone Link",
                "content_hash": "h2",
                "deleted": True,
            },
        ],
    )
    monkeypatch.setattr(_dbs_mod, "_embed_and_store_rows", lambda rows: 0)
    return db_path


def test_pull_dbs_imports_live_item_with_entities(db_conn: sqlite3.Connection, fake_dbs) -> None:
    """A live item becomes a memory with source and tags linked as entities."""
    result = pull_dbs(db_path=str(fake_dbs))

    assert result["fetched"] == 1  # the deleted item is excluded at the SQL level
    assert result["created"] == 1
    assert result["imported"] == 1

    row = db_conn.execute("SELECT id, content, category, tags, source, metadata FROM memories").fetchone()
    assert row["content"] == "Cool Article\n\nSome notes about it"
    assert row["category"] == "link"
    assert json.loads(row["tags"]) == ["ai", "reading"]
    assert row["source"] == "dbs:raindrop"
    metadata = json.loads(row["metadata"])
    assert metadata["dbs_source"] == "raindrop"
    assert metadata["dbs_external_id"] == "1"
    assert metadata["dbs_content_hash"] == "h1"

    entity_names = {
        r["name"]
        for r in db_conn.execute(
            """SELECT e.name FROM entities e
               JOIN memory_entities me ON me.entity_id = e.id
               WHERE me.memory_id = ?""",
            (row["id"],),
        ).fetchall()
    }
    assert entity_names == {"raindrop", "ai", "reading"}

    source_entity = db_conn.execute("SELECT kind FROM entities WHERE name = 'raindrop'").fetchone()
    assert source_entity["kind"] == "dbs_source"
    tag_entity = db_conn.execute("SELECT kind FROM entities WHERE name = 'ai'").fetchone()
    assert tag_entity["kind"] == "tag"


def test_pull_dbs_excludes_deleted_items(db_conn: sqlite3.Connection, fake_dbs) -> None:
    pull_dbs(db_path=str(fake_dbs))
    count = db_conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"]
    assert count == 1


def test_pull_dbs_rerun_is_idempotent(db_conn: sqlite3.Connection, fake_dbs) -> None:
    """Re-running an import over an unchanged item skips it instead of duplicating."""
    first = pull_dbs(db_path=str(fake_dbs))
    assert first["imported"] == 1

    second = pull_dbs(db_path=str(fake_dbs))
    assert second["imported"] == 0
    assert second["already_imported"] == 1

    count = db_conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"]
    assert count == 1


def test_pull_dbs_picks_up_edited_item(db_conn: sqlite3.Connection, fake_dbs) -> None:
    """An item edited after its first import gets a fresh, superseding memory.

    Unlike the file-export pipeline's item_created_at-only incremental cutoff
    (dbs's own docs/BACKLOG.md #4), this compares actual content_hash every
    time, so it has no equivalent staleness gap.
    """
    first = pull_dbs(db_path=str(fake_dbs))
    assert first["created"] == 1
    old_id = db_conn.execute("SELECT id FROM memories").fetchone()["id"]

    conn = sqlite3.connect(str(fake_dbs))
    conn.execute(
        "UPDATE items SET title = ?, content_hash = ? WHERE external_id = '1'",
        ("Cool Article (edited)", "h1-v2"),
    )
    conn.commit()
    conn.close()

    second = pull_dbs(db_path=str(fake_dbs))
    assert second["created"] == 0
    assert second["updated"] == 1
    assert second["imported"] == 1

    rows = db_conn.execute("SELECT id, content, superseded_by FROM memories ORDER BY created_at").fetchall()
    assert len(rows) == 2
    old_row = next(r for r in rows if r["id"] == old_id)
    new_row = next(r for r in rows if r["id"] != old_id)
    assert old_row["superseded_by"] == new_row["id"]
    assert new_row["content"].startswith("Cool Article (edited)")

    # dbs_imports now tracks the new memory/hash for this identity.
    tracked = db_conn.execute(
        "SELECT memory_id, content_hash FROM dbs_imports WHERE dbs_source='raindrop' AND external_id='1'"
    ).fetchone()
    assert tracked["memory_id"] == new_row["id"]
    assert tracked["content_hash"] == "h1-v2"


def test_pull_dbs_dry_run_writes_nothing(db_conn: sqlite3.Connection, fake_dbs) -> None:
    result = pull_dbs(db_path=str(fake_dbs), dry_run=True)

    assert result["to_import"] == 1
    assert result["imported"] == 0
    count = db_conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"]
    assert count == 0


def test_pull_dbs_source_and_item_type_filters(db_conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "dbs.sqlite3"
    _make_dbs_db(
        db_path,
        [
            {"source": "raindrop", "external_id": "1", "item_kind": "link", "title": "A", "content_hash": "h1"},
            {"source": "reddit", "external_id": "2", "item_kind": "post", "title": "B", "content_hash": "h2"},
        ],
    )
    monkeypatch.setattr(_dbs_mod, "_embed_and_store_rows", lambda rows: 0)

    by_source = pull_dbs(db_path=str(db_path), source="reddit")
    assert by_source["fetched"] == 1

    by_type = pull_dbs(db_path=str(db_path), item_type="link")
    assert by_type["fetched"] == 1


def test_pull_dbs_missing_db_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        pull_dbs(db_path=str(tmp_path / "does-not-exist.sqlite3"))


# ---------------------------------------------------------------------------
# Connector registration (Phase 4)
# ---------------------------------------------------------------------------


def test_dbs_registered_as_connector() -> None:
    """Importing this module registers 'dbs' in the shared registry, purely
    for discovery -- pull_dbs never calls through it."""
    import remind_me_mcp.importer as _importer_mod

    assert "dbs" in _importer_mod._CONNECTORS
    assert _importer_mod._CONNECTORS["dbs"] is _dbs_mod._dbs_connector


def test_dbs_not_reachable_via_import_chat_file() -> None:
    """'dbs' is registered for discovery only -- it's not a valid
    import_chat_file kind (IMPORT_KINDS stays narrower than _CONNECTORS)."""
    from remind_me_mcp.importer import IMPORT_KINDS

    assert "dbs" not in IMPORT_KINDS
