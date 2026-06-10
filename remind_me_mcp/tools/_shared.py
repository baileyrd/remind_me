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
