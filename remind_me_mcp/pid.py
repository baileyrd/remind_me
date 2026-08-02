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
from typing import TYPE_CHECKING, Any

from remind_me_mcp.config import DB_PATH, PID_FILE
from remind_me_mcp.db import _now_iso

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger("remind_me_mcp.pid")

# ---------------------------------------------------------------------------
# PID file operations
# ---------------------------------------------------------------------------


def _pid_is_alive(pid: int) -> bool:
    """Check whether a process with the given PID is currently running.

    On POSIX, os.kill(pid, 0) is a safe existence check. On Windows, signal 0
    is aliased to CTRL_C_EVENT (GenerateConsoleCtrlEvent), which raises OSError
    for a live process that isn't a console process-group leader — so we use
    OpenProcess via ctypes instead, which is a real existence check there.
    """
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
            process_query_limited_information, False, pid
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another user — still alive.
        return True
    return True


def _read_pid_file(path: Path | None = None) -> dict[str, Any] | None:
    """Read a PID file and verify the recorded process is still alive.

    Checks whether the process listed in the PID file is running via
    _pid_is_alive(). Removes stale or malformed PID files automatically.

    Args:
        path: The PID file to read. Defaults to PID_FILE (the UI dashboard's
            file); pass MCP_PID_FILE for the standalone/combined MCP server's
            own single-instance lock (issue #126).

    Returns:
        The parsed PID file dict (with keys pid, host, port, url,
        started_at) if the server is running, or None if no server is
        running or the PID file is stale/missing/malformed.
    """
    path = path or PID_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        pid = data.get("pid")
        # Check if process is actually alive
        if pid:
            if _pid_is_alive(pid):
                return data
            # Process is dead, clean up stale PID file
            path.unlink(missing_ok=True)
            return None
        return None
    except (json.JSONDecodeError, KeyError, TypeError):
        path.unlink(missing_ok=True)
        return None


def _write_pid_file(host: str, port: int, path: Path | None = None) -> None:
    """Write a JSON PID file recording the current process and server address.

    Called immediately after a server starts. The file is used by
    _read_pid_file() (and, for the UI server, _check_ui_server_health()) to
    detect running instances.

    Args:
        host: The hostname or IP address the server is bound to.
        port: The TCP port the server is listening on.
        path: The PID file to write. Defaults to PID_FILE.
    """
    (path or PID_FILE).write_text(json.dumps({
        "pid": os.getpid(),
        "host": host,
        "port": port,
        "url": f"http://{host}:{port}",
        "started_at": _now_iso(),
    }, indent=2))


def _remove_pid_file(path: Path | None = None) -> None:
    """Remove a PID file on server shutdown.

    Safe to call even if the file does not exist (missing_ok=True).
    Registered as an atexit handler and SIGTERM/SIGINT handler in __main__.py.

    Args:
        path: The PID file to remove. Defaults to PID_FILE.
    """
    (path or PID_FILE).unlink(missing_ok=True)


def _check_ui_server_health(url: str) -> bool:
    """Perform a quick HTTP health check against the UI server.

    Sends a GET request to the unauthenticated {url}/health liveness route
    with a 2-second timeout (SE-04 — /api/stats requires a bearer token, so
    probing it would report a healthy authenticated server as down).
    Used to distinguish a live server from a stale PID file.

    Args:
        url: Base URL of the dashboard server, e.g. 'http://127.0.0.1:5199'.

    Returns:
        True if the server responds with HTTP 200, False otherwise.
    """
    import urllib.request
    try:
        req = urllib.request.Request(url + "/health", method="GET")
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
    "_pid_is_alive",
    "_read_pid_file",
    "_write_pid_file",
    "_remove_pid_file",
    "_check_ui_server_health",
    "get_server_status",
]
