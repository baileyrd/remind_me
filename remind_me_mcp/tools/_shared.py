"""
remind_me_mcp.tools._shared — Infrastructure shared by all tool submodules.

Holds the module-level mutable state (fire-and-forget background task set,
FTS sanitize fallback flag) and the small helpers used across the tool
submodules.

Patchability note (HY-02): the test suite and benchmarks monkeypatch names on
the ``remind_me_mcp.tools`` package (e.g. ``tools._get_db``,
``tools.FTS_SANITIZE_FALLBACK``, ``tools._embed_and_store``). The package
``__init__`` re-exports everything defined here, and submodules look those
names up *through the package namespace at call time* (``_pkg.<name>``), so a
patch applied to ``remind_me_mcp.tools.<name>`` affects every submodule —
exactly as it did when tools was a single module.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Coroutine

from remind_me_mcp.updater import pop_update_notice

log = logging.getLogger("remind_me_mcp.tools")

# When True, a query that isn't valid FTS5 (e.g. a natural-language question with
# punctuation) is retried as a sanitized OR-of-terms expression instead of being
# dropped. Disable to restore the legacy "skip keyword tier on syntax error"
# behavior — used by the before/after benchmark to quantify the fix's impact.
FTS_SANITIZE_FALLBACK = True


# ---------------------------------------------------------------------------
# Fire-and-forget background tasks (PF-04)
# ---------------------------------------------------------------------------

# The event loop holds only weak references to tasks: a fire-and-forget
# asyncio.create_task() result with no other reference can be garbage
# collected mid-flight, silently dropping embeddings or access updates.
_background_tasks: set[asyncio.Task[Any]] = set()


def _spawn_task(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
    """Schedule *coro* fire-and-forget while keeping a strong reference (PF-04).

    The task is held in a module-level set and discards itself on completion,
    so it can neither be garbage-collected mid-flight nor leak.

    Args:
        coro: The coroutine to run in the background.

    Returns:
        The created task (callers usually ignore it).
    """
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _public_memory(memory: dict) -> dict:
    """Return a copy of *memory* without internal underscore-prefixed fields (HY-05).

    Ranking internals (``_rrf_score``, ``_keyword_rank``, ``_search_method``,
    ``_rerank_score``, ...) must not leak into JSON responses; the useful ones
    are exposed via the ``debug_signals`` block when ``verbose`` is set.

    Args:
        memory: A memory dict possibly augmented with internal ranking keys.

    Returns:
        A shallow copy with all underscore-prefixed keys removed.
    """
    return {k: v for k, v in memory.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# Update notice helper
# ---------------------------------------------------------------------------


def _maybe_update_notice(response: str) -> str:
    """Append a one-shot update notice to the response if available.

    The notice fires once (on the first tool call after startup) then clears.

    Args:
        response: The original tool response string.

    Returns:
        The response, possibly with an appended update notice.
    """
    notice = pop_update_notice()
    if notice:
        return response + "\n\n---\n" + notice
    return response


def _maybe_maintenance_notice(response: str) -> str:
    """Append a throttled maintenance nudge to a *markdown* response.

    The backlog counts previously lived only in ``remind_me_server_status`` and
    ``remind_me_watch_status`` — tools a conversational session never calls, so
    a growing queue of un-decomposed captures was invisible in practice. This
    piggybacks the signal onto responses Claude actually reads, the same idiom
    :func:`_maybe_update_notice` already uses for update notices.

    Callers must apply this only on markdown return paths. The JSON paths go
    through ``_envelope_json``, and appending prose to a JSON document would
    make it unparseable — which is why the existing update notice is wired the
    same way.

    Args:
        response: The original markdown tool response.

    Returns:
        The response, possibly with an appended nudge. Never raises: a
        diagnostic must not be the thing that breaks a search.
    """
    try:
        # Deferred import: resolving through the package namespace at call time
        # keeps the tests' `tools._get_db` monkeypatch effective (HY-02), and
        # importing the package at module scope here would be circular.
        from remind_me_mcp import tools as _pkg
        from remind_me_mcp.maintenance import maybe_maintenance_notice

        notice = maybe_maintenance_notice(_pkg._get_db())
    except Exception:  # noqa: BLE001 — a nudge never breaks its host response
        log.debug("Maintenance notice failed", exc_info=True)
        return response
    if notice:
        return response + "\n\n---\n" + notice
    return response


def _maybe_search_notices(response: str) -> str:
    """Append **at most one** advisory to a markdown search response.

    Search is the busiest response in the system and the one Claude reads most
    closely, which makes it both the best place to put an advisory and the
    easiest place to ruin with clutter. A maintenance backlog is concrete work
    to do, so it outranks the standing feedback affordance; stacking both onto
    one result would start training the reader to skip the tail of every
    search, which is the failure mode both signals exist to avoid.

    Markdown paths only, for the same reason as
    :func:`_maybe_maintenance_notice` — the JSON envelope must stay parseable.

    Args:
        response: The original markdown search response.

    Returns:
        The response with an update notice (if any) plus at most one advisory.
        Never raises.
    """
    out = _maybe_update_notice(response)
    try:
        from remind_me_mcp import tools as _pkg
        from remind_me_mcp.maintenance import maybe_feedback_hint, maybe_maintenance_notice

        notice = maybe_maintenance_notice(_pkg._get_db()) or maybe_feedback_hint()
    except Exception:  # noqa: BLE001 — an advisory never breaks its host response
        log.debug("Search notices failed", exc_info=True)
        return out
    if notice:
        return out + "\n\n---\n" + notice
    return out


# ---------------------------------------------------------------------------
# Contradiction-supersession preview (gap #5 false-positive mitigation)
# ---------------------------------------------------------------------------


def _supersession_preview(
    db: Any, superseded_ids: list[str], max_len: int = 80
) -> list[dict[str, str]]:
    """Fetch a short content preview for each just-superseded memory id.

    db._supersede_contradicting_facts hides an old memory from every real
    read path (search, entity/subject/predicate lookups) the instant a new
    fact shares its (subject, predicate) with a different object. That's
    correct for a genuine contradiction ("I live in Seattle" -> "I moved to
    Boston") but a false positive for a reused generic (subject, predicate)
    pair across unrelated facts -- and the old content stays invisible
    until someone thinks to remind_me_get the bare id a caller might not
    even notice. Surfacing what got hidden, inline in the same tool
    response, lets the caller catch a false positive immediately instead of
    silently losing a memory to a missing search result.

    Args:
        db: An open SQLite connection.
        superseded_ids: IDs returned by _supersede_contradicting_facts.
        max_len: Truncate each preview to this many characters.

    Returns:
        A list of {"id": ..., "content": ...} dicts, one per id that still
        resolves to a row (best-effort; a since-deleted row is skipped).
    """
    if not superseded_ids:
        return []
    placeholders = ",".join("?" for _ in superseded_ids)
    rows = db.execute(
        f"SELECT id, content FROM memories WHERE id IN ({placeholders})",
        superseded_ids,
    ).fetchall()
    by_id = {row["id"]: row["content"] for row in rows}
    previews: list[dict[str, str]] = []
    for sid in superseded_ids:
        content = by_id.get(sid)
        if content is None:
            continue
        if len(content) > max_len:
            content = content[: max_len - 1].rstrip() + "…"
        previews.append({"id": sid, "content": content})
    return previews
