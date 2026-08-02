"""
remind_me_mcp.cli — `add` / `search` / `list` command-line subcommands (issue #189).

Dispatch: __main__.main() checks sys.argv[1] against _SUBCOMMANDS *before*
building its existing flag-based argparse.ArgumentParser, and routes matching
invocations here instead. This is a separate code path, not a modification of
the existing parser (no subparsers grafted onto it, no new positional
arguments it has to tolerate) — the existing `--serve-ui`/`--serve-remote`/
`--status`/`--version`/`--check-update`/`--update`/`--list-backups`/
`--restore` flags are completely unaffected, since none of their invocations
ever pass "add", "search", or "list" as the first argument.

Reuses the real MCP tool logic (`tools.crud.memory_add`/`memory_list`,
`tools.search.memory_search`) directly via `asyncio.run()` rather than
reimplementing retrieval/ranking or duplicating the add/list SQL: FastMCP's
`@mcp.tool(...)` decorator (see `mcp.server.fastmcp.FastMCP.tool`) registers
the function as a callable tool and returns the *original* function
unchanged, so `memory_add`/`memory_search`/`memory_list` are plain
`async def` functions taking a Pydantic input model and returning a string —
nothing about them is FastMCP-call-machinery-specific (no `Context` param, no
reliance on request/session state), so calling them directly here executes
the exact same code path an MCP client's tool call would.

Single-instance lock (issue #126): this module deliberately never touches
`pid._acquire_mcp_lock`/`MCP_PID_FILE`. That lock exists to stop two *server*
processes (each running their own background sync thread, folder watcher,
reminder scheduler, etc.) from racing each other against the same DB file —
it is not a general-purpose "only one process may open this SQLite file"
guard. A CLI command opens a connection, does one small read or write, and
exits; it starts no background threads and holds the connection for a few
milliseconds. Concurrent access with a running server is safe by the same
WAL-mode contract the README already documents for concurrent reads: SQLite's
WAL journal mode (set by db._get_db() on every connection, CLI or server)
allows any number of concurrent readers plus exactly one writer without
blocking readers; a second writer (e.g. the CLI writing while the server's
reminder scheduler or a sync cycle is mid-write) doesn't corrupt anything —
it simply queues behind `PRAGMA busy_timeout` (5000ms, the same pragma both
sides set) until the first writer commits, then proceeds. A CLI write that
still can't acquire the writer lock within that window raises
`sqlite3.OperationalError: database is locked`, surfaced as a clean CLI error
(see `_run`) rather than a silent hang or a raised traceback.

First run: no separate "initialize the store" step. `db._get_db()` (called
internally by every tool function below) creates `REMIND_ME_MCP_DIR` and
`memory.db` and runs schema migrations to the current version on first
connection — identical to what happens the first time the MCP server itself
starts. The CLI does not special-case a missing database file; it just works,
matching that existing first-run behavior.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import TYPE_CHECKING

from pydantic import ValidationError

# Importing remind_me_mcp.tools (not just remind_me_mcp.server) runs every
# submodule's @mcp.tool decorators, which is what populates the
# remind_me_mcp.tools package namespace (_pkg.<name>) that memory_add's body
# resolves helpers through at call time (HY-02) — importing only
# tools.crud/tools.search directly would skip that registration and leave
# names like `_pkg._embed_and_store` unresolved.
import remind_me_mcp.tools  # noqa: F401 — registration side effect, not a direct symbol use

if TYPE_CHECKING:
    from collections.abc import Sequence

# Subcommand names __main__.main() checks sys.argv[1] against before falling
# back to the existing flag parser. Kept here (not in __main__) so the two
# modules' argument-parsing concerns stay separated.
SUBCOMMANDS = frozenset({"add", "search", "list"})


def _split_tags(raw: str) -> list[str]:
    """Parse a comma-separated --tags value into a list, dropping blanks.

    ``""`` (the default, --tags omitted) yields ``[]``, matching
    MemoryAddInput.tags's own default_factory=list.
    """
    return [t.strip() for t in raw.split(",") if t.strip()]


def _build_parser() -> argparse.ArgumentParser:
    """Build the `add`/`search`/`list` subcommand parser, separate from
    __main__.main()'s existing flag-based ArgumentParser (see module
    docstring for why these are never merged into one parser)."""
    parser = argparse.ArgumentParser(
        prog="remind-me-mcp",
        description="Command-line access to the remind-me memory store (same REMIND_ME_MCP_DIR as the server).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_p = subparsers.add_parser("add", help="Add a new memory")
    add_p.add_argument("content", help="Memory content to store")
    add_p.add_argument(
        "--category", default="general", help="Category for organization (default: general)"
    )
    add_p.add_argument(
        "--tags", default="", help="Comma-separated tags, e.g. --tags work,important"
    )

    search_p = subparsers.add_parser("search", help="Search memories (hybrid keyword + semantic)")
    search_p.add_argument("query", help="Search query")
    search_p.add_argument("--limit", type=int, default=20, help="Max results (default: 20)")
    search_p.add_argument("--json", action="store_true", dest="as_json", help="Output as JSON")

    list_p = subparsers.add_parser("list", help="List memories (no ranking — browse by filter)")
    list_p.add_argument("--limit", type=int, default=20, help="Max results (default: 20)")
    list_p.add_argument("--category", default=None, help="Filter by category")
    list_p.add_argument("--json", action="store_true", dest="as_json", help="Output as JSON")

    return parser


def _cmd_add(args: argparse.Namespace) -> int:
    from remind_me_mcp.models import MemoryAddInput
    from remind_me_mcp.tools.crud import memory_add

    try:
        params = MemoryAddInput(
            content=args.content, category=args.category, tags=_split_tags(args.tags)
        )
    except ValidationError as e:
        print(f"Error: invalid input — {e}", file=sys.stderr)
        return 1

    result = asyncio.run(memory_add(params))
    print(result)
    return 1 if result.startswith("Error:") else 0


def _cmd_search(args: argparse.Namespace) -> int:
    from remind_me_mcp.models import MemorySearchInput, ResponseFormat
    from remind_me_mcp.tools.search import memory_search

    try:
        params = MemorySearchInput(
            query=args.query,
            limit=args.limit,
            response_format=ResponseFormat.JSON if args.as_json else ResponseFormat.MARKDOWN,
        )
    except ValidationError as e:
        print(f"Error: invalid input — {e}", file=sys.stderr)
        return 1

    result = asyncio.run(memory_search(params))
    print(result)
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    from remind_me_mcp.models import MemoryListInput, ResponseFormat
    from remind_me_mcp.tools.crud import memory_list

    try:
        params = MemoryListInput(
            category=args.category,
            limit=args.limit,
            response_format=ResponseFormat.JSON if args.as_json else ResponseFormat.MARKDOWN,
        )
    except ValidationError as e:
        print(f"Error: invalid input — {e}", file=sys.stderr)
        return 1

    result = asyncio.run(memory_list(params))
    print(result)
    return 0


_HANDLERS = {"add": _cmd_add, "search": _cmd_search, "list": _cmd_list}


def run_cli(argv: Sequence[str]) -> int:
    """Parse and dispatch an `add`/`search`/`list` invocation.

    Args:
        argv: The subcommand and its arguments, e.g. ``["add", "content",
            "--category", "work"]`` — NOT including the program name
            (mirrors argparse.parse_args's own convention).

    Returns:
        Process exit code: 0 on success, non-zero on validation or DB error.
        Malformed arguments (missing required value, unknown flag) are
        handled by argparse itself, which prints a usage message to stderr
        and exits 2 — never a raw traceback.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return _HANDLERS[args.command](args)
    except Exception as exc:  # noqa: BLE001 — CLI boundary: never leak a traceback to the user
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        # Release the connection this command opened (WAL checkpoint on last
        # close) rather than leaving it open until process exit — a CLI
        # command is a one-shot, not a long-lived server.
        from remind_me_mcp.db import _close_db

        _close_db()
