"""
remind_me_mcp.pid — PID file management and server instance detection.

Tracks running UI server instances via a JSON PID file. Provides functions
to read, write, and remove the PID file, health-check the server, and
report combined server status.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from remind_me_mcp.config import DB_PATH, PID_FILE
from remind_me_mcp.db import _now_iso

log = logging.getLogger("remind_me_mcp.pid")

# ---------------------------------------------------------------------------
# PID file operations
# ---------------------------------------------------------------------------


def _read_pid_file() -> dict[str, Any] | None:
    """Read the PID file and verify the recorded process is still alive.

    Checks whether the process listed in the PID file is running via
    os.kill(pid, 0). Removes stale or malformed PID files automatically.

    Returns:
        The parsed PID file dict (with keys pid, host, port, url,
        started_at) if the server is running, or None if no server is
        running or the PID file is stale/missing/malformed.
    """
    if not PID_FILE.exists():
        return None
    try:
        data = json.loads(PID_FILE.read_text())
        pid = data.get("pid")
        # Check if process is actually alive
        if pid:
            try:
                os.kill(pid, 0)  # signal 0 = just check existence
                return data
            except OSError:
                # Process is dead, clean up stale PID file
                PID_FILE.unlink(missing_ok=True)
                return None
        return None
    except (json.JSONDecodeError, KeyError, TypeError):
        PID_FILE.unlink(missing_ok=True)
        return None


def _write_pid_file(host: str, port: int) -> None:
    """Write a JSON PID file recording the current process and server address.

    Called immediately after the UI server starts. The file is used by
    _read_pid_file() and _check_ui_server_health() to detect running instances.

    Args:
        host: The hostname or IP address the server is bound to.
        port: The TCP port the server is listening on.
    """
    PID_FILE.write_text(json.dumps({
        "pid": os.getpid(),
        "host": host,
        "port": port,
        "url": f"http://{host}:{port}",
        "started_at": _now_iso(),
    }, indent=2))


def _remove_pid_file() -> None:
    """Remove the PID file on server shutdown.

    Safe to call even if the file does not exist (missing_ok=True).
    Registered as an atexit handler and SIGTERM/SIGINT handler in __main__.py.
    """
    PID_FILE.unlink(missing_ok=True)


def _check_ui_server_health(url: str) -> bool:
    """Perform a quick HTTP health check against the UI server.

    Sends a GET request to {url}/api/stats with a 2-second timeout.
    Used to distinguish a live server from a stale PID file.

    Args:
        url: Base URL of the dashboard server, e.g. 'http://127.0.0.1:5199'.

    Returns:
        True if the server responds with HTTP 200, False otherwise.
    """
    import urllib.request
    try:
        req = urllib.request.Request(url + "/api/stats", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except OSError:
        return False


def get_server_status() -> dict[str, Any]:
    """Get the current status of the UI dashboard server.

    Combines PID file inspection and an HTTP health check to determine
    whether the server is actually running and responding.

    Returns:
        Dict with keys: ui_server ('running' or 'stopped'), ui_url,
        db_path, db_exists. Running instances also include ui_pid and
        ui_started.
    """
    info = _read_pid_file()
    if info and _check_ui_server_health(info.get("url", "")):
        return {
            "ui_server": "running",
            "ui_url": info["url"],
            "ui_pid": info["pid"],
            "ui_started": info.get("started_at", "unknown"),
            "db_path": str(DB_PATH),
            "db_exists": DB_PATH.exists(),
        }
    return {
        "ui_server": "stopped",
        "ui_url": None,
        "db_path": str(DB_PATH),
        "db_exists": DB_PATH.exists(),
    }


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "_read_pid_file",
    "_write_pid_file",
    "_remove_pid_file",
    "_check_ui_server_health",
    "get_server_status",
]
