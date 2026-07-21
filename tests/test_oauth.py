"""
Behavior tests for the single-user OAuth 2.1 authorization server (FT-07).

Covers the metadata documents (RFC 8414 / RFC 9728), dynamic client
registration (RFC 7591), the full PKCE authorization-code flow end-to-end
(authorize → owner-token consent → code → token exchange → authenticated MCP
round-trip), denial paths (wrong owner credential, PKCE mismatch, single-use
codes), refresh rotation, deterministic access-token expiry (frozen clock),
RFC 7009 revocation, per-client revocation via the remind_me_revoke_clients
tool, legacy secret-path/bearer coexistence, the issuer-unset fallback, and
state-file hygiene. All via the ASGI TestClient — no real network listeners.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import stat
import sys
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import pytest

if TYPE_CHECKING:
    from pathlib import Path

import remind_me_mcp.__main__ as main_mod
import remind_me_mcp.config as cfg
import remind_me_mcp.oauth as oauth_mod
from remind_me_mcp.oauth import OAuthStateStore
from remind_me_mcp.remote import build_remote_app

_OWNER = "owner-connector-token-ft07"
_ISSUER = "https://machine.tailnet.ts.net"
_REDIRECT = "https://claude.ai/api/mcp/auth_callback"

_MCP_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "ft07-test", "version": "0"},
    },
}
_MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


# ---------------------------------------------------------------------------
# Fixtures + flow helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def oauth_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """TestClient against the OAuth-enabled remote app with its lifespan running.

    Mirrors the FT-05 remote_client fixture (reset session manager, disable
    the startup git-fetch) and points MEMORY_DIR at a fresh tmp dir so the
    OAuth state file is per-test.
    """
    from starlette.testclient import TestClient

    monkeypatch.setattr(cfg, "AUTO_UPDATE_CHECK", False)
    monkeypatch.setattr(cfg, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(main_mod.mcp, "_session_manager", None)

    app = build_remote_app(_OWNER, issuer=_ISSUER)
    with TestClient(app, base_url=_ISSUER, raise_server_exceptions=False) as client:
        yield client


def _pkce_pair() -> tuple[str, str]:
    """Return a (code_verifier, S256 code_challenge) pair."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _register(client: Any, name: str = "claude.ai") -> dict[str, Any]:
    """Dynamically register a client (RFC 7591) and return its record."""
    r = client.post(
        "/register",
        json={
            "client_name": name,
            "redirect_uris": [_REDIRECT],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _authorize(
    client: Any,
    info: dict[str, Any],
    challenge: str,
    owner_token: str = _OWNER,
    action: str = "approve",
    state: str = "st4te",
) -> Any:
    """Drive /authorize → /consent and return the final redirect response."""
    r = client.get(
        "/authorize",
        params={
            "client_id": info["client_id"],
            "redirect_uri": _REDIRECT,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        },
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    consent_url = r.headers["location"]
    assert consent_url.startswith("/consent?txn=")

    page = client.get(consent_url)
    assert page.status_code == 200, page.text
    txn = parse_qs(urlparse(consent_url).query)["txn"][0]

    return client.post(
        "/consent",
        data={"txn": txn, "owner_token": owner_token, "action": action},
        follow_redirects=False,
    )


def _approve_for_code(client: Any, info: dict[str, Any], challenge: str) -> str:
    """Run the consent flow and extract the authorization code."""
    r = _authorize(client, info, challenge)
    assert r.status_code == 302, r.text
    query = parse_qs(urlparse(r.headers["location"]).query)
    assert query["state"] == ["st4te"]
    return query["code"][0]


def _exchange_code(client: Any, info: dict[str, Any], code: str, verifier: str) -> Any:
    """POST the authorization-code grant to /token.

    No client_secret: registration always forces token_endpoint_auth_method
    "none" (SEC-08) regardless of what the client requests, so a real
    client is never issued one to send here.
    """
    return client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _REDIRECT,
            "client_id": info["client_id"],
            "code_verifier": verifier,
        },
    )


def _obtain_tokens(client: Any, info: dict[str, Any]) -> dict[str, Any]:
    """Full register-less PKCE flow for an already-registered client → token payload."""
    verifier, challenge = _pkce_pair()
    code = _approve_for_code(client, info, challenge)
    r = _exchange_code(client, info, code, verifier)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Metadata documents
# ---------------------------------------------------------------------------


def test_as_metadata_served(oauth_client) -> None:
    """RFC 8414 metadata points every endpoint at the configured issuer."""
    r = oauth_client.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200, r.text
    meta = r.json()
    assert meta["issuer"].rstrip("/") == _ISSUER
    assert meta["authorization_endpoint"] == f"{_ISSUER}/authorize"
    assert meta["token_endpoint"] == f"{_ISSUER}/token"
    assert meta["registration_endpoint"] == f"{_ISSUER}/register"
    assert meta["revocation_endpoint"] == f"{_ISSUER}/revoke"
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert "authorization_code" in meta["grant_types_supported"]
    assert "refresh_token" in meta["grant_types_supported"]


def test_protected_resource_metadata_served(oauth_client) -> None:
    """RFC 9728 metadata names the MCP endpoint and the issuer, at both well-known paths."""
    for path in (
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-protected-resource",
    ):
        r = oauth_client.get(path)
        assert r.status_code == 200, (path, r.text)
        meta = r.json()
        assert meta["resource"] == f"{_ISSUER}/mcp"
        assert [u.rstrip("/") for u in meta["authorization_servers"]] == [_ISSUER]


def test_mcp_401_advertises_resource_metadata(oauth_client) -> None:
    """An unauthenticated /mcp hit returns 401 with the discovery hint."""
    r = oauth_client.post("/mcp", json=_MCP_INITIALIZE, headers=_MCP_HEADERS)
    assert r.status_code == 401
    www = r.headers.get("www-authenticate", "")
    assert "resource_metadata=" in www
    assert "/.well-known/oauth-protected-resource/mcp" in www


# ---------------------------------------------------------------------------
# Dynamic client registration (RFC 7591)
# ---------------------------------------------------------------------------


def test_dcr_registers_client(oauth_client, tmp_path: Path) -> None:
    """POST /register issues a client_id and persists the client."""
    info = _register(oauth_client)
    assert info["client_id"]
    assert info["client_name"] == "claude.ai"

    store = OAuthStateStore(tmp_path / "oauth.json")
    clients = store.list_clients()
    assert [c["client_id"] for c in clients] == [info["client_id"]]


def test_dcr_forces_no_client_secret(oauth_client, tmp_path: Path) -> None:
    """SEC-08: registration always forces token_endpoint_auth_method="none"
    and issues no client_secret, even when the client explicitly requests
    client_secret_post -- there's no secret to leak in plaintext at rest
    (unlike the SDK's default behavior, which would auto-generate one and
    store it verbatim), and PKCE already provides proof of possession."""
    info = _register(oauth_client)
    assert info["token_endpoint_auth_method"] == "none"
    # exclude_none=True on the registration response means an unset
    # client_secret is omitted entirely, not present-with-null.
    assert "client_secret" not in info
    assert "client_secret_expires_at" not in info

    # Nothing sensitive lands in the state file either.
    raw = json.loads((tmp_path / "oauth.json").read_text())
    persisted = raw["clients"][info["client_id"]]
    assert persisted.get("client_secret") is None
    assert persisted["token_endpoint_auth_method"] == "none"


def test_token_exchange_ignores_a_stray_client_secret(oauth_client) -> None:
    """Since no client_secret is ever issued, /token doesn't check one --
    a client mistakenly sending one is simply ignored, not rejected."""
    info = _register(oauth_client)
    verifier, challenge = _pkce_pair()
    code = _approve_for_code(oauth_client, info, challenge)

    r = oauth_client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _REDIRECT,
            "client_id": info["client_id"],
            "client_secret": "some-value-that-was-never-issued",
            "code_verifier": verifier,
        },
    )
    assert r.status_code == 200, r.text


def test_state_file_permissions(oauth_client, tmp_path: Path) -> None:
    """The OAuth state file is created with 0600 perms (SE-01 conventions)."""
    _register(oauth_client)
    state_file = tmp_path / "oauth.json"
    assert state_file.is_file()
    if sys.platform != "win32":
        # POSIX mode bits aren't meaningful on Windows: os.chmod() there only
        # toggles the read-only DOS attribute, so a real per-owner 0600 isn't
        # achievable without ACLs. The chmod call itself still runs on every
        # platform (best-effort); this assertion just can't verify it there.
        assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
    # And it never stores raw tokens — only hashes (spot-check after a flow).
    info = _register(oauth_client)
    tokens = _obtain_tokens(oauth_client, info)
    raw = state_file.read_text(encoding="utf-8")
    assert tokens["access_token"] not in raw
    assert tokens["refresh_token"] not in raw


# ---------------------------------------------------------------------------
# PKCE authorization-code flow
# ---------------------------------------------------------------------------


def test_full_pkce_flow_end_to_end(oauth_client) -> None:
    """register → authorize → consent → code → token → authenticated MCP session."""
    info = _register(oauth_client)
    verifier, challenge = _pkce_pair()

    code = _approve_for_code(oauth_client, info, challenge)
    r = _exchange_code(oauth_client, info, code, verifier)
    assert r.status_code == 200, r.text
    tokens = r.json()
    assert tokens["token_type"] == "Bearer"
    assert tokens["expires_in"] == oauth_mod.ACCESS_TOKEN_TTL
    assert tokens["refresh_token"]

    headers = {**_MCP_HEADERS, "Authorization": f"Bearer {tokens['access_token']}"}
    r = oauth_client.post("/mcp", json=_MCP_INITIALIZE, headers=headers)
    assert r.status_code == 200, r.text
    assert "protocolVersion" in r.text
    session_id = r.headers.get("mcp-session-id")
    assert session_id

    headers["mcp-session-id"] = session_id
    r = oauth_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=headers,
    )
    assert r.status_code in (200, 202), r.text
    r = oauth_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert "remind_me_revoke_clients" in r.text


def test_consent_page_shows_client_and_requires_valid_txn(oauth_client) -> None:
    """GET /consent renders the form for a live txn and 400s an unknown one."""
    info = _register(oauth_client, name="My Claude")
    _verifier, challenge = _pkce_pair()
    r = oauth_client.get(
        "/authorize",
        params={
            "client_id": info["client_id"],
            "redirect_uri": _REDIRECT,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    page = oauth_client.get(r.headers["location"])
    assert page.status_code == 200
    assert "My Claude" in page.text
    assert _REDIRECT in page.text

    assert oauth_client.get("/consent?txn=nope").status_code == 400
    assert oauth_client.post("/consent", data={"txn": "nope", "owner_token": _OWNER, "action": "approve"}).status_code == 400


def test_wrong_owner_credential_denied(oauth_client) -> None:
    """A wrong owner token auto-denies — access_denied redirect, no code, txn consumed."""
    info = _register(oauth_client)
    _verifier, challenge = _pkce_pair()

    r = _authorize(oauth_client, info, challenge, owner_token="not-the-owner-token")
    assert r.status_code == 302
    query = parse_qs(urlparse(r.headers["location"]).query)
    assert query["error"] == ["access_denied"]
    assert query["state"] == ["st4te"]
    assert "code" not in query


def test_explicit_deny_same_as_bad_credential(oauth_client) -> None:
    """Deny with the RIGHT credential produces the identical access_denied redirect."""
    info = _register(oauth_client)
    _verifier, challenge = _pkce_pair()

    r = _authorize(oauth_client, info, challenge, action="deny")
    assert r.status_code == 302
    query = parse_qs(urlparse(r.headers["location"]).query)
    assert query["error"] == ["access_denied"]
    assert "code" not in query


def test_pkce_mismatch_rejected(oauth_client) -> None:
    """A code_verifier that does not match the challenge is invalid_grant."""
    info = _register(oauth_client)
    _verifier, challenge = _pkce_pair()
    code = _approve_for_code(oauth_client, info, challenge)

    wrong_verifier, _ = _pkce_pair()
    r = _exchange_code(oauth_client, info, code, wrong_verifier)
    assert r.status_code == 400, r.text
    assert r.json()["error"] == "invalid_grant"


def test_authorization_code_single_use(oauth_client) -> None:
    """A consumed authorization code cannot be exchanged twice."""
    info = _register(oauth_client)
    verifier, challenge = _pkce_pair()
    code = _approve_for_code(oauth_client, info, challenge)

    assert _exchange_code(oauth_client, info, code, verifier).status_code == 200
    replay = _exchange_code(oauth_client, info, code, verifier)
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"


# ---------------------------------------------------------------------------
# Refresh grant + expiry
# ---------------------------------------------------------------------------


def test_refresh_grant_rotates(oauth_client) -> None:
    """The refresh grant issues a new pair and retires the old refresh token."""
    info = _register(oauth_client)
    tokens = _obtain_tokens(oauth_client, info)

    r = oauth_client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": info["client_id"],
        },
    )
    assert r.status_code == 200, r.text
    rotated = r.json()
    assert rotated["access_token"] != tokens["access_token"]
    assert rotated["refresh_token"] != tokens["refresh_token"]

    # New access token works on /mcp.
    r = oauth_client.post(
        "/mcp",
        json=_MCP_INITIALIZE,
        headers={**_MCP_HEADERS, "Authorization": f"Bearer {rotated['access_token']}"},
    )
    assert r.status_code == 200, r.text

    # Old refresh token is dead (rotation).
    replay = oauth_client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": info["client_id"],
        },
    )
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"


def test_expired_access_token_rejected(oauth_client, monkeypatch: pytest.MonkeyPatch) -> None:
    """An access token past ACCESS_TOKEN_TTL stops authenticating (frozen clock)."""
    info = _register(oauth_client)

    clock = {"now": time.time()}
    monkeypatch.setattr(oauth_mod, "_now", lambda: clock["now"])

    tokens = _obtain_tokens(oauth_client, info)
    headers = {**_MCP_HEADERS, "Authorization": f"Bearer {tokens['access_token']}"}
    assert oauth_client.post("/mcp", json=_MCP_INITIALIZE, headers=headers).status_code == 200

    clock["now"] += oauth_mod.ACCESS_TOKEN_TTL + 1
    assert oauth_client.post("/mcp", json=_MCP_INITIALIZE, headers=headers).status_code == 401


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


def test_revocation_endpoint_kills_access_and_refresh(oauth_client) -> None:
    """RFC 7009 /revoke with either token kills the client's whole session.

    client_secret="" (empty, not omitted): the installed SDK's
    RevocationRequest model declares client_secret as str | None with no
    default, so pydantic requires the *key* to be present even for a "none"
    auth-method client -- the value itself is never checked (client.client_secret
    is falsy). This is an upstream SDK quirk, not something remind_me_mcp
    controls; the actually-relied-upon revocation path in this codebase is
    the remind_me_revoke_clients tool, which operates on OAuthStateStore
    directly and never goes through this HTTP endpoint at all.
    """
    info = _register(oauth_client)
    tokens = _obtain_tokens(oauth_client, info)

    r = oauth_client.post(
        "/revoke",
        data={
            "token": tokens["refresh_token"],
            "client_id": info["client_id"],
            "client_secret": "",
        },
    )
    assert r.status_code == 200, r.text

    # Access token no longer authenticates.
    r = oauth_client.post(
        "/mcp",
        json=_MCP_INITIALIZE,
        headers={**_MCP_HEADERS, "Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert r.status_code == 401
    # Refresh token no longer grants.
    r = oauth_client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": info["client_id"],
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


async def test_revoke_clients_tool_lists_and_revokes(
    oauth_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """remind_me_revoke_clients lists registered clients and revokes by client_id.

    The tool talks to the same state file the live server re-reads, so the
    revoked client's access token dies immediately (the cross-process story).
    """
    from remind_me_mcp.tools.admin import remind_me_revoke_clients

    info = _register(oauth_client)
    tokens = _obtain_tokens(oauth_client, info)

    listing = json.loads(await remind_me_revoke_clients())
    assert [c["client_id"] for c in listing["clients"]] == [info["client_id"]]
    assert listing["clients"][0]["access_tokens"] == 1
    assert listing["clients"][0]["refresh_tokens"] == 1

    revoked = json.loads(await remind_me_revoke_clients(client_id=info["client_id"]))
    assert revoked["status"] == "revoked"
    assert revoked["access_tokens"] == 1
    assert revoked["refresh_tokens"] == 1

    # Live server rejects the revoked client's token without a restart.
    r = oauth_client.post(
        "/mcp",
        json=_MCP_INITIALIZE,
        headers={**_MCP_HEADERS, "Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert r.status_code == 401
    assert json.loads(await remind_me_revoke_clients())["clients"] == []

    unknown = json.loads(await remind_me_revoke_clients(client_id="no-such-client"))
    assert unknown["status"] == "error"


# ---------------------------------------------------------------------------
# Legacy coexistence (FT-05 stays working in OAuth mode)
# ---------------------------------------------------------------------------


def test_legacy_secret_path_still_works(oauth_client) -> None:
    """/mcp/<connector-token> completes an MCP initialize in OAuth mode."""
    r = oauth_client.post(f"/mcp/{_OWNER}", json=_MCP_INITIALIZE, headers=_MCP_HEADERS)
    assert r.status_code == 200, r.text
    assert "protocolVersion" in r.text


def test_legacy_bearer_still_works(oauth_client) -> None:
    """Authorization: Bearer <connector-token> on /mcp still authenticates."""
    r = oauth_client.post(
        "/mcp",
        json=_MCP_INITIALIZE,
        headers={**_MCP_HEADERS, "Authorization": f"Bearer {_OWNER}"},
    )
    assert r.status_code == 200, r.text


def test_wrong_secret_path_and_bearer_rejected(oauth_client) -> None:
    """Probes keep failing closed: bad path 404, bad bearer 401, others 404."""
    assert oauth_client.post("/mcp/wrong", json=_MCP_INITIALIZE, headers=_MCP_HEADERS).status_code == 404
    assert (
        oauth_client.post(
            "/mcp", json=_MCP_INITIALIZE, headers={**_MCP_HEADERS, "Authorization": "Bearer nope"}
        ).status_code
        == 401
    )
    assert oauth_client.get("/api/stats").status_code == 404
    assert oauth_client.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# Issuer handling
# ---------------------------------------------------------------------------


def test_oauth_inactive_without_issuer(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No issuer → FT-05 app: a warning is logged and no OAuth routes exist."""
    monkeypatch.setattr(main_mod.mcp, "_session_manager", None)
    with caplog.at_level("WARNING", logger="remind_me_mcp.remote"):
        app = build_remote_app(_OWNER)
    assert "REMIND_ME_REMOTE_ISSUER" in caplog.text

    route_paths = {route.path for route in app.routes}
    assert "/authorize" not in route_paths
    assert "/token" not in route_paths
    assert "/register" not in route_paths
    assert "/consent" not in route_paths


def test_issuer_must_be_an_origin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An issuer with a path (or plain http) is rejected at build time."""
    monkeypatch.setattr(cfg, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(main_mod.mcp, "_session_manager", None)

    with pytest.raises(ValueError):
        build_remote_app(_OWNER, issuer="https://machine.example/path")
    with pytest.raises(ValueError):
        build_remote_app(_OWNER, issuer="http://machine.example")


# ---------------------------------------------------------------------------
# State store unit behavior
# ---------------------------------------------------------------------------


def test_store_tolerates_missing_and_corrupt_files(tmp_path: Path) -> None:
    """A missing or corrupt state file reads as empty instead of raising."""
    store = OAuthStateStore(tmp_path / "oauth.json")
    assert store.list_clients() == []
    assert store.get_client("x") is None
    assert store.revoke_client("x") is None

    (tmp_path / "oauth.json").write_text("{not json", encoding="utf-8")
    assert store.list_clients() == []


def test_store_delete_tokens_for_client(tmp_path: Path) -> None:
    """delete_tokens_for_client drops only the targeted client's tokens."""
    store = OAuthStateStore(tmp_path / "oauth.json")
    store.put_token("access_tokens", "tok-a", {"client_id": "c1"})
    store.put_token("refresh_tokens", "tok-r", {"client_id": "c1"})
    store.put_token("access_tokens", "tok-b", {"client_id": "c2"})

    counts = store.delete_tokens_for_client("c1")
    assert counts == {"access_tokens": 1, "refresh_tokens": 1}
    assert store.get_token("access_tokens", "tok-a") is None
    assert store.get_token("access_tokens", "tok-b") == {"client_id": "c2"}
