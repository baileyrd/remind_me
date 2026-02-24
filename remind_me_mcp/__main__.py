"""
remind_me_mcp.__main__ — CLI argument parsing and mode dispatch.

Supports multiple execution modes:
  - MCP stdio mode (default): runs the FastMCP server over stdin/stdout
  - UI server mode (--serve-ui): starts the Starlette dashboard HTTP server
  - Status mode (--status): checks if the dashboard is running and exits
  - Version mode (--version): prints the installed version and exits
  - Check-update mode (--check-update): checks for updates and exits
  - Update mode (--update): pulls latest changes and reinstalls

Usage:
  python -m remind_me_mcp [--serve-ui] [--ui-port PORT] [--ui-host HOST]
                           [--status] [--version] [--check-update] [--update]
"""

from __future__ import annotations

import argparse
import atexit
import logging
import signal
import sys

import remind_me_mcp.tools  # noqa: F401 — ensure tools are registered before mcp.run()
from remind_me_mcp.api import _build_api_app
from remind_me_mcp.config import SERVE_UI, UI_PORT
from remind_me_mcp.pid import (
    _check_ui_server_health,
    _read_pid_file,
    _remove_pid_file,
    _write_pid_file,
    get_server_status,
)
from remind_me_mcp.server import mcp

log = logging.getLogger("remind_me_mcp.__main__")

__all__ = ["main"]


def main() -> None:
    """Parse CLI arguments and dispatch to the appropriate execution mode."""
    parser = argparse.ArgumentParser(description="Remind Me MCP Server")
    parser.add_argument(
        "--serve-ui",
        action="store_true",
        default=SERVE_UI,
        help="Start the HTTP dashboard UI server",
    )
    parser.add_argument(
        "--ui-port",
        type=int,
        default=UI_PORT,
        help="Port for the dashboard UI (default: 5199)",
    )
    parser.add_argument(
        "--ui-host",
        type=str,
        default="127.0.0.1",
        help="Host to bind the UI server (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Check if the UI server is running and exit",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print the installed version and exit",
    )
    parser.add_argument(
        "--check-update",
        action="store_true",
        help="Check for available updates and exit",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Pull latest changes from origin and reinstall",
    )
    args = parser.parse_args()

    # -- Version mode --
    if args.version:
        from remind_me_mcp import __version__

        print(f"remind-me-mcp {__version__}")
        sys.exit(0)

    # -- Check-update mode --
    if args.check_update:
        from remind_me_mcp.updater import check_for_update

        status = check_for_update()
        if status.error:
            print(f"Error: {status.error}")
            sys.exit(1)
        print(f"Installed: {status.installed_version} (commit {status.local_commit})")
        print(f"Remote:    commit {status.remote_commit}")
        if status.update_available:
            print(f"\nUpdate available — {status.commits_behind} commit(s) behind")
            if status.commit_messages:
                print("\nRecent changes:")
                for msg in status.commit_messages[:10]:
                    print(f"  {msg}")
            print("\nRun 'remind-me-mcp --update' to apply.")
        else:
            print("\nUp to date.")
        sys.exit(0)

    # -- Update mode --
    if args.update:
        from remind_me_mcp.updater import check_for_update, perform_update

        print("Checking for updates...")
        status = check_for_update()
        if status.error:
            print(f"Error: {status.error}")
            sys.exit(1)
        if not status.update_available:
            print(f"Already up to date at {status.installed_version} (commit {status.local_commit}).")
            sys.exit(0)

        print(f"Update available: {status.commits_behind} commit(s) behind")
        print("Pulling and reinstalling...")
        result = perform_update()
        if not result.success:
            print(f"Update failed: {result.error}")
            sys.exit(1)
        print(f"Updated: {result.previous_version} -> {result.new_version}")
        print(f"Commits: {result.previous_commit} -> {result.new_commit}")
        if result.restart_required:
            print("\nRestart the MCP server for changes to take effect.")
        sys.exit(0)

    # -- Status check mode --
    if args.status:
        status = get_server_status()
        if status["ui_server"] == "running":
            print(f"\u2713 Dashboard running at {status['ui_url']} (PID {status['ui_pid']})")
        else:
            print("\u2717 Dashboard not running")
        print(f"  Database: {status['db_path']} ({'exists' if status['db_exists'] else 'missing'})")
        sys.exit(0)

    # -- UI server mode --
    if args.serve_ui:
        import uvicorn

        # Check if already running
        existing = _read_pid_file()
        if existing and _check_ui_server_health(existing.get("url", "")):
            log.warning(
                "Dashboard is already running at %s (PID %d). "
                "Stop it first or use a different port with --ui-port.",
                existing["url"],
                existing["pid"],
            )
            sys.exit(1)

        # Write PID file and register cleanup
        _write_pid_file(args.ui_host, args.ui_port)
        atexit.register(_remove_pid_file)

        def _signal_handler(signum, frame):
            """Handle SIGTERM and SIGINT by cleaning up PID file and exiting."""
            _remove_pid_file()
            sys.exit(0)

        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)

        app = _build_api_app()
        log.info("Starting Remind Me dashboard at http://%s:%d", args.ui_host, args.ui_port)
        uvicorn.run(app, host=args.ui_host, port=args.ui_port, log_level="info")

    # -- MCP stdio mode --
    else:
        # Check if UI server is running and log it
        existing = _read_pid_file()
        if existing and _check_ui_server_health(existing.get("url", "")):
            log.info("Dashboard UI is running at %s", existing["url"])
        mcp.run()


if __name__ == "__main__":
    main()
