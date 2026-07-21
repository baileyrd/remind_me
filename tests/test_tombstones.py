"""
Tests for the deleted_at tombstone / delete-propagation feature (gap #11).

Covers the v15->v16 schema migration, memory_delete's soft-vs-hard-delete
split (config.SYNC_ENABLED), read-path exclusion of tombstoned memories
(MCP tools and the REST API), sync's apply-path handling of an incoming
tombstone (skip embedding, clean up chunks), and the tombstone compaction
pass.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from remind_me_mcp.db import _ensure_schema, _now_iso
from remind_me_mcp.models import (
    MemoryDeleteInput,
    MemoryListInput,
    MemorySearchInput,
    MemoryUpdateInput,
)
from remind_me_mcp.tools import memory_delete, memory_get, memory_list, memory_search, memory_update

# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


def test_v15_to_v16_adds_deleted_at_column(db_conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in db_conn.execute("PRAGMA table_info(memories)").fetchall()}
    assert "deleted_at" in cols


def test_v15_to_v16_is_idempotent() -> None:
    """Running the migration on an already-migrated DB (or twice) doesn't error."""
    from remind_me_mcp.db import _migrate_schema

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    _ensure_schema(db)
    _migrate_schema(db)  # second run — must be a safe no-op
    cols = {r["name"] for r in db.execute("PRAGMA table_info(memories)").fetchall()}
    assert "deleted_at" in cols
    db.close()


def test_deleted_at_rides_the_update_outbox_trigger(db_conn: sqlite3.Connection) -> None:
    """A soft-delete UPDATE produces a normal outbox row whose payload carries
    deleted_at -- no new trigger/operation type needed."""
    db_conn.execute(
        "INSERT OR REPLACE INTO sync_flags (key, value) VALUES ('sync_enabled', '1')"
    )
    now = _now_iso()
    db_conn.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
           VALUES ('m1', 'content', 'general', '[]', 'manual', '{}', ?, ?)""",
        (now, now),
    )
    db_conn.commit()

    later = _now_iso()
    db_conn.execute(
        "UPDATE memories SET deleted_at = ?, updated_at = ? WHERE id = 'm1'", (later, later)
    )
    db_conn.commit()

    row = db_conn.execute(
        "SELECT operation, payload FROM sync_outbox WHERE memory_id = 'm1' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row["operation"] == "update"
    payload = json.loads(row["payload"])
    assert payload["deleted_at"] == later


# ---------------------------------------------------------------------------
# memory_delete (crud.py) — soft vs hard delete
# ---------------------------------------------------------------------------


async def test_memory_delete_hard_deletes_row_when_sync_disabled(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """Default (no sync configured): delete behaves exactly as before -- the
    row is truly gone, not just excluded."""
    mem = memory_factory(content="hard delete me")
    result = await memory_delete(MemoryDeleteInput(memory_id=mem["id"]))
    assert "deleted" in result.lower()

    row = db_conn.execute("SELECT * FROM memories WHERE id = ?", (mem["id"],)).fetchone()
    assert row is None


async def test_memory_delete_soft_deletes_when_sync_enabled(
    db_conn: sqlite3.Connection, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With sync configured: the row survives as a tombstone (deleted_at set),
    not removed -- that's what lets the deletion propagate."""
    import remind_me_mcp.tools.crud as crud_mod

    monkeypatch.setattr(crud_mod, "SYNC_ENABLED", True)

    mem = memory_factory(content="soft delete me")
    result = await memory_delete(MemoryDeleteInput(memory_id=mem["id"]))
    assert "deleted" in result.lower()

    row = db_conn.execute("SELECT * FROM memories WHERE id = ?", (mem["id"],)).fetchone()
    assert row is not None
    assert row["deleted_at"] is not None
    assert row["updated_at"] is not None


async def test_memory_delete_soft_deleted_memory_excluded_from_get(
    db_conn: sqlite3.Connection, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import remind_me_mcp.tools.crud as crud_mod

    monkeypatch.setattr(crud_mod, "SYNC_ENABLED", True)
    mem = memory_factory(content="soft deleted, should 404-equivalent")
    await memory_delete(MemoryDeleteInput(memory_id=mem["id"]))

    result = await memory_get(mem["id"])
    assert "not found" in result.lower()


async def test_memory_delete_soft_deleted_memory_excluded_from_list(
    db_conn: sqlite3.Connection, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import remind_me_mcp.tools.crud as crud_mod

    monkeypatch.setattr(crud_mod, "SYNC_ENABLED", True)
    mem = memory_factory(content="soft deleted, should not list")
    memory_factory(content="a different, kept memory")
    await memory_delete(MemoryDeleteInput(memory_id=mem["id"]))

    result = await memory_list(MemoryListInput(limit=50, offset=0))
    assert mem["id"] not in result
    assert "kept memory" in result


async def test_memory_delete_already_deleted_reports_not_found(
    db_conn: sqlite3.Connection, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting an already-tombstoned memory is idempotent-looking: 'not
    found', not a second successful delete."""
    import remind_me_mcp.tools.crud as crud_mod

    monkeypatch.setattr(crud_mod, "SYNC_ENABLED", True)
    mem = memory_factory(content="delete me twice")
    first = await memory_delete(MemoryDeleteInput(memory_id=mem["id"]))
    assert "deleted" in first.lower()

    second = await memory_delete(MemoryDeleteInput(memory_id=mem["id"]))
    assert "not found" in second.lower()


async def test_memory_delete_soft_delete_cleans_up_chunk_vectors(
    db_conn_with_vec: sqlite3.Connection, mock_embedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A soft delete still cleans up the (now-pointless) chunk vectors, same
    as a hard delete -- the row persists as a tombstone, but its embeddings
    don't need to."""
    import remind_me_mcp.tools.crud as crud_mod
    from remind_me_mcp.models import MemoryAddInput
    from remind_me_mcp.tools import memory_add

    monkeypatch.setattr(crud_mod, "SYNC_ENABLED", True)

    await memory_add(MemoryAddInput(content="chunked memory to soft delete"))
    row = db_conn_with_vec.execute("SELECT rowid, id FROM memories").fetchone()
    assert (
        db_conn_with_vec.execute(
            "SELECT COUNT(*) FROM vec_chunks WHERE memory_rowid = ?", (row["rowid"],)
        ).fetchone()[0]
        > 0
    )

    result = await memory_delete(MemoryDeleteInput(memory_id=row["id"]))
    assert "deleted" in result

    assert db_conn_with_vec.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0] == 0
    assert db_conn_with_vec.execute("SELECT COUNT(*) FROM memories_vec").fetchone()[0] == 0
    # The tombstone row itself must still exist (unlike a hard delete).
    still_there = db_conn_with_vec.execute(
        "SELECT deleted_at FROM memories WHERE id = ?", (row["id"],)
    ).fetchone()
    assert still_there is not None
    assert still_there["deleted_at"] is not None


async def test_memory_update_excludes_soft_deleted(
    db_conn: sqlite3.Connection, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tombstoned memory can't be resurrected via update — it reports
    'not found', same as get/list."""
    import remind_me_mcp.tools.crud as crud_mod

    monkeypatch.setattr(crud_mod, "SYNC_ENABLED", True)
    mem = memory_factory(content="deleted then updated?")
    await memory_delete(MemoryDeleteInput(memory_id=mem["id"]))

    result = await memory_update(
        MemoryUpdateInput(memory_id=mem["id"], content="resurrected content")
    )
    assert "not found" in result.lower()


async def test_memory_search_excludes_soft_deleted(
    db_conn: sqlite3.Connection, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import remind_me_mcp.tools.crud as crud_mod

    monkeypatch.setattr(crud_mod, "SYNC_ENABLED", True)
    mem = memory_factory(content="unique zephyr gadget memory")
    await memory_delete(MemoryDeleteInput(memory_id=mem["id"]))

    result = await memory_search(MemorySearchInput(query="zephyr gadget"))
    assert mem["id"] not in result


# ---------------------------------------------------------------------------
# sync.py apply-path — _upsert_one / _upsert_records
# ---------------------------------------------------------------------------


class _FakeSyncEmbedder:
    """Never actually used by these tests (no embed_rows should be built for
    a tombstoned record), but present so _get_embedder() returns non-None."""

    def embed(self, texts, *, role="passage"):
        raise AssertionError("a tombstoned record must never be embedded")

    def embed_one(self, text, *, role="passage"):
        raise AssertionError("a tombstoned record must never be embedded")


@pytest.fixture()
def sync_db_tombstones(monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    """Like test_sync.py's sync_db fixture, but with sqlite-vec loaded (for
    the chunk-cleanup path) and the embedder patchable per-test."""
    import remind_me_mcp.db as _db_mod
    import remind_me_mcp.sync as sync

    sqlite_vec = pytest.importorskip("sqlite_vec", reason="sqlite-vec not installed")

    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    _ensure_schema(db)
    db.execute(
        "INSERT OR REPLACE INTO sync_flags (key, value) VALUES ('sync_enabled', '1')"
    )
    db.commit()

    monkeypatch.setattr(_db_mod, "_get_db", lambda: db)
    monkeypatch.setattr(sync, "_get_db", lambda: db)
    monkeypatch.setattr(_db_mod, "_get_embedder", lambda: None)

    yield db
    db.close()


def _insert_memory(db: sqlite3.Connection, mem_id: str, content: str = "content") -> None:
    now = _now_iso()
    db.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
           VALUES (?, ?, 'general', '[]', 'manual', '{}', ?, ?)""",
        (mem_id, content, now, now),
    )
    db.commit()


def _make_record(mem_id: str, content: str = "remote content", **overrides) -> dict:
    now = _now_iso()
    rec = {
        "id": mem_id,
        "content": content,
        "category": "general",
        "tags": [],
        "source": "manual",
        "metadata": {},
        "created_at": now,
        "updated_at": now,
        "capture_id": None,
        "node_id": "remote-node",
    }
    rec.update(overrides)
    return rec


def test_upsert_one_applies_deleted_at_via_lww(sync_db_tombstones: sqlite3.Connection) -> None:
    from remind_me_mcp.sync import _upsert_one

    _insert_memory(sync_db_tombstones, "m1")
    later = _now_iso()
    rec = _make_record("m1", updated_at=later, deleted_at=later)

    rowid = _upsert_one(sync_db_tombstones, rec)
    assert rowid is not None

    row = sync_db_tombstones.execute(
        "SELECT deleted_at FROM memories WHERE id = 'm1'"
    ).fetchone()
    assert row["deleted_at"] == later


def test_upsert_one_stale_tombstone_loses_lww(sync_db_tombstones: sqlite3.Connection) -> None:
    """An incoming tombstone with an OLDER updated_at than the local copy
    must not resurrect-by-overwrite -- LWW still governs a delete exactly
    like any other field change."""
    from remind_me_mcp.sync import _upsert_one

    now = datetime.now(UTC)
    _insert_memory(sync_db_tombstones, "m1")
    fresh_local_update = (now + timedelta(days=1)).isoformat()
    sync_db_tombstones.execute(
        "UPDATE memories SET content = 'edited locally after the delete', updated_at = ? WHERE id = 'm1'",
        (fresh_local_update,),
    )
    sync_db_tombstones.commit()

    stale_tombstone = _make_record(
        "m1",
        updated_at=(now - timedelta(days=1)).isoformat(),
        deleted_at=(now - timedelta(days=1)).isoformat(),
    )
    rowid = _upsert_one(sync_db_tombstones, stale_tombstone)
    assert rowid is None  # lost LWW

    row = sync_db_tombstones.execute(
        "SELECT content, deleted_at FROM memories WHERE id = 'm1'"
    ).fetchone()
    assert row["deleted_at"] is None
    assert row["content"] == "edited locally after the delete"


def test_upsert_records_skips_embedding_tombstoned_record(
    sync_db_tombstones: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import remind_me_mcp.db as _db_mod
    from remind_me_mcp.sync import _upsert_records

    monkeypatch.setattr(_db_mod, "_get_embedder", lambda: _FakeSyncEmbedder())

    now = _now_iso()
    rec = _make_record("new-tombstone", updated_at=now, deleted_at=now)
    result = _upsert_records(sync_db_tombstones, [rec])

    assert result.applied == 1
    row = sync_db_tombstones.execute(
        "SELECT deleted_at FROM memories WHERE id = 'new-tombstone'"
    ).fetchone()
    assert row["deleted_at"] == now
    # _FakeSyncEmbedder.embed/embed_one raise AssertionError if ever called;
    # reaching here without one confirms the record was never embedded.


def test_upsert_records_cleans_up_chunks_for_incoming_tombstone(
    sync_db_tombstones: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A memory that already has chunk vectors locally gets them removed once
    a remote tombstone for it is applied."""
    import remind_me_mcp.db as _db_mod
    from remind_me_mcp import ann_index
    from remind_me_mcp.sync import _upsert_records

    monkeypatch.setattr(_db_mod, "_get_embedder", lambda: _FakeSyncEmbedder())

    _insert_memory(sync_db_tombstones, "m1", "will be tombstoned")
    rowid = sync_db_tombstones.execute(
        "SELECT rowid FROM memories WHERE id = 'm1'"
    ).fetchone()[0]
    # Simulate this memory already having chunk vectors (as if embedded
    # earlier, before the remote tombstone arrives).
    cur = sync_db_tombstones.execute(
        "INSERT INTO memories_vec(embedding) VALUES (?)",
        (b"\x00" * (384 * 4),),
    )
    vec_rowid = cur.lastrowid
    sync_db_tombstones.execute(
        "INSERT INTO vec_chunks(vec_rowid, memory_rowid, chunk_ix) VALUES (?, ?, 0)",
        (vec_rowid, rowid),
    )
    sync_db_tombstones.commit()
    ann_index.reset_for_tests()

    later = _now_iso()
    rec = _make_record("m1", updated_at=later, deleted_at=later)
    result = _upsert_records(sync_db_tombstones, [rec])

    assert result.applied == 1
    assert (
        sync_db_tombstones.execute(
            "SELECT COUNT(*) FROM vec_chunks WHERE memory_rowid = ?", (rowid,)
        ).fetchone()[0]
        == 0
    )


# ---------------------------------------------------------------------------
# _compact_tombstones
# ---------------------------------------------------------------------------


def test_compact_tombstones_removes_old_tombstones(
    sync_db_tombstones: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import remind_me_mcp.config as _cfg
    from remind_me_mcp.sync import _compact_tombstones

    monkeypatch.setattr(_cfg, "TOMBSTONE_RETENTION_DAYS", 30)

    old_ts = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    _insert_memory(sync_db_tombstones, "old-tombstone")
    sync_db_tombstones.execute(
        "UPDATE memories SET deleted_at = ? WHERE id = 'old-tombstone'", (old_ts,)
    )
    sync_db_tombstones.commit()

    removed = _compact_tombstones(sync_db_tombstones)
    assert removed == 1
    row = sync_db_tombstones.execute(
        "SELECT * FROM memories WHERE id = 'old-tombstone'"
    ).fetchone()
    assert row is None  # truly gone now, not just tombstoned


def test_compact_tombstones_keeps_recent_tombstones(
    sync_db_tombstones: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import remind_me_mcp.config as _cfg
    from remind_me_mcp.sync import _compact_tombstones

    monkeypatch.setattr(_cfg, "TOMBSTONE_RETENTION_DAYS", 180)

    recent_ts = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    _insert_memory(sync_db_tombstones, "recent-tombstone")
    sync_db_tombstones.execute(
        "UPDATE memories SET deleted_at = ? WHERE id = 'recent-tombstone'", (recent_ts,)
    )
    sync_db_tombstones.commit()

    removed = _compact_tombstones(sync_db_tombstones)
    assert removed == 0
    row = sync_db_tombstones.execute(
        "SELECT * FROM memories WHERE id = 'recent-tombstone'"
    ).fetchone()
    assert row is not None  # still there — not old enough to compact yet


def test_compact_tombstones_ignores_non_deleted_memories(
    sync_db_tombstones: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import remind_me_mcp.config as _cfg
    from remind_me_mcp.sync import _compact_tombstones

    monkeypatch.setattr(_cfg, "TOMBSTONE_RETENTION_DAYS", 0)  # even a 0-day window...
    _insert_memory(sync_db_tombstones, "never-deleted")

    removed = _compact_tombstones(sync_db_tombstones)
    assert removed == 0  # ...never touches a memory with deleted_at IS NULL
    row = sync_db_tombstones.execute(
        "SELECT * FROM memories WHERE id = 'never-deleted'"
    ).fetchone()
    assert row is not None


def test_compact_tombstones_cleans_up_chunks_and_entity_links(
    sync_db_tombstones: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import remind_me_mcp.config as _cfg
    from remind_me_mcp.db import _link_memory_entity, _upsert_entity
    from remind_me_mcp.sync import _compact_tombstones

    monkeypatch.setattr(_cfg, "TOMBSTONE_RETENTION_DAYS", 30)

    old_ts = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    _insert_memory(sync_db_tombstones, "old-tombstone")
    rowid = sync_db_tombstones.execute(
        "SELECT rowid FROM memories WHERE id = 'old-tombstone'"
    ).fetchone()[0]
    cur = sync_db_tombstones.execute(
        "INSERT INTO memories_vec(embedding) VALUES (?)", (b"\x00" * (384 * 4),)
    )
    sync_db_tombstones.execute(
        "INSERT INTO vec_chunks(vec_rowid, memory_rowid, chunk_ix) VALUES (?, ?, 0)",
        (cur.lastrowid, rowid),
    )
    eid = _upsert_entity(sync_db_tombstones, "Some Entity")
    _link_memory_entity(sync_db_tombstones, "old-tombstone", eid)
    sync_db_tombstones.execute(
        "UPDATE memories SET deleted_at = ? WHERE id = 'old-tombstone'", (old_ts,)
    )
    sync_db_tombstones.commit()

    _compact_tombstones(sync_db_tombstones)

    assert sync_db_tombstones.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0] == 0
    assert (
        sync_db_tombstones.execute(
            "SELECT COUNT(*) FROM memory_entities WHERE memory_id = 'old-tombstone'"
        ).fetchone()[0]
        == 0
    )
    # The entity itself survives -- only the link to the compacted memory goes.
    assert sync_db_tombstones.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 1
