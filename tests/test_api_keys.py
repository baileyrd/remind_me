"""
Tests for remind_me_mcp.api_keys (issue #185): named, scope-limited dashboard
API keys layered on top of the single default REMIND_ME_API_KEY.

Covers:
  - ApiKeyStore unit behavior: create/list/revoke/verify, hash-at-rest,
    reserved-name and duplicate-name guards, file permissions.
  - BearerAuthMiddleware scope enforcement (api.py): a 'read'-scoped key
    authenticates GET but is rejected 403 on mutating routes; a
    'read-write'-scoped key has full access; the backward-compat default key
    is unaffected and not revocable through this store.
  - The remind_me_api_key MCP tool: create/list/revoke round-trip.
"""

from __future__ import annotations

import inspect
import json
import stat
import sys
from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from remind_me_mcp.api import _build_api_app
from remind_me_mcp.api_keys import DEFAULT_KEY_NAME, SCOPES, ApiKeyStore

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

# ---------------------------------------------------------------------------
# ApiKeyStore — unit tests
# ---------------------------------------------------------------------------


def test_create_key_returns_plaintext_once(tmp_path: Path) -> None:
    """create_key returns a usable, sufficiently-random plaintext key."""
    store = ApiKeyStore(tmp_path / "api_keys.json")
    key = store.create_key("dashboard-viewer", "read")
    assert isinstance(key, str)
    # secrets.token_urlsafe(32) -> 43 chars, matching every other
    # auto-generated credential in this codebase (dashboard key, connector
    # token, ICS token, OAuth tokens).
    assert len(key) >= 32


def test_hash_at_rest_plaintext_never_persisted(tmp_path: Path) -> None:
    """The persisted file must never contain the plaintext key anywhere (SE-01 discipline)."""
    store = ApiKeyStore(tmp_path / "api_keys.json")
    key = store.create_key("dashboard-viewer", "read")

    raw = store.path.read_text(encoding="utf-8")
    assert key not in raw

    data = json.loads(raw)
    assert data["keys"][0]["name"] == "dashboard-viewer"
    assert data["keys"][0]["scope"] == "read"
    assert "key_hash" in data["keys"][0]
    assert data["keys"][0]["key_hash"] != key
    # A SHA-256 hex digest is 64 chars.
    assert len(data["keys"][0]["key_hash"]) == 64


def test_list_keys_never_exposes_hash_or_plaintext(tmp_path: Path) -> None:
    store = ApiKeyStore(tmp_path / "api_keys.json")
    key = store.create_key("dashboard-viewer", "read")

    listed = store.list_keys()
    assert len(listed) == 1
    entry = listed[0]
    assert entry["name"] == "dashboard-viewer"
    assert entry["scope"] == "read"
    assert entry["created_at"]
    assert "key_hash" not in entry
    assert key not in json.dumps(entry)


def test_verify_matches_created_key(tmp_path: Path) -> None:
    store = ApiKeyStore(tmp_path / "api_keys.json")
    key = store.create_key("ci-bot", "read-write")

    record = store.verify(key)
    assert record == {"name": "ci-bot", "scope": "read-write"}


def test_verify_rejects_wrong_key(tmp_path: Path) -> None:
    store = ApiKeyStore(tmp_path / "api_keys.json")
    store.create_key("ci-bot", "read-write")

    assert store.verify("not-the-right-key") is None
    assert store.verify("") is None


def test_verify_rejects_close_but_wrong_key(tmp_path: Path) -> None:
    """A near-miss key (one changed character) must not authenticate."""
    store = ApiKeyStore(tmp_path / "api_keys.json")
    key = store.create_key("ci-bot", "read-write")

    tampered = key[:-1] + ("a" if key[-1] != "a" else "b")
    assert store.verify(tampered) is None


def test_create_duplicate_name_raises(tmp_path: Path) -> None:
    store = ApiKeyStore(tmp_path / "api_keys.json")
    store.create_key("ci-bot", "read")
    with pytest.raises(ValueError, match="already exists"):
        store.create_key("ci-bot", "read-write")


def test_create_invalid_scope_raises(tmp_path: Path) -> None:
    store = ApiKeyStore(tmp_path / "api_keys.json")
    with pytest.raises(ValueError, match="scope must be one of"):
        store.create_key("ci-bot", "admin")


def test_create_empty_name_raises(tmp_path: Path) -> None:
    store = ApiKeyStore(tmp_path / "api_keys.json")
    with pytest.raises(ValueError, match="name is required"):
        store.create_key("   ", "read")


def test_create_reserved_default_name_raises(tmp_path: Path) -> None:
    store = ApiKeyStore(tmp_path / "api_keys.json")
    with pytest.raises(ValueError, match="reserved"):
        store.create_key(DEFAULT_KEY_NAME, "read")


def test_revoke_removes_key_immediately(tmp_path: Path) -> None:
    store = ApiKeyStore(tmp_path / "api_keys.json")
    key = store.create_key("ci-bot", "read")

    assert store.verify(key) is not None
    assert store.revoke_key("ci-bot") is True
    assert store.verify(key) is None
    assert store.list_keys() == []


def test_revoke_unknown_name_returns_false(tmp_path: Path) -> None:
    store = ApiKeyStore(tmp_path / "api_keys.json")
    assert store.revoke_key("no-such-key") is False


def test_revoke_default_name_raises(tmp_path: Path) -> None:
    """The backward-compat default key is config-managed, not app-managed."""
    store = ApiKeyStore(tmp_path / "api_keys.json")
    with pytest.raises(ValueError, match="config-managed"):
        store.revoke_key(DEFAULT_KEY_NAME)


def test_file_permissions_are_0600(tmp_path: Path) -> None:
    store = ApiKeyStore(tmp_path / "api_keys.json")
    store.create_key("ci-bot", "read")

    if sys.platform != "win32":
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_store_tolerates_missing_file(tmp_path: Path) -> None:
    store = ApiKeyStore(tmp_path / "api_keys.json")
    assert store.list_keys() == []
    assert store.verify("anything") is None


def test_store_tolerates_corrupt_file(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "api_keys.json"
    path.write_text("{not json", encoding="utf-8")
    store = ApiKeyStore(path)
    with caplog.at_level("WARNING", logger="remind_me_mcp.api_keys"):
        assert store.list_keys() == []


def test_create_key_propagates_genuine_write_failure(tmp_path: Path) -> None:
    """A write that genuinely fails (e.g. the parent directory vanished) must
    raise, not silently report success for a key that was never persisted."""
    missing_dir = tmp_path / "does-not-exist"
    store = ApiKeyStore(missing_dir / "api_keys.json")
    with pytest.raises(OSError):
        store.create_key("ci-bot", "read")


def test_multiple_keys_independent(tmp_path: Path) -> None:
    store = ApiKeyStore(tmp_path / "api_keys.json")
    read_key = store.create_key("viewer", "read")
    rw_key = store.create_key("automation", "read-write")

    assert store.verify(read_key) == {"name": "viewer", "scope": "read"}
    assert store.verify(rw_key) == {"name": "automation", "scope": "read-write"}

    store.revoke_key("viewer")
    assert store.verify(read_key) is None
    assert store.verify(rw_key) == {"name": "automation", "scope": "read-write"}


def test_scopes_constant_is_read_and_read_write() -> None:
    assert set(SCOPES) == {"read", "read-write"}


def test_module_uses_compare_digest_for_verification() -> None:
    """Source-level check that key comparison is constant-time (SE-05).

    Mirrors test_config.py's source-level check style: a simple correctness
    test cannot itself prove timing-safety, so this asserts the constant-time
    primitive is actually used in the verification path, alongside the
    correctness tests above (test_verify_rejects_close_but_wrong_key etc).
    """
    import remind_me_mcp.api_keys as api_keys_mod

    source = inspect.getsource(api_keys_mod)
    assert "hmac.compare_digest" in source


# ---------------------------------------------------------------------------
# HTTP-level scope enforcement (BearerAuthMiddleware + ApiKeyStore)
# ---------------------------------------------------------------------------


@pytest.fixture()
def scoped_client(db_conn: sqlite3.Connection, monkeypatch, tmp_path: Path):
    """A TestClient with the default key set AND an isolated ApiKeyStore.

    MEMORY_DIR is pointed at a fresh tmp_path so the ApiKeyStore _build_api_app
    wires up reads/writes there, independent of other tests and the real
    ~/.remind-me/ directory.
    """
    import remind_me_mcp.config as _cfg
    import remind_me_mcp.importer as _importer_mod

    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db_conn)
    monkeypatch.setattr(_cfg, "API_KEY", "default-secret-key")
    monkeypatch.setattr(_cfg, "MEMORY_DIR", tmp_path)

    store = ApiKeyStore(tmp_path / "api_keys.json")
    app = _build_api_app()
    client = TestClient(app)
    return client, store


def test_default_key_unaffected_by_scoped_keys(scoped_client) -> None:
    """Backward compat: the default key keeps full read-write access regardless
    of what scoped keys exist."""
    client, store = scoped_client
    store.create_key("viewer", "read")

    headers = {"Authorization": "Bearer default-secret-key"}
    assert client.get("/api/stats", headers=headers).status_code == 200
    r = client.post("/api/memories", json={"content": "hello"}, headers=headers)
    assert r.status_code == 201


def test_read_scoped_key_authenticates_get(scoped_client) -> None:
    client, store = scoped_client
    key = store.create_key("viewer", "read")

    r = client.get("/api/stats", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200


@pytest.mark.parametrize("method,path,body", [
    ("POST", "/api/memories", {"content": "hi"}),
    ("PUT", "/api/memories/fake-id", {"content": "hi"}),
    ("PATCH", "/api/memories/fake-id", {"content": "hi"}),
])
def test_read_scoped_key_rejected_on_mutating_routes(scoped_client, method, path, body) -> None:
    """A read-scoped key gets a clear 403 on any mutating route, not 401/404."""
    client, store = scoped_client
    key = store.create_key("viewer", "read")

    r = client.request(
        method, path, json=body, headers={"Authorization": f"Bearer {key}"}
    )
    assert r.status_code == 403
    data = r.json()
    assert "error" in data
    assert "viewer" in data["error"]
    assert "read" in data["error"].lower()


def test_read_scoped_key_rejected_on_delete(scoped_client) -> None:
    client, store = scoped_client
    key = store.create_key("viewer", "read")

    r = client.delete("/api/memories/fake-id", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 403


def test_read_write_scoped_key_can_mutate(scoped_client) -> None:
    client, store = scoped_client
    key = store.create_key("automation", "read-write")

    r = client.post(
        "/api/memories", json={"content": "hi"}, headers={"Authorization": f"Bearer {key}"}
    )
    assert r.status_code == 201


def test_revoked_scoped_key_stops_authenticating_immediately(scoped_client) -> None:
    client, store = scoped_client
    key = store.create_key("viewer", "read")

    assert client.get("/api/stats", headers={"Authorization": f"Bearer {key}"}).status_code == 200
    store.revoke_key("viewer")
    r = client.get("/api/stats", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 401


def test_unknown_bearer_token_still_401(scoped_client) -> None:
    client, _store = scoped_client
    r = client.get("/api/stats", headers={"Authorization": "Bearer totally-made-up"})
    assert r.status_code == 401


def test_ics_feed_route_unaffected_by_scoped_key_enforcement(scoped_client) -> None:
    """The secret-path ICS feed route bypasses the bearer scheme entirely --
    a read-scoped key must not change that route's behavior at all."""
    client, store = scoped_client
    store.create_key("viewer", "read")

    # No Authorization header at all -- reaches the route handler, which does
    # its own token check and 404s on a wrong/missing path token, never 403.
    r = client.get("/api/reminders/wrong-token.ics")
    assert r.status_code == 404


def test_health_route_unaffected_by_scoped_keys(scoped_client) -> None:
    client, store = scoped_client
    store.create_key("viewer", "read")
    assert client.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# remind_me_api_key MCP tool
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_tool_dir(monkeypatch, tmp_path: Path) -> Path:
    import remind_me_mcp.config as _cfg

    monkeypatch.setattr(_cfg, "MEMORY_DIR", tmp_path)
    return tmp_path


async def test_tool_create_returns_plaintext_key_once(isolated_tool_dir: Path) -> None:
    from remind_me_mcp.tools.admin import remind_me_api_key

    result = json.loads(await remind_me_api_key(action="create", name="viewer", scope="read"))
    assert result["status"] == "created"
    assert result["name"] == "viewer"
    assert result["scope"] == "read"
    assert result["key"]
    assert "warning" in result

    # Persisted store only has the hash, never the plaintext.
    raw = (isolated_tool_dir / "api_keys.json").read_text(encoding="utf-8")
    assert result["key"] not in raw


async def test_tool_list_includes_default_and_created_keys_no_material(isolated_tool_dir: Path) -> None:
    from remind_me_mcp.tools.admin import remind_me_api_key

    created = json.loads(await remind_me_api_key(action="create", name="viewer", scope="read"))
    listing = json.loads(await remind_me_api_key(action="list"))

    names = {k["name"] for k in listing["keys"]}
    assert names == {DEFAULT_KEY_NAME, "viewer"}
    assert created["key"] not in json.dumps(listing)
    for entry in listing["keys"]:
        assert "key_hash" not in entry
        assert "key" not in entry


async def test_tool_revoke_removes_named_key(isolated_tool_dir: Path) -> None:
    from remind_me_mcp.tools.admin import remind_me_api_key

    await remind_me_api_key(action="create", name="viewer", scope="read")
    revoked = json.loads(await remind_me_api_key(action="revoke", name="viewer"))
    assert revoked["status"] == "revoked"

    listing = json.loads(await remind_me_api_key(action="list"))
    names = {k["name"] for k in listing["keys"]}
    assert names == {DEFAULT_KEY_NAME}


async def test_tool_revoke_default_key_fails_cleanly(isolated_tool_dir: Path) -> None:
    from remind_me_mcp.tools.admin import remind_me_api_key

    result = json.loads(await remind_me_api_key(action="revoke", name=DEFAULT_KEY_NAME))
    assert result["status"] == "error"
    assert "config-managed" in result["error"]


async def test_tool_revoke_unknown_key_fails_cleanly(isolated_tool_dir: Path) -> None:
    from remind_me_mcp.tools.admin import remind_me_api_key

    result = json.loads(await remind_me_api_key(action="revoke", name="does-not-exist"))
    assert result["status"] == "error"


async def test_tool_create_without_name_fails_cleanly(isolated_tool_dir: Path) -> None:
    from remind_me_mcp.tools.admin import remind_me_api_key

    result = json.loads(await remind_me_api_key(action="create", name="", scope="read"))
    assert result["status"] == "error"


async def test_tool_create_duplicate_name_fails_cleanly(isolated_tool_dir: Path) -> None:
    from remind_me_mcp.tools.admin import remind_me_api_key

    await remind_me_api_key(action="create", name="viewer", scope="read")
    result = json.loads(await remind_me_api_key(action="create", name="viewer", scope="read"))
    assert result["status"] == "error"
    assert "already exists" in result["error"]


async def test_tool_unknown_action_fails_cleanly(isolated_tool_dir: Path) -> None:
    from remind_me_mcp.tools.admin import remind_me_api_key

    result = json.loads(await remind_me_api_key(action="destroy"))
    assert result["status"] == "error"


async def test_tool_created_key_authenticates_over_http(isolated_tool_dir: Path, db_conn, monkeypatch) -> None:
    """End-to-end: a key created via the MCP tool authenticates against the
    live dashboard API with the correct scope enforcement."""
    import remind_me_mcp.config as _cfg
    import remind_me_mcp.importer as _importer_mod
    from remind_me_mcp.tools.admin import remind_me_api_key

    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db_conn)
    monkeypatch.setattr(_cfg, "API_KEY", "default-secret-key")

    created = json.loads(await remind_me_api_key(action="create", name="viewer", scope="read"))
    key = created["key"]

    app = _build_api_app()
    client = TestClient(app)

    assert client.get("/api/stats", headers={"Authorization": f"Bearer {key}"}).status_code == 200
    r = client.post(
        "/api/memories", json={"content": "hi"}, headers={"Authorization": f"Bearer {key}"}
    )
    assert r.status_code == 403
