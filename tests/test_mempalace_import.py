"""Tests for remind_me_mcp.mempalace_import — the MemPalace ChromaDB bulk importer."""

from __future__ import annotations

import json
import threading
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


def test_pull_mempalace_closes_the_chromadb_client(
    db_conn: sqlite3.Connection, fake_palace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard for issue #146.

    _open_collection returned only the collection, never the client that
    created it -- a PersistentClient owns its own SQLite handle and
    background threads that nothing closed, leaking one client per
    pull_mempalace call. The fix returns (client, collection) and the
    caller closes the client in a finally block.
    """
    import chromadb
    import chromadb.api.client

    real_close = chromadb.api.client.Client.close
    close_calls = []

    def spy_close(self):
        close_calls.append(self)
        return real_close(self)

    monkeypatch.setattr(chromadb.api.client.Client, "close", spy_close)

    pull_mempalace()

    assert len(close_calls) == 1


def test_pull_mempalace_closes_the_client_even_on_failure(
    db_conn: sqlite3.Connection, fake_palace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client must be closed even when the collection.get() call itself
    raises -- a finally block, not a bare close-after-success."""
    import chromadb
    import chromadb.api.client

    real_close = chromadb.api.client.Client.close
    close_calls = []

    def spy_close(self):
        close_calls.append(self)
        return real_close(self)

    monkeypatch.setattr(chromadb.api.client.Client, "close", spy_close)
    monkeypatch.setattr(
        _mempalace_mod,
        "_open_collection",
        lambda: (
            chromadb.PersistentClient(path=str(_mempalace_mod.MEMPALACE_PATH)),
            _BoomCollection(),
        ),
    )

    with pytest.raises(RuntimeError, match="boom"):
        pull_mempalace()

    assert len(close_calls) == 1


class _BoomCollection:
    def get(self, *a, **kw):
        raise RuntimeError("boom")


def test_pull_mempalace_concurrent_calls_do_not_duplicate(
    db_conn_concurrent: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two overlapping pull_mempalace calls over the same never-before-imported
    drawer must not both create a memory (issue #136, mirrors dbs_import.py's
    SEC-10 fix): previously, the mempalace_imports dedup lookup ran outside
    _import_lock, so two racing calls could each decide "not yet imported,"
    each mint a different id via wall-clock-salted _make_id, and both writes
    would succeed -- a permanent orphan duplicate that mempalace_imports'
    (drawer_id) INSERT OR IGNORE (first writer wins) never caught. Uses
    db_conn_concurrent (per-thread connections to a shared WAL-mode file)
    since this genuinely fans out across OS threads.
    """
    chromadb = pytest.importorskip("chromadb")

    palace_path = tmp_path / "palace"
    client = chromadb.PersistentClient(path=str(palace_path))
    collection = client.create_collection(_mempalace_mod.COLLECTION_NAME)
    collection.add(
        ids=["drawer_race_1"],
        documents=[OPAQUE_CONTENT],
        metadatas=[{"wing": "zed", "room": "general"}],
    )
    monkeypatch.setattr(_mempalace_mod, "MEMPALACE_PATH", palace_path)
    monkeypatch.setattr(_mempalace_mod, "_embed_and_store_rows", lambda rows: 0)

    barrier = threading.Barrier(2)
    results: list[dict] = []
    results_lock = threading.Lock()

    def call_pull() -> None:
        barrier.wait(timeout=5)
        result = pull_mempalace(wing="zed")
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=call_pull) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(results) == 2
    assert sum(r["imported"] for r in results) == 1
    assert sum(r["already_imported"] for r in results) == 1

    count = db_conn_concurrent.execute(
        "SELECT COUNT(*) AS c FROM memories"
    ).fetchone()["c"]
    assert count == 1


# ---------------------------------------------------------------------------
# Connector registration (Phase 4)
# ---------------------------------------------------------------------------


def test_mempalace_registered_as_connector() -> None:
    """Importing this module registers 'mempalace' in the shared registry,
    purely for discovery -- pull_mempalace never calls through it."""
    import remind_me_mcp.importer as _importer_mod

    assert "mempalace" in _importer_mod._CONNECTORS
    assert _importer_mod._CONNECTORS["mempalace"] is _mempalace_mod._mempalace_connector


def test_mempalace_connector_native_frontmatter() -> None:
    chunks, raw_entries = _mempalace_mod._mempalace_connector(NATIVE_CONTENT, {})
    assert raw_entries == 1
    assert len(chunks) == 1
    content, meta = chunks[0]
    assert content == "Microsoft Project Online migration deadline is September 2026."
    assert meta["category"] == "fact"
    assert meta["id"] == "6bb2c33ed386"


def test_mempalace_connector_opaque_content() -> None:
    chunks, raw_entries = _mempalace_mod._mempalace_connector(OPAQUE_CONTENT, {})
    assert raw_entries == 1
    assert chunks == [(OPAQUE_CONTENT, {})]


def test_mempalace_not_reachable_via_import_chat_file() -> None:
    """'mempalace' is registered for discovery only -- it's not a valid
    import_chat_file kind (IMPORT_KINDS stays narrower than _CONNECTORS)."""
    from remind_me_mcp.importer import IMPORT_KINDS

    assert "mempalace" not in IMPORT_KINDS
