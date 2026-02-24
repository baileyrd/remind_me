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
    """Read the PID file to check if a UI server is running."""
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
    """Write PID file when UI server starts."""
    PID_FILE.write_text(json.dumps({
        "pid": os.getpid(),
        "host": host,
        "port": port,
        "url": f"http://{host}:{port}",
        "started_at": _now_iso(),
    }, indent=2))


def _remove_pid_file() -> None:
    """Clean up PID file on shutdown."""
    PID_FILE.unlink(missing_ok=True)


def _check_ui_server_health(url: str) -> bool:
    """Quick check if the UI server is actually responding."""
    import urllib.request
    try:
        req = urllib.request.Request(url + "/api/stats", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def get_server_status() -> dict[str, Any]:
    """Get the current status of all running instances."""
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
