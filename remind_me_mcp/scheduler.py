"""
remind_me_mcp.scheduler — Time-based reminder delivery (issue #179).

Polls memories for a due reminder (``remind_at <= now``, not soft-deleted,
not yet recorded in ``reminder_deliveries``) every
``REMIND_ME_REMINDER_POLL_INTERVAL`` seconds and delivers each exactly once.
The delivery-tracking table (not a bare "remind_at is in the past" check) is
what lets a reminder that comes due while the server is offline fire exactly
once on the next poll after restart, instead of firing on every subsequent
poll forever or being silently dropped.

Delivery is a log line plus a fan-out to any configured outbound notification
channel (:mod:`remind_me_mcp.notifications`, issue #180) -- ``notify()`` is a
no-op when nothing is configured, so the default hook always logs and always
attempts to notify, regardless of whether a webhook/SMTP channel is set up.

Lifecycle: :func:`start_scheduler` is called from the server lifespan
unconditionally (unlike the folder watcher, which is opt-in via
``REMIND_ME_WATCH_DIRS`` -- reminders have no separate enable switch, only a
poll interval) and :func:`stop_scheduler` signals the thread and joins it
before the database connections are closed (SE-07), mirroring
``watcher.py``/``sync.py``'s thread lifecycle.

The same loop also carries the optional scheduled-digest check (issue #188,
``remind_me_mcp.digest.maybe_send_scheduled_digest``) rather than a second
background thread. A digest's natural cadence (daily/weekly at the coarsest)
is far coarser than this loop's default 60s reminder-poll interval, but
``maybe_send_scheduled_digest`` is a single disabled-by-default attribute
check when ``REMIND_ME_DIGEST_INTERVAL`` is unset -- so piggybacking costs a
zero-config server nothing extra per tick, while a second thread would add
its own lifecycle (start/stop/join, another daemon thread name, another
failure mode to log) purely to poll something on a much longer timescale
that this thread already wakes up for anyway.

Edit-history revision compaction (issue #187, ``db._compact_revisions``)
piggybacks on this same loop for the same reason, but for a different
reason than the digest check: ``sync._compact_tombstones`` (the closest
precedent) only ever runs from ``sync.py``'s loop because that loop is
gated on ``config.SYNC_ENABLED`` -- a non-syncing node hard-deletes
immediately and never accumulates tombstones to compact. Revisions,
however, are captured on every genuine content edit regardless of whether
sync is configured at all, so gating their pruning on sync being enabled
would leave a single, never-synced device's ``memory_revisions`` table
growing forever. This loop runs unconditionally (unlike ``sync.py``'s),
which is exactly the property revision compaction needs.

Analytics trend snapshot capture and retention (issue #186,
``analytics.maybe_capture_analytics_snapshot`` /
``db._compact_analytics_snapshots``) piggyback on this same loop too, for
the combined reasons above: capture is throttled to roughly once a day via
a persisted watermark (mirroring the digest check's own throttle exactly),
and -- like revision compaction -- retention runs unconditionally rather
than being gated on ``config.SYNC_ENABLED``, since snapshots accumulate
regardless of whether sync is configured.

Watched-saved-search polling (issue #194,
``saved_searches.maybe_poll_watched_searches``) piggybacks on this same loop
for the same reason the digest check does: a saved search's underlying
content changes far less often than a reminder's due time, so a
persisted-watermark due-check (its own, coarser
``REMIND_ME_SAVED_SEARCH_POLL_INTERVAL``, default 300s) inside this loop is
cheap even when nothing is watched, while a second thread would add its own
lifecycle purely to poll something on a much longer timescale this thread
already wakes up for. Unlike the digest check, it is not itself opt-in --
whether a poll pass actually does anything is gated per-search by
``saved_searches.watch``, not by a separate global enable switch.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from remind_me_mcp import notifications
from remind_me_mcp.config import REMINDER_POLL_INTERVAL
from remind_me_mcp.db import (
    _compact_analytics_snapshots,
    _compact_revisions,
    _get_db,
    _now_iso,
    _row_to_dict,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

log = logging.getLogger("remind_me_mcp.scheduler")


def _log_delivery(memory: dict[str, Any]) -> None:
    """Log the due reminder at INFO level. Kept separate from notification
    fan-out (below) so logging always happens regardless of whether any
    notification channel is configured, and stays independently testable.
    """
    content = memory.get("content") or ""
    preview = content[:200] + ("…" if len(content) > 200 else "")
    log.info(
        "Reminder due — memory `%s` (remind_at=%s): %s",
        memory.get("id"),
        memory.get("remind_at"),
        preview,
    )


def _default_delivery(memory: dict[str, Any]) -> None:
    """Default delivery hook: log, then fan out to any configured notifier.

    ``notifications.notify`` (issue #180) is a no-op when no webhook/SMTP
    channel is configured, so this always logs *and* always attempts to
    notify -- one call site, no branching on configuration here. The memory's
    content becomes the notification body; the id and remind_at timestamp
    identify which reminder fired.
    """
    _log_delivery(memory)
    subject = f"Reminder due: memory `{memory.get('id')}`"
    body = memory.get("content") or ""
    notifications.notify(subject, body)


# Delivery seam — swap this module-level callable to plug in a different
# delivery mechanism entirely; poll_once()'s due-reminder query never changes.
_delivery_hook: Callable[[dict[str, Any]], None] = _default_delivery


def poll_once(db: sqlite3.Connection | None = None) -> int:
    """Deliver every currently-due, not-yet-delivered reminder once.

    A reminder is due when ``remind_at <= now``, the memory is not
    soft-deleted, and no ``reminder_deliveries`` row exists yet for this
    exact ``(memory_id, remind_at)`` pair. After delivery, a row is inserted
    so the same reminder is not re-delivered on a later poll.

    Args:
        db: Connection to use; defaults to the shared per-thread connection.

    Returns:
        The number of reminders delivered this pass.
    """
    db = db if db is not None else _get_db()
    now = _now_iso()
    rows = db.execute(
        """SELECT m.* FROM memories m
            WHERE m.remind_at IS NOT NULL
              AND m.deleted_at IS NULL
              AND m.remind_at <= ?
              AND NOT EXISTS (
                  SELECT 1 FROM reminder_deliveries rd
                   WHERE rd.memory_id = m.id AND rd.remind_at = m.remind_at
              )
            ORDER BY m.remind_at ASC""",
        (now,),
    ).fetchall()

    delivered = 0
    for row in rows:
        memory = _row_to_dict(row)
        _delivery_hook(memory)
        db.execute(
            """INSERT INTO reminder_deliveries (memory_id, remind_at, delivered_at)
               VALUES (?, ?, ?)""",
            (memory["id"], memory["remind_at"], _now_iso()),
        )
        delivered += 1
    if delivered:
        db.commit()
    return delivered


# ---------------------------------------------------------------------------
# Thread lifecycle (mirrors sync.py's module-level start/stop thread shape)
# ---------------------------------------------------------------------------

_stop = threading.Event()
_thread: threading.Thread | None = None
_thread_lock = threading.Lock()


def start_scheduler() -> threading.Thread:
    """Start the background reminder-polling loop (idempotent).

    Unconditional, unlike the folder watcher: reminders have no separate
    opt-in configuration, only a poll interval
    (``REMIND_ME_REMINDER_POLL_INTERVAL``). The check-then-act is
    lock-protected (mirroring ``watcher.FolderWatcher.start``/
    ``sync.start_sync_thread``) so two concurrent callers can't both pass the
    liveness check and start two competing loops.

    Returns:
        The running scheduler thread.
    """
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return _thread

        def _run() -> None:
            log.info(
                "Reminder scheduler thread starting — interval=%ds",
                REMINDER_POLL_INTERVAL,
            )
            while not _stop.is_set():
                try:
                    delivered = poll_once()
                    if delivered:
                        log.info("Reminder scheduler delivered %d reminder(s)", delivered)
                except Exception as e:
                    log.error("Reminder scheduler poll failed: %s", e, exc_info=True)
                try:
                    from remind_me_mcp.digest import maybe_send_scheduled_digest

                    maybe_send_scheduled_digest()
                except Exception as e:
                    log.error("Scheduled digest check failed: %s", e, exc_info=True)
                try:
                    from remind_me_mcp.saved_searches import maybe_poll_watched_searches

                    maybe_poll_watched_searches()
                except Exception as e:
                    log.error("Saved search watch-poll check failed: %s", e, exc_info=True)
                try:
                    _compact_revisions(_get_db())
                except Exception as e:
                    log.error("Revision compaction failed: %s", e, exc_info=True)
                try:
                    from remind_me_mcp.analytics import maybe_capture_analytics_snapshot

                    maybe_capture_analytics_snapshot(_get_db())
                except Exception as e:
                    log.error("Analytics snapshot capture failed: %s", e, exc_info=True)
                try:
                    _compact_analytics_snapshots(_get_db())
                except Exception as e:
                    log.error("Analytics snapshot compaction failed: %s", e, exc_info=True)
                _stop.wait(REMINDER_POLL_INTERVAL)
            log.info("Reminder scheduler thread stopped")

        _stop.clear()
        _thread = threading.Thread(target=_run, daemon=True, name="reminder-scheduler")
        _thread.start()
        return _thread


def stop_scheduler(timeout: float = 10.0) -> None:
    """Signal the loop to stop and join the thread (no-op when not running).

    Called from the server lifespan shutdown alongside ``watcher.stop_watcher()``,
    before the database connections are closed (SE-07), so an in-flight poll
    cannot write to a closed handle.

    Args:
        timeout: Max seconds to wait for the thread to exit.
    """
    global _thread
    _stop.set()
    with _thread_lock:
        thread = _thread
        _thread = None
    if thread is not None and thread.is_alive():
        thread.join(timeout)


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "poll_once",
    "start_scheduler",
    "stop_scheduler",
]
