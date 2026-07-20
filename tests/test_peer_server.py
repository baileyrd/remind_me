"""
Tests for remind_me_mcp.peer_server — the lightweight HTTP server peers use
for push/pull sync over Tailscale.

A real server is started on an ephemeral 127.0.0.1 port for each test; the
database is a shared in-memory SQLite connection (check_same_thread=False so
the handler thread can use it). Only the network peer is real — no test
touches ~/.remind-me.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

import remind_me_mcp.peer_server as peer_server
from remind_me_mcp.db import _ensure_schema, _entity_id, _now_iso

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


def test_empty_secret_never_authenticates(
    peer_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SY-09: with no SYNC_SECRET configured, nothing authenticates."""
    monkeypatch.setattr(peer_server, "SYNC_SECRET", "")
    resp = httpx.get(f"{peer_url}/health", headers={"Authorization": "Bearer"})
    assert resp.status_code == 401
    resp = httpx.get(f"{peer_url}/health")
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


def test_pull_keyset_cursor_includes_boundary_ties(
    peer_url: str, peer_db: sqlite3.Connection
) -> None:
    """SY-04/SY-09: with since_id, ties at the boundary timestamp are paged
    through instead of being skipped."""
    ts = "2026-02-01T00:00:00+00:00"
    for i in range(4):
        insert_memory(peer_db, f"tie-{i}", updated_at=ts)

    # Resume after tie-1: must return tie-2 and tie-3, not skip the timestamp.
    resp = httpx.get(
        f"{peer_url}/sync/pull",
        params={"since": ts, "since_id": "tie-1"},
        headers=AUTH,
    )
    assert [r["id"] for r in resp.json()["records"]] == ["tie-2", "tie-3"]


def test_pull_without_since_id_keeps_legacy_semantics(
    peer_url: str, peer_db: sqlite3.Connection
) -> None:
    """Old clients (no since_id param) keep the strict updated_at comparison."""
    ts = "2026-02-01T00:00:00+00:00"
    insert_memory(peer_db, "at-boundary", updated_at=ts)
    insert_memory(peer_db, "after", updated_at="2026-03-01T00:00:00+00:00")

    resp = httpx.get(f"{peer_url}/sync/pull", params={"since": ts}, headers=AUTH)
    assert [r["id"] for r in resp.json()["records"]] == ["after"]


def test_pull_invalid_limit_rejected(peer_url: str) -> None:
    """SY-09: a non-numeric limit is a 400, not a server crash."""
    resp = httpx.get(
        f"{peer_url}/sync/pull", params={"limit": "lots"}, headers=AUTH
    )
    assert resp.status_code == 400


def test_pull_limit_is_capped(peer_url: str, peer_db: sqlite3.Connection) -> None:
    """SY-09: the limit param cannot exceed MAX_PULL_LIMIT."""
    for i in range(3):
        insert_memory(peer_db, f"m{i}", updated_at=f"2026-01-0{i + 1}T00:00:00+00:00")

    resp = httpx.get(
        f"{peer_url}/sync/pull", params={"limit": 999999}, headers=AUTH
    )
    assert resp.status_code == 200
    assert resp.json()["count"] == 3  # capped, query still well-formed

    resp = httpx.get(f"{peer_url}/sync/pull", params={"limit": -5}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["count"] == 1  # clamped up to 1


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
    body = resp.json()
    assert body["accepted"] == 2
    assert body["processed_ids"] == ["p1", "p2"]
    assert body["failed"] == 0
    rows = peer_db.execute("SELECT id FROM memories ORDER BY id").fetchall()
    assert [r["id"] for r in rows] == ["p1", "p2"]


def test_push_stale_record_still_processed(
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
    body = resp.json()
    assert body["accepted"] == 0
    # LWW-stale records are reported processed so the sender stops resending.
    assert body["processed_ids"] == ["p1"]
    row = peer_db.execute("SELECT content FROM memories WHERE id = 'p1'").fetchone()
    assert row["content"] == "newer local"


def test_push_reports_failed_records(
    peer_url: str, peer_db: sqlite3.Connection
) -> None:
    """SY-03/SY-09: malformed records are skipped, counted, and excluded
    from processed_ids — the rest of the batch still lands."""
    good = make_record("ok-1")
    bad = {"id": "broken", "updated_at": "2026-01-01T00:00:00+00:00"}
    resp = httpx.post(
        f"{peer_url}/sync/push",
        json={"node_id": "other-node", "records": [good, bad]},
        headers=AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] == 1
    assert body["processed_ids"] == ["ok-1"]
    assert body["failed"] == 1


# ---------------------------------------------------------------------------
# Unknown routes / malformed input
# ---------------------------------------------------------------------------


def test_unknown_get_route_404(peer_url: str) -> None:
    resp = httpx.get(f"{peer_url}/nope", headers=AUTH)
    assert resp.status_code == 404


def test_unknown_post_route_404(peer_url: str) -> None:
    resp = httpx.post(f"{peer_url}/nope", json={}, headers=AUTH)
    assert resp.status_code == 404


def test_push_malformed_json_returns_400(peer_url: str) -> None:
    """SY-09: unparseable JSON is a clean 400, not a handler crash."""
    resp = httpx.post(
        f"{peer_url}/sync/push",
        content=b"{this is not json",
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_push_non_object_payload_returns_400(peer_url: str) -> None:
    resp = httpx.post(f"{peer_url}/sync/push", json=[1, 2, 3], headers=AUTH)
    assert resp.status_code == 400


def test_push_records_not_a_list_returns_400(peer_url: str) -> None:
    resp = httpx.post(
        f"{peer_url}/sync/push", json={"records": "oops"}, headers=AUTH
    )
    assert resp.status_code == 400


def test_push_empty_body_returns_400(peer_url: str) -> None:
    resp = httpx.post(f"{peer_url}/sync/push", headers=AUTH)
    assert resp.status_code == 400


def test_push_oversized_body_rejected(
    peer_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SY-09: request bodies over the cap get 413 without being read."""
    monkeypatch.setattr(peer_server, "MAX_BODY_BYTES", 64)
    resp = httpx.post(
        f"{peer_url}/sync/push",
        json={"node_id": "x", "records": [make_record("big", "y" * 500)]},
        headers=AUTH,
    )
    assert resp.status_code == 413


def test_start_peer_server_requires_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """SY-09: the peer server refuses to start without a sync secret."""
    monkeypatch.setattr(peer_server, "SYNC_SECRET", "")
    assert peer_server.start_peer_server() is None


def test_start_peer_server_port_in_use(monkeypatch: pytest.MonkeyPatch) -> None:
    """start_peer_server returns None when the port is taken."""
    import socket
    from http.server import ThreadingHTTPServer

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]

    def bind_loopback(addr, handler):
        # Pin to the loopback port the socket already holds so the
        # in-use check trips regardless of the configured bind address.
        return ThreadingHTTPServer(("127.0.0.1", port), handler)

    monkeypatch.setattr(peer_server, "PEER_PORT", port)
    monkeypatch.setattr(peer_server, "SYNC_SECRET", SECRET)
    monkeypatch.setattr(peer_server, "ThreadingHTTPServer", bind_loopback)
    try:
        assert peer_server.start_peer_server() is None
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Entity graph endpoints (FT-04)
# ---------------------------------------------------------------------------


def insert_entity(
    db: sqlite3.Connection,
    name: str,
    *,
    kind: str | None = None,
    aliases: list[str] | None = None,
    updated_at: str | None = None,
    node_id: str | None = None,
) -> str:
    now = updated_at or _now_iso()
    eid = _entity_id(name)
    db.execute(
        """INSERT INTO entities (id, name, kind, aliases, created_at, updated_at, node_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (eid, name, kind, json.dumps(aliases or []), now, now, node_id),
    )
    db.commit()
    return eid


def test_pull_entities_returns_tagged_records(
    peer_url: str, peer_db: sqlite3.Connection
) -> None:
    eid = insert_entity(
        peer_db, "Bailey Robertson", kind="person", aliases=["Bailey"]
    )
    resp = httpx.get(f"{peer_url}/sync/pull_entities", headers=AUTH)
    assert resp.status_code == 200
    records = resp.json()["records"]
    assert len(records) == 1
    rec = records[0]
    assert rec["record_type"] == "entity"
    assert rec["id"] == eid
    assert rec["name"] == "Bailey Robertson"
    assert rec["aliases"] == ["Bailey"]  # deserialized list on the wire


def test_pull_entities_keyset_cursor(
    peer_url: str, peer_db: sqlite3.Connection
) -> None:
    ts1 = "2026-01-01T00:00:00+00:00"
    ts2 = "2026-02-01T00:00:00+00:00"
    insert_entity(peer_db, "Old Entity", updated_at=ts1)
    eid_new = insert_entity(peer_db, "New Entity", updated_at=ts2)
    resp = httpx.get(
        f"{peer_url}/sync/pull_entities",
        params={"since": ts1, "since_id": _entity_id("Old Entity")},
        headers=AUTH,
    )
    ids = [r["id"] for r in resp.json()["records"]]
    assert ids == [eid_new]


def test_pull_entities_excludes_node(
    peer_url: str, peer_db: sqlite3.Connection
) -> None:
    insert_entity(peer_db, "Mine", node_id="puller-node")
    theirs = insert_entity(peer_db, "Theirs", node_id="other-node")
    resp = httpx.get(
        f"{peer_url}/sync/pull_entities",
        params={"exclude_node": "puller-node"},
        headers=AUTH,
    )
    ids = [r["id"] for r in resp.json()["records"]]
    assert ids == [theirs]


def test_pull_links_returns_tagged_records(
    peer_url: str, peer_db: sqlite3.Connection
) -> None:
    now = _now_iso()
    peer_db.execute(
        "INSERT INTO memory_entities (memory_id, entity_id, created_at) VALUES (?, ?, ?)",
        ("mem-1", "ent-1", now),
    )
    peer_db.commit()
    resp = httpx.get(f"{peer_url}/sync/pull_links", headers=AUTH)
    assert resp.status_code == 200
    records = resp.json()["records"]
    assert records == [{
        "record_type": "memory_entity",
        "id": "mem-1|ent-1",
        "memory_id": "mem-1",
        "entity_id": "ent-1",
        "created_at": now,
    }]


def test_pull_links_keyset_cursor(
    peer_url: str, peer_db: sqlite3.Connection
) -> None:
    ts = "2026-01-01T00:00:00+00:00"
    peer_db.executemany(
        "INSERT INTO memory_entities (memory_id, entity_id, created_at) VALUES (?, ?, ?)",
        [("m1", "e1", ts), ("m1", "e2", ts), ("m2", "e1", ts)],
    )
    peer_db.commit()
    # Cursor sits at (ts, 'm1|e2') -> only 'm2|e1' remains.
    resp = httpx.get(
        f"{peer_url}/sync/pull_links",
        params={"since": ts, "since_id": "m1|e2"},
        headers=AUTH,
    )
    ids = [r["id"] for r in resp.json()["records"]]
    assert ids == ["m2|e1"]


def test_pull_entity_endpoints_require_auth(peer_url: str) -> None:
    assert httpx.get(f"{peer_url}/sync/pull_entities").status_code == 401
    assert httpx.get(f"{peer_url}/sync/pull_links").status_code == 401
    assert httpx.get(f"{peer_url}/sync/pull_entity_relations").status_code == 401


def test_pull_entity_relations_returns_tagged_records(
    peer_url: str, peer_db: sqlite3.Connection
) -> None:
    from remind_me_mcp.db import _entity_relation_id

    now = _now_iso()
    rid = _entity_relation_id("subj-1", "works_with", "obj-1")
    peer_db.execute(
        """INSERT INTO entity_relations
           (id, subject_entity_id, relation, object_entity_id, created_at, updated_at, node_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (rid, "subj-1", "works_with", "obj-1", now, now, "local-node"),
    )
    peer_db.commit()

    resp = httpx.get(f"{peer_url}/sync/pull_entity_relations", headers=AUTH)
    assert resp.status_code == 200
    records = resp.json()["records"]
    assert records == [{
        "id": rid,
        "subject_entity_id": "subj-1",
        "relation": "works_with",
        "object_entity_id": "obj-1",
        "created_at": now,
        "updated_at": now,
        "node_id": "local-node",
        "record_type": "entity_relation",
    }]


def test_pull_entity_relations_keyset_cursor(
    peer_url: str, peer_db: sqlite3.Connection
) -> None:
    from remind_me_mcp.db import _entity_relation_id

    ts = "2026-01-01T00:00:00+00:00"
    peer_db.executemany(
        """INSERT INTO entity_relations
           (id, subject_entity_id, relation, object_entity_id, created_at, updated_at, node_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [
            (_entity_relation_id("s1", "r1", "o1"), "s1", "r1", "o1", ts, ts, None),
            (_entity_relation_id("s2", "r2", "o2"), "s2", "r2", "o2", ts, ts, None),
        ],
    )
    peer_db.commit()
    first_id = _entity_relation_id("s1", "r1", "o1")
    second_id = _entity_relation_id("s2", "r2", "o2")
    # Cursor sits right after whichever id sorts first — only the other remains.
    ordered = sorted([first_id, second_id])
    resp = httpx.get(
        f"{peer_url}/sync/pull_entity_relations",
        params={"since": ts, "since_id": ordered[0]},
        headers=AUTH,
    )
    ids = [r["id"] for r in resp.json()["records"]]
    assert ids == [ordered[1]]


def test_push_entity_and_link_records(
    peer_url: str, peer_db: sqlite3.Connection
) -> None:
    """The push endpoint applies mixed memory/entity/link/entity_relation
    batches and reports composite link ids in processed_ids."""
    from remind_me_mcp.db import _entity_relation_id

    now = _now_iso()
    eid = _entity_id("Pushed Entity")
    eid2 = _entity_id("Other Entity")
    rid = _entity_relation_id(eid, "relates_to", eid2)
    records = [
        make_record("mem-x", "memory body"),
        {
            "record_type": "entity",
            "id": eid,
            "name": "Pushed Entity",
            "kind": "tool",
            "aliases": ["pe"],
            "created_at": now,
            "updated_at": now,
            "node_id": "other-node",
        },
        {
            "record_type": "memory_entity",
            "id": f"mem-x|{eid}",
            "memory_id": "mem-x",
            "entity_id": eid,
            "created_at": now,
        },
        {
            "record_type": "entity_relation",
            "id": rid,
            "subject_entity_id": eid,
            "relation": "relates_to",
            "object_entity_id": eid2,
            "created_at": now,
            "updated_at": now,
            "node_id": "other-node",
        },
    ]
    resp = httpx.post(
        f"{peer_url}/sync/push",
        json={"node_id": "other-node", "records": records},
        headers=AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] == 4
    assert set(body["processed_ids"]) == {"mem-x", eid, f"mem-x|{eid}", rid}
    row = peer_db.execute(
        "SELECT * FROM entity_relations WHERE id = ?", (rid,)
    ).fetchone()
    assert row["subject_entity_id"] == eid
    assert row["object_entity_id"] == eid2

    ent = peer_db.execute("SELECT * FROM entities WHERE id = ?", (eid,)).fetchone()
    assert ent["kind"] == "tool"
    link = peer_db.execute("SELECT * FROM memory_entities").fetchone()
    assert (link["memory_id"], link["entity_id"]) == ("mem-x", eid)
