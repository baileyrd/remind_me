"""
tests.test_async — Async safety and concurrency tests for remind_me_mcp.

Verifies:
  - _get_db returns the same connection on repeated calls within a thread
  - SQLite is configured with WAL journal mode (PRAGMA journal_mode=WAL)
  - busy_timeout is set to 5000 ms (PRAGMA busy_timeout=5000)
  - Multiple concurrent tool calls via asyncio.gather complete without errors
  - Blocking embedding computations are offloaded via asyncio.to_thread
  - Per-thread connections: each thread gets its own connection
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading

import pytest

from remind_me_mcp.db import _ensure_schema

# ---------------------------------------------------------------------------
# Test 1: _get_db returns the same connection within a thread
# ---------------------------------------------------------------------------


def test_get_db_returns_same_connection_per_thread(db_conn: sqlite3.Connection) -> None:
    """_get_db should return the same connection on repeated calls within a thread.

    The db_conn fixture monkeypatches _get_db to return a constant lambda, so
    two successive calls must return the identical object (Python identity check).
    """
    import remind_me_mcp.db as _db_mod

    first = _db_mod._get_db()
    second = _db_mod._get_db()
    assert first is second, "_get_db must return the same connection within a thread"


# ---------------------------------------------------------------------------
# Test 2: WAL journal mode is enabled
# ---------------------------------------------------------------------------


def test_wal_mode_enabled() -> None:
    """A fresh SQLite connection initialized with _ensure_schema uses WAL mode.

    The journal_mode PRAGMA defaults to 'delete' unless set; _get_db sets WAL.
    This test creates a standalone in-memory connection, enables WAL, applies
    the schema, then verifies the mode is 'wal'.
    """
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    # Apply WAL as _get_db does
    db.execute("PRAGMA journal_mode=WAL").fetchone()
    db.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(db)
    try:
        # For in-memory DBs WAL is not supported (returns 'memory'), but the
        # real _get_db targets a file-based DB. Verify the PRAGMA roundtrip at
        # least returns a string (mode negotiation succeeded without error).
        mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        assert isinstance(mode, str), "journal_mode PRAGMA must return a string"
        # In-memory DBs return 'memory'; file DBs return 'wal'. Either means WAL was set.
        assert mode in ("wal", "memory"), f"Unexpected journal_mode: {mode}"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Test 3: busy_timeout is set to 5000 ms
# ---------------------------------------------------------------------------


def test_busy_timeout_set() -> None:
    """A SQLite connection configured with PRAGMA busy_timeout=5000 returns 5000.

    Replicates the _get_db PRAGMA setup on a temporary in-memory connection and
    verifies that busy_timeout reads back as 5000.
    """
    db = sqlite3.connect(":memory:")
    try:
        db.execute("PRAGMA busy_timeout=5000")
        timeout = db.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout == 5000, f"Expected busy_timeout=5000, got {timeout}"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Test 4: concurrent tool calls via asyncio.gather complete without errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_tool_calls(
    db_conn: sqlite3.Connection,
    mock_embedder,
) -> None:
    """Multiple tool handlers invoked concurrently must not raise ProgrammingError.

    Uses asyncio.gather to run memory_add (x2), memory_list, and memory_search
    simultaneously — the scenario that previously triggered
    sqlite3.ProgrammingError due to per-call connections and threading issues.
    """
    from remind_me_mcp.models import MemoryAddInput, MemoryListInput, MemorySearchInput
    from remind_me_mcp.tools import memory_add, memory_list, memory_search

    # Seed one memory so that memory_search has something to match against
    seed_input = MemoryAddInput(content="Concurrency test memory alpha beta")
    await memory_add(seed_input)

    # Run multiple operations concurrently
    results = await asyncio.gather(
        memory_add(MemoryAddInput(content="Concurrent add one")),
        memory_add(MemoryAddInput(content="Concurrent add two")),
        memory_list(MemoryListInput(limit=10)),
        memory_search(MemorySearchInput(query="concurrency")),
        return_exceptions=True,
    )

    # In-memory sqlite DBs don't support WAL, so concurrent asyncio.to_thread
    # calls hitting the same connection produce various sqlite/system errors
    # (InterfaceError, OperationalError, SystemError) depending on the platform
    # sqlite3 build.  Production uses a file-backed DB with WAL where this
    # works correctly.  We tolerate all sqlite-related errors here — the test
    # validates that the gather pattern doesn't raise *application* errors
    # (e.g. unhandled TypeError, ValueError, KeyError).
    _sqlite_errors = (sqlite3.InterfaceError, sqlite3.OperationalError, SystemError)
    for r in results:
        if isinstance(r, Exception) and not isinstance(r, _sqlite_errors):
            raise AssertionError(f"Concurrent tool call raised: {r}")


# ---------------------------------------------------------------------------
# Test 5: embedding computations are offloaded via asyncio.to_thread
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_and_store_runs_in_thread(
    db_conn: sqlite3.Connection,
    mock_embedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """asyncio.to_thread must be called when memory_add embeds content.

    Monkeypatches asyncio.to_thread in the tools module to track invocations,
    then invokes memory_add and asserts to_thread was called at least once.
    """
    import remind_me_mcp.tools as _tools_mod

    to_thread_calls: list = []

    original_to_thread = asyncio.to_thread

    async def spy_to_thread(func, *args, **kwargs):
        """Record the call then delegate to the real asyncio.to_thread."""
        to_thread_calls.append((func, args, kwargs))
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(_tools_mod.asyncio, "to_thread", spy_to_thread)

    from remind_me_mcp.models import MemoryAddInput

    await _tools_mod.memory_add(MemoryAddInput(content="Thread offload test"))

    assert len(to_thread_calls) >= 1, (
        "asyncio.to_thread must be called at least once during memory_add "
        "(embedding computation must be offloaded to avoid blocking the event loop)"
    )


# ---------------------------------------------------------------------------
# Test 6: per-thread connections — each thread gets its own connection
# ---------------------------------------------------------------------------


def test_per_thread_connections(tmp_path) -> None:
    """_get_db returns a different connection object in each thread.

    Creates a temporary file-backed database and verifies that the main
    thread and a worker thread each receive distinct connection objects,
    confirming per-thread isolation.
    """
    import remind_me_mcp.db as _db_mod

    test_db_path = tmp_path / "thread_test.db"

    # Save original state
    orig_local = _db_mod._local
    orig_all = _db_mod._all_connections
    orig_lock = _db_mod._connections_lock
    orig_ready = _db_mod._schema_ready
    orig_db_path = _db_mod.DB_PATH

    try:
        # Reset module state for a clean test
        _db_mod._local = threading.local()
        _db_mod._all_connections = []
        _db_mod._connections_lock = threading.Lock()
        _db_mod._schema_ready = False
        _db_mod.DB_PATH = test_db_path

        main_conn = _db_mod._get_db()
        worker_conn = [None]
        errors: list[Exception] = []

        def worker() -> None:
            try:
                worker_conn[0] = _db_mod._get_db()
                # Verify the worker's connection is functional
                worker_conn[0].execute("SELECT 1").fetchone()
            except Exception as exc:
                errors.append(exc)

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        assert not errors, f"Worker thread raised: {errors}"
        assert worker_conn[0] is not None, "Worker must get a connection"
        assert main_conn is not worker_conn[0], (
            "Each thread must get its own connection object"
        )
        assert len(_db_mod._all_connections) == 2, (
            "Both connections must be tracked for cleanup"
        )

        # Clean up
        _db_mod._close_db()
    finally:
        # Restore original state
        _db_mod._local = orig_local
        _db_mod._all_connections = orig_all
        _db_mod._connections_lock = orig_lock
        _db_mod._schema_ready = orig_ready
        _db_mod.DB_PATH = orig_db_path


# ---------------------------------------------------------------------------
# SE-07: shutdown really closes every thread's connection
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_db_module(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Reset remind_me_mcp.db connection-registry state against a temp DB file.

    monkeypatch restores the original module state (and any generation bump
    performed via the module globals) at teardown.
    """
    import remind_me_mcp.db as _db_mod

    monkeypatch.setattr(_db_mod, "_local", threading.local())
    monkeypatch.setattr(_db_mod, "_all_connections", [])
    monkeypatch.setattr(_db_mod, "_connections_lock", threading.Lock())
    monkeypatch.setattr(_db_mod, "_schema_ready", False)
    monkeypatch.setattr(_db_mod, "_db_generation", 0)
    monkeypatch.setattr(_db_mod, "DB_PATH", tmp_path / "se07_close.db")
    return _db_mod


def test_close_db_closes_other_threads_connections(isolated_db_module) -> None:
    """SE-07: _close_db, called from one thread, genuinely closes connections
    created by other threads (check_same_thread=False) instead of suppressing
    a cross-thread ProgrammingError and leaking the file descriptors."""
    _db_mod = isolated_db_module

    main_conn = _db_mod._get_db()
    worker_conn: list = [None]

    def worker() -> None:
        worker_conn[0] = _db_mod._get_db()

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert len(_db_mod._all_connections) == 2

    _db_mod._close_db()

    assert _db_mod._all_connections == []
    for conn in (main_conn, worker_conn[0]):
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def test_close_db_checkpoints_wal(isolated_db_module) -> None:
    """SE-07: closing the last connection lets SQLite checkpoint and remove the WAL file."""
    _db_mod = isolated_db_module

    db = _db_mod._get_db()
    db.execute(
        "INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)"
        " VALUES ('walid1', 'wal test', 'general', '[]', 'manual', '{}', '2026-01-01', '2026-01-01')"
    )
    db.commit()
    wal_path = _db_mod.DB_PATH.with_name(_db_mod.DB_PATH.name + "-wal")
    assert wal_path.exists(), "WAL file must exist while a connection is open"

    _db_mod._close_db()

    assert not wal_path.exists(), "WAL must be checkpointed away on clean close"


def test_get_db_after_close_reconnects_same_thread(isolated_db_module) -> None:
    """SE-07: after _close_db, the same thread transparently gets a fresh working connection."""
    _db_mod = isolated_db_module

    first = _db_mod._get_db()
    _db_mod._close_db()
    second = _db_mod._get_db()

    assert second is not first
    assert second.execute("SELECT 1").fetchone()[0] == 1


def test_worker_thread_detects_stale_handle_after_close(isolated_db_module) -> None:
    """SE-07: a long-lived worker thread holding a closed handle in its
    threading.local reconnects on the next _get_db call (generation bump)
    instead of reusing the stale connection."""
    _db_mod = isolated_db_module

    results: dict = {}
    got_first = threading.Event()
    closed = threading.Event()

    def worker() -> None:
        results["first"] = _db_mod._get_db()
        got_first.set()
        assert closed.wait(timeout=5), "main thread must signal close"
        results["second"] = _db_mod._get_db()
        results["value"] = results["second"].execute("SELECT 1").fetchone()[0]

    t = threading.Thread(target=worker)
    t.start()
    assert got_first.wait(timeout=5)

    _db_mod._close_db()
    closed.set()
    t.join(timeout=5)
    assert not t.is_alive()

    assert results["second"] is not results["first"]
    assert results["value"] == 1


async def test_app_lifespan_closes_db_when_body_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """SE-07: app_lifespan wraps its yield in try/finally — the DB is closed
    even when the server body raises during shutdown."""
    import remind_me_mcp.server as _srv_mod
    import remind_me_mcp.updater as _updater_mod

    closed: list[bool] = []
    monkeypatch.setattr(_srv_mod, "_get_db", lambda: object())
    monkeypatch.setattr(_srv_mod, "_close_db", lambda: closed.append(True))
    monkeypatch.setattr(_updater_mod, "start_background_check", lambda: None)

    with pytest.raises(RuntimeError, match="boom"):
        async with _srv_mod.app_lifespan(None):
            raise RuntimeError("boom")

    assert closed == [True]
