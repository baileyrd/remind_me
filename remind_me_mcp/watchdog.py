"""
remind_me_mcp.watchdog — visibility into a tool call that's stuck, not just slow.

The gap this closes (issue #128): when a tool call hangs, the only symptom
visible from outside the process is "the MCP call timed out after N seconds" —
nothing in the server itself says *where* it's stuck. Diagnosing a real
incident (a correlated SQL subquery pegging a CPU core for minutes, see
maintenance.UNNORMALIZED_WHERE's fix) required manually installing and
running ``py-spy dump`` against the live process.

``faulthandler.dump_traceback_later`` is the stdlib answer: it arms a
watchdog in a separate OS thread that, after a timeout, dumps every thread's
current stack to a file — including a thread stuck in synchronous CPU-bound
code (a plain ``asyncio`` timer task cannot do this, since a blocked event
loop can't run anything else, including its own watchdog). No new
dependency, and it works precisely in the failure mode that mattered here.

Calls can run concurrently, so arming/disarming is reference-counted rather
than tied to one call: the watchdog stays armed as long as at least one call
is in flight, and only cancels once the last one finishes. A call that
finishes quickly must never cancel a watchdog a slower, still-running call
armed.

Safety: this must never be the reason a tool call fails. Both env-configured
disable (``REMIND_ME_SLOW_CALL_SECONDS=0``) and a runtime environment where
``sys.stderr`` has no usable file descriptor (e.g. some test harnesses'
captured streams) turn this into a complete no-op, and every faulthandler
call is wrapped defensively regardless.
"""

from __future__ import annotations

import faulthandler
import logging
import os
import sys
import threading

log = logging.getLogger("remind_me_mcp.watchdog")

SLOW_CALL_SECONDS = float(os.environ.get("REMIND_ME_SLOW_CALL_SECONDS", "30") or 0)

_lock = threading.Lock()
_inflight = 0
_usable: bool | None = None


def _watchdog_usable() -> bool:
    """Can we actually arm faulthandler against sys.stderr right now?

    Cached after the first check (checked lazily, not at import time, so a
    test harness that swaps sys.stderr in before the first tool call is
    still detected correctly).
    """
    global _usable
    if _usable is None:
        try:
            sys.stderr.fileno()
            _usable = True
        except Exception:
            _usable = False
    return _usable


def arm() -> None:
    """Mark one call as started; arms the watchdog on the 0->1 transition."""
    global _inflight
    if SLOW_CALL_SECONDS <= 0 or not _watchdog_usable():
        return
    with _lock:
        _inflight += 1
        if _inflight == 1:
            try:
                faulthandler.dump_traceback_later(
                    SLOW_CALL_SECONDS, repeat=True, exit=False
                )
            except Exception:
                log.debug("Could not arm the slow-call watchdog", exc_info=True)


def disarm() -> None:
    """Mark one call as finished; cancels the watchdog once none remain."""
    global _inflight
    if SLOW_CALL_SECONDS <= 0 or not _watchdog_usable():
        return
    with _lock:
        if _inflight <= 0:
            return  # unbalanced disarm (e.g. a bug elsewhere) -- nothing to cancel
        _inflight -= 1
        if _inflight == 0:
            try:
                faulthandler.cancel_dump_traceback_later()
            except Exception:
                log.debug("Could not disarm the slow-call watchdog", exc_info=True)


def status() -> dict[str, object]:
    """Report the watchdog's own state, for remind_me_server_status."""
    return {
        "enabled": SLOW_CALL_SECONDS > 0 and _watchdog_usable(),
        "threshold_seconds": SLOW_CALL_SECONDS,
        "calls_in_flight": _inflight,
    }


__all__ = ["SLOW_CALL_SECONDS", "arm", "disarm", "status"]
