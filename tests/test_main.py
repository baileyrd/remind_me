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
    """--serve-mcp runs FastMCP with the streamable-http transport on the given host/port.

    SE-03: FastMCP.run() accepts no host/port kwargs (it would raise TypeError
    on the installed SDK); the bind address must go through mcp.settings.
    """
    calls: list[dict] = []
    monkeypatch.setattr(main_mod.mcp, "run", lambda *a, **kw: calls.append(kw))
    monkeypatch.setattr(main_mod.mcp.settings, "host", "sentinel-host")
    monkeypatch.setattr(main_mod.mcp.settings, "port", -1)

    _run_main(monkeypatch, "--serve-mcp", "--mcp-host", "0.0.0.0", "--mcp-port", "9999")

    assert calls == [{"transport": "streamable-http"}]
    assert main_mod.mcp.settings.host == "0.0.0.0"
    assert main_mod.mcp.settings.port == 9999


def test_serve_mcp_default_host_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """--serve-mcp defaults come from config (127.0.0.1:8767), applied via mcp.settings."""
    from remind_me_mcp.config import MCP_HTTP_HOST, MCP_HTTP_PORT

    calls: list[dict] = []
    monkeypatch.setattr(main_mod.mcp, "run", lambda *a, **kw: calls.append(kw))
    monkeypatch.setattr(main_mod.mcp.settings, "host", "sentinel-host")
    monkeypatch.setattr(main_mod.mcp.settings, "port", -1)

    _run_main(monkeypatch, "--serve-mcp")

    assert calls == [{"transport": "streamable-http"}]
    assert main_mod.mcp.settings.host == MCP_HTTP_HOST
    assert main_mod.mcp.settings.port == MCP_HTTP_PORT


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
    monkeypatch.setattr(cfg, "MCP_HTTP_SECRET", "s3cret")
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


# ---------------------------------------------------------------------------
# SEC-04: combined mode's /mcp must never be reachable without a secret,
# even when REMIND_ME_MCP_HTTP_SECRET is unset (previously: an empty
# middleware list, so the entire MCP tool-call surface was open by default
# whenever both --serve-mcp and --serve-ui were passed).
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_mcp_secret_dir(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Any:
    """Point MEMORY_DIR at a fresh per-test dir and clear any env secret."""
    import remind_me_mcp.config as cfg

    monkeypatch.setattr(cfg, "MCP_HTTP_SECRET", None)
    monkeypatch.setattr(cfg, "MEMORY_DIR", tmp_path)
    return tmp_path


def test_unset_secret_still_requires_auth(
    monkeypatch: pytest.MonkeyPatch, isolated_mcp_secret_dir: Any
) -> None:
    """With REMIND_ME_MCP_HTTP_SECRET unset, a secret is still auto-generated
    and /mcp still rejects unauthenticated requests -- the whole point of
    the fix: there is no way to end up with an open /mcp in combined mode."""
    from starlette.testclient import TestClient

    import remind_me_mcp.config as cfg

    monkeypatch.setattr(cfg, "API_KEY", "disabled")
    monkeypatch.setattr(cfg, "AUTO_UPDATE_CHECK", False)
    monkeypatch.setattr(main_mod.mcp, "_session_manager", None)

    app, secret = main_mod._build_combined_app()
    assert secret  # a real secret was generated, not None/empty
    with TestClient(app, base_url="http://127.0.0.1:5199", raise_server_exceptions=False) as client:
        assert client.post("/mcp", json=_MCP_INITIALIZE, headers=_MCP_HEADERS).status_code == 401
        r = client.post(
            "/mcp",
            json=_MCP_INITIALIZE,
            headers={**_MCP_HEADERS, "Authorization": f"Bearer {secret}"},
        )
        assert r.status_code == 200, r.text


def test_secret_generated_and_persisted(isolated_mcp_secret_dir: Any) -> None:
    """First resolution generates a high-entropy secret and persists it (0600)."""
    import stat
    import sys

    from remind_me_mcp.config import resolve_mcp_http_secret

    secret = resolve_mcp_http_secret()

    secret_file = isolated_mcp_secret_dir / "mcp_http_secret"
    assert secret_file.is_file()
    assert secret_file.read_text(encoding="utf-8").strip() == secret
    assert len(secret) >= 32
    if sys.platform != "win32":
        assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600


def test_secret_reused_across_calls(isolated_mcp_secret_dir: Any) -> None:
    """Subsequent resolutions return the persisted secret, not a fresh one."""
    from remind_me_mcp.config import resolve_mcp_http_secret

    first = resolve_mcp_http_secret()
    second = resolve_mcp_http_secret()
    assert first == second


def test_secret_rotation_by_deleting_file(isolated_mcp_secret_dir: Any) -> None:
    """Deleting the secret file rotates the credential on next resolution."""
    from remind_me_mcp.config import resolve_mcp_http_secret

    first = resolve_mcp_http_secret()
    (isolated_mcp_secret_dir / "mcp_http_secret").unlink()
    second = resolve_mcp_http_secret()
    assert first != second


def test_env_secret_wins_over_secret_file(
    isolated_mcp_secret_dir: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REMIND_ME_MCP_HTTP_SECRET overrides the persisted secret and writes no file."""
    import remind_me_mcp.config as cfg
    from remind_me_mcp.config import resolve_mcp_http_secret

    monkeypatch.setattr(cfg, "MCP_HTTP_SECRET", "  env-secret  ")
    assert resolve_mcp_http_secret() == "env-secret"
    assert not (isolated_mcp_secret_dir / "mcp_http_secret").exists()


def test_ephemeral_secret_when_unwritable(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """An unwritable MEMORY_DIR yields an ephemeral secret -- /mcp never falls open."""
    import remind_me_mcp.config as cfg
    from remind_me_mcp.config import resolve_mcp_http_secret

    blocker = tmp_path / "not_a_dir"
    blocker.write_text("file, not a directory")
    monkeypatch.setattr(cfg, "MCP_HTTP_SECRET", None)
    monkeypatch.setattr(cfg, "MEMORY_DIR", blocker)

    with caplog.at_level("WARNING", logger="remind_me_mcp.config"):
        secret = resolve_mcp_http_secret()

    assert secret
    assert "ephemeral" in caplog.text


# ---------------------------------------------------------------------------
# SE-03: combined mode must run the MCP app's lifespan
# ---------------------------------------------------------------------------


_MCP_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "se03-test", "version": "0"},
    },
}
_MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture()
def _combined_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prepare an isolated combined-app build: a fixed known MCP secret
    (auth is always on for combined mode, SEC-04), dashboard auth off,
    startup git-fetch disabled (SE-06), and a fresh StreamableHTTP session
    manager (its .run() is once-only per instance)."""
    import remind_me_mcp.config as cfg

    monkeypatch.setattr(cfg, "MCP_HTTP_SECRET", "se03-test-secret")
    monkeypatch.setattr(cfg, "API_KEY", "disabled")
    monkeypatch.setattr(cfg, "AUTO_UPDATE_CHECK", False)
    monkeypatch.setattr(main_mod.mcp, "_session_manager", None)


def test_combined_app_lifespan_starts_mcp_session_manager(
    monkeypatch: pytest.MonkeyPatch, _combined_env: None
) -> None:
    """SE-03 regression: the combined app delegates its lifespan to the MCP
    sub-app, so the StreamableHTTP session manager is running and a real MCP
    initialize request on /mcp succeeds (previously: 'Task group is not
    initialized' / 500 on every /mcp request, and the app lifespan that opens
    the DB and starts sync never ran)."""
    from starlette.testclient import TestClient

    app, secret = main_mod._build_combined_app()
    # TestClient as context manager runs the lifespan; base_url must be a
    # localhost host to satisfy the SDK's DNS-rebinding protection.
    with TestClient(app, base_url="http://127.0.0.1:5199") as client:
        r = client.post(
            "/mcp",
            json=_MCP_INITIALIZE,
            headers={**_MCP_HEADERS, "Authorization": f"Bearer {secret}"},
        )
        assert r.status_code == 200, r.text
        assert "protocolVersion" in r.text

        # The dashboard app is still mounted and served alongside /mcp
        assert client.get("/health").status_code == 200
        assert client.get("/").status_code == 200


def test_combined_app_serves_mcp_at_exact_mcp_path(
    monkeypatch: pytest.MonkeyPatch, _combined_env: None
) -> None:
    """SE-03: the MCP endpoint lives at /mcp exactly (not /mcp/mcp as the old
    nested-mount layout produced)."""
    app, _secret = main_mod._build_combined_app()
    route_paths = {route.path for route in app.routes}
    assert "/mcp" in route_paths
    assert "/mcp/mcp" not in route_paths


def test_combined_app_accepts_valid_mcp_token_with_lifespan(
    monkeypatch: pytest.MonkeyPatch, _combined_env: None
) -> None:
    """SE-03/SE-05: with MCP_HTTP_SECRET set, the shared bearer middleware
    admits the correct token and the request reaches a live session manager."""
    from starlette.testclient import TestClient

    import remind_me_mcp.config as cfg

    monkeypatch.setattr(cfg, "MCP_HTTP_SECRET", "s3cret")

    app, secret = main_mod._build_combined_app()
    assert secret == "s3cret"
    with TestClient(app, base_url="http://127.0.0.1:5199", raise_server_exceptions=False) as client:
        # Wrong/missing token -> 401 from the shared middleware
        assert client.post("/mcp", json=_MCP_INITIALIZE, headers=_MCP_HEADERS).status_code == 401

        # Correct token -> full MCP initialize round-trip
        r = client.post(
            "/mcp",
            json=_MCP_INITIALIZE,
            headers={**_MCP_HEADERS, "Authorization": "Bearer s3cret"},
        )
        assert r.status_code == 200, r.text

        # Dashboard routes are not gated by the MCP secret
        assert client.get("/health").status_code == 200
