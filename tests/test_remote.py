"""
Behavior tests for the remote MCP connector (FT-05).

Covers connector-token generation/persistence/rotation, the secret-path +
bearer auth gate (SecretPathMiddleware), a full MCP initialize round-trip
over the Streamable HTTP transport via the ASGI TestClient (no real network
listener), CLI dispatch (--serve-remote, disabled by default), and the
remote-status reporting surfaced through remind_me_server_status.
"""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

import remind_me_mcp.__main__ as main_mod
import remind_me_mcp.config as cfg
from remind_me_mcp.remote import (
    SecretPathMiddleware,
    build_remote_app,
    get_remote_status,
    redact_token,
)

# ---------------------------------------------------------------------------
# Connector token resolution (config.resolve_connector_token)
# ---------------------------------------------------------------------------


@pytest.fixture()
def token_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point MEMORY_DIR at a fresh per-test directory with no env override."""
    monkeypatch.setattr(cfg, "REMOTE_MCP_TOKEN", None)
    monkeypatch.setattr(cfg, "MEMORY_DIR", tmp_path)
    return tmp_path


def test_token_generated_and_persisted(token_dir: Path) -> None:
    """First use generates a high-entropy token and persists it with 0600 perms."""
    token = cfg.resolve_connector_token()

    token_file = token_dir / "connector_token"
    assert token_file.is_file()
    assert token_file.read_text(encoding="utf-8").strip() == token
    assert len(token) >= 32
    mode = stat.S_IMODE(token_file.stat().st_mode)
    assert mode == 0o600


def test_token_reused_across_calls(token_dir: Path) -> None:
    """Subsequent resolutions return the persisted token, not a fresh one."""
    first = cfg.resolve_connector_token()
    second = cfg.resolve_connector_token()
    assert first == second


def test_token_rotation_by_deleting_file(token_dir: Path) -> None:
    """Deleting the token file rotates the credential on next resolution."""
    first = cfg.resolve_connector_token()
    (token_dir / "connector_token").unlink()
    second = cfg.resolve_connector_token()
    assert first != second
    assert (token_dir / "connector_token").read_text(encoding="utf-8").strip() == second


def test_env_var_token_wins(token_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """REMIND_ME_REMOTE_TOKEN overrides the persisted token and writes no file."""
    monkeypatch.setattr(cfg, "REMOTE_MCP_TOKEN", "  env-token  ")
    assert cfg.resolve_connector_token() == "env-token"
    assert not (token_dir / "connector_token").exists()


def test_ephemeral_token_when_unwritable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An unwritable MEMORY_DIR yields an ephemeral token — never an open endpoint."""
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("file, not a directory")
    monkeypatch.setattr(cfg, "REMOTE_MCP_TOKEN", None)
    monkeypatch.setattr(cfg, "MEMORY_DIR", blocker)

    with caplog.at_level("WARNING", logger="remind_me_mcp.config"):
        token = cfg.resolve_connector_token()

    assert token
    assert "ephemeral" in caplog.text


def test_first_generation_logs_full_token_then_redacts(
    token_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The full token is logged exactly once — at generation time."""
    with caplog.at_level("INFO", logger="remind_me_mcp.config"):
        token = cfg.resolve_connector_token()
    assert token in caplog.text

    caplog.clear()
    with caplog.at_level("INFO", logger="remind_me_mcp.config"):
        cfg.resolve_connector_token()
    assert token not in caplog.text


def test_redact_token_preview() -> None:
    """redact_token keeps only a short prefix of the secret."""
    assert redact_token("abcdefghij") == "abcd…"
    assert "abcdefghij"[4:] not in redact_token("abcdefghij")
    assert redact_token("ab") == "…"


# ---------------------------------------------------------------------------
# Remote app: secret path + bearer auth + MCP round-trip
# ---------------------------------------------------------------------------

_MCP_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "ft05-test", "version": "0"},
    },
}
_MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
_TOKEN = "test-connector-token-ft05"


@pytest.fixture()
def remote_client(monkeypatch: pytest.MonkeyPatch):
    """TestClient against the remote app with its lifespan running.

    Resets the global FastMCP session manager (its .run() is once-only per
    instance) and disables the startup git-fetch (SE-06). The base_url is a
    NON-localhost host: behind a tunnel the public hostname is arbitrary, so
    this also pins the DNS-rebinding-protection opt-out.
    """
    from starlette.testclient import TestClient

    monkeypatch.setattr(cfg, "AUTO_UPDATE_CHECK", False)
    monkeypatch.setattr(main_mod.mcp, "_session_manager", None)

    app = build_remote_app(_TOKEN)
    with TestClient(app, base_url="https://machine.tailnet.ts.net", raise_server_exceptions=False) as client:
        yield client


def test_secret_path_accepts_mcp_initialize(remote_client) -> None:
    """POST /mcp/<token> completes a real MCP initialize round-trip."""
    r = remote_client.post(f"/mcp/{_TOKEN}", json=_MCP_INITIALIZE, headers=_MCP_HEADERS)
    assert r.status_code == 200, r.text
    assert "protocolVersion" in r.text


def test_secret_path_trailing_slash_accepted(remote_client) -> None:
    """claude.ai may normalise the URL with a trailing slash — still served."""
    r = remote_client.post(f"/mcp/{_TOKEN}/", json=_MCP_INITIALIZE, headers=_MCP_HEADERS)
    assert r.status_code == 200, r.text


def test_secret_path_initialize_and_list_tools(remote_client) -> None:
    """A full initialize → initialized → tools/list session over the secret path."""
    r = remote_client.post(f"/mcp/{_TOKEN}", json=_MCP_INITIALIZE, headers=_MCP_HEADERS)
    assert r.status_code == 200, r.text
    session_id = r.headers.get("mcp-session-id")
    assert session_id, "server must assign a streamable-http session id"

    headers = {**_MCP_HEADERS, "mcp-session-id": session_id}
    r = remote_client.post(
        f"/mcp/{_TOKEN}",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=headers,
    )
    assert r.status_code in (200, 202), r.text

    r = remote_client.post(
        f"/mcp/{_TOKEN}",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert "remind_me_server_status" in r.text


def test_wrong_token_rejected_404(remote_client) -> None:
    """A wrong or truncated token in the path is a 404 — no oracle, no MCP."""
    assert remote_client.post("/mcp/wrong-token", json=_MCP_INITIALIZE, headers=_MCP_HEADERS).status_code == 404
    assert remote_client.post(f"/mcp/{_TOKEN[:-1]}", json=_MCP_INITIALIZE, headers=_MCP_HEADERS).status_code == 404
    assert remote_client.post("/mcp/", json=_MCP_INITIALIZE, headers=_MCP_HEADERS).status_code == 404


def test_bare_mcp_requires_bearer(remote_client) -> None:
    """/mcp without the secret path needs Authorization: Bearer <token>."""
    assert remote_client.post("/mcp", json=_MCP_INITIALIZE, headers=_MCP_HEADERS).status_code == 401
    assert (
        remote_client.post(
            "/mcp", json=_MCP_INITIALIZE, headers={**_MCP_HEADERS, "Authorization": "Bearer nope"}
        ).status_code
        == 401
    )

    r = remote_client.post(
        "/mcp",
        json=_MCP_INITIALIZE,
        headers={**_MCP_HEADERS, "Authorization": f"Bearer {_TOKEN}"},
    )
    assert r.status_code == 200, r.text


def test_health_unauthenticated(remote_client) -> None:
    """/health passes without any credential (SE-04 parity) and leaks nothing."""
    r = remote_client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_unrelated_paths_rejected(remote_client) -> None:
    """Anything outside /health and the MCP endpoint is a 404."""
    assert remote_client.get("/").status_code == 404
    assert remote_client.get("/api/stats").status_code == 404
    assert remote_client.get(f"/api/{_TOKEN}").status_code == 404


def test_middleware_passes_non_http_scopes_through() -> None:
    """Lifespan scopes bypass the gate so the app lifespan always runs."""
    import asyncio

    seen: list[str] = []

    async def inner(scope, receive, send) -> None:
        seen.append(scope["type"])

    mw = SecretPathMiddleware(inner, token=_TOKEN)
    asyncio.run(mw({"type": "lifespan"}, None, None))  # type: ignore[arg-type]
    assert seen == ["lifespan"]


# ---------------------------------------------------------------------------
# CLI dispatch (--serve-remote; disabled by default)
# ---------------------------------------------------------------------------


def _run_main(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    """Invoke main() with the given CLI arguments."""
    monkeypatch.setattr("sys.argv", ["remind-me-mcp", *argv])
    main_mod.main()


def test_serve_remote_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    """--serve-remote routes to _run_remote with the parsed host/port."""
    import uvicorn

    calls: list[Any] = []
    monkeypatch.setattr(main_mod, "_run_remote", lambda args: calls.append(args))
    monkeypatch.setattr(main_mod.mcp, "run", lambda *a, **kw: pytest.fail("stdio branch must not run"))
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: pytest.fail("uvicorn must not run here"))

    _run_main(monkeypatch, "--serve-remote", "--remote-host", "0.0.0.0", "--remote-port", "9009")

    assert len(calls) == 1
    assert calls[0].remote_host == "0.0.0.0"
    assert calls[0].remote_port == 9009


def test_serve_remote_takes_precedence_over_other_serve_flags(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """--serve-remote is standalone: combined/UI branches are skipped with a warning."""
    calls: list[Any] = []
    monkeypatch.setattr(main_mod, "_run_remote", lambda args: calls.append(args))
    monkeypatch.setattr(main_mod, "_run_combined", lambda args: pytest.fail("combined branch must not run"))

    with caplog.at_level("WARNING", logger="remind_me_mcp.__main__"):
        _run_main(monkeypatch, "--serve-remote", "--serve-ui", "--serve-mcp")

    assert len(calls) == 1
    assert "standalone" in caplog.text


def test_remote_mode_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no flags and no env, main() serves stdio — never the remote connector.

    REMOTE_MCP comes from the environment at import time; this asserts both
    the parsed config default and the dispatch behavior.
    """
    assert main_mod.REMOTE_MCP is False

    ran: list[str] = []
    monkeypatch.setattr(main_mod, "_run_remote", lambda args: pytest.fail("remote branch must not run"))
    monkeypatch.setattr(main_mod.mcp, "run", lambda *a, **kw: ran.append("stdio"))
    monkeypatch.setattr(main_mod, "_read_pid_file", lambda: None)

    _run_main(monkeypatch)

    assert ran == ["stdio"]


def test_run_remote_serves_built_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_run_remote resolves the token, builds the app, and hands it to uvicorn."""
    import argparse

    import uvicorn

    uvicorn_calls: list[dict] = []
    monkeypatch.setattr(cfg, "REMOTE_MCP_TOKEN", "fixed-token")
    monkeypatch.setattr(cfg, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(main_mod.mcp, "_session_manager", None)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: uvicorn_calls.append({"app": app, **kw}))

    main_mod._run_remote(argparse.Namespace(remote_host="127.0.0.1", remote_port=8768))

    assert len(uvicorn_calls) == 1
    assert uvicorn_calls[0]["host"] == "127.0.0.1"
    assert uvicorn_calls[0]["port"] == 8768
    route_paths = {route.path for route in uvicorn_calls[0]["app"].routes}
    assert "/mcp" in route_paths
    assert "/health" in route_paths


def test_run_remote_logs_redacted_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The startup log shows the endpoint with the token redacted, never in full."""
    import argparse

    import uvicorn

    monkeypatch.setattr(cfg, "REMOTE_MCP_TOKEN", "super-secret-connector-token")
    monkeypatch.setattr(cfg, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(main_mod.mcp, "_session_manager", None)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)

    with caplog.at_level("INFO", logger="remind_me_mcp.__main__"):
        main_mod._run_remote(argparse.Namespace(remote_host="127.0.0.1", remote_port=8768))

    assert "/mcp/supe…" in caplog.text
    assert "super-secret-connector-token" not in caplog.text


# ---------------------------------------------------------------------------
# Status reporting
# ---------------------------------------------------------------------------


def test_get_remote_status_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Default config reports the connector disabled with no token on disk."""
    monkeypatch.setattr(cfg, "REMOTE_MCP", False)
    monkeypatch.setattr(cfg, "REMOTE_MCP_TOKEN", None)
    monkeypatch.setattr(cfg, "REMOTE_MCP_ISSUER", None)
    monkeypatch.setattr(cfg, "MEMORY_DIR", tmp_path)

    status = get_remote_status()
    assert status["enabled"] is False
    assert status["token_configured"] is False
    assert status["token_file"] == str(tmp_path / "connector_token")
    assert status["oauth_enabled"] is False
    assert status["oauth_clients"] == 0


def test_get_remote_status_enabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Enabled config reports host/port and detects a persisted token."""
    (tmp_path / "connector_token").write_text("tok\n")
    monkeypatch.setattr(cfg, "REMOTE_MCP", True)
    monkeypatch.setattr(cfg, "REMOTE_MCP_TOKEN", None)
    monkeypatch.setattr(cfg, "REMOTE_MCP_ISSUER", "https://machine.tailnet.ts.net")
    monkeypatch.setattr(cfg, "REMOTE_MCP_HOST", "0.0.0.0")
    monkeypatch.setattr(cfg, "REMOTE_MCP_PORT", 9999)
    monkeypatch.setattr(cfg, "MEMORY_DIR", tmp_path)

    status = get_remote_status()
    assert status == {
        "enabled": True,
        "host": "0.0.0.0",
        "port": 9999,
        "token_file": str(tmp_path / "connector_token"),
        "token_configured": True,
        "oauth_enabled": True,
        "issuer": "https://machine.tailnet.ts.net",
        "oauth_state_file": str(tmp_path / "oauth.json"),
        "oauth_clients": 0,
    }


async def test_server_status_reports_remote_connector(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """remind_me_server_status includes the remote-MCP connector section."""
    import remind_me_mcp.tools as _tools_mod
    from remind_me_mcp.tools.admin import remind_me_server_status

    monkeypatch.setattr(
        _tools_mod,
        "get_server_status",
        lambda: {
            "ui_server": "stopped",
            "ui_url": None,
            "ui_pid": None,
            "ui_started": None,
            "db_path": "/tmp/test/memory.db",
            "db_exists": True,
        },
    )
    monkeypatch.setattr(cfg, "REMOTE_MCP", False)
    monkeypatch.setattr(cfg, "REMOTE_MCP_TOKEN", None)
    monkeypatch.setattr(cfg, "MEMORY_DIR", tmp_path)

    result = await remind_me_server_status()
    assert "Remind Me Server Status" in result
    assert "Remote MCP connector" in result
    assert "REMIND_ME_REMOTE_MCP" in result

    # Enabled config flips the section to the connector endpoint.
    monkeypatch.setattr(cfg, "REMOTE_MCP", True)
    result = await remind_me_server_status()
    assert "Remote MCP connector:** ✓ Enabled" in result
    assert "/mcp/<token>" in result
