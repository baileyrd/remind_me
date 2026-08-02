"""
remind_me_mcp.events — Raw automation event stream for memory mutations (issue #198).

Deliberately a separate config/delivery path from ``notifications.py``
(issue #180), not a second use of it, even though both POST JSON to a
configured webhook URL:

- ``notifications.notify()`` (``REMIND_ME_NOTIFY_WEBHOOK_URL``) is for
  human-facing, *throttled* alerts -- a fired reminder, a faulted sync
  verdict -- meant to be read by a person on a phone/Slack/ntfy. It fans out
  to multiple channels (webhook + email) and, for sync faults, deliberately
  suppresses repeat alerts within a window (``NOTIFY_SYNC_FAULT_INTERVAL``)
  to avoid alert fatigue.
- ``emit_event()`` here (``REMIND_ME_EVENT_WEBHOOK_URL``) is for
  *automation* consumers -- a webhook relay, a second indexer, an audit log
  -- that want to know about every memory create/update/delete as it
  happens. Completeness is the point: there is no throttling, since
  suppressing "repeat" events would silently drop real mutations a consumer
  needs to see. The payload is metadata-only by design (``event``,
  ``memory_id``, ``category``, ``timestamp``) -- never memory content, since
  this is an event-notification stream, not a content-sync mechanism (a
  consumer that wants the content can call back through the REST API/MCP
  tools using the memory_id).

Delivery mechanics:

- Reuses ``notifications._post_json`` for the actual HTTP-POST-with-timeout
  logic (the one piece of plumbing genuinely shared with
  ``WebhookNotifier.send``) rather than copy-pasting it -- see that
  function's docstring for why the throttling/subject-body shape is NOT
  pulled in alongside it.
- Fire-and-forget, but reference-held: ``emit_event`` must never block or
  slow down the ``remind_me_add``/``remind_me_update``/``remind_me_delete``
  call (or their REST equivalents) that triggered it, so the POST happens on
  a background ``asyncio.Task`` rather than being awaited inline. Per
  BACKLOG's PF-04 (see ``remind_me_mcp/tools/_shared.py``'s
  ``_spawn_task``/``_background_tasks``, the fix for exactly this bug
  class), the event loop holds only a *weak* reference to a task -- a
  fire-and-forget ``asyncio.create_task()`` result with no other reference
  can be garbage-collected mid-flight, silently dropping the POST. This
  module keeps its own module-level ``_background_tasks`` set following the
  identical pattern rather than importing ``tools._spawn_task`` directly, so
  ``remind_me_mcp.events`` (a top-level module used by both ``api.py`` and
  ``tools/crud.py``) has no dependency on the ``tools`` package -- consistent
  with the rest of the codebase's layering, where ``tools/*`` imports
  top-level modules, never the reverse.
- Degrades gracefully like ``notifications.notify()``: unconfigured
  (``REMIND_ME_EVENT_WEBHOOK_URL`` unset) is a true no-op -- no task is even
  created -- and a failing POST (connection error, timeout, non-2xx) is
  caught and logged, never raised into the caller.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from remind_me_mcp.config import EVENT_WEBHOOK_TIMEOUT, EVENT_WEBHOOK_URL
from remind_me_mcp.db import _now_iso
from remind_me_mcp.notifications import _post_json

if TYPE_CHECKING:
    from collections.abc import Coroutine

    import httpx

log = logging.getLogger("remind_me_mcp.events")

# ---------------------------------------------------------------------------
# Fire-and-forget background tasks (PF-04 pattern, see module docstring)
# ---------------------------------------------------------------------------

_background_tasks: set[asyncio.Task[Any]] = set()


def _spawn_task(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
    """Schedule *coro* fire-and-forget while keeping a strong reference (PF-04).

    Held in a module-level set and discards itself on completion, so it can
    neither be garbage-collected mid-flight nor leak. Mirrors
    ``remind_me_mcp.tools._shared._spawn_task`` exactly (see this module's
    docstring for why this is a separate set rather than a shared import).

    Args:
        coro: The coroutine to run in the background.

    Returns:
        The created task.
    """
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


# ---------------------------------------------------------------------------
# Event emission
# ---------------------------------------------------------------------------


async def _post_event(
    url: str,
    payload: dict[str, Any],
    timeout: float,
    client: httpx.Client | None,
) -> None:
    """Background-task body: POST *payload*, catching and logging any failure.

    Runs the (blocking) ``httpx`` call in a worker thread via
    ``asyncio.to_thread`` so it never blocks the event loop this task was
    spawned on -- mirroring how ``api.py``'s route handlers keep blocking
    work off the loop (PF-06).
    """
    try:
        await asyncio.to_thread(_post_json, url, payload, timeout, client)
    except Exception as e:  # noqa: BLE001 — an event POST must never raise
        log.warning("Event webhook POST to %s failed: %s", url, e)


def emit_event(
    event: str,
    memory_id: str,
    category: str,
    *,
    client: httpx.Client | None = None,
) -> asyncio.Task[None] | None:
    """Fire a fire-and-forget, unthrottled event POST for a memory mutation.

    A true no-op when ``REMIND_ME_EVENT_WEBHOOK_URL`` is unset -- no task is
    created -- so callers (``remind_me_add``/``update``/``delete`` and their
    REST equivalents) can call this unconditionally without checking
    availability themselves first, the same discipline as
    ``notifications.notify()``. Never raises: any failure happens inside the
    background task and is caught and logged there (see ``_post_event``).

    The POST body is deliberately metadata-only --
    ``{"event": ..., "memory_id": ..., "category": ..., "timestamp": ...}`` --
    memory content is never included (see the module docstring's "scope
    limit, not an oversight" note).

    Args:
        event: One of ``"created"``, ``"updated"``, ``"deleted"``.
        memory_id: The affected memory's id.
        category: The memory's category at the time of the event.
        client: Optional injected ``httpx.Client``, for tests -- mirrors
            ``WebhookNotifier``'s constructor-injection pattern. A fresh
            one-shot ``httpx.post`` is used when omitted.

    Returns:
        The spawned background ``asyncio.Task`` (mainly useful for tests to
        await), or ``None`` when the event webhook isn't configured.
    """
    if not EVENT_WEBHOOK_URL:
        return None
    payload = {
        "event": event,
        "memory_id": memory_id,
        "category": category,
        "timestamp": _now_iso(),
    }
    return _spawn_task(_post_event(EVENT_WEBHOOK_URL, payload, EVENT_WEBHOOK_TIMEOUT, client))


__all__ = [
    "emit_event",
]
