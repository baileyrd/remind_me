"""
remind_me_mcp.server — FastMCP server instance and application lifespan.

Defines the global `mcp` FastMCP instance and the async lifespan context
manager that opens the database at startup and closes it on shutdown.

IMPORTANT: This module must NOT import from tools.py. Instead, tools.py
imports `mcp` from this module and registers handlers onto it, avoiding
circular imports.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import FastMCP

from remind_me_mcp import watchdog
from remind_me_mcp.config import DB_PATH, SYNC_ENABLED
from remind_me_mcp.db import _close_db, _get_db
from remind_me_mcp.telemetry import maybe_span

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mcp.types import ContentBlock

log = logging.getLogger("remind_me_mcp.server")


class _TracedFastMCP(FastMCP):
    """FastMCP with an OTEL span wrapped around every tool call (Phase 7a).

    ``call_tool`` is the single choke point every MCP tool invocation passes
    through, but wrapping it AFTER construction (``mcp.call_tool = ...``)
    doesn't work: FastMCP's own ``__init__`` registers ``self.call_tool`` as
    the protocol-level handler while it runs, capturing whichever method the
    instance's actual class resolves at that moment. Subclassing overrides
    the method before that registration happens (Python's normal MRO
    lookup), so this is the only reliable place to intercept every call
    without touching each of the ~40 individually-decorated tool functions.
    ``maybe_span`` is a no-op unless REMIND_ME_OTEL_ENABLED is set, so this
    has no effect (and negligible overhead) by default.

    This is also the choke point for the slow-call watchdog (issue #128,
    see ``remind_me_mcp.watchdog``): arming/disarming here means a stuck
    call gets its stack dumped automatically, without needing an operator to
    already have py-spy installed and know to reach for it.
    """

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> Sequence[ContentBlock] | dict[str, Any]:
        with maybe_span(f"tool.{name}"):
            watchdog.arm()
            try:
                return await super().call_tool(name, arguments)
            finally:
                watchdog.disarm()


# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def app_lifespan(app: FastMCP):
    """Open the database at startup and close it on shutdown.

    Passed as the lifespan argument to the FastMCP constructor. On startup,
    opens the SQLite connection (triggering schema creation/migration),
    logs the database path, and kicks off a background update check.
    On shutdown (after yield), closes the connection.

    Args:
        app: The FastMCP application instance (unused, provided by the framework).

    Yields:
        Dict with key 'db' containing the open sqlite3.Connection.
    """
    db = _get_db()
    log.info("Remind Me MCP started — db at %s", DB_PATH)

    from remind_me_mcp.updater import start_background_check
    start_background_check()

    # Imported unconditionally (not just under SYNC_ENABLED) so shutdown can
    # always call them — both are no-ops when never started.
    from remind_me_mcp.peer_server import stop_peer_server
    from remind_me_mcp.sync import stop_sync_thread

    if SYNC_ENABLED:
        from remind_me_mcp.peer_server import start_peer_server
        from remind_me_mcp.sync import start_sync_thread
        start_peer_server()
        start_sync_thread()
        log.info("Sync started")

    # FT-03: folder watcher — start_watcher() is a no-op unless
    # REMIND_ME_WATCH_DIRS is configured with at least one valid directory.
    from remind_me_mcp.watcher import start_watcher, stop_watcher
    start_watcher()

    # Issue #179: reminder scheduler — unconditional, unlike the watcher
    # above (reminders have no separate opt-in, only a poll interval).
    from remind_me_mcp.scheduler import start_scheduler, stop_scheduler
    start_scheduler()

    # FT-09/Phase 5a: push/webhook ingestion — start_webhook_server() is a
    # no-op unless REMIND_ME_WEBHOOK_SECRET is configured.
    from remind_me_mcp.webhook_server import start_webhook_server, stop_webhook_server
    start_webhook_server()

    # FT-08: LLM Wiki — seed the maintainer schema and reconcile the file-backed
    # index into the DB at startup so external edits (hand edits, git pull) are
    # picked up. Best-effort: a wiki problem must never block server startup.
    try:
        from remind_me_mcp import wiki
        wiki.ensure_schema_file()
        stats = wiki.reconcile()
        log.info("Wiki ready at %s — %d page(s) indexed", wiki.wiki_dir(), stats["pages"])
    except Exception:  # noqa: BLE001 — never let the wiki layer break startup
        log.warning("Wiki startup reconcile failed", exc_info=True)

    try:
        yield {"db": db}
    finally:
        # FT-03/FT-09/SY-*/SE-07: stop the watcher, webhook, peer server, and
        # sync threads *before* closing the database connections so an
        # in-flight scan, request, or sync cycle never writes to a closed
        # handle. Issue #179: the reminder scheduler stops alongside the
        # watcher, for the same reason.
        stop_watcher()
        stop_scheduler()
        stop_webhook_server()
        stop_peer_server()
        stop_sync_thread()
        # Gap #10: persist the optional ANN index (if it was ever built this
        # process) before closing the DB connection it was built against —
        # a no-op when usearch isn't installed or no search has run yet.
        from remind_me_mcp.ann_index import save_index
        save_index()
        # Flush/shutdown the OTEL tracer (no-op unless tracing was ever
        # enabled) so the final batch of spans isn't silently dropped —
        # BatchSpanProcessor exports on a background thread that nothing
        # else joins.
        from remind_me_mcp import telemetry
        telemetry.shutdown()
        # SE-07: always close every tracked connection, even when the body
        # raised — otherwise file descriptors leak and the WAL is never
        # checkpointed.
        _close_db()

# ---------------------------------------------------------------------------
# Server instructions
# ---------------------------------------------------------------------------

# Carried in the MCP `initialize` response and surfaced to the model by the
# client, every session, in every client. Before this existed, the only
# behavioural guidance was a prose block in the README that the user had to
# paste into each client's custom instructions by hand — per-client, silently
# absent wherever it wasn't pasted, and free to drift from the tools it
# described. Keep it operational and short: it costs context on every session,
# so it earns its length only by changing what Claude actually does.
SERVER_INSTRUCTIONS = """\
remind_me is the user's persistent memory across Claude sessions, clients, and
machines. Anything stored here is available in future conversations, on their
other devices, and in their other Claude clients.

## When to retrieve

Call `remind_me_search` before answering whenever the answer depends on
something only the user's own history establishes — their preferences,
decisions, projects, people, tools, or past problems and how they were
resolved. Prefer searching over guessing, and over asking the user to repeat
context they have already given you.

`remind_me_search` is the entry point: it fuses keyword and semantic matching
and auto-routes its ranking strategy per query. Use `remind_me_list` only to
browse a known category or tag — not to find something. For a question about
one specific person, project, or tool, `remind_me_entity` returns that
entity's facts and linked memories directly, and `remind_me_entity_traverse`
chains relations across hops. To load synthesised background instead of raw
fragments, use `remind_me_wiki_load`.

## When to store

Store durable facts, preferences, decisions, and resolutions with
`remind_me_add` as they come up — you do not need to be asked each time.
Prefer one atomic fact per memory, and pass `subject`/`predicate`/`object`
plus `entities` when the fact concerns a specific person, project, or tool, so
it joins the knowledge graph instead of sitting as loose prose. Do not store
secrets, credentials, transient chatter, or anything the user asks you not to
keep. To persist a whole conversation, `remind_me_auto_capture` stores the
verbatim dialog and a distilled summary as two linked memories.

## Feedback

`remind_me_feedback` records whether a retrieved memory was actually helpful,
which tunes future ranking. Send it when a result was clearly useful or
clearly wrong — not after every search.

## Maintenance

The batch and admin tools (decompose, normalize, extract/annotate, reclassify,
consolidate, wiki compile, import, sync, backup) are operator workflows, not
conversational ones. Run them when the user asks, or when a tool response
nudges you to — not spontaneously. Each multi-step loop also has a matching
prompt that drives it end to end.
"""

# ---------------------------------------------------------------------------
# MCP server instance
# ---------------------------------------------------------------------------

mcp = _TracedFastMCP(
    "remind_me_mcp", instructions=SERVER_INSTRUCTIONS, lifespan=app_lifespan
)

# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "mcp",
    "app_lifespan",
    "SERVER_INSTRUCTIONS",
]
