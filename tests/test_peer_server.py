"""
Tests for remind_me_mcp.peer_server — the lightweight HTTP server peers use
for push/pull sync over Tailscale.

A real server is started on an ephemeral 127.0.0.1 port for each test; the
database is a shared in-memory SQLite connection (check_same_thread=False so
the handler thread can use it). Only the network peer is real — no test
touches ~/.remind-me.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

import remind_me_mcp.peer_server as peer_server
from remind_me_mcp.db import _ensure_schema, _now_iso

SECRET = "test-secret"
AUTH = {"Authorization": f"Bearer {SECRET}"}


@pytest.fixture()
def peer_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[sqlite3.Connection]:
    """In-memory DB shared with the peer server handler thread."""
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    _ensure_schema(db)

    import remind_me_mcp.db as _db_mod
    import remind_me_mcp.sync as _sync_mod

    monkeypatch.setattr(_db_mod, "_get_db", lambda: db)
    monkeypatch.setattr(_sync_mod, "_get_db", lambda: db)
    monkeypatch.setattr(peer_server, "_get_db", lambda: db)
    monkeypatch.setattr(_db_mod, "_get_embedder", lambda: None)

    yield db
    db.close()


@pytest.fixture()
def peer_url(
    peer_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> Iterator[str]:
    """A live peer server bound to an ephemeral localhost port."""
    monkeypatch.setattr(peer_server, "SYNC_SECRET", SECRET)
    monkeypatch.setattr(peer_server, "NODE_ID", "test-node")

    from http.server import HTTPServer
    from threading import Thread

    server = HTTPServer(("127.0.0.1", 0), peer_server.PeerHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def insert_memory(
    db: sqlite3.Connection,
    mem_id: str,
    content: str = "content",
    *,
    updated_at: str | None = None,
    node_id: str | None = None,
) -> None:
    now = updated_at or _now_iso()
    db.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata,
                                 created_at, updated_at, node_id)
           VALUES (?, ?, 'general', '[]', 'manual', '{}', ?, ?, ?)""",
        (mem_id, content, now, now, node_id),
    )
    db.commit()


def make_record(mem_id: str, content: str = "pushed", **overrides) -> dict:
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
        "node_id": "other-node",
    }
    rec.update(overrides)
    return rec


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_health_with_valid_secret(peer_url: str) -> None:
    resp = httpx.get(f"{peer_url}/health", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["node_id"] == "test-node"


def test_missing_secret_rejected(peer_url: str) -> None:
    resp = httpx.get(f"{peer_url}/health")
    assert resp.status_code == 401


def test_wrong_secret_rejected(peer_url: str) -> None:
    resp = httpx.get(
        f"{peer_url}/health", headers={"Authorization": "Bearer wrong"}
    )
    assert resp.status_code == 401


def test_post_requires_auth(peer_url: str) -> None:
    resp = httpx.post(f"{peer_url}/sync/push", json={"records": []})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Pull
# ---------------------------------------------------------------------------


def test_pull_returns_records(peer_url: str, peer_db: sqlite3.Connection) -> None:
    insert_memory(peer_db, "m1", "first")
    insert_memory(peer_db, "m2", "second")

    resp = httpx.get(f"{peer_url}/sync/pull", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    ids = {r["id"] for r in body["records"]}
    assert ids == {"m1", "m2"}
    # tags/metadata are decoded for the wire
    assert body["records"][0]["tags"] == []
    assert body["records"][0]["metadata"] == {}


def test_pull_respects_since(peer_url: str, peer_db: sqlite3.Connection) -> None:
    insert_memory(peer_db, "old", updated_at="2026-01-01T00:00:00+00:00")
    insert_memory(peer_db, "new", updated_at="2026-03-01T00:00:00+00:00")

    resp = httpx.get(
        f"{peer_url}/sync/pull",
        params={"since": "2026-02-01T00:00:00+00:00"},
        headers=AUTH,
    )
    body = resp.json()
    assert [r["id"] for r in body["records"]] == ["new"]


def test_pull_excludes_node(peer_url: str, peer_db: sqlite3.Connection) -> None:
    insert_memory(peer_db, "mine", node_id=None)
    insert_memory(peer_db, "theirs", node_id="other-node")

    resp = httpx.get(
        f"{peer_url}/sync/pull",
        params={"exclude_node": "other-node"},
        headers=AUTH,
    )
    body = resp.json()
    assert [r["id"] for r in body["records"]] == ["mine"]


def test_pull_respects_limit(peer_url: str, peer_db: sqlite3.Connection) -> None:
    for i in range(5):
        insert_memory(peer_db, f"m{i}", updated_at=f"2026-01-0{i + 1}T00:00:00+00:00")

    resp = httpx.get(
        f"{peer_url}/sync/pull", params={"limit": 2}, headers=AUTH
    )
    body = resp.json()
    assert body["count"] == 2
    assert [r["id"] for r in body["records"]] == ["m0", "m1"]


def test_pull_orders_by_updated_at(peer_url: str, peer_db: sqlite3.Connection) -> None:
    insert_memory(peer_db, "late", updated_at="2026-03-01T00:00:00+00:00")
    insert_memory(peer_db, "early", updated_at="2026-01-01T00:00:00+00:00")

    resp = httpx.get(f"{peer_url}/sync/pull", headers=AUTH)
    assert [r["id"] for r in resp.json()["records"]] == ["early", "late"]


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------


def test_push_upserts_records(peer_url: str, peer_db: sqlite3.Connection) -> None:
    resp = httpx.post(
        f"{peer_url}/sync/push",
        json={"node_id": "other-node", "records": [make_record("p1"), make_record("p2")]},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 2
    rows = peer_db.execute("SELECT id FROM memories ORDER BY id").fetchall()
    assert [r["id"] for r in rows] == ["p1", "p2"]


def test_push_stale_record_not_counted(
    peer_url: str, peer_db: sqlite3.Connection
) -> None:
    insert_memory(peer_db, "p1", "newer local", updated_at="2026-06-01T00:00:00+00:00")
    rec = make_record(
        "p1", "stale",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    resp = httpx.post(
        f"{peer_url}/sync/push",
        json={"node_id": "other-node", "records": [rec]},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 0
    row = peer_db.execute("SELECT content FROM memories WHERE id = 'p1'").fetchone()
    assert row["content"] == "newer local"


# ---------------------------------------------------------------------------
# Unknown routes / malformed input
# ---------------------------------------------------------------------------


def test_unknown_get_route_404(peer_url: str) -> None:
    resp = httpx.get(f"{peer_url}/nope", headers=AUTH)
    assert resp.status_code == 404


def test_unknown_post_route_404(peer_url: str) -> None:
    resp = httpx.post(f"{peer_url}/nope", json={}, headers=AUTH)
    assert resp.status_code == 404


def test_push_malformed_json(peer_url: str) -> None:
    """CURRENT BEHAVIOR (bug, SY-09): unparseable JSON crashes the handler
    instead of returning 400."""
    try:
        resp = httpx.post(
            f"{peer_url}/sync/push",
            content=b"{this is not json",
            headers={**AUTH, "Content-Type": "application/json"},
        )
        status: int | None = resp.status_code
    except httpx.HTTPError:
        status = None  # connection died — also "not a clean 400"
    assert status != 200  # SY-09 will assert status == 400


def test_start_peer_server_port_in_use(monkeypatch: pytest.MonkeyPatch) -> None:
    """start_peer_server returns None when the port is taken."""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]

    monkeypatch.setattr(peer_server, "PEER_PORT", port)
    monkeypatch.setattr(peer_server, "SYNC_SECRET", SECRET)
    try:
        # Bind to the same loopback port the socket already holds.
        monkeypatch.setattr(peer_server, "HTTPServer", _bind_loopback(port))
        result = peer_server.start_peer_server()
        assert result is None
    finally:
        sock.close()


def _bind_loopback(port: int):
    """HTTPServer factory pinned to 127.0.0.1 so the in-use check trips."""
    from http.server import HTTPServer

    def factory(addr, handler):
        return HTTPServer(("127.0.0.1", port), handler)

    return factory
