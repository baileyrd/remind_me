"""
Behavior tests for remind_me_mcp.__main__ — CLI argument parsing and mode dispatch.

Every test drives the real main() entry point via sys.argv. Network-facing
boundaries (uvicorn.run, FastMCP.run, updater subprocess calls, PID-file
health checks) are monkeypatched; argument parsing, dispatch logic, and
console output are exercised for real.
"""

from __future__ import annotations

import argparse
import signal
from typing import Any

import pytest

import remind_me_mcp.__main__ as main_mod
import remind_me_mcp.updater as updater_mod
from remind_me_mcp.updater import UpdateResult, UpdateStatus


def _run_main(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    """Invoke main() with the given CLI arguments."""
    monkeypatch.setattr("sys.argv", ["remind-me-mcp", *argv])
    main_mod.main()


def _run_main_expect_exit(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    """Invoke main() expecting sys.exit; return the exit code."""
    with pytest.raises(SystemExit) as excinfo:
        _run_main(monkeypatch, *argv)
    return 0 if excinfo.value.code is None else int(excinfo.value.code)


def _status(**overrides: Any) -> UpdateStatus:
    defaults: dict[str, Any] = {
        "installed_version": "1.0.0",
        "local_commit": "abc1234",
        "remote_commit": "def5678",
        "update_available": False,
        "commits_behind": 0,
    }
    defaults.update(overrides)
    return UpdateStatus(**defaults)


# ---------------------------------------------------------------------------
# --version
# ---------------------------------------------------------------------------


def test_version_prints_and_exits_zero(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """--version prints the package version and exits 0."""
    from remind_me_mcp import __version__

    code = _run_main_expect_exit(monkeypatch, "--version")
    out = capsys.readouterr().out
    assert code == 0
    assert f"remind-me-mcp {__version__}" in out


# ---------------------------------------------------------------------------
# --check-update
# ---------------------------------------------------------------------------


def test_check_update_error_exits_one(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """A failed update check prints the error and exits 1."""
    monkeypatch.setattr(updater_mod, "check_for_update", lambda: _status(error="not a git repo"))

    code = _run_main_expect_exit(monkeypatch, "--check-update")
    assert code == 1
    assert "Error: not a git repo" in capsys.readouterr().out


def test_check_update_up_to_date(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """An up-to-date install reports versions and exits 0."""
    monkeypatch.setattr(updater_mod, "check_for_update", lambda: _status())

    code = _run_main_expect_exit(monkeypatch, "--check-update")
    out = capsys.readouterr().out
    assert code == 0
    assert "Installed: 1.0.0 (commit abc1234)" in out
    assert "Up to date." in out


def test_check_update_available_lists_commits(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """An available update prints commits behind and recent messages."""
    monkeypatch.setattr(
        updater_mod,
        "check_for_update",
        lambda: _status(update_available=True, commits_behind=2, commit_messages=["fix: a", "feat: b"]),
    )

    code = _run_main_expect_exit(monkeypatch, "--check-update")
    out = capsys.readouterr().out
    assert code == 0
    assert "Update available — 2 commit(s) behind" in out
    assert "fix: a" in out
    assert "feat: b" in out
    assert "--update" in out


# ---------------------------------------------------------------------------
# --update
# ---------------------------------------------------------------------------


def test_update_check_error_exits_one(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """--update aborts with exit 1 when the pre-check fails."""
    monkeypatch.setattr(updater_mod, "check_for_update", lambda: _status(error="offline"))

    code = _run_main_expect_exit(monkeypatch, "--update")
    assert code == 1
    assert "Error: offline" in capsys.readouterr().out


def test_update_already_up_to_date(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """--update exits 0 without updating when nothing is behind."""
    monkeypatch.setattr(updater_mod, "check_for_update", lambda: _status())
    monkeypatch.setattr(updater_mod, "perform_update", lambda **kw: pytest.fail("must not update"))

    code = _run_main_expect_exit(monkeypatch, "--update")
    assert code == 0
    assert "Already up to date at 1.0.0 (commit abc1234)." in capsys.readouterr().out


def test_update_success_with_restart_notice(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """A successful update reports versions/commits and the restart hint."""
    monkeypatch.setattr(
        updater_mod, "check_for_update", lambda: _status(update_available=True, commits_behind=1)
    )
    result = UpdateResult(
        success=True,
        previous_commit="abc1234",
        new_commit="def5678",
        previous_version="1.0.0",
        new_version="1.1.0",
        restart_required=True,
    )
    monkeypatch.setattr(updater_mod, "perform_update", lambda: result)

    code = _run_main_expect_exit(monkeypatch, "--update")
    out = capsys.readouterr().out
    assert code == 0
    assert "Updated: 1.0.0 -> 1.1.0" in out
    assert "Commits: abc1234 -> def5678" in out
    assert "Restart the MCP server" in out


def test_update_failure_exits_one(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """A failed update prints the error and exits 1."""
    monkeypatch.setattr(
        updater_mod, "check_for_update", lambda: _status(update_available=True, commits_behind=1)
    )
    monkeypatch.setattr(updater_mod, "perform_update", lambda: UpdateResult(success=False, error="git pull failed"))

    code = _run_main_expect_exit(monkeypatch, "--update")
    assert code == 1
    assert "Update failed: git pull failed" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# --status
# ---------------------------------------------------------------------------


def test_status_running(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """--status reports a running dashboard with URL and PID."""
    monkeypatch.setattr(
        main_mod,
        "get_server_status",
        lambda: {
            "ui_server": "running",
            "ui_url": "http://127.0.0.1:5199",
            "ui_pid": 4242,
            "ui_started": "2026-01-01T00:00:00Z",
            "db_path": "/tmp/memory.db",
            "db_exists": True,
        },
    )

    code = _run_main_expect_exit(monkeypatch, "--status")
    out = capsys.readouterr().out
    assert code == 0
    assert "Dashboard running at http://127.0.0.1:5199 (PID 4242)" in out
    assert "Database: /tmp/memory.db (exists)" in out


def test_status_stopped(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """--status reports a stopped dashboard and missing database."""
    monkeypatch.setattr(
        main_mod,
        "get_server_status",
        lambda: {"ui_server": "stopped", "ui_url": None, "db_path": "/tmp/memory.db", "db_exists": False},
    )

    code = _run_main_expect_exit(monkeypatch, "--status")
    out = capsys.readouterr().out
    assert code == 0
    assert "Dashboard not running" in out
    assert "Database: /tmp/memory.db (missing)" in out


# ---------------------------------------------------------------------------
# MCP stdio mode (default, no flags)
# ---------------------------------------------------------------------------


def test_default_mode_runs_mcp_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no flags, main() runs the FastMCP server over stdio."""
    calls: list[dict] = []
    monkeypatch.setattr(main_mod.mcp, "run", lambda *a, **kw: calls.append({"args": a, "kwargs": kw}))
    monkeypatch.setattr(main_mod, "_read_pid_file", lambda: None)

    _run_main(monkeypatch)

    assert calls == [{"args": (), "kwargs": {}}]


def test_default_mode_logs_running_dashboard(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Stdio mode notes an already-running dashboard before serving."""
    monkeypatch.setattr(main_mod.mcp, "run", lambda *a, **kw: None)
    monkeypatch.setattr(
        main_mod, "_read_pid_file", lambda: {"pid": 4242, "url": "http://127.0.0.1:5199"}
    )
    monkeypatch.setattr(main_mod, "_check_ui_server_health", lambda url: True)

    with caplog.at_level("INFO", logger="remind_me_mcp.__main__"):
        _run_main(monkeypatch)

    assert "Dashboard UI is running at http://127.0.0.1:5199" in caplog.text


# ---------------------------------------------------------------------------
# --serve-mcp (standalone HTTP transport)
# ---------------------------------------------------------------------------


def test_serve_mcp_standalone(monkeypatch: pytest.MonkeyPatch) -> None:
    """--serve-mcp runs FastMCP with the streamable-http transport on the given host/port."""
    calls: list[dict] = []
    monkeypatch.setattr(main_mod.mcp, "run", lambda *a, **kw: calls.append(kw))

    _run_main(monkeypatch, "--serve-mcp", "--mcp-host", "0.0.0.0", "--mcp-port", "9999")

    assert calls == [{"transport": "streamable-http", "host": "0.0.0.0", "port": 9999}]


def test_serve_mcp_default_host_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """--serve-mcp defaults come from config (127.0.0.1:8767)."""
    from remind_me_mcp.config import MCP_HTTP_HOST, MCP_HTTP_PORT

    calls: list[dict] = []
    monkeypatch.setattr(main_mod.mcp, "run", lambda *a, **kw: calls.append(kw))

    _run_main(monkeypatch, "--serve-mcp")

    assert calls == [{"transport": "streamable-http", "host": MCP_HTTP_HOST, "port": MCP_HTTP_PORT}]


# ---------------------------------------------------------------------------
# --serve-ui (dashboard server)
# ---------------------------------------------------------------------------


def test_serve_ui_starts_uvicorn_and_writes_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    """--serve-ui writes the PID file, registers signal handlers, and serves the API app."""
    import uvicorn

    uvicorn_calls: list[dict] = []
    pid_writes: list[tuple] = []
    handlers: dict[int, Any] = {}

    monkeypatch.setattr(main_mod, "_read_pid_file", lambda: None)
    monkeypatch.setattr(main_mod, "_write_pid_file", lambda host, port: pid_writes.append((host, port)))
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: uvicorn_calls.append({"app": app, **kw}))
    monkeypatch.setattr(signal, "signal", lambda signum, handler: handlers.setdefault(signum, handler))

    _run_main(monkeypatch, "--serve-ui", "--ui-host", "0.0.0.0", "--ui-port", "6042")

    assert pid_writes == [("0.0.0.0", 6042)]
    assert len(uvicorn_calls) == 1
    assert uvicorn_calls[0]["host"] == "0.0.0.0"
    assert uvicorn_calls[0]["port"] == 6042
    assert signal.SIGTERM in handlers
    assert signal.SIGINT in handlers


def test_serve_ui_signal_handler_cleans_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    """The registered SIGTERM handler removes the PID file and exits 0."""
    import uvicorn

    removed: list[bool] = []
    handlers: dict[int, Any] = {}

    monkeypatch.setattr(main_mod, "_read_pid_file", lambda: None)
    monkeypatch.setattr(main_mod, "_write_pid_file", lambda host, port: None)
    monkeypatch.setattr(main_mod, "_remove_pid_file", lambda: removed.append(True))
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)
    monkeypatch.setattr(signal, "signal", lambda signum, handler: handlers.setdefault(signum, handler))

    _run_main(monkeypatch, "--serve-ui")

    with pytest.raises(SystemExit) as excinfo:
        handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert excinfo.value.code == 0
    assert removed == [True]


def test_serve_ui_refuses_when_already_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """--serve-ui exits 1 when a healthy instance is already running."""
    import uvicorn

    monkeypatch.setattr(
        main_mod, "_read_pid_file", lambda: {"pid": 4242, "url": "http://127.0.0.1:5199"}
    )
    monkeypatch.setattr(main_mod, "_check_ui_server_health", lambda url: True)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: pytest.fail("must not start uvicorn"))

    code = _run_main_expect_exit(monkeypatch, "--serve-ui")
    assert code == 1


def test_serve_ui_starts_when_pid_file_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale (unhealthy) PID file does not block startup."""
    import uvicorn

    uvicorn_calls: list[dict] = []
    monkeypatch.setattr(
        main_mod, "_read_pid_file", lambda: {"pid": 4242, "url": "http://127.0.0.1:5199"}
    )
    monkeypatch.setattr(main_mod, "_check_ui_server_health", lambda url: False)
    monkeypatch.setattr(main_mod, "_write_pid_file", lambda host, port: None)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: uvicorn_calls.append(kw))

    _run_main(monkeypatch, "--serve-ui")

    assert len(uvicorn_calls) == 1


# ---------------------------------------------------------------------------
# Combined mode (--serve-mcp --serve-ui)
# ---------------------------------------------------------------------------


def test_both_flags_dispatch_to_combined(monkeypatch: pytest.MonkeyPatch) -> None:
    """--serve-mcp together with --serve-ui dispatches to the combined server, not the standalone branches."""
    import uvicorn

    combined_calls: list[argparse.Namespace] = []
    monkeypatch.setattr(main_mod, "_run_combined", lambda args: combined_calls.append(args))
    monkeypatch.setattr(main_mod.mcp, "run", lambda *a, **kw: pytest.fail("standalone MCP branch must not run"))
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: pytest.fail("UI-only branch must not run"))

    _run_main(monkeypatch, "--serve-mcp", "--serve-ui", "--ui-port", "7001")

    assert len(combined_calls) == 1
    assert combined_calls[0].ui_port == 7001


def test_run_combined_mounts_mcp_and_dashboard(monkeypatch: pytest.MonkeyPatch) -> None:
    """_run_combined serves a single Starlette app mounting /mcp and the dashboard at /."""
    import uvicorn

    import remind_me_mcp.config as cfg

    uvicorn_calls: list[dict] = []
    monkeypatch.setattr(cfg, "MCP_HTTP_SECRET", None)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: uvicorn_calls.append({"app": app, **kw}))

    args = argparse.Namespace(ui_host="127.0.0.1", ui_port=5199)
    main_mod._run_combined(args)

    assert len(uvicorn_calls) == 1
    assert uvicorn_calls[0]["host"] == "127.0.0.1"
    assert uvicorn_calls[0]["port"] == 5199
    mounted = {route.path for route in uvicorn_calls[0]["app"].routes}
    assert "/mcp" in mounted
    assert "" in mounted or "/" in mounted


def test_run_combined_bearer_auth_rejects_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    """With MCP_HTTP_SECRET set, the mounted MCP app rejects missing/wrong bearer tokens."""
    import uvicorn
    from starlette.testclient import TestClient

    import remind_me_mcp.config as cfg

    captured: list[Any] = []
    monkeypatch.setattr(cfg, "MCP_HTTP_SECRET", "s3cret")
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.append(app))

    main_mod._run_combined(argparse.Namespace(ui_host="127.0.0.1", ui_port=5199))

    client = TestClient(captured[0], raise_server_exceptions=False)
    assert client.get("/mcp/").status_code == 401
    assert client.get("/mcp/", headers={"Authorization": "Bearer wrong"}).status_code == 401
