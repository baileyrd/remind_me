"""
remind_me_mcp.scheduler — Time-based reminder delivery (issue #179).

Polls memories for a due reminder (``remind_at <= now``, not soft-deleted,
not yet recorded in ``reminder_deliveries``) every
``REMIND_ME_REMINDER_POLL_INTERVAL`` seconds and delivers each exactly once.
The delivery-tracking table (not a bare "remind_at is in the past" check) is
what lets a reminder that comes due while the server is offline fire exactly
once on the next poll after restart, instead of firing on every subsequent
poll forever or being silently dropped.

Delivery itself is a log line for now -- issue #180 (outbound notification
channels) will plug a real delivery mechanism into the ``_delivery_hook``
seam below without needing to touch the due-query/poll logic here.

Lifecycle: :func:`start_scheduler` is called from the server lifespan
unconditionally (unlike the folder watcher, which is opt-in via
``REMIND_ME_WATCH_DIRS`` -- reminders have no separate enable switch, only a
poll interval) and :func:`stop_scheduler` signals the thread and joins it
before the database connections are closed (SE-07), mirroring
``watcher.py``/``sync.py``'s thread lifecycle.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from remind_me_mcp.config import REMINDER_POLL_INTERVAL
from remind_me_mcp.db import _get_db, _now_iso, _row_to_dict

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

log = logging.getLogger("remind_me_mcp.scheduler")


def _log_delivery(memory: dict[str, Any]) -> None:
    """Default delivery hook: log the due reminder at INFO level.

    Issue #180 (outbound notification channels) replaces or wraps this
    callable once a real delivery mechanism (email, push, etc.) lands --
    the due-query/poll logic in :func:`poll_once` never needs to change.
    """
    content = memory.get("content") or ""
    preview = content[:200] + ("…" if len(content) > 200 else "")
    log.info(
        "Reminder due — memory `%s` (remind_at=%s): %s",
        memory.get("id"),
        memory.get("remind_at"),
        preview,
    )


# Delivery seam for issue #180 — swap this module-level callable to plug in a
# real notification channel; poll_once()'s due-reminder query never changes.
_delivery_hook: Callable[[dict[str, Any]], None] = _log_delivery


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
