"""
Tests for remind_me_mcp.sync — outbox push, pull, upsert conflict resolution,
peer discovery, outbox pruning, and the sync cycle.

Network traffic is mocked with httpx.MockTransport; the database is a real
in-memory SQLite connection with the full schema (so the outbox triggers fire
exactly as in production). No test touches ~/.remind-me.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
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
# Timestamp canonicalization (SY-08)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-06-01T12:00:00+00:00", "2026-06-01T12:00:00+00:00"),
        ("2026-06-01T12:00:00Z", "2026-06-01T12:00:00+00:00"),
        ("2026-06-01 12:00:00", "2026-06-01T12:00:00+00:00"),
        ("2026-06-01T14:00:00+02:00", "2026-06-01T12:00:00+00:00"),
        ("2026-06-01T12:00:00.123456+00:00", "2026-06-01T12:00:00.123456+00:00"),
    ],
)
def test_canon_ts(raw: str, expected: str) -> None:
    assert sync._canon_ts(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "not a time", None, 42, []])
def test_canon_ts_rejects_garbage(raw: Any) -> None:
    with pytest.raises(ValueError):
        sync._canon_ts(raw)


# ---------------------------------------------------------------------------
# Outbox push (per-remote tracking — SY-02)
# ---------------------------------------------------------------------------


async def test_push_outbox_sends_unsent_rows(sync_db: sqlite3.Connection) -> None:
    insert_memory(sync_db, "mem-1", "alpha", tags=["a", "b"])
    insert_memory(sync_db, "mem-2", "beta")

    recorder = RequestRecorder()
    async with mock_client(recorder) as client:
        await sync._push_outbox(client, "http://hub", "hub")

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
        result = await sync._push_outbox(client, "http://hub", "hub")
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
        await sync._push_outbox(client, "http://hub", "hub")

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
            await sync._push_outbox(client, "http://hub", "hub")

    assert sync_db.execute("SELECT COUNT(*) FROM sync_sends").fetchone()[0] == 0


async def test_push_outbox_tracks_sends_per_remote(sync_db: sqlite3.Connection) -> None:
    """SY-02: each remote receives every outbox row, tracked independently."""
    insert_memory(sync_db, "mem-1", "alpha")

    recorder_a = RequestRecorder()
    async with mock_client(recorder_a) as client:
        await sync._push_outbox(client, "http://hub", "hub")
    assert len(recorder_a.requests) == 1

    # A second remote must still receive the same rows.
    recorder_b = RequestRecorder()
    async with mock_client(recorder_b) as client:
        await sync._push_outbox(client, "http://peer", "peer-b")
    assert len(recorder_b.requests) == 1
    ids = [r["id"] for r in json.loads(recorder_b.requests[0].content)["records"]]
    assert ids == ["mem-1"]

    # And re-pushing to the first remote sends nothing new.
    recorder_a2 = RequestRecorder()
    async with mock_client(recorder_a2) as client:
        await sync._push_outbox(client, "http://hub", "hub")
    assert recorder_a2.requests == []


async def test_push_outbox_marks_only_processed_records(
    sync_db: sqlite3.Connection,
) -> None:
    """SY-02: when the remote reports which records it processed, only those
    are marked sent; the rest are retried on the next push."""
    insert_memory(sync_db, "mem-1", "alpha")
    insert_memory(sync_db, "mem-2", "beta")

    recorder = RequestRecorder(
        responses={"/sync/push": {"accepted": 1, "processed_ids": ["mem-1"]}}
    )
    async with mock_client(recorder) as client:
        marked = await sync._push_outbox(client, "http://peer", "peer-x")
    assert marked == 1

    # mem-2 was not processed, so the next push retries it.
    recorder2 = RequestRecorder()
    async with mock_client(recorder2) as client:
        await sync._push_outbox(client, "http://peer", "peer-x")
    assert len(recorder2.requests) == 1
    ids = [r["id"] for r in json.loads(recorder2.requests[0].content)["records"]]
    assert ids == ["mem-2"]


async def test_push_outbox_count_only_remote_marks_batch(
    sync_db: sqlite3.Connection,
) -> None:
    """A legacy remote that only returns a count still marks the whole batch
    (LWW-stale records would never be re-accepted, so retrying is useless)."""
    insert_memory(sync_db, "mem-1")
    insert_memory(sync_db, "mem-2")

    recorder = RequestRecorder(responses={"/sync/push": {"accepted": 1}})
    async with mock_client(recorder) as client:
        marked = await sync._push_outbox(client, "http://hub", "hub")
    assert marked == 2

    recorder2 = RequestRecorder()
    async with mock_client(recorder2) as client:
        await sync._push_outbox(client, "http://hub", "hub")
    assert recorder2.requests == []


# ---------------------------------------------------------------------------
# Pull (keyset cursor + drain loop — SY-04)
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

    # Keyset cursor advanced to the newest record we received
    log_row = sync_db.execute(
        "SELECT last_pull, last_pull_id FROM sync_log WHERE remote_id = 'hub'"
    ).fetchone()
    assert log_row is not None
    assert log_row["last_pull"] == rec["updated_at"]
    assert log_row["last_pull_id"] == "remote-1"


async def test_pull_remote_sends_since_cursor(sync_db: sqlite3.Connection) -> None:
    sync_db.execute(
        """INSERT INTO sync_log (remote_id, last_pull, last_pull_id)
           VALUES ('hub', '2026-01-01T00:00:00+00:00', 'mem-z')"""
    )
    sync_db.commit()

    recorder = RequestRecorder()
    async with mock_client(recorder) as client:
        await sync._pull_remote(client, "http://hub", "hub")

    assert len(recorder.requests) == 1
    params = dict(recorder.requests[0].url.params)
    assert params["since"] == "2026-01-01T00:00:00+00:00"
    assert params["since_id"] == "mem-z"
    assert params["exclude_node"] == sync.NODE_ID


async def test_pull_remote_empty(sync_db: sqlite3.Connection) -> None:
    recorder = RequestRecorder()
    async with mock_client(recorder) as client:
        count = await sync._pull_remote(client, "http://hub", "hub")
    assert count == 0
    # No cursor written when nothing was received
    assert sync_db.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0] == 0


def _paging_pull_handler(all_records: list[dict], page_size: int):
    """Simulate a keyset-paginated server over a fixed record set."""

    def handler(request: httpx.Request) -> dict:
        params = dict(request.url.params)
        since = params.get("since", "")
        since_id = params.get("since_id", "")
        matching = sorted(
            (r for r in all_records if (r["updated_at"], r["id"]) > (since, since_id)),
            key=lambda r: (r["updated_at"], r["id"]),
        )
        page = matching[:page_size]
        return {"records": page, "count": len(page)}

    return handler


async def test_pull_remote_drains_multiple_pages(
    sync_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SY-04: one sync cycle keeps pulling until a short page arrives."""
    monkeypatch.setattr(sync, "PULL_PAGE_SIZE", 2)
    ts1 = "2026-06-01T00:00:00+00:00"
    ts2 = "2026-06-02T00:00:00+00:00"
    all_records = [
        make_record("rec-a", "a", created_at=ts1, updated_at=ts1),
        make_record("rec-b", "b", created_at=ts1, updated_at=ts1),
        make_record("rec-c", "c", created_at=ts2, updated_at=ts2),
    ]
    recorder = RequestRecorder(
        responses={"/sync/pull": _paging_pull_handler(all_records, 2)}
    )
    async with mock_client(recorder) as client:
        count = await sync._pull_remote(client, "http://hub", "hub")

    assert count == 3
    assert len(recorder.requests) == 2  # full page, then short page
    got = sync_db.execute("SELECT id FROM memories ORDER BY id").fetchall()
    assert [r["id"] for r in got] == ["rec-a", "rec-b", "rec-c"]

    log_row = sync_db.execute(
        "SELECT last_pull, last_pull_id FROM sync_log WHERE remote_id = 'hub'"
    ).fetchone()
    assert log_row["last_pull"] == ts2
    assert log_row["last_pull_id"] == "rec-c"


async def test_pull_remote_boundary_ties_not_lost(
    sync_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SY-04: records sharing the page-boundary timestamp must all arrive."""
    monkeypatch.setattr(sync, "PULL_PAGE_SIZE", 2)
    ts = "2026-06-01T00:00:00+00:00"
    all_records = [
        make_record(f"tie-{i}", f"tie {i}", created_at=ts, updated_at=ts)
        for i in range(5)
    ]
    recorder = RequestRecorder(
        responses={"/sync/pull": _paging_pull_handler(all_records, 2)}
    )
    async with mock_client(recorder) as client:
        count = await sync._pull_remote(client, "http://hub", "hub")

    assert count == 5
    assert sync_db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 5


async def test_pull_remote_stuck_cursor_stops(
    sync_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server that keeps replaying the same full page cannot trap the cycle."""
    monkeypatch.setattr(sync, "PULL_PAGE_SIZE", 1)
    rec = make_record("loop-1", "same page forever")
    recorder = RequestRecorder(responses={"/sync/pull": {"records": [rec]}})
    async with mock_client(recorder) as client:
        await sync._pull_remote(client, "http://hub", "hub")

    # First page consumed, second page identical -> cursor no progress -> stop.
    assert len(recorder.requests) <= 3


# ---------------------------------------------------------------------------
# _upsert_records — conflict resolution (SY-03)
# ---------------------------------------------------------------------------


def test_upsert_inserts_new_record(sync_db: sqlite3.Connection) -> None:
    result = sync._upsert_records(sync_db, [make_record("new-1", "hello")])
    assert result.applied == 1
    assert result.failed == 0
    assert result.processed_ids == ["new-1"]
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
    result = sync._upsert_records(sync_db, [rec])
    assert result.applied == 1
    row = sync_db.execute("SELECT content FROM memories WHERE id = 'm1'").fetchone()
    assert row["content"] == "newer remote"


def test_upsert_older_loses(sync_db: sqlite3.Connection) -> None:
    insert_memory(sync_db, "m1", "newer local", updated_at="2026-03-01T00:00:00+00:00")
    rec = make_record(
        "m1", "stale remote",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    result = sync._upsert_records(sync_db, [rec])
    assert result.applied == 0
    # Stale records still count as successfully processed (no retry needed)
    assert result.processed_ids == ["m1"]
    row = sync_db.execute("SELECT content FROM memories WHERE id = 'm1'").fetchone()
    assert row["content"] == "newer local"


def test_upsert_equal_timestamp_is_noop(sync_db: sqlite3.Connection) -> None:
    ts = "2026-03-01T00:00:00+00:00"
    insert_memory(sync_db, "m1", "local", updated_at=ts)
    rec = make_record("m1", "remote echo", created_at=ts, updated_at=ts)
    result = sync._upsert_records(sync_db, [rec])
    assert result.applied == 0
    row = sync_db.execute("SELECT content FROM memories WHERE id = 'm1'").fetchone()
    assert row["content"] == "local"


def test_upsert_parses_string_tags_and_metadata(sync_db: sqlite3.Connection) -> None:
    rec = make_record("m1", tags='["x", "y"]', metadata='{"k": "v"}')
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


def test_upsert_preserves_extended_columns(sync_db: sqlite3.Connection) -> None:
    """SY-03: vitality/classification/structured fields survive the wire."""
    rec = make_record(
        "rich-1",
        "rich record",
        client="claude-code",
        accessed_at="2026-05-01T00:00:00+00:00",
        access_count=7,
        decay_rate=0.05,
        vitality=0.83,
        base_weight=1.5,
        status="active",
        memory_type="decision",
        source_capture_id="cap-9",
        subject="Bailey",
        predicate="prefers",
        object="dark mode",
        superseded_by=None,
    )
    result = sync._upsert_records(sync_db, [rec])
    assert result.applied == 1
    row = sync_db.execute("SELECT * FROM memories WHERE id = 'rich-1'").fetchone()
    assert row["memory_type"] == "decision"
    assert row["access_count"] == 7
    assert row["vitality"] == pytest.approx(0.83)
    assert row["base_weight"] == pytest.approx(1.5)
    assert row["decay_rate"] == pytest.approx(0.05)
    assert row["accessed_at"] == "2026-05-01T00:00:00+00:00"
    assert row["subject"] == "Bailey"
    assert row["predicate"] == "prefers"
    assert row["object"] == "dark mode"
    assert row["client"] == "claude-code"
    assert row["source_capture_id"] == "cap-9"


def test_upsert_extended_columns_updated_on_conflict(
    sync_db: sqlite3.Connection,
) -> None:
    """SY-03: an LWW-winning update also refreshes the extended columns."""
    insert_memory(sync_db, "m1", "v1", updated_at="2026-01-01T00:00:00+00:00")
    rec = make_record(
        "m1", "v2",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-02-01T00:00:00+00:00",
        memory_type="preference",
        vitality=0.42,
        superseded_by="m2",
    )
    sync._upsert_records(sync_db, [rec])
    row = sync_db.execute("SELECT * FROM memories WHERE id = 'm1'").fetchone()
    assert row["memory_type"] == "preference"
    assert row["vitality"] == pytest.approx(0.42)
    assert row["superseded_by"] == "m2"


def test_upsert_supersession_tombstone_propagates(sync_db: sqlite3.Connection) -> None:
    """A remote update that marks a record superseded must apply locally."""
    insert_memory(sync_db, "old-fact", "obsolete", updated_at="2026-01-01T00:00:00+00:00")
    rec = make_record(
        "old-fact",
        "obsolete",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-02-01T00:00:00+00:00",
        superseded_by="new-fact",
    )
    result = sync._upsert_records(sync_db, [rec])
    assert result.applied == 1
    row = sync_db.execute(
        "SELECT superseded_by FROM memories WHERE id = 'old-fact'"
    ).fetchone()
    assert row["superseded_by"] == "new-fact"


def test_upsert_tolerates_records_from_older_nodes(sync_db: sqlite3.Connection) -> None:
    """SY-03: records missing newer schema fields get schema defaults."""
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
    result = sync._upsert_records(sync_db, [rec])
    assert result.applied == 1
    row = sync_db.execute("SELECT * FROM memories WHERE id = 'v2-era'").fetchone()
    assert row["status"] == "active"
    assert row["memory_type"] == "unclassified"
    assert row["access_count"] == 0
    assert row["client"] == "unknown"
    assert row["accessed_at"] == row["created_at"]


def test_upsert_skips_malformed_records(sync_db: sqlite3.Connection) -> None:
    """SY-03: a bad record must not poison the rest of the batch."""
    good_before = make_record("good-1", "first")
    missing_content = {"id": "bad-1", "updated_at": _now_iso(), "created_at": _now_iso()}
    not_a_dict = "totally wrong"
    missing_id = make_record("x")
    del missing_id["id"]
    good_after = make_record("good-2", "last")

    result = sync._upsert_records(
        sync_db, [good_before, missing_content, not_a_dict, missing_id, good_after]
    )
    assert result.applied == 2
    assert result.failed == 3
    assert result.processed_ids == ["good-1", "good-2"]
    ids = {r["id"] for r in sync_db.execute("SELECT id FROM memories").fetchall()}
    assert ids == {"good-1", "good-2"}
    # No transaction left open
    assert not sync_db.in_transaction


def test_upsert_normalizes_timestamps(sync_db: sqlite3.Connection) -> None:
    """SY-08: heterogeneous remote timestamps are canonicalized on ingest."""
    rec = make_record(
        "z-ts",
        created_at="2026-06-01 10:00:00",  # SQLite trigger style
        updated_at="2026-06-01T12:00:00Z",  # hub style
    )
    result = sync._upsert_records(sync_db, [rec])
    assert result.applied == 1
    row = sync_db.execute(
        "SELECT created_at, updated_at FROM memories WHERE id = 'z-ts'"
    ).fetchone()
    assert row["created_at"] == "2026-06-01T10:00:00+00:00"
    assert row["updated_at"] == "2026-06-01T12:00:00+00:00"


def test_upsert_z_timestamp_beats_local_format(sync_db: sqlite3.Connection) -> None:
    """SY-08: a Z-suffixed remote timestamp must still win LWW correctly."""
    insert_memory(sync_db, "m1", "local", updated_at="2026-01-01T00:00:00+00:00")
    rec = make_record(
        "m1", "remote", created_at="2026-01-01T00:00:00Z", updated_at="2026-02-01T00:00:00Z"
    )
    result = sync._upsert_records(sync_db, [rec])
    assert result.applied == 1
    row = sync_db.execute("SELECT content FROM memories WHERE id = 'm1'").fetchone()
    assert row["content"] == "remote"


def test_upsert_unparseable_timestamp_fails_record(sync_db: sqlite3.Connection) -> None:
    rec = make_record("m1", updated_at="not a time")
    result = sync._upsert_records(sync_db, [rec])
    assert result.applied == 0
    assert result.failed == 1
    assert result.processed_ids == []


# ---------------------------------------------------------------------------
# Echo suppression (SY-05)
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


def test_upsert_echo_suppression_spares_local_edits(sync_db: sqlite3.Connection) -> None:
    """SY-05: only the outbox rows created BY the upsert are suppressed —
    a pending local edit's row must survive."""
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
    assert local_row["sent_at"] == "", "local edit's outbox row must stay pending"

    echo_rows = sync_db.execute(
        "SELECT sent_at FROM sync_outbox WHERE memory_id = 'm1' AND id != ?",
        (local_outbox_id,),
    ).fetchall()
    assert echo_rows
    assert all(r["sent_at"] != "" for r in echo_rows)


# ---------------------------------------------------------------------------
# Embedding on ingest (SY-06)
# ---------------------------------------------------------------------------


def test_upsert_embeds_applied_records(
    sync_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    embedded: list[tuple[int, str]] = []

    def fake_embed(rows: list[tuple[int, str]]) -> int:
        embedded.extend(rows)
        return len(rows)

    monkeypatch.setattr(sync, "_embed_and_store_rows", fake_embed)

    insert_memory(sync_db, "stale", "newer local", updated_at="2026-03-01T00:00:00+00:00")
    recs = [
        make_record("fresh", "embed me"),
        make_record(
            "stale", "loses lww",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        ),
    ]
    sync._upsert_records(sync_db, recs)

    contents = [c for _, c in embedded]
    assert contents == ["embed me"], "only applied records are embedded"


def test_upsert_embedding_failure_is_nonfatal(
    sync_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(rows: list[tuple[int, str]]) -> int:
        raise RuntimeError("embedder exploded")

    monkeypatch.setattr(sync, "_embed_and_store_rows", boom)
    result = sync._upsert_records(sync_db, [make_record("m1", "content")])
    assert result.applied == 1
    assert sync_db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Outbox pruning (SY-07)
# ---------------------------------------------------------------------------


def test_prune_outbox_removes_old_and_suppressed_rows(
    sync_db: sqlite3.Connection,
) -> None:
    old_ts = (datetime.now(UTC) - timedelta(days=45)).isoformat()
    fresh_ts = _now_iso()
    sync_db.executemany(
        """INSERT INTO sync_outbox (memory_id, operation, payload, created_at, sent_at)
           VALUES (?, 'insert', '{}', ?, ?)""",
        [
            ("old-row", old_ts, ""),       # past retention -> pruned
            ("echo-row", fresh_ts, fresh_ts),  # suppressed echo -> pruned
            ("fresh-row", fresh_ts, ""),   # pending -> kept
        ],
    )
    sync_db.execute(
        "INSERT INTO sync_sends (remote_id, outbox_id, sent_at) "
        "SELECT 'hub', id, ? FROM sync_outbox", (fresh_ts,)
    )
    sync_db.commit()

    removed = sync._prune_outbox(sync_db)
    assert removed == 2

    remaining = sync_db.execute("SELECT memory_id FROM sync_outbox").fetchall()
    assert [r["memory_id"] for r in remaining] == ["fresh-row"]
    # Orphaned per-remote send markers were cleaned up too
    assert sync_db.execute("SELECT COUNT(*) FROM sync_sends").fetchone()[0] == 1


def test_outbox_triggers_gated_on_sync_enabled(sync_db: sqlite3.Connection) -> None:
    """SY-07: with the sync_enabled flag off, the outbox does not accumulate."""
    sync_db.execute("UPDATE sync_flags SET value = '0' WHERE key = 'sync_enabled'")
    sync_db.commit()

    insert_memory(sync_db, "silent", "no outbox row")
    assert sync_db.execute("SELECT COUNT(*) FROM sync_outbox").fetchone()[0] == 0

    # Re-enabling the flag turns the triggers back on.
    sync_db.execute("UPDATE sync_flags SET value = '1' WHERE key = 'sync_enabled'")
    sync_db.commit()
    insert_memory(sync_db, "loud", "outbox row")
    rows = sync_db.execute("SELECT memory_id FROM sync_outbox").fetchall()
    assert [r["memory_id"] for r in rows] == ["loud"]


def test_outbox_trigger_timestamp_is_canonical(sync_db: sqlite3.Connection) -> None:
    """SY-08: trigger-written created_at uses canonical ISO-8601 UTC."""
    insert_memory(sync_db, "ts-check", "content")
    row = sync_db.execute(
        "SELECT created_at FROM sync_outbox WHERE memory_id = 'ts-check'"
    ).fetchone()
    created = row["created_at"]
    assert "T" in created
    assert created.endswith("+00:00")
    # Round-trips through the canonicalizer unchanged
    assert sync._canon_ts(created) == created


# ---------------------------------------------------------------------------
# v9 schema migration / sync_enabled reconciliation
# ---------------------------------------------------------------------------


def test_v9_schema_objects_exist(sync_db: sqlite3.Connection) -> None:
    """SY-02/SY-04/SY-09: v9 adds sync_sends, sync_flags, last_pull_id, and
    the memories(updated_at) index."""
    tables = {
        r[0]
        for r in sync_db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {"sync_sends", "sync_flags"} <= tables

    indexes = {
        r[0]
        for r in sync_db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    assert "idx_memories_updated_at" in indexes
    assert "idx_outbox_created_at" in indexes

    cols = {r[1] for r in sync_db.execute("PRAGMA table_info(sync_log)").fetchall()}
    assert "last_pull_id" in cols


def test_reconcile_disabled_truncates_outbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """SY-07: with sync disabled, startup truncates whatever accumulated."""
    import remind_me_mcp.config as config

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    monkeypatch.setattr(config, "SYNC_ENABLED", True)
    _ensure_schema(db)
    db.execute(
        """INSERT INTO memories (id, content, created_at, updated_at)
           VALUES ('m1', 'hello', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"""
    )
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM sync_outbox").fetchone()[0] == 1

    # Restart with sync disabled -> outbox is truncated, flag flips to '0'.
    monkeypatch.setattr(config, "SYNC_ENABLED", False)
    _db_mod._reconcile_sync_enabled_flag(db)
    assert db.execute("SELECT COUNT(*) FROM sync_outbox").fetchone()[0] == 0
    flag = db.execute(
        "SELECT value FROM sync_flags WHERE key = 'sync_enabled'"
    ).fetchone()
    assert flag["value"] == "0"
    db.close()


def test_reconcile_enable_backfills_outbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """SY-07: enabling sync backfills memories created while it was off."""
    import remind_me_mcp.config as config

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    monkeypatch.setattr(config, "SYNC_ENABLED", False)
    _ensure_schema(db)
    db.execute(
        """INSERT INTO memories (id, content, created_at, updated_at)
           VALUES ('quiet', 'made while sync off', '2026-01-01T00:00:00+00:00',
                   '2026-01-01T00:00:00+00:00')"""
    )
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM sync_outbox").fetchone()[0] == 0

    # Restart with sync enabled -> the memory is backfilled into the outbox.
    monkeypatch.setattr(config, "SYNC_ENABLED", True)
    _db_mod._reconcile_sync_enabled_flag(db)
    rows = db.execute("SELECT memory_id, payload FROM sync_outbox").fetchall()
    assert [r["memory_id"] for r in rows] == ["quiet"]
    payload = json.loads(rows[0]["payload"])
    assert payload["content"] == "made while sync off"
    assert payload["status"] == "active"  # full-column payload
    db.close()


# ---------------------------------------------------------------------------
# Peer discovery (SY-09: STATIC_PEERS / TAILSCALE_SOCKET)
# ---------------------------------------------------------------------------


async def test_discover_peers_without_tailscale(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """With no Tailscale daemon socket, discovery degrades to no peers."""
    import remind_me_mcp.config as config

    monkeypatch.setattr(config, "TAILSCALE_SOCKET", str(tmp_path / "missing.sock"))
    monkeypatch.setattr(config, "STATIC_PEERS", [])
    peers = await sync._discover_peers()
    assert peers == []


async def test_discover_peers_returns_static_peers(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """SY-09: STATIC_PEERS from config are honored."""
    import remind_me_mcp.config as config

    monkeypatch.setattr(config, "TAILSCALE_SOCKET", str(tmp_path / "missing.sock"))
    monkeypatch.setattr(
        config,
        "STATIC_PEERS",
        [{"node_id": "laptop", "url": "http://100.64.0.9:8766"}],
    )
    peers = await sync._discover_peers()
    assert peers == [{"node_id": "laptop", "url": "http://100.64.0.9:8766"}]


async def test_discover_peers_ignores_malformed_static_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    import remind_me_mcp.config as config

    monkeypatch.setattr(config, "TAILSCALE_SOCKET", str(tmp_path / "missing.sock"))
    monkeypatch.setattr(
        config,
        "STATIC_PEERS",
        ["not-a-dict", {"node_id": "x"}, {"url": "http://ok:1"}, 42],
    )
    peers = await sync._discover_peers()
    assert peers == []


def test_tailscale_socket_config_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """SY-09: TAILSCALE_SOCKET config overrides the platform default."""
    import remind_me_mcp.config as config

    monkeypatch.setattr(config, "TAILSCALE_SOCKET", "/custom/tailscaled.sock")
    assert sync._tailscale_socket() == "/custom/tailscaled.sock"

    monkeypatch.setattr(config, "TAILSCALE_SOCKET", "")
    monkeypatch.setattr(sync.sys, "platform", "darwin")
    assert sync._tailscale_socket() == "/var/run/tailscaled.socket"
    monkeypatch.setattr(sync.sys, "platform", "linux")
    assert sync._tailscale_socket() == "/var/run/tailscale/tailscaled.sock"


async def test_discover_peers_parses_tailscale_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """Tailscale peers come from the local API: online peers with IPs only,
    deduplicated against STATIC_PEERS by URL."""
    import remind_me_mcp.config as config

    status = {
        "Peer": {
            "p1": {"Online": True, "TailscaleIPs": ["100.64.0.1"], "HostName": "alpha"},
            "p2": {"Online": False, "TailscaleIPs": ["100.64.0.2"], "HostName": "off"},
            "p3": {"Online": True, "TailscaleIPs": [], "HostName": "no-ip"},
            "p4": {"Online": True, "TailscaleIPs": ["100.64.0.4"], "HostName": "static-dup"},
        }
    }
    recorder = RequestRecorder(responses={"/localapi/v0/status": status})
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(config, "TAILSCALE_SOCKET", str(tmp_path / "fake.sock"))
    monkeypatch.setattr(
        config,
        "STATIC_PEERS",
        [{"node_id": "static-dup", "url": f"http://100.64.0.4:{sync.PEER_PORT}"}],
    )
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_async_client(
            **{**kw, "transport": httpx.MockTransport(recorder)}
        ),
    )

    peers = await sync._discover_peers()
    by_node = {p["node_id"]: p["url"] for p in peers}
    assert by_node == {
        "static-dup": f"http://100.64.0.4:{sync.PEER_PORT}",
        "alpha": f"http://100.64.0.1:{sync.PEER_PORT}",
    }


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


async def test_sync_once_prunes_outbox(
    sync_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SY-07: each cycle prunes echo-suppressed/expired outbox rows."""
    old_ts = (datetime.now(UTC) - timedelta(days=45)).isoformat()
    sync_db.execute(
        """INSERT INTO sync_outbox (memory_id, operation, payload, created_at, sent_at)
           VALUES ('ancient', 'insert', '{}', ?, '')""",
        (old_ts,),
    )
    sync_db.commit()

    recorder = RequestRecorder()
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(sync, "HUB_URL", "")
    monkeypatch.setattr(sync, "_discover_peers", _no_peers)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_async_client(
            **{**kw, "transport": httpx.MockTransport(recorder)}
        ),
    )

    await sync._sync_once()
    assert sync_db.execute("SELECT COUNT(*) FROM sync_outbox").fetchone()[0] == 0


async def test_sync_once_wraps_cycle_in_telemetry_span(
    sync_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 7a: each sync cycle is wrapped in a 'sync.cycle' OTEL span
    (a no-op unless REMIND_ME_OTEL_ENABLED is set)."""
    recorder = RequestRecorder()
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(sync, "HUB_URL", "")
    monkeypatch.setattr(sync, "_discover_peers", _no_peers)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_async_client(
            **{**kw, "transport": httpx.MockTransport(recorder)}
        ),
    )

    spans: list[str] = []
    real_maybe_span = sync.maybe_span

    def spy_maybe_span(name, **attrs):
        spans.append(name)
        return real_maybe_span(name, **attrs)

    monkeypatch.setattr(sync, "maybe_span", spy_maybe_span)

    await sync._sync_once()

    assert spans == ["sync.cycle"]


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
        return [{"node_id": sync.NODE_ID, "url": "http://self"}]

    monkeypatch.setattr(sync, "NODE_ID", "this-node")
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


async def test_sync_loop_returns_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sync, "SYNC_ENABLED", False)
    await sync.sync_loop()  # returns immediately, no network or DB access


# The deliberate SystemExit that stops the daemon thread trips pytest's
# unhandled-thread-exception detector; that is the expected mechanism here.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_start_sync_thread_runs_cycles(monkeypatch: pytest.MonkeyPatch) -> None:
    """The daemon thread drives _sync_once on its own event loop."""
    import threading

    ran = threading.Event()

    async def fake_sync_once() -> None:
        ran.set()
        # SystemExit is not caught by the thread's except Exception — it
        # terminates the loop so no orphan thread outlives the test.
        raise SystemExit

    monkeypatch.setattr(sync, "_sync_once", fake_sync_once)
    thread = sync.start_sync_thread()
    assert thread.daemon
    assert ran.wait(timeout=5), "_sync_once never ran in the sync thread"
    thread.join(timeout=5)
    assert not thread.is_alive()


# ---------------------------------------------------------------------------
# Entity graph sync (FT-04)
# ---------------------------------------------------------------------------


def make_entity_record(name: str, **overrides: Any) -> dict:
    """Build a wire-format entity record like an FT-04 peer would send."""
    from remind_me_mcp.db import _entity_id

    now = _now_iso()
    rec = {
        "record_type": "entity",
        "id": _entity_id(name),
        "name": name,
        "kind": None,
        "aliases": [],
        "created_at": now,
        "updated_at": now,
        "node_id": "remote-node",
    }
    rec.update(overrides)
    return rec


def make_link_record(memory_id: str, entity_id: str, **overrides: Any) -> dict:
    rec = {
        "record_type": "memory_entity",
        "id": f"{memory_id}|{entity_id}",
        "memory_id": memory_id,
        "entity_id": entity_id,
        "created_at": _now_iso(),
    }
    rec.update(overrides)
    return rec


def insert_entity(
    db: sqlite3.Connection,
    name: str,
    *,
    kind: str | None = None,
    aliases: list[str] | None = None,
    updated_at: str | None = None,
) -> str:
    """Insert an entity through SQL so the outbox triggers fire."""
    from remind_me_mcp.db import _entity_id

    now = updated_at or _now_iso()
    eid = _entity_id(name)
    db.execute(
        """INSERT INTO entities (id, name, kind, aliases, created_at, updated_at, node_id)
           VALUES (?, ?, ?, ?, ?, ?, 'local-node')""",
        (eid, name, kind, json.dumps(aliases or []), now, now),
    )
    db.commit()
    return eid


async def test_push_outbox_sends_entity_and_link_records(
    sync_db: sqlite3.Connection,
) -> None:
    """Entity/link outbox rows ride the same push, tagged with record_type,
    with aliases deserialized into a real list on the wire."""
    eid = insert_entity(sync_db, "Bailey Robertson", kind="person", aliases=["Bailey"])
    sync_db.execute(
        "INSERT INTO memory_entities (memory_id, entity_id, created_at) VALUES (?, ?, ?)",
        ("mem-1", eid, _now_iso()),
    )
    sync_db.commit()

    recorder = RequestRecorder()
    async with mock_client(recorder) as client:
        await sync._push_outbox(client, "http://peer", "peer-x")

    records = json.loads(recorder.requests[0].content)["records"]
    by_type = {r.get("record_type", "memory"): r for r in records}
    assert by_type["entity"]["id"] == eid
    assert by_type["entity"]["aliases"] == ["Bailey"]
    assert by_type["memory_entity"]["id"] == f"mem-1|{eid}"
    assert by_type["memory_entity"]["entity_id"] == eid


async def test_push_marks_link_records_via_composite_id(
    sync_db: sqlite3.Connection,
) -> None:
    """processed_ids matching works for link records (synthetic memory|entity id)."""
    eid = insert_entity(sync_db, "remind_me")
    sync_db.execute(
        "INSERT INTO memory_entities (memory_id, entity_id, created_at) VALUES (?, ?, ?)",
        ("mem-1", eid, _now_iso()),
    )
    sync_db.commit()

    recorder = RequestRecorder(
        responses={"/sync/push": {
            "accepted": 2,
            "processed_ids": [eid, f"mem-1|{eid}"],
        }}
    )
    async with mock_client(recorder) as client:
        marked = await sync._push_outbox(client, "http://peer", "peer-x")
    assert marked == 2

    # Nothing left to retry.
    recorder2 = RequestRecorder()
    async with mock_client(recorder2) as client:
        await sync._push_outbox(client, "http://peer", "peer-x")
    assert recorder2.requests == []


def test_upsert_entity_record_inserts(sync_db: sqlite3.Connection) -> None:
    rec = make_entity_record("Tailscale", kind="tool", aliases=["ts"])
    result = sync._upsert_records(sync_db, [rec])
    assert result.applied == 1
    assert result.processed_ids == [rec["id"]]
    row = sync_db.execute("SELECT * FROM entities WHERE id = ?", (rec["id"],)).fetchone()
    assert row["name"] == "Tailscale"
    assert row["kind"] == "tool"
    assert json.loads(row["aliases"]) == ["ts"]
    assert row["node_id"] == "remote-node"


def test_upsert_entity_lww_newer_wins_aliases_union(
    sync_db: sqlite3.Connection,
) -> None:
    """LWW on updated_at for name/kind EXCEPT aliases, which union-merge."""
    eid = insert_entity(
        sync_db, "Bailey", aliases=["B"], updated_at="2026-01-01T00:00:00+00:00"
    )
    rec = make_entity_record(
        "bailey",  # remote casing wins LWW (same deterministic id)
        kind="person",
        aliases=["Bails"],
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-02-01T00:00:00+00:00",
    )
    result = sync._upsert_records(sync_db, [rec])
    assert result.applied == 1
    row = sync_db.execute("SELECT * FROM entities WHERE id = ?", (eid,)).fetchone()
    assert row["name"] == "bailey"
    assert row["kind"] == "person"
    assert row["updated_at"] == "2026-02-01T00:00:00+00:00"
    # Union: local aliases survive the LWW loss of the row.
    assert json.loads(row["aliases"]) == ["B", "Bails"]


def test_upsert_entity_lww_loser_still_merges_aliases(
    sync_db: sqlite3.Connection,
) -> None:
    """A stale record cannot rename the entity but its aliases still merge,
    without bumping updated_at (no churn loops)."""
    eid = insert_entity(
        sync_db, "Bailey", kind="person", aliases=["B"],
        updated_at="2026-03-01T00:00:00+00:00",
    )
    rec = make_entity_record(
        "Bailey",
        aliases=["Bails", "B"],
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    rec["name"] = "STALE NAME"  # same id, stale rename attempt
    result = sync._upsert_records(sync_db, [rec])
    assert result.applied == 1  # aliases changed -> local state changed
    row = sync_db.execute("SELECT * FROM entities WHERE id = ?", (eid,)).fetchone()
    assert row["name"] == "Bailey"
    assert row["kind"] == "person"
    assert row["updated_at"] == "2026-03-01T00:00:00+00:00"
    assert json.loads(row["aliases"]) == ["B", "Bails"]


def test_upsert_entity_identical_record_is_noop(sync_db: sqlite3.Connection) -> None:
    ts = "2026-01-01T00:00:00+00:00"
    eid = insert_entity(sync_db, "Bailey", aliases=["B"], updated_at=ts)
    rec = make_entity_record(
        "Bailey", aliases=["B"], created_at=ts, updated_at=ts
    )
    result = sync._upsert_records(sync_db, [rec])
    assert result.applied == 0
    assert result.processed_ids == [eid]  # stale/no-op still counts processed


def test_upsert_entity_suppresses_echo_outbox_rows(
    sync_db: sqlite3.Connection,
) -> None:
    """SY-05 pattern: the outbox rows created by applying a remote entity
    record are marked sent so they are never pushed back out."""
    rec = make_entity_record("Echo Entity")
    sync._upsert_records(sync_db, [rec])
    rows = sync_db.execute(
        "SELECT sent_at FROM sync_outbox WHERE memory_id = ?", (rec["id"],)
    ).fetchall()
    assert rows, "entity outbox trigger should have fired"
    assert all(r["sent_at"] != "" for r in rows)


def test_upsert_entity_malformed_is_isolated(sync_db: sqlite3.Connection) -> None:
    bad = {"record_type": "entity", "id": "x", "updated_at": _now_iso()}  # no name
    good = make_entity_record("Good Entity")
    result = sync._upsert_records(sync_db, [bad, good])
    assert result.applied == 1
    assert result.failed == 1
    assert result.processed_ids == [good["id"]]


def test_upsert_unknown_record_type_is_defensive(
    sync_db: sqlite3.Connection,
) -> None:
    """A record kind from a newer peer is skipped without poisoning the batch."""
    future = {"record_type": "hologram", "id": "h1", "updated_at": _now_iso()}
    good = make_record("mem-ok")
    result = sync._upsert_records(sync_db, [future, good])
    assert result.applied == 1
    assert result.failed == 1
    assert result.processed_ids == ["mem-ok"]


def test_upsert_link_record_insert_or_ignore(sync_db: sqlite3.Connection) -> None:
    rec = make_link_record("mem-1", "ent-1")
    first = sync._upsert_records(sync_db, [rec])
    assert first.applied == 1
    assert first.processed_ids == ["mem-1|ent-1"]
    again = sync._upsert_records(sync_db, [rec])
    assert again.applied == 0
    assert again.processed_ids == ["mem-1|ent-1"]
    assert sync_db.execute("SELECT COUNT(*) FROM memory_entities").fetchone()[0] == 1


def test_upsert_link_suppresses_echo_outbox_rows(sync_db: sqlite3.Connection) -> None:
    rec = make_link_record("mem-9", "ent-9")
    sync._upsert_records(sync_db, [rec])
    rows = sync_db.execute(
        "SELECT sent_at FROM sync_outbox WHERE memory_id = 'mem-9'"
    ).fetchall()
    assert rows
    assert all(r["sent_at"] != "" for r in rows)


async def test_pull_entities_upserts_and_advances_cursor(
    sync_db: sqlite3.Connection,
) -> None:
    rec = make_entity_record("Pulled Entity", updated_at="2026-05-01T00:00:00+00:00")
    recorder = RequestRecorder(
        responses={"/sync/pull_entities": {"records": [rec], "count": 1}}
    )
    async with mock_client(recorder) as client:
        count = await sync._pull_entities(client, "http://peer", "peer-x")
    assert count == 1
    assert sync_db.execute(
        "SELECT COUNT(*) FROM entities WHERE id = ?", (rec["id"],)
    ).fetchone()[0] == 1
    # Cursor stored under its own remote key, separate from the memory cursor.
    log_row = sync_db.execute(
        "SELECT last_pull, last_pull_id FROM sync_log WHERE remote_id = 'peer-x#entities'"
    ).fetchone()
    assert log_row["last_pull"] == "2026-05-01T00:00:00+00:00"
    assert log_row["last_pull_id"] == rec["id"]


async def test_pull_links_upserts_records(sync_db: sqlite3.Connection) -> None:
    rec = make_link_record("mem-a", "ent-a")
    recorder = RequestRecorder(
        responses={"/sync/pull_links": {"records": [rec], "count": 1}}
    )
    async with mock_client(recorder) as client:
        count = await sync._pull_links(client, "http://peer", "peer-x")
    assert count == 1
    row = sync_db.execute("SELECT * FROM memory_entities").fetchone()
    assert (row["memory_id"], row["entity_id"]) == ("mem-a", "ent-a")
    log_row = sync_db.execute(
        "SELECT last_pull_id FROM sync_log WHERE remote_id = 'peer-x#links'"
    ).fetchone()
    assert log_row["last_pull_id"] == "mem-a|ent-a"


async def test_pull_entities_tolerates_pre_ft04_peer(
    sync_db: sqlite3.Connection,
) -> None:
    """An old peer 404s the entity endpoints; the pull is a silent no-op."""
    recorder = RequestRecorder()  # 404s everything but the legacy endpoints
    async with mock_client(recorder) as client:
        assert await sync._pull_entities(client, "http://old-peer", "old") == 0
        assert await sync._pull_links(client, "http://old-peer", "old") == 0
    assert sync_db.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0] == 0


async def test_two_db_entity_round_trip(
    sync_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Entities and links converge between two databases: A pushes to B (via
    B's _upsert_records), then B's rows are pulled back into A — including a
    same-name entity created independently on both sides (deterministic ids
    make them the same row; aliases union)."""
    db_b = sqlite3.connect(":memory:", check_same_thread=False)
    db_b.row_factory = sqlite3.Row
    _ensure_schema(db_b)
    db_b.execute(
        "INSERT OR REPLACE INTO sync_flags (key, value) VALUES ('sync_enabled', '1')"
    )
    db_b.commit()

    # Same entity created independently on both machines, different aliases.
    eid = insert_entity(
        sync_db, "Bailey Robertson", kind="person", aliases=["Bailey"],
        updated_at="2026-01-01T00:00:00+00:00",
    )
    eid_b = insert_entity(
        db_b, "Bailey Robertson", aliases=["BR"],
        updated_at="2026-02-01T00:00:00+00:00",
    )
    assert eid == eid_b  # deterministic ids converge
    insert_memory(sync_db, "mem-1", "Bailey fact")
    sync_db.execute(
        "INSERT INTO memory_entities (memory_id, entity_id, created_at) VALUES (?, ?, ?)",
        ("mem-1", eid, _now_iso()),
    )
    sync_db.commit()

    # --- A pushes its outbox; the "remote" applies records into db_b ---
    def push_handler(request: httpx.Request) -> dict:
        records = json.loads(request.content)["records"]
        result = sync._upsert_records(db_b, records)
        return {
            "accepted": result.applied,
            "processed_ids": result.processed_ids,
            "failed": result.failed,
        }

    recorder = RequestRecorder(responses={"/sync/push": push_handler})
    async with mock_client(recorder) as client:
        await sync._push_outbox(client, "http://b", "node-b")

    row_b = db_b.execute("SELECT * FROM entities WHERE id = ?", (eid,)).fetchone()
    assert row_b is not None
    # B's row was newer -> keeps B's updated_at; aliases union both sides.
    assert row_b["updated_at"] == "2026-02-01T00:00:00+00:00"
    assert set(json.loads(row_b["aliases"])) == {"Bailey", "BR"}
    assert row_b["kind"] == "person"  # filled from A even though B's row won LWW
    link_b = db_b.execute("SELECT * FROM memory_entities").fetchone()
    assert (link_b["memory_id"], link_b["entity_id"]) == ("mem-1", eid)

    # --- A pulls B's entities back; both sides converge on the alias set ---
    def pull_entities_handler(request: httpx.Request) -> dict:
        rows = db_b.execute("SELECT * FROM entities").fetchall()
        records = []
        for r in rows:
            d = dict(r)
            d["aliases"] = json.loads(d["aliases"])
            d["record_type"] = "entity"
            records.append(d)
        return {"records": records, "count": len(records)}

    recorder2 = RequestRecorder(
        responses={"/sync/pull_entities": pull_entities_handler}
    )
    async with mock_client(recorder2) as client:
        await sync._pull_entities(client, "http://b", "node-b")

    row_a = sync_db.execute("SELECT * FROM entities WHERE id = ?", (eid,)).fetchone()
    assert set(json.loads(row_a["aliases"])) == {"Bailey", "BR"}
    assert row_a["updated_at"] == "2026-02-01T00:00:00+00:00"
    db_b.close()


def test_reconcile_enable_backfills_entity_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SY-07 + FT-04: enabling sync backfills entities and links too."""
    import remind_me_mcp.config as config

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    monkeypatch.setattr(config, "SYNC_ENABLED", False)
    _ensure_schema(db)
    now = _now_iso()
    db.execute(
        """INSERT INTO entities (id, name, kind, aliases, created_at, updated_at)
           VALUES ('e1', 'Quiet Entity', NULL, '[]', ?, ?)""",
        (now, now),
    )
    db.execute(
        "INSERT INTO memory_entities (memory_id, entity_id, created_at) VALUES ('m1', 'e1', ?)",
        (now,),
    )
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM sync_outbox").fetchone()[0] == 0

    monkeypatch.setattr(config, "SYNC_ENABLED", True)
    _db_mod._reconcile_sync_enabled_flag(db)
    payloads = [
        json.loads(r["payload"])
        for r in db.execute("SELECT payload FROM sync_outbox").fetchall()
    ]
    types = sorted(p.get("record_type", "memory") for p in payloads)
    assert types == ["entity", "memory_entity"]
    db.close()


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


# ---------------------------------------------------------------------------
# Entity relation sync (Phase 3)
# ---------------------------------------------------------------------------


def make_entity_relation_record(
    subject_entity_id: str, relation: str, object_entity_id: str, **overrides: Any
) -> dict:
    """Build a wire-format entity_relation record like a Phase-3 peer would send."""
    from remind_me_mcp.db import _entity_relation_id

    now = _now_iso()
    rec = {
        "record_type": "entity_relation",
        "id": _entity_relation_id(subject_entity_id, relation, object_entity_id),
        "subject_entity_id": subject_entity_id,
        "relation": relation,
        "object_entity_id": object_entity_id,
        "created_at": now,
        "updated_at": now,
        "node_id": "remote-node",
    }
    rec.update(overrides)
    return rec


def insert_entity_relation(
    db: sqlite3.Connection,
    subject_entity_id: str,
    relation: str,
    object_entity_id: str,
) -> str:
    """Insert an entity_relations row through SQL so the outbox trigger fires."""
    from remind_me_mcp.db import _entity_relation_id

    now = _now_iso()
    rid = _entity_relation_id(subject_entity_id, relation, object_entity_id)
    db.execute(
        """INSERT INTO entity_relations
           (id, subject_entity_id, relation, object_entity_id, created_at, updated_at, node_id)
           VALUES (?, ?, ?, ?, ?, ?, 'local-node')""",
        (rid, subject_entity_id, relation, object_entity_id, now, now),
    )
    db.commit()
    return rid


async def test_push_outbox_sends_entity_relation_records(
    sync_db: sqlite3.Connection,
) -> None:
    rid = insert_entity_relation(sync_db, "subj-1", "works_with", "obj-1")

    recorder = RequestRecorder()
    async with mock_client(recorder) as client:
        await sync._push_outbox(client, "http://peer", "peer-x")

    records = json.loads(recorder.requests[0].content)["records"]
    by_type = {r.get("record_type", "memory"): r for r in records}
    assert by_type["entity_relation"]["id"] == rid
    assert by_type["entity_relation"]["subject_entity_id"] == "subj-1"
    assert by_type["entity_relation"]["relation"] == "works_with"
    assert by_type["entity_relation"]["object_entity_id"] == "obj-1"


def test_upsert_entity_relation_record_inserts(sync_db: sqlite3.Connection) -> None:
    rec = make_entity_relation_record("subj-1", "works_with", "obj-1")
    result = sync._upsert_records(sync_db, [rec])
    assert result.applied == 1
    assert result.processed_ids == [rec["id"]]
    row = sync_db.execute(
        "SELECT * FROM entity_relations WHERE id = ?", (rec["id"],)
    ).fetchone()
    assert row["subject_entity_id"] == "subj-1"
    assert row["relation"] == "works_with"
    assert row["object_entity_id"] == "obj-1"


def test_upsert_entity_relation_insert_or_ignore(sync_db: sqlite3.Connection) -> None:
    rec = make_entity_relation_record("subj-1", "works_with", "obj-1")
    first = sync._upsert_records(sync_db, [rec])
    assert first.applied == 1
    again = sync._upsert_records(sync_db, [rec])
    assert again.applied == 0
    assert again.processed_ids == [rec["id"]]
    assert sync_db.execute(
        "SELECT COUNT(*) FROM entity_relations"
    ).fetchone()[0] == 1


def test_upsert_entity_relation_suppresses_echo_outbox_rows(
    sync_db: sqlite3.Connection,
) -> None:
    rec = make_entity_relation_record("subj-9", "knows", "obj-9")
    sync._upsert_records(sync_db, [rec])
    rows = sync_db.execute(
        "SELECT sent_at FROM sync_outbox WHERE memory_id = ?", (rec["id"],)
    ).fetchall()
    assert rows
    assert all(r["sent_at"] != "" for r in rows)


def test_upsert_entity_relation_malformed_is_isolated(sync_db: sqlite3.Connection) -> None:
    """A record missing required keys is skipped without poisoning the batch."""
    bad = {"record_type": "entity_relation", "id": "r1"}  # missing subject/relation/object
    good = make_entity_relation_record("subj-ok", "knows", "obj-ok")
    result = sync._upsert_records(sync_db, [bad, good])
    assert result.applied == 1
    assert result.failed == 1
    assert result.processed_ids == [good["id"]]


async def test_pull_entity_relations_upserts_and_advances_cursor(
    sync_db: sqlite3.Connection,
) -> None:
    rec = make_entity_relation_record(
        "subj-1", "works_with", "obj-1", created_at="2026-05-01T00:00:00+00:00"
    )
    recorder = RequestRecorder(
        responses={"/sync/pull_entity_relations": {"records": [rec], "count": 1}}
    )
    async with mock_client(recorder) as client:
        count = await sync._pull_entity_relations(client, "http://peer", "peer-x")
    assert count == 1
    assert sync_db.execute(
        "SELECT COUNT(*) FROM entity_relations WHERE id = ?", (rec["id"],)
    ).fetchone()[0] == 1
    # Cursor stored under its own remote key, separate from entities/links.
    log_row = sync_db.execute(
        "SELECT last_pull, last_pull_id FROM sync_log WHERE remote_id = 'peer-x#entity_relations'"
    ).fetchone()
    assert log_row["last_pull"] == "2026-05-01T00:00:00+00:00"
    assert log_row["last_pull_id"] == rec["id"]


async def test_pull_entity_relations_tolerates_old_peer(
    sync_db: sqlite3.Connection,
) -> None:
    """An old peer/hub 404s the endpoint; the pull is a silent no-op."""
    recorder = RequestRecorder()  # 404s everything but the legacy endpoints
    async with mock_client(recorder) as client:
        assert await sync._pull_entity_relations(client, "http://old-peer", "old") == 0
    assert sync_db.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0] == 0
