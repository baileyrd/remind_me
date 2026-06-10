"""
Tests for remind_me_mcp.sync — outbox push, pull, upsert conflict resolution,
peer discovery, and the sync cycle.

Network traffic is mocked with httpx.MockTransport; the database is a real
in-memory SQLite connection with the full schema (so the outbox triggers fire
exactly as in production). No test touches ~/.remind-me.

NOTE: several tests below intentionally pin CURRENT buggy behavior (marked
with the backlog item that will change them) so the fixes are visible as
test diffs.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import httpx
import pytest

import remind_me_mcp.db as _db_mod
import remind_me_mcp.sync as sync
from remind_me_mcp.db import _ensure_schema, _now_iso

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def sync_db(monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    """In-memory DB with full schema, wired into db + sync modules.

    The sync_flags gate is switched on (as it would be on a sync-enabled
    node) so the outbox triggers fire.
    """
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    _ensure_schema(db)
    db.execute(
        "INSERT OR REPLACE INTO sync_flags (key, value) VALUES ('sync_enabled', '1')"
    )
    db.commit()

    monkeypatch.setattr(_db_mod, "_get_db", lambda: db)
    monkeypatch.setattr(sync, "_get_db", lambda: db)
    # Never attempt to load a real embedding model in tests.
    monkeypatch.setattr(_db_mod, "_get_embedder", lambda: None)

    yield db
    db.close()


def insert_memory(
    db: sqlite3.Connection,
    mem_id: str,
    content: str = "test content",
    *,
    updated_at: str | None = None,
    tags: list[str] | None = None,
    node_id: str | None = None,
    **extra: Any,
) -> None:
    """Insert a memory through SQL so the outbox triggers fire."""
    now = updated_at or _now_iso()
    db.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata,
                                 created_at, updated_at, node_id)
           VALUES (?, ?, 'general', ?, 'manual', '{}', ?, ?, ?)""",
        (mem_id, content, json.dumps(tags or []), now, now, node_id),
    )
    if extra:
        sets = ", ".join(f"{k} = ?" for k in extra)
        db.execute(f"UPDATE memories SET {sets} WHERE id = ?", (*extra.values(), mem_id))
    db.commit()


def make_record(mem_id: str, content: str = "remote content", **overrides: Any) -> dict:
    """Build a wire-format memory record like a hub/peer would send."""
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


class RequestRecorder:
    """MockTransport handler that records requests and replays canned responses."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.responses = responses or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path in self.responses:
            body = self.responses[path]
            if callable(body):
                body = body(request)
            return httpx.Response(200, json=body)
        if path == "/sync/push":
            payload = json.loads(request.content)
            return httpx.Response(200, json={"accepted": len(payload.get("records", []))})
        if path == "/sync/pull":
            return httpx.Response(200, json={"records": [], "count": 0})
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404, json={"error": "not found"})


def mock_client(handler: RequestRecorder) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Outbox push
# ---------------------------------------------------------------------------


async def test_push_outbox_sends_unsent_rows(sync_db: sqlite3.Connection) -> None:
    insert_memory(sync_db, "mem-1", "alpha", tags=["a", "b"])
    insert_memory(sync_db, "mem-2", "beta")

    recorder = RequestRecorder()
    async with mock_client(recorder) as client:
        await sync._push_outbox(client, "http://hub")

    assert len(recorder.requests) == 1
    body = json.loads(recorder.requests[0].content)
    assert body["node_id"] == sync.NODE_ID
    ids = [r["id"] for r in body["records"]]
    assert ids == ["mem-1", "mem-2"]
    # Tags must be deserialized into a real list on the wire
    assert body["records"][0]["tags"] == ["a", "b"]
    assert isinstance(body["records"][0]["metadata"], dict)


async def test_push_outbox_nothing_to_send(sync_db: sqlite3.Connection) -> None:
    recorder = RequestRecorder()
    async with mock_client(recorder) as client:
        result = await sync._push_outbox(client, "http://hub")
    assert result == 0
    assert recorder.requests == []


async def test_push_outbox_batches(
    sync_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sync, "BATCH_SIZE", 2)
    for i in range(5):
        insert_memory(sync_db, f"mem-{i}", f"content {i}")

    recorder = RequestRecorder()
    async with mock_client(recorder) as client:
        await sync._push_outbox(client, "http://hub")

    # 5 rows with batch size 2 -> 3 requests (2 + 2 + 1)
    assert len(recorder.requests) == 3
    sizes = [len(json.loads(r.content)["records"]) for r in recorder.requests]
    assert sizes == [2, 2, 1]


async def test_push_outbox_http_error_leaves_rows_unsent(
    sync_db: sqlite3.Connection,
) -> None:
    insert_memory(sync_db, "mem-1")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await sync._push_outbox(client, "http://hub")

    rows = sync_db.execute("SELECT sent_at FROM sync_outbox").fetchall()
    assert all(r["sent_at"] == "" for r in rows)


async def test_push_outbox_sent_marking_is_global(sync_db: sqlite3.Connection) -> None:
    """CURRENT BEHAVIOR (bug, SY-02): the first successful push marks rows
    sent for everyone — a second remote never receives them."""
    insert_memory(sync_db, "mem-1", "alpha")

    recorder_a = RequestRecorder()
    async with mock_client(recorder_a) as client:
        await sync._push_outbox(client, "http://hub")
    assert len(recorder_a.requests) == 1

    recorder_b = RequestRecorder()
    async with mock_client(recorder_b) as client:
        await sync._push_outbox(client, "http://peer")
    assert recorder_b.requests == []  # SY-02 will flip this


async def test_push_outbox_partial_accept_marks_whole_batch(
    sync_db: sqlite3.Connection,
) -> None:
    """CURRENT BEHAVIOR (bug, SY-02): even when the remote accepts fewer
    records than sent, the whole batch is marked sent."""
    insert_memory(sync_db, "mem-1")
    insert_memory(sync_db, "mem-2")

    recorder = RequestRecorder(responses={"/sync/push": {"accepted": 1}})
    async with mock_client(recorder) as client:
        await sync._push_outbox(client, "http://hub")

    rows = sync_db.execute("SELECT sent_at FROM sync_outbox").fetchall()
    assert all(r["sent_at"] != "" for r in rows)


# ---------------------------------------------------------------------------
# Pull
# ---------------------------------------------------------------------------


async def test_pull_remote_upserts_records(sync_db: sqlite3.Connection) -> None:
    rec = make_record("remote-1", "pulled content")
    recorder = RequestRecorder(
        responses={"/sync/pull": {"records": [rec], "count": 1}}
    )
    async with mock_client(recorder) as client:
        count = await sync._pull_remote(client, "http://hub", "hub")

    assert count == 1
    row = sync_db.execute(
        "SELECT * FROM memories WHERE id = 'remote-1'"
    ).fetchone()
    assert row is not None
    assert row["content"] == "pulled content"

    # Cursor advanced to the newest record we received
    log_row = sync_db.execute(
        "SELECT last_pull FROM sync_log WHERE remote_id = 'hub'"
    ).fetchone()
    assert log_row is not None
    assert log_row["last_pull"] == rec["updated_at"]


async def test_pull_remote_sends_since_cursor(sync_db: sqlite3.Connection) -> None:
    sync_db.execute(
        "INSERT INTO sync_log (remote_id, last_pull) VALUES ('hub', '2026-01-01T00:00:00+00:00')"
    )
    sync_db.commit()

    recorder = RequestRecorder()
    async with mock_client(recorder) as client:
        await sync._pull_remote(client, "http://hub", "hub")

    assert len(recorder.requests) == 1
    params = dict(recorder.requests[0].url.params)
    assert params["since"] == "2026-01-01T00:00:00+00:00"
    assert params["exclude_node"] == sync.NODE_ID


async def test_pull_remote_empty(sync_db: sqlite3.Connection) -> None:
    recorder = RequestRecorder()
    async with mock_client(recorder) as client:
        count = await sync._pull_remote(client, "http://hub", "hub")
    assert count == 0
    # No cursor written when nothing was received
    assert sync_db.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0] == 0


async def test_pull_remote_single_page_per_cycle(
    sync_db: sqlite3.Connection,
) -> None:
    """CURRENT BEHAVIOR (bug, SY-04): only one page is pulled per cycle and
    the resume cursor uses strict '>' on updated_at, losing boundary ties."""
    ts = "2026-06-01T00:00:00+00:00"
    page = [make_record(f"tie-{i}", f"tie {i}", created_at=ts, updated_at=ts) for i in range(3)]
    recorder = RequestRecorder(responses={"/sync/pull": {"records": page}})

    async with mock_client(recorder) as client:
        count = await sync._pull_remote(client, "http://hub", "hub")

    assert count == 3
    assert len(recorder.requests) == 1  # no drain loop yet (SY-04)
    log_row = sync_db.execute(
        "SELECT last_pull FROM sync_log WHERE remote_id = 'hub'"
    ).fetchone()
    assert log_row["last_pull"] == ts  # next pull uses strict '>' and skips ties


# ---------------------------------------------------------------------------
# _upsert_records — conflict resolution
# ---------------------------------------------------------------------------


def test_upsert_inserts_new_record(sync_db: sqlite3.Connection) -> None:
    upserted = sync._upsert_records(sync_db, [make_record("new-1", "hello")])
    assert upserted == 1
    row = sync_db.execute("SELECT * FROM memories WHERE id = 'new-1'").fetchone()
    assert row["content"] == "hello"
    assert row["node_id"] == "remote-node"


def test_upsert_newer_wins(sync_db: sqlite3.Connection) -> None:
    insert_memory(sync_db, "m1", "old local", updated_at="2026-01-01T00:00:00+00:00")
    rec = make_record(
        "m1", "newer remote",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-02-01T00:00:00+00:00",
    )
    upserted = sync._upsert_records(sync_db, [rec])
    assert upserted == 1
    row = sync_db.execute("SELECT content FROM memories WHERE id = 'm1'").fetchone()
    assert row["content"] == "newer remote"


def test_upsert_older_loses(sync_db: sqlite3.Connection) -> None:
    insert_memory(sync_db, "m1", "newer local", updated_at="2026-03-01T00:00:00+00:00")
    rec = make_record(
        "m1", "stale remote",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    upserted = sync._upsert_records(sync_db, [rec])
    assert upserted == 0
    row = sync_db.execute("SELECT content FROM memories WHERE id = 'm1'").fetchone()
    assert row["content"] == "newer local"


def test_upsert_equal_timestamp_is_noop(sync_db: sqlite3.Connection) -> None:
    ts = "2026-03-01T00:00:00+00:00"
    insert_memory(sync_db, "m1", "local", updated_at=ts)
    rec = make_record("m1", "remote echo", created_at=ts, updated_at=ts)
    upserted = sync._upsert_records(sync_db, [rec])
    assert upserted == 0
    row = sync_db.execute("SELECT content FROM memories WHERE id = 'm1'").fetchone()
    assert row["content"] == "local"


def test_upsert_parses_string_tags_and_metadata(sync_db: sqlite3.Connection) -> None:
    rec = make_record(
        "m1", tags='["x", "y"]', metadata='{"k": "v"}'
    )
    sync._upsert_records(sync_db, [rec])
    row = sync_db.execute("SELECT tags, metadata FROM memories WHERE id = 'm1'").fetchone()
    assert json.loads(row["tags"]) == ["x", "y"]
    assert json.loads(row["metadata"]) == {"k": "v"}


def test_upsert_malformed_tags_become_empty(sync_db: sqlite3.Connection) -> None:
    rec = make_record("m1", tags="{not json", metadata="[broken")
    sync._upsert_records(sync_db, [rec])
    row = sync_db.execute("SELECT tags, metadata FROM memories WHERE id = 'm1'").fetchone()
    assert json.loads(row["tags"]) == []
    assert json.loads(row["metadata"]) == {}


def test_upsert_drops_extended_columns(sync_db: sqlite3.Connection) -> None:
    """CURRENT BEHAVIOR (bug, SY-03): vitality/classification/structured
    fields present in the payload are silently discarded on receive."""
    rec = make_record(
        "rich-1",
        "rich record",
        memory_type="decision",
        access_count=7,
        vitality=0.83,
        subject="Bailey",
        predicate="prefers",
        object="dark mode",
    )
    upserted = sync._upsert_records(sync_db, [rec])
    assert upserted == 1
    row = sync_db.execute("SELECT * FROM memories WHERE id = 'rich-1'").fetchone()
    # All of these arrive in the payload but are dropped today:
    assert row["memory_type"] == "unclassified"  # SY-03 will preserve "decision"
    assert row["access_count"] == 0
    assert row["vitality"] == 1.0
    assert row["subject"] is None


def test_upsert_tolerates_records_from_older_nodes(sync_db: sqlite3.Connection) -> None:
    """Records missing newer schema fields get schema defaults."""
    now = _now_iso()
    rec = {
        "id": "v2-era",
        "content": "from an old node",
        "category": "general",
        "tags": [],
        "source": "manual",
        "metadata": {},
        "created_at": now,
        "updated_at": now,
    }
    upserted = sync._upsert_records(sync_db, [rec])
    assert upserted == 1
    row = sync_db.execute("SELECT * FROM memories WHERE id = 'v2-era'").fetchone()
    assert row["status"] == "active"
    assert row["memory_type"] == "unclassified"
    assert row["access_count"] == 0


def test_upsert_malformed_record_poisons_batch(sync_db: sqlite3.Connection) -> None:
    """CURRENT BEHAVIOR (bug, SY-03): a record missing a required key raises
    out of the loop, losing the rest of the batch."""
    good_before = make_record("good-1", "first")
    missing_content = {"id": "bad-1", "updated_at": _now_iso(), "created_at": _now_iso()}
    good_after = make_record("good-2", "last")

    with pytest.raises(KeyError):
        sync._upsert_records(sync_db, [good_before, missing_content, good_after])

    ids = {r["id"] for r in sync_db.execute("SELECT id FROM memories").fetchall()}
    assert "good-2" not in ids  # SY-03 will make the batch survive


def test_upsert_stores_timestamps_verbatim(sync_db: sqlite3.Connection) -> None:
    """CURRENT BEHAVIOR (bug, SY-08): heterogeneous remote timestamp formats
    are stored as-is, breaking string-based LWW ordering."""
    rec = make_record(
        "z-ts",
        created_at="2026-06-01 10:00:00",  # SQLite trigger style
        updated_at="2026-06-01T12:00:00Z",  # hub style
    )
    sync._upsert_records(sync_db, [rec])
    row = sync_db.execute(
        "SELECT created_at, updated_at FROM memories WHERE id = 'z-ts'"
    ).fetchone()
    assert row["created_at"] == "2026-06-01 10:00:00"  # SY-08 will canonicalize
    assert row["updated_at"] == "2026-06-01T12:00:00Z"


# ---------------------------------------------------------------------------
# Echo suppression
# ---------------------------------------------------------------------------


def test_upsert_suppresses_echo_outbox_rows(sync_db: sqlite3.Connection) -> None:
    """Applying a remote record fires the outbox triggers; those rows must
    not be pushed back out."""
    rec = make_record("echo-1", "from remote")
    sync._upsert_records(sync_db, [rec])

    rows = sync_db.execute(
        "SELECT sent_at FROM sync_outbox WHERE memory_id = 'echo-1'"
    ).fetchall()
    assert rows, "outbox trigger should have fired for the upsert"
    assert all(r["sent_at"] != "" for r in rows)


def test_upsert_echo_suppression_swallows_local_edits(
    sync_db: sqlite3.Connection,
) -> None:
    """CURRENT BEHAVIOR (bug, SY-05): suppression marks ALL pending outbox
    rows for the memory — including a concurrent local edit's row."""
    insert_memory(sync_db, "m1", "local edit", updated_at="2026-01-01T00:00:00+00:00")
    local_rows = sync_db.execute(
        "SELECT id FROM sync_outbox WHERE memory_id = 'm1'"
    ).fetchall()
    assert len(local_rows) == 1
    local_outbox_id = local_rows[0]["id"]

    rec = make_record(
        "m1", "remote version",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-02-01T00:00:00+00:00",
    )
    sync._upsert_records(sync_db, [rec])

    local_row = sync_db.execute(
        "SELECT sent_at FROM sync_outbox WHERE id = ?", (local_outbox_id,)
    ).fetchone()
    # The local edit is lost from sync today. SY-05 will keep it pending.
    assert local_row["sent_at"] != ""


# ---------------------------------------------------------------------------
# Peer discovery
# ---------------------------------------------------------------------------


async def test_discover_peers_without_tailscale() -> None:
    """With no Tailscale daemon socket, discovery degrades to no peers."""
    peers = await sync._discover_peers()
    assert peers == []


async def test_probe_peer_healthy() -> None:
    recorder = RequestRecorder()
    async with mock_client(recorder) as client:
        ok = await sync._probe_peer(client, {"node_id": "p", "url": "http://peer"})
    assert ok is True


async def test_probe_peer_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ok = await sync._probe_peer(client, {"node_id": "p", "url": "http://peer"})
    assert ok is False


# ---------------------------------------------------------------------------
# Sync cycle
# ---------------------------------------------------------------------------


async def _no_peers() -> list[dict[str, str]]:
    return []


async def test_sync_once_hub_push_and_pull(
    sync_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    insert_memory(sync_db, "mem-1", "to hub")
    rec = make_record("from-hub", "hub content")
    recorder = RequestRecorder(
        responses={"/sync/pull": {"records": [rec], "count": 1}}
    )

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(sync, "HUB_URL", "http://hub")
    monkeypatch.setattr(sync, "_discover_peers", _no_peers)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_async_client(
            **{**kw, "transport": httpx.MockTransport(recorder)}
        ),
    )

    await sync._sync_once()

    paths = [r.url.path for r in recorder.requests]
    assert "/sync/push" in paths
    assert "/sync/pull" in paths
    assert (
        sync_db.execute("SELECT COUNT(*) FROM memories WHERE id = 'from-hub'").fetchone()[0]
        == 1
    )


async def test_sync_once_hub_error_does_not_raise(
    sync_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("hub down")

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(sync, "HUB_URL", "http://hub")
    monkeypatch.setattr(sync, "_discover_peers", _no_peers)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_async_client(
            **{**kw, "transport": httpx.MockTransport(handler)}
        ),
    )

    await sync._sync_once()  # must not raise


async def test_sync_once_skips_self_peer(
    sync_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = RequestRecorder()
    real_async_client = httpx.AsyncClient

    async def fake_peers() -> list[dict[str, str]]:
        return [{"node_id": sync.NODE_ID or "this-node", "url": "http://self"}]

    monkeypatch.setattr(sync, "NODE_ID", sync.NODE_ID or "this-node")
    monkeypatch.setattr(sync, "HUB_URL", "")
    monkeypatch.setattr(sync, "_discover_peers", fake_peers)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_async_client(
            **{**kw, "transport": httpx.MockTransport(recorder)}
        ),
    )

    await sync._sync_once()
    assert recorder.requests == []


async def test_sync_once_syncs_healthy_peer(
    sync_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    insert_memory(sync_db, "mem-1", "to peer")
    recorder = RequestRecorder()
    real_async_client = httpx.AsyncClient

    async def fake_peers() -> list[dict[str, str]]:
        return [{"node_id": "other-node", "url": "http://peer"}]

    monkeypatch.setattr(sync, "HUB_URL", "")
    monkeypatch.setattr(sync, "_discover_peers", fake_peers)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_async_client(
            **{**kw, "transport": httpx.MockTransport(recorder)}
        ),
    )

    await sync._sync_once()
    paths = [r.url.path for r in recorder.requests]
    assert "/health" in paths
    assert "/sync/push" in paths
    assert "/sync/pull" in paths
