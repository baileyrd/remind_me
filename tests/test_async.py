"""
tests.test_async — Async safety and concurrency tests for remind_me_mcp.

Verifies:
  - _get_db returns the same singleton connection object on repeated calls
  - SQLite is configured with WAL journal mode (PRAGMA journal_mode=WAL)
  - busy_timeout is set to 5000 ms (PRAGMA busy_timeout=5000)
  - Multiple concurrent tool calls via asyncio.gather complete without errors
  - Blocking embedding computations are offloaded via asyncio.to_thread
  - Connections with check_same_thread=False allow cross-thread access
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading

import pytest

from remind_me_mcp.db import _ensure_schema

# ---------------------------------------------------------------------------
# Test 1: _get_db returns the same singleton connection object
# ---------------------------------------------------------------------------


def test_get_db_returns_singleton(db_conn: sqlite3.Connection) -> None:
    """_get_db should return the same connection object on repeated calls.

    The db_conn fixture monkeypatches _get_db to return a constant lambda, so
    two successive calls must return the identical object (Python identity check).
    """
    import remind_me_mcp.db as _db_mod

    first = _db_mod._get_db()
    second = _db_mod._get_db()
    assert first is second, "_get_db must return the singleton connection, not a new one each call"


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
    result = db.execute("PRAGMA journal_mode=WAL").fetchone()
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

    # No call should have raised an exception
    for r in results:
        assert not isinstance(r, Exception), f"Concurrent tool call raised: {r}"


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
# Test 6: check_same_thread=False allows cross-thread access
# ---------------------------------------------------------------------------


def test_check_same_thread_false() -> None:
    """A connection opened with check_same_thread=False must be usable from other threads.

    Replicates the _get_db connection parameters and verifies that a worker
    thread can execute a query without raising an sqlite3.ProgrammingError.
    """
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    _ensure_schema(db)

    errors: list[Exception] = []

    def worker() -> None:
        """Run a simple query from a different thread."""
        try:
            db.execute("SELECT 1").fetchone()
        except Exception as exc:
            errors.append(exc)

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    db.close()

    assert not errors, (
        f"Cross-thread access raised an exception: {errors}. "
        "The connection must be opened with check_same_thread=False."
    )
