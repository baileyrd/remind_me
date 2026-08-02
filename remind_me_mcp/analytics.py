"""
remind_me_mcp.analytics — Daily analytics trend snapshots (issue #186).

The vault-health data every other tool already exposes
(:func:`remind_me_mcp.vitality.build_vitality_report`'s vitality-bucket
distribution, and ``GET /api/stats``'s category counts) is point-in-time
only: nothing previously recorded what those numbers *were* a week or a
month ago, only what they are right now. This module takes a single daily
rollup snapshot -- reusing the exact same report-building function those
tools already call, so a snapshot can never disagree with the live report --
and stores it in the ``analytics_snapshots`` table (v24->v25 migration,
:mod:`remind_me_mcp.db`) for a dashboard trend chart to plot.

Scheduled capture (:func:`maybe_capture_analytics_snapshot`) piggybacks on
:mod:`remind_me_mcp.scheduler`'s existing poll loop rather than a second
background thread, mirroring :mod:`remind_me_mcp.digest`'s scheduled-digest
pattern exactly: a persisted watermark in the ``sync_flags`` key/value table
(``analytics_last_snapshot_at``) throttles the (cheap, but not free --
building the vitality report scans every memory) capture attempt to once per
``SNAPSHOT_INTERVAL_SECONDS``, durable across restarts so a restart mid-day
doesn't immediately re-fire. Unlike the digest, this capture is NOT opt-in --
there's no equivalent of ``REMIND_ME_DIGEST_INTERVAL``, since a trend chart
with no history is a worse default than one small daily row.

:func:`capture_analytics_snapshot` carries its own independent
idempotent-per-day guard (checking for an existing same-day row before
inserting) on top of the watermark throttle above -- belt and suspenders:
the watermark is what keeps a healthy server from attempting a capture on
every 60-second poll tick, but the per-day guard is what makes the function
itself safe to call directly (as every test in this module's test suite
does), and what protects against a watermark that is somehow stale or
missing (e.g. restored from an older backup) causing a same-day duplicate.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from remind_me_mcp.db import _get_db
from remind_me_mcp.vitality import build_vitality_report

if TYPE_CHECKING:
    import sqlite3

log = logging.getLogger("remind_me_mcp.analytics")

SNAPSHOT_INTERVAL_SECONDS = 86400
"""Target cadence for scheduled capture: once per day. Not user-configurable
(unlike REMIND_ME_DIGEST_INTERVAL) -- a daily rollup is the whole point of a
long-range trend chart, so there's no sensible "weekly" or "hourly" variant
to offer."""

_SNAPSHOT_FLAG_KEY = "analytics_last_snapshot_at"


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def _category_counts(db: sqlite3.Connection) -> dict[str, int]:
    """Return ``{category: count}`` over non-deleted memories.

    Same query and shape as ``GET /api/stats``'s ``categories`` field
    (api.py's ``api_stats`` handler) -- kept as its own small helper here
    (rather than importing api.py, which pulls in the whole Starlette route
    module) since ``build_vitality_report`` doesn't compute category counts
    at all, only the memory_type-keyed ``decay_distribution``.
    """
    rows = db.execute(
        "SELECT category, COUNT(*) as cnt FROM memories "
        "WHERE deleted_at IS NULL GROUP BY category"
    ).fetchall()
    return {r["category"]: r["cnt"] for r in rows}


def capture_analytics_snapshot(
    db: sqlite3.Connection, now: datetime | None = None
) -> int | None:
    """Insert one analytics_snapshots row for *today*, unless one already exists.

    Idempotent per calendar day (UTC): checks for an existing row whose
    ``captured_at`` falls on the same date as *now* before inserting, so a
    server restart mid-day -- or this function being called more than once
    on the same day for any other reason -- never produces a duplicate. This
    is a date comparison, not an exact-timestamp comparison, precisely so it
    survives a restart at a different second than the original capture.

    Args:
        db: An open SQLite connection.
        now: Clock override for tests. Defaults to the current UTC time.

    Returns:
        The new row's id, or ``None`` if a snapshot for today already
        existed (no row inserted).
    """
    now = now or datetime.now(UTC)
    today = now.date().isoformat()

    existing = db.execute(
        "SELECT id FROM analytics_snapshots WHERE date(captured_at) = ?", (today,)
    ).fetchone()
    if existing is not None:
        return None

    report = build_vitality_report(db)
    category_counts = _category_counts(db)

    cur = db.execute(
        """INSERT INTO analytics_snapshots
               (captured_at, total_memories, vitality_buckets, category_counts)
           VALUES (?, ?, ?, ?)""",
        (
            now.isoformat(),
            report["total_memories"],
            json.dumps(report["vitality_buckets"]),
            json.dumps(category_counts),
        ),
    )
    db.commit()
    log.debug("Captured analytics snapshot for %s (id=%s)", today, cur.lastrowid)
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Trend read
# ---------------------------------------------------------------------------


def get_analytics_trend(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return the full snapshot history, oldest first.

    Args:
        db: An open SQLite connection.

    Returns:
        A list of ``{captured_at, total_memories, vitality_buckets,
        category_counts}`` dicts (``vitality_buckets``/``category_counts``
        already decoded from their stored JSON text). Empty list when no
        snapshot has been captured yet.
    """
    rows = db.execute(
        """SELECT captured_at, total_memories, vitality_buckets, category_counts
             FROM analytics_snapshots
            ORDER BY captured_at ASC"""
    ).fetchall()
    return [
        {
            "captured_at": r["captured_at"],
            "total_memories": r["total_memories"],
            "vitality_buckets": json.loads(r["vitality_buckets"]),
            "category_counts": json.loads(r["category_counts"]),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Scheduled capture (piggybacks on scheduler.py's poll loop, issue #186)
# ---------------------------------------------------------------------------


def _snapshot_watermark(db: sqlite3.Connection) -> datetime | None:
    """Read the persisted 'last snapshot captured' timestamp, or None if never."""
    row = db.execute(
        "SELECT value FROM sync_flags WHERE key = ?", (_SNAPSHOT_FLAG_KEY,)
    ).fetchone()
    if row is None:
        return None
    try:
        dt = datetime.fromisoformat(str(row["value"]))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _mark_snapshot_captured(db: sqlite3.Connection, when: datetime | None = None) -> None:
    """Persist *when* (default: now) as the 'last snapshot captured' watermark.

    Reuses the ``sync_flags`` key/value table under its own key, exactly
    mirroring :func:`remind_me_mcp.digest._mark_digest_sent` -- no new table
    needed for a single cross-restart timestamp.
    """
    ts = (when or datetime.now(UTC)).isoformat()
    db.execute(
        "INSERT INTO sync_flags (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (_SNAPSHOT_FLAG_KEY, ts),
    )
    db.commit()


def is_analytics_snapshot_due(
    db: sqlite3.Connection, interval_seconds: int = SNAPSHOT_INTERVAL_SECONDS
) -> bool:
    """Whether a scheduled snapshot capture is due, given the persisted watermark.

    Mirrors :func:`remind_me_mcp.digest.is_digest_due` exactly.

    Args:
        db: An open SQLite connection.
        interval_seconds: The target cadence in seconds.

    Returns:
        True when never captured before (so a fresh install captures on its
        first scheduler tick rather than waiting a full day) or when the
        interval has elapsed since the last capture; False otherwise.
    """
    last = _snapshot_watermark(db)
    if last is None:
        return True
    return (datetime.now(UTC) - last).total_seconds() >= interval_seconds


def maybe_capture_analytics_snapshot(db: sqlite3.Connection | None = None) -> bool:
    """Capture today's analytics snapshot if the daily interval elapsed.

    Called once per :mod:`remind_me_mcp.scheduler` poll tick -- cheap when
    not yet due (a single ``sync_flags`` lookup), only building the full
    vitality report (a scan of every memory) on the roughly-once-per-day tick
    where it's actually due.

    The watermark is claimed *before* capturing, mirroring
    :func:`remind_me_mcp.digest.maybe_send_scheduled_digest`'s discipline:
    bounds a persisting capture failure to one retry per interval rather than
    every tick.

    Args:
        db: Connection to use; defaults to the shared per-thread connection.

    Returns:
        True if a capture was due and attempted this call, False if not yet
        due.
    """
    db = db if db is not None else _get_db()
    if not is_analytics_snapshot_due(db):
        return False

    _mark_snapshot_captured(db)
    try:
        capture_analytics_snapshot(db)
    except Exception as e:  # noqa: BLE001 — a scheduler tick must never raise over this
        log.warning("Scheduled analytics snapshot capture failed: %s", e)
    return True


__all__ = [
    "SNAPSHOT_INTERVAL_SECONDS",
    "capture_analytics_snapshot",
    "get_analytics_trend",
    "is_analytics_snapshot_due",
    "maybe_capture_analytics_snapshot",
]
