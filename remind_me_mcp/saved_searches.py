"""
remind_me_mcp.saved_searches — saved/watched search core logic (issue #194).

Follows the reminders.py/digest.py precedent (issues #179/#188): the plain,
FastMCP-free logic lives here, and ``tools/saved_searches.py`` is a thin
wrapper around it plus response formatting -- exactly like
``tools/reminders.py`` wraps ``reminders.list_reminders``/
``digest.build_digest_data``.

**Reusing remind_me_search's core rather than re-extracting it**: issue #194
asked to check whether ``remind_me_search`` was already factored into a
callable-without-FastMCP core (per the #188 precedent) and, if not, to
extract one. It was checked: ``tools.search.memory_search`` -- the function
``@mcp.tool`` decorates -- is already a plain ``async def`` that takes only
the ``MemorySearchInput`` pydantic model, with no ``Context``/request object
and no dependency on FastMCP's dispatch machinery to run; the existing test
suite already calls it directly, unwrapped, well over 80 times
(``await memory_search(params)``) across ``tests/test_tools.py`` and others.
That already satisfies "callable without going through FastMCP" in every
practical sense. Re-extracting its ~400-line hybrid-retrieval body into a
second module on top of that would duplicate logic with no behavioral
benefit, and would risk silently diverging from remind_me_search's real
behavior (structured-query routing, RRF weighting, reranking, the
``_pkg.<name>`` patchable-lookup seam ~80 existing tests already monkeypatch
through -- see search.py's own module docstring for HY-02). So
:func:`execute_saved_search` below calls ``memory_search`` directly instead:
a saved search is, precisely, a stored, replayable ``remind_me_search`` call,
and this guarantees byte-for-byte identical behavior to calling
``remind_me_search`` with the same params, because it *is* that call.

**Storage shape for "already notified" state**: unlike the digest check's
single ``sync_flags`` watermark (one global "have I sent since X" fact),
watch-polling needs a per-(saved search, memory) fact -- "have I already
notified about this specific match" -- which a single key/value row per
search cannot represent (a search's matching set changes over time as
memories are added; the watermark would have to be a memory id, and a new
match could arrive with an *older* id than one already seen, e.g. a
backdated import, silently missing it). A dedicated
``saved_search_seen_memories`` table (memory id set per saved search) is the
right shape instead -- see the v26->v27 migration's docstring in db.py.

**First-poll seeding** (the easy-to-get-wrong edge case the issue calls
out): turning on ``watch=true`` on a saved search that already has matches
must not notify on all of them -- none of them are actually *new*, they were
just never looked at before. :func:`poll_saved_search` detects "this is the
first poll ever for this saved search" by checking whether
``saved_search_seen_memories`` has *any* row for it yet; if not, every
currently-matching memory id is recorded as seen and the function returns
without calling ``notify()`` at all. Only a *later* poll, whose results
contain an id absent from the now-seeded seen-set, is a genuine new match.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from remind_me_mcp import config, notifications
from remind_me_mcp.db import _get_db, _make_id, _now_iso
from remind_me_mcp.models import MemorySearchInput, ResponseFormat

if TYPE_CHECKING:
    import sqlite3

log = logging.getLogger("remind_me_mcp.saved_searches")

# Cap on results fetched per poll for new-match diffing -- deliberately the
# model's own max (MemorySearchInput.limit <= 100), not an unbounded fetch:
# a saved search that regularly matches more than 100 memories needs a
# narrower query, not a bigger poll window.
_POLL_RESULT_LIMIT = 100

_POLL_FLAG_KEY = "saved_search_last_polled_at"


# ---------------------------------------------------------------------------
# CRUD (plain SQL against saved_searches / saved_search_seen_memories)
# ---------------------------------------------------------------------------


def _row_to_saved_search(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    try:
        d["filters"] = json.loads(d["filters"]) if d.get("filters") else {}
    except (TypeError, ValueError):
        log.warning("Malformed filters JSON for saved search %r; treating as empty", d.get("name"))
        d["filters"] = {}
    d["watch"] = bool(d["watch"])
    return d


def save_search(
    db: sqlite3.Connection,
    name: str,
    query: str,
    category: str | None = None,
    tags: list[str] | None = None,
    include_sensitive: bool = False,
    watch: bool = False,
) -> dict[str, Any]:
    """Create a saved search, or update it in place if *name* already exists.

    An update-by-name (rather than a duplicate insert) mirrors
    ``remind_me_wiki_write``'s "same name is the same logical thing"
    convention for pages -- re-saving under the same name is how a caller is
    expected to change a saved search's query/filters/watch flag, not a
    separate toggle tool.

    Args:
        db: An open SQLite connection.
        name: Unique name identifying this saved search.
        query: The search query to store and later re-run.
        category: Optional category filter to store alongside the query.
        tags: Optional tag filter (memory must have ALL of these).
        include_sensitive: Whether re-running this search includes memories
            marked sensitive (issue #195). Defaults to False, mirroring
            remind_me_search's own default.
        watch: Whether the background scheduler should poll this search for
            new matches (see :func:`poll_saved_search`).

    Returns:
        The stored saved search as a dict (id, name, query, filters, watch,
        created_at, updated_at).
    """
    filters = {"category": category, "tags": tags, "include_sensitive": include_sensitive}
    now = _now_iso()
    existing = db.execute(
        "SELECT id FROM saved_searches WHERE name = ?", (name,)
    ).fetchone()
    if existing is not None:
        search_id = existing["id"]
        db.execute(
            """UPDATE saved_searches
               SET query = ?, filters = ?, watch = ?, updated_at = ?
               WHERE id = ?""",
            (query, json.dumps(filters), int(watch), now, search_id),
        )
    else:
        search_id = _make_id(name)
        db.execute(
            """INSERT INTO saved_searches
                   (id, name, query, filters, watch, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (search_id, name, query, json.dumps(filters), int(watch), now, now),
        )
    db.commit()
    row = db.execute("SELECT * FROM saved_searches WHERE id = ?", (search_id,)).fetchone()
    return _row_to_saved_search(row)


def list_saved_searches(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every saved search, alphabetical by name.

    Args:
        db: An open SQLite connection.

    Returns:
        A list of saved search dicts (see :func:`save_search`'s return shape).
    """
    rows = db.execute("SELECT * FROM saved_searches ORDER BY name ASC").fetchall()
    return [_row_to_saved_search(r) for r in rows]


def get_saved_search(db: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    """Fetch one saved search by name, or None if it doesn't exist.

    Args:
        db: An open SQLite connection.
        name: The saved search's name.

    Returns:
        The saved search dict, or None.
    """
    row = db.execute("SELECT * FROM saved_searches WHERE name = ?", (name,)).fetchone()
    return _row_to_saved_search(row) if row is not None else None


def delete_saved_search(db: sqlite3.Connection, name: str) -> bool:
    """Delete a saved search by name, and its seen-memory tracking rows.

    Explicit cleanup of ``saved_search_seen_memories`` (rather than leaving
    orphaned rows keyed by an id that no longer resolves to anything): those
    rows are otherwise unreachable dead weight the moment the parent saved
    search is gone -- nothing will ever query them again by that id -- so
    deleting them here is straightforward hygiene, matching this codebase's
    general discipline around not leaving orphaned rows behind a delete
    (e.g. ``memory_delete``'s chunk-vector cleanup, DI-01 in BACKLOG.md).

    Args:
        db: An open SQLite connection.
        name: The saved search's name.

    Returns:
        True if a saved search with this name existed and was deleted,
        False if there was nothing to delete.
    """
    row = db.execute("SELECT id FROM saved_searches WHERE name = ?", (name,)).fetchone()
    if row is None:
        return False
    search_id = row["id"]
    db.execute(
        "DELETE FROM saved_search_seen_memories WHERE saved_search_id = ?", (search_id,)
    )
    db.execute("DELETE FROM saved_searches WHERE id = ?", (search_id,))
    db.commit()
    return True


# ---------------------------------------------------------------------------
# Running a saved search through the real remind_me_search core
# ---------------------------------------------------------------------------


def build_search_params(saved: dict[str, Any], **overrides: Any) -> MemorySearchInput:
    """Build the MemorySearchInput a saved search's stored query/filters imply.

    Args:
        saved: A saved search dict (see :func:`save_search`'s return shape).
        **overrides: Extra/overriding MemorySearchInput fields -- used by
            :func:`poll_saved_search` to force response_format=json and a
            larger limit for diffing, without those choices leaking into
            :func:`execute_saved_search`'s on-demand (tool-facing) defaults.

    Returns:
        A MemorySearchInput ready to pass to ``tools.search.memory_search``.
    """
    filters = saved.get("filters") or {}
    kwargs: dict[str, Any] = {
        "query": saved["query"],
        "category": filters.get("category"),
        "tags": filters.get("tags"),
        "include_sensitive": bool(filters.get("include_sensitive", False)),
    }
    kwargs.update(overrides)
    return MemorySearchInput(**kwargs)


async def execute_saved_search(saved: dict[str, Any], **overrides: Any) -> str:
    """Re-run a saved search's stored query/filters through the real search core.

    Calls ``tools.search.memory_search`` directly (imported lazily -- see
    this module's docstring for why importing the ``tools`` package at
    saved_searches.py's own module-load time is avoided, mirroring
    scheduler.py's lazy imports of digest/analytics), so the result is
    guaranteed identical to calling ``remind_me_search`` with equivalent
    params -- it *is* that same call, not a re-implementation of it.

    Args:
        saved: A saved search dict.
        **overrides: See :func:`build_search_params`.

    Returns:
        The exact string ``remind_me_search`` would return for these params.
    """
    from remind_me_mcp.tools.search import memory_search

    params = build_search_params(saved, **overrides)
    return await memory_search(params)


# ---------------------------------------------------------------------------
# Watch polling
# ---------------------------------------------------------------------------


def _seen_memory_ids(db: sqlite3.Connection, saved_search_id: str) -> set[str]:
    rows = db.execute(
        "SELECT memory_id FROM saved_search_seen_memories WHERE saved_search_id = ?",
        (saved_search_id,),
    ).fetchall()
    return {r["memory_id"] for r in rows}


def _has_any_seen(db: sqlite3.Connection, saved_search_id: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM saved_search_seen_memories WHERE saved_search_id = ? LIMIT 1",
        (saved_search_id,),
    ).fetchone()
    return row is not None


def _mark_seen(
    db: sqlite3.Connection, saved_search_id: str, memory_ids: list[str], when: str | None = None
) -> None:
    ts = when or _now_iso()
    db.executemany(
        """INSERT OR IGNORE INTO saved_search_seen_memories
               (saved_search_id, memory_id, first_seen_at)
           VALUES (?, ?, ?)""",
        [(saved_search_id, mid, ts) for mid in memory_ids],
    )


async def poll_saved_search(db: sqlite3.Connection, saved: dict[str, Any]) -> int:
    """Poll one watched saved search once; notify on genuinely new matches.

    Runs the saved search's stored query/filters (via
    :func:`execute_saved_search`, forcing JSON output and a larger result
    cap so the diff isn't truncated by the token-budget envelope), then:

    - **First-ever poll** for this saved search (no
      ``saved_search_seen_memories`` rows exist for it yet): every
      currently-matching memory id is recorded as seen and this returns 0
      WITHOUT calling ``notify()`` -- turning on ``watch=true`` for a
      search that already has matches must not read as a flood of "new"
      matches, since none of them are actually new. See this module's
      docstring for the full reasoning.
    - **Later polls**: any matching id absent from the already-seen set is
      a genuine new match -- one ``notify()`` call per new match,
      identifying the saved search by name and the matching memory by id
      and a content preview, then recorded as seen so it never re-fires.

    Args:
        db: An open SQLite connection.
        saved: The saved search dict to poll (from :func:`get_saved_search`
            or :func:`list_saved_searches`).

    Returns:
        The number of genuinely new matches notified this call (0 on the
        seeding poll, and 0 whenever nothing new matched).
    """
    result_json = await execute_saved_search(
        saved,
        response_format=ResponseFormat.JSON,
        limit=_POLL_RESULT_LIMIT,
        token_budget=0,
    )
    try:
        data = json.loads(result_json)
    except (TypeError, ValueError):
        log.warning(
            "Saved search %r returned non-JSON output during poll -- skipping", saved["name"]
        )
        return 0

    memories = data.get("memories", [])
    current_ids = [m["id"] for m in memories]

    if not _has_any_seen(db, saved["id"]):
        # First poll ever: seed without notifying (see docstring).
        _mark_seen(db, saved["id"], current_ids)
        db.commit()
        return 0

    seen_ids = _seen_memory_ids(db, saved["id"])
    new_ids = [mid for mid in current_ids if mid not in seen_ids]

    for mid in new_ids:
        memory: dict[str, Any] = next((m for m in memories if m["id"] == mid), {})
        content = (memory.get("content") or "")[:200]
        subject = f"Saved search '{saved['name']}' has a new match"
        body = (
            f"New match for saved search '{saved['name']}' (query: {saved['query']!r}): "
            f"memory `{mid}` — {content}"
        )
        notifications.notify(subject, body)

    _mark_seen(db, saved["id"], current_ids)
    db.commit()
    return len(new_ids)


def poll_watched_saved_searches(db: sqlite3.Connection) -> int:
    """Poll every ``watch=true`` saved search once. Synchronous entry point.

    Bridges into the async search core via ``asyncio.run`` per watched
    search: the scheduler's poll loop runs on a plain background thread, not
    an asyncio event loop, so there is no already-running loop to piggyback
    a coroutine onto (unlike e.g. an API request handler) -- this is the
    same bridging shape any other synchronous caller of an async coroutine
    needs. A search that isn't ``watch=true`` is never fetched at all (the
    SQL filter below), so ``watch=false`` saved searches cost nothing here.

    One search's poll failure is logged and does not stop the others --
    matches the reminder/digest/revision/analytics checks' own
    per-concern isolation in scheduler.py's poll loop.

    Args:
        db: An open SQLite connection.

    Returns:
        The total number of new matches notified across every watched
        search this pass.
    """
    total = 0
    rows = db.execute("SELECT * FROM saved_searches WHERE watch = 1").fetchall()
    for row in rows:
        saved = _row_to_saved_search(row)
        try:
            total += asyncio.run(poll_saved_search(db, saved))
        except Exception as e:  # noqa: BLE001 — one search's failure must not block the others
            log.error("Saved search poll failed for %r: %s", saved["name"], e, exc_info=True)
    return total


# ---------------------------------------------------------------------------
# Scheduler-loop due-check (mirrors digest.py's persisted-watermark shape)
# ---------------------------------------------------------------------------


def _poll_watermark(db: sqlite3.Connection) -> datetime | None:
    """Read the persisted 'last watch-poll pass' timestamp, or None if never run."""
    row = db.execute(
        "SELECT value FROM sync_flags WHERE key = ?", (_POLL_FLAG_KEY,)
    ).fetchone()
    if row is None:
        return None
    try:
        dt = datetime.fromisoformat(str(row["value"]))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _mark_polled(db: sqlite3.Connection, when: datetime | None = None) -> None:
    """Persist *when* (default: now) as the 'last watch-poll pass' watermark.

    Reuses the ``sync_flags`` key/value table under its own key, exactly
    like ``digest._mark_digest_sent`` -- so a server restart mid-interval
    does not immediately re-run the poll pass.
    """
    ts = (when or datetime.now(UTC)).isoformat()
    db.execute(
        "INSERT INTO sync_flags (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (_POLL_FLAG_KEY, ts),
    )
    db.commit()


def is_saved_search_poll_due(db: sqlite3.Connection, interval_seconds: int) -> bool:
    """Whether a watch-poll pass is due, given the persisted watermark.

    Args:
        db: An open SQLite connection.
        interval_seconds: The configured poll interval
            (config.SAVED_SEARCH_POLL_INTERVAL).

    Returns:
        True when never run before (so it runs on the first scheduler tick
        rather than waiting a full interval) or the interval has elapsed;
        False otherwise.
    """
    last = _poll_watermark(db)
    if last is None:
        return True
    return (datetime.now(UTC) - last).total_seconds() >= interval_seconds


def maybe_poll_watched_searches(db: sqlite3.Connection | None = None) -> int:
    """Run a watch-poll pass if the configured interval has elapsed.

    Called once per :mod:`remind_me_mcp.scheduler` poll tick, mirroring
    ``digest.maybe_send_scheduled_digest``'s piggyback shape exactly: the
    due-check itself is cheap (one ``sync_flags`` read), and the actual poll
    (:func:`poll_watched_saved_searches`) is a no-op query when no saved
    search has ``watch=true``. The watermark is claimed *before* polling,
    mirroring digest/sync-fault-notification's own discipline, so a slow or
    failing poll pass is retried at most once per interval rather than every
    tick.

    Unlike the digest interval (which is disabled by default,
    ``REMIND_ME_DIGEST_INTERVAL=""``), this always runs on its own coarser
    cadence -- whether it does anything is gated per-search by
    ``saved_searches.watch``, not by a separate global enable switch.

    Args:
        db: Connection to use; defaults to the shared per-thread connection.

    Returns:
        The number of new matches notified this pass (0 if not yet due, or
        if due but nothing new matched).
    """
    db = db if db is not None else _get_db()
    if not is_saved_search_poll_due(db, config.SAVED_SEARCH_POLL_INTERVAL):
        return 0
    _mark_polled(db)
    try:
        return poll_watched_saved_searches(db)
    except Exception as e:  # noqa: BLE001 — a scheduler tick must never raise over this
        log.warning("Saved search watch-poll pass failed: %s", e)
        return 0


__all__ = [
    "build_search_params",
    "delete_saved_search",
    "execute_saved_search",
    "get_saved_search",
    "is_saved_search_poll_due",
    "list_saved_searches",
    "maybe_poll_watched_searches",
    "poll_saved_search",
    "poll_watched_saved_searches",
    "save_search",
]
