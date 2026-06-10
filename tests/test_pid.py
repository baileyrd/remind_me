"""
Behavior tests for remind_me_mcp.pid — PID-file lifecycle and server status.

All tests redirect PID_FILE to tmp_path so they never touch the real
~/.remind-me/. Process-liveness and HTTP health checks are exercised at
their true boundaries (os.kill / urllib.request.urlopen).
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest

import remind_me_mcp.pid as pid_mod
from remind_me_mcp.pid import (
    _check_ui_server_health,
    _read_pid_file,
    _remove_pid_file,
    _write_pid_file,
    get_server_status,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def pid_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the module-level PID_FILE constant to a per-test temp path."""
    p = tmp_path / "server.pid"
    monkeypatch.setattr(pid_mod, "PID_FILE", p)
    return p


# ---------------------------------------------------------------------------
# Write / read roundtrip
# ---------------------------------------------------------------------------


def test_write_pid_file_records_current_process(pid_file: Path) -> None:
    """_write_pid_file writes a JSON document describing this process."""
    _write_pid_file("127.0.0.1", 5199)

    assert pid_file.exists()
    data = json.loads(pid_file.read_text())
    assert data["pid"] == os.getpid()
    assert data["host"] == "127.0.0.1"
    assert data["port"] == 5199
    assert data["url"] == "http://127.0.0.1:5199"
    assert data["started_at"]  # ISO timestamp, non-empty


def test_read_pid_file_roundtrip_live_process(pid_file: Path) -> None:
    """_read_pid_file returns the parsed dict when the recorded PID is alive."""
    _write_pid_file("127.0.0.1", 6001)

    data = _read_pid_file()
    assert data is not None
    assert data["pid"] == os.getpid()
    assert data["url"] == "http://127.0.0.1:6001"
    # File is left in place for a live process
    assert pid_file.exists()


def test_read_pid_file_missing_returns_none(pid_file: Path) -> None:
    """No PID file means no running server."""
    assert _read_pid_file() is None


# ---------------------------------------------------------------------------
# Stale / malformed PID files
# ---------------------------------------------------------------------------


def test_read_pid_file_stale_pid_cleans_up(pid_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A PID file pointing at a dead process is treated as stale and removed."""
    pid_file.write_text(json.dumps({"pid": 4194304, "host": "127.0.0.1", "port": 5199, "url": "http://127.0.0.1:5199"}))

    def dead_kill(pid: int, sig: int) -> None:
        raise ProcessLookupError(pid)

    monkeypatch.setattr(os, "kill", dead_kill)

    assert _read_pid_file() is None
    assert not pid_file.exists()


def test_read_pid_file_malformed_json_cleans_up(pid_file: Path) -> None:
    """A corrupt PID file is removed and reported as no server running."""
    pid_file.write_text("{not valid json")

    assert _read_pid_file() is None
    assert not pid_file.exists()


def test_read_pid_file_missing_pid_key_returns_none(pid_file: Path) -> None:
    """A PID file without a 'pid' value yields None (no liveness to verify)."""
    pid_file.write_text(json.dumps({"host": "127.0.0.1", "port": 5199}))

    assert _read_pid_file() is None


# ---------------------------------------------------------------------------
# Removal
# ---------------------------------------------------------------------------


def test_remove_pid_file_deletes_file(pid_file: Path) -> None:
    """_remove_pid_file deletes an existing PID file."""
    _write_pid_file("127.0.0.1", 5199)
    assert pid_file.exists()

    _remove_pid_file()
    assert not pid_file.exists()


def test_remove_pid_file_idempotent(pid_file: Path) -> None:
    """_remove_pid_file is safe to call when no PID file exists."""
    assert not pid_file.exists()
    _remove_pid_file()  # must not raise
    assert not pid_file.exists()


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc) -> None:
        return None


def test_check_ui_server_health_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 response from /api/stats means the server is healthy."""
    import urllib.request

    seen_urls: list[str] = []

    def fake_urlopen(req, timeout=None):
        seen_urls.append(req.full_url)
        return _FakeResponse(200)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert _check_ui_server_health("http://127.0.0.1:5199") is True
    assert seen_urls == ["http://127.0.0.1:5199/api/stats"]


def test_check_ui_server_health_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-200 response is reported as unhealthy."""
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(503))

    assert _check_ui_server_health("http://127.0.0.1:5199") is False


def test_check_ui_server_health_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connection failures (OSError family) are reported as unhealthy."""
    import urllib.error
    import urllib.request

    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert _check_ui_server_health("http://127.0.0.1:5199") is False


# ---------------------------------------------------------------------------
# Combined server status
# ---------------------------------------------------------------------------


def test_get_server_status_running(pid_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A live PID file plus a healthy HTTP check reports 'running'."""
    _write_pid_file("127.0.0.1", 5199)
    monkeypatch.setattr(pid_mod, "_check_ui_server_health", lambda url: True)

    status = get_server_status()
    assert status["ui_server"] == "running"
    assert status["ui_url"] == "http://127.0.0.1:5199"
    assert status["ui_pid"] == os.getpid()
    assert status["ui_started"]
    assert isinstance(status["db_path"], str)
    assert isinstance(status["db_exists"], bool)


def test_get_server_status_stopped_when_no_pid_file(pid_file: Path) -> None:
    """No PID file reports 'stopped' with a None URL."""
    status = get_server_status()
    assert status["ui_server"] == "stopped"
    assert status["ui_url"] is None
    assert isinstance(status["db_path"], str)
    assert isinstance(status["db_exists"], bool)


def test_get_server_status_stopped_when_unhealthy(pid_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A live PID but failing health check still reports 'stopped'."""
    _write_pid_file("127.0.0.1", 5199)
    monkeypatch.setattr(pid_mod, "_check_ui_server_health", lambda url: False)

    status = get_server_status()
    assert status["ui_server"] == "stopped"
    assert status["ui_url"] is None
