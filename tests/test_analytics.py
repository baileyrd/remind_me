"""
Tests for remind_me_mcp.analytics — daily analytics trend snapshots (issue #186).

Covers the v24->v25 schema migration (analytics_snapshots table + index),
capture_analytics_snapshot's row shape and per-day idempotency,
_compact_analytics_snapshots retention, the scheduler-loop wiring for both
capture and retention (mirroring test_digest.py's/test_history.py's
scheduler-loop-wiring test shape), and the GET /api/analytics/trend route
(shape, auth including issue #185 scoped read keys, and the empty-history
case). Follows test_digest.py's/test_history.py's shape: db_conn/
memory_factory fixtures for an isolated in-memory database, a local
scheduler_db fixture for the thread-wiring tests.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from remind_me_mcp import analytics
from remind_me_mcp.api import _build_api_app
from remind_me_mcp.api_keys import ApiKeyStore

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


def test_v24_to_v25_adds_analytics_snapshots_table(db_conn: sqlite3.Connection) -> None:
    tables = {
        r["name"]
        for r in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert "analytics_snapshots" in tables

    cols = {r["name"] for r in db_conn.execute("PRAGMA table_info(analytics_snapshots)").fetchall()}
    assert cols == {"id", "captured_at", "total_memories", "vitality_buckets", "category_counts"}


def test_v24_to_v25_adds_captured_at_index(db_conn: sqlite3.Connection) -> None:
    indexes = {
        r["name"]
        for r in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'analytics_snapshots'"
        ).fetchall()
    }
    assert "idx_analytics_snapshots_captured_at" in indexes


def test_schema_version_is_25(db_conn: sqlite3.Connection) -> None:
    version = db_conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 25


# ---------------------------------------------------------------------------
# capture_analytics_snapshot
# ---------------------------------------------------------------------------


def test_capture_inserts_a_row_from_known_state(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    memory_factory(content="a work memory", category="work")
    memory_factory(content="another work memory", category="work")
    memory_factory(content="a personal memory", category="personal")

    row_id = analytics.capture_analytics_snapshot(db_conn)
    assert row_id is not None

    row = db_conn.execute(
        "SELECT * FROM analytics_snapshots WHERE id = ?", (row_id,)
    ).fetchone()
    assert row["total_memories"] == 3
    category_counts = json.loads(row["category_counts"])
    assert category_counts == {"work": 2, "personal": 1}
    vitality_buckets = json.loads(row["vitality_buckets"])
    assert vitality_buckets.get("0.75+") == 3  # freshly-created memories


def test_capture_matches_build_vitality_report(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """The snapshot's vitality_buckets/total_memories must be exactly what
    vitality.build_vitality_report itself would compute right now -- the
    snapshot must never be able to disagree with the live report."""
    from remind_me_mcp.vitality import build_vitality_report

    memory_factory(content="one")
    memory_factory(content="two", memory_type="decision")

    expected = build_vitality_report(db_conn)
    row_id = analytics.capture_analytics_snapshot(db_conn)
    row = db_conn.execute(
        "SELECT * FROM analytics_snapshots WHERE id = ?", (row_id,)
    ).fetchone()

    assert row["total_memories"] == expected["total_memories"]
    assert json.loads(row["vitality_buckets"]) == expected["vitality_buckets"]


def test_capture_twice_same_day_does_not_duplicate(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    memory_factory(content="hello")

    first = analytics.capture_analytics_snapshot(db_conn)
    second = analytics.capture_analytics_snapshot(db_conn)

    assert first is not None
    assert second is None
    count = db_conn.execute("SELECT COUNT(*) FROM analytics_snapshots").fetchone()[0]
    assert count == 1


def test_capture_on_two_different_dates_creates_two_rows(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    memory_factory(content="hello")

    day1 = datetime(2026, 1, 1, tzinfo=UTC)
    day2 = datetime(2026, 1, 2, tzinfo=UTC)

    first = analytics.capture_analytics_snapshot(db_conn, now=day1)
    second = analytics.capture_analytics_snapshot(db_conn, now=day2)

    assert first is not None
    assert second is not None
    assert first != second
    count = db_conn.execute("SELECT COUNT(*) FROM analytics_snapshots").fetchone()[0]
    assert count == 2


def test_capture_restart_mid_day_is_idempotent_by_date_not_exact_timestamp(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """A server restart a few seconds/minutes later on the same day must not
    produce a duplicate -- the idempotency check is by calendar date, not
    exact timestamp equality."""
    memory_factory(content="hello")

    morning = datetime(2026, 1, 1, 8, 0, 0, tzinfo=UTC)
    evening = datetime(2026, 1, 1, 20, 0, 0, tzinfo=UTC)

    first = analytics.capture_analytics_snapshot(db_conn, now=morning)
    second = analytics.capture_analytics_snapshot(db_conn, now=evening)

    assert first is not None
    assert second is None
    count = db_conn.execute("SELECT COUNT(*) FROM analytics_snapshots").fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# get_analytics_trend
# ---------------------------------------------------------------------------


def test_get_analytics_trend_empty(db_conn: sqlite3.Connection) -> None:
    assert analytics.get_analytics_trend(db_conn) == []


def test_get_analytics_trend_returns_oldest_first(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    memory_factory(content="hello")
    day1 = datetime(2026, 1, 1, tzinfo=UTC)
    day2 = datetime(2026, 1, 2, tzinfo=UTC)
    analytics.capture_analytics_snapshot(db_conn, now=day2)
    analytics.capture_analytics_snapshot(db_conn, now=day1)

    trend = analytics.get_analytics_trend(db_conn)
    assert len(trend) == 2
    assert trend[0]["captured_at"] < trend[1]["captured_at"]
    for snap in trend:
        assert set(snap) == {"captured_at", "total_memories", "vitality_buckets", "category_counts"}
        assert isinstance(snap["vitality_buckets"], dict)
        assert isinstance(snap["category_counts"], dict)


# ---------------------------------------------------------------------------
# is_analytics_snapshot_due / maybe_capture_analytics_snapshot
# ---------------------------------------------------------------------------


def test_is_due_when_never_captured(db_conn: sqlite3.Connection) -> None:
    assert analytics.is_analytics_snapshot_due(db_conn) is True


def test_maybe_capture_captures_once_then_throttles_within_the_interval(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    memory_factory(content="hello")

    first = analytics.maybe_capture_analytics_snapshot(db_conn)
    second = analytics.maybe_capture_analytics_snapshot(db_conn)

    assert first is True
    assert second is False
    count = db_conn.execute("SELECT COUNT(*) FROM analytics_snapshots").fetchone()[0]
    assert count == 1


def test_maybe_capture_fires_again_once_the_interval_elapses(
    db_conn: sqlite3.Connection, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory_factory(content="hello")

    analytics.maybe_capture_analytics_snapshot(db_conn)
    # Simulate the interval having elapsed by rewinding the persisted watermark.
    stale = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    db_conn.execute(
        "UPDATE sync_flags SET value = ? WHERE key = 'analytics_last_snapshot_at'",
        (stale,),
    )
    db_conn.commit()

    second = analytics.maybe_capture_analytics_snapshot(db_conn)
    assert second is True


# ---------------------------------------------------------------------------
# _compact_analytics_snapshots retention
# ---------------------------------------------------------------------------


def _insert_snapshot(db: sqlite3.Connection, captured_at: str, total: int = 1) -> None:
    db.execute(
        """INSERT INTO analytics_snapshots
               (captured_at, total_memories, vitality_buckets, category_counts)
           VALUES (?, ?, '{}', '{}')""",
        (captured_at, total),
    )
    db.commit()


def test_compact_analytics_snapshots_prunes_old_rows(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import remind_me_mcp.db as db_mod

    monkeypatch.setattr(db_mod, "ANALYTICS_RETENTION_DAYS", 30)

    old_ts = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    _insert_snapshot(db_conn, old_ts)

    removed = db_mod._compact_analytics_snapshots(db_conn)
    assert removed == 1
    count = db_conn.execute("SELECT COUNT(*) FROM analytics_snapshots").fetchone()[0]
    assert count == 0


def test_compact_analytics_snapshots_keeps_recent_rows(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import remind_me_mcp.db as db_mod

    monkeypatch.setattr(db_mod, "ANALYTICS_RETENTION_DAYS", 730)

    recent_ts = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    _insert_snapshot(db_conn, recent_ts)

    removed = db_mod._compact_analytics_snapshots(db_conn)
    assert removed == 0
    count = db_conn.execute("SELECT COUNT(*) FROM analytics_snapshots").fetchone()[0]
    assert count == 1


def test_compact_analytics_snapshots_mixed_ages_only_prunes_old(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import remind_me_mcp.db as db_mod

    monkeypatch.setattr(db_mod, "ANALYTICS_RETENTION_DAYS", 30)

    old_ts = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    recent_ts = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    _insert_snapshot(db_conn, old_ts, total=1)
    _insert_snapshot(db_conn, recent_ts, total=2)

    removed = db_mod._compact_analytics_snapshots(db_conn)
    assert removed == 1
    remaining = db_conn.execute("SELECT total_memories FROM analytics_snapshots").fetchall()
    assert [r["total_memories"] for r in remaining] == [2]


# ---------------------------------------------------------------------------
# Scheduler-loop wiring (extends the existing reminder poll loop, issue #186)
# ---------------------------------------------------------------------------


@pytest.fixture()
def scheduler_db(db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    """Point remind_me_mcp.scheduler at the shared in-memory test database
    (mirrors test_digest.py's/test_history.py's fixture of the same name)."""
    import remind_me_mcp.scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "_get_db", lambda: db_conn)
    return db_conn


def test_scheduler_loop_checks_for_a_due_snapshot_each_tick(
    scheduler_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The snapshot-capture check rides the existing reminder-poll thread
    (design decision: no second background thread) -- assert the loop
    actually calls it, using a short poll interval and a real
    (bounded-wait) thread rather than a sleep guess."""
    import remind_me_mcp.analytics as analytics_mod
    import remind_me_mcp.scheduler as scheduler_mod

    called = threading.Event()

    def _spy(db):
        called.set()
        return False

    monkeypatch.setattr(analytics_mod, "maybe_capture_analytics_snapshot", _spy)
    monkeypatch.setattr(scheduler_mod, "REMINDER_POLL_INTERVAL", 0.05)

    thread = scheduler_mod.start_scheduler()
    try:
        assert called.wait(timeout=3.0)
    finally:
        scheduler_mod.stop_scheduler(timeout=5.0)
        assert not thread.is_alive()


def test_compact_analytics_snapshots_invoked_from_scheduler_loop(
    scheduler_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integration-level check: the always-on reminder scheduler loop --
    not sync.py's SYNC_ENABLED-gated loop -- is what calls
    _compact_analytics_snapshots, since snapshots accumulate regardless of
    whether sync is configured. Follows test_history.py's scheduler-loop-
    wiring test shape."""
    import remind_me_mcp.scheduler as scheduler_mod

    called = threading.Event()

    def _spy(db):
        called.set()
        return 0

    monkeypatch.setattr(scheduler_mod, "_compact_analytics_snapshots", _spy)
    monkeypatch.setattr(scheduler_mod, "REMINDER_POLL_INTERVAL", 0.05)

    thread = scheduler_mod.start_scheduler()
    try:
        assert called.wait(timeout=3.0)
    finally:
        scheduler_mod.stop_scheduler(timeout=5.0)
        assert not thread.is_alive()


# ---------------------------------------------------------------------------
# GET /api/analytics/trend
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(db_conn, monkeypatch):
    """Mirrors test_api.py's `client` fixture: auth disabled, isolated db."""
    import remind_me_mcp.config as _cfg
    import remind_me_mcp.importer as _importer_mod

    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db_conn)
    monkeypatch.setattr(_cfg, "API_KEY", "disabled")

    app = _build_api_app()
    return TestClient(app)


@pytest.fixture()
def scoped_client(db_conn: sqlite3.Connection, monkeypatch, tmp_path: Path):
    """Mirrors test_api_keys.py's `scoped_client` fixture: default key plus
    an isolated ApiKeyStore for issue #185 scoped-key auth checks."""
    import remind_me_mcp.config as _cfg
    import remind_me_mcp.importer as _importer_mod

    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db_conn)
    monkeypatch.setattr(_cfg, "API_KEY", "default-secret-key")
    monkeypatch.setattr(_cfg, "MEMORY_DIR", tmp_path)

    store = ApiKeyStore(tmp_path / "api_keys.json")
    app = _build_api_app()
    client = TestClient(app)
    return client, store


def test_api_analytics_trend_empty_history_returns_empty_array(client: TestClient) -> None:
    response = client.get("/api/analytics/trend")
    assert response.status_code == 200
    data = response.json()
    assert data["snapshots"] == []


def test_api_analytics_trend_returns_expected_shape(
    client: TestClient, db_conn: sqlite3.Connection, memory_factory
) -> None:
    memory_factory(content="one", category="work")
    analytics.capture_analytics_snapshot(db_conn)

    response = client.get("/api/analytics/trend")
    assert response.status_code == 200
    data = response.json()
    assert len(data["snapshots"]) == 1
    snap = data["snapshots"][0]
    assert set(snap) == {"captured_at", "total_memories", "vitality_buckets", "category_counts"}
    assert snap["total_memories"] == 1
    assert snap["category_counts"] == {"work": 1}


def test_api_analytics_trend_requires_auth_when_enabled(
    scoped_client, db_conn: sqlite3.Connection
) -> None:
    client, _store = scoped_client
    r = client.get("/api/analytics/trend")
    assert r.status_code == 401


def test_api_analytics_trend_rejects_unknown_bearer_token(scoped_client) -> None:
    client, _store = scoped_client
    r = client.get(
        "/api/analytics/trend", headers={"Authorization": "Bearer totally-made-up"}
    )
    assert r.status_code == 401


def test_api_analytics_trend_default_key_can_read(scoped_client) -> None:
    client, _store = scoped_client
    r = client.get(
        "/api/analytics/trend", headers={"Authorization": "Bearer default-secret-key"}
    )
    assert r.status_code == 200
    assert r.json()["snapshots"] == []


def test_api_analytics_trend_read_scoped_key_can_read(scoped_client) -> None:
    """A read-scoped key (issue #185) can GET this route -- it's a GET, so
    it's not in _SCOPE_MUTATING_METHODS and composes with no extra logic."""
    client, store = scoped_client
    key = store.create_key("viewer", "read")

    r = client.get("/api/analytics/trend", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    assert r.json()["snapshots"] == []
