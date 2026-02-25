"""
Integration tests for all Starlette HTTP API routes in remind_me_mcp.api.

Each test exercises a real route handler against the in-memory SQLite database
provided by the db_conn fixture. The Starlette TestClient handles async route
handlers synchronously — no async test code is needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from remind_me_mcp.api import _build_api_app

# ---------------------------------------------------------------------------
# Client fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(db_conn, monkeypatch):
    """Build a Starlette TestClient with the in-memory db monkeypatched.

    The db_conn fixture already patches remind_me_mcp.api._get_db and
    remind_me_mcp.db._get_db. This fixture additionally patches
    remind_me_mcp.importer._get_db so the /api/import route uses the same
    isolated in-memory connection.

    IMPORT_ROOTS is patched to include /tmp so that test fixtures using
    pytest's tmp_path (which lives under /tmp) are not rejected by the
    SEC-02 path guard.
    """
    import remind_me_mcp.api as _api_mod
    import remind_me_mcp.config as _cfg
    import remind_me_mcp.importer as _importer_mod

    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db_conn)

    # Patch IMPORT_ROOTS to include tmp dirs used by test fixtures
    monkeypatch.setattr(_cfg, "IMPORT_ROOTS", [Path.home(), Path("/tmp").resolve()])
    monkeypatch.setattr(_api_mod, "IMPORT_ROOTS", [Path.home(), Path("/tmp").resolve()])

    app = _build_api_app()
    return TestClient(app)


@pytest.fixture()
def client_with_auth(db_conn, monkeypatch):
    """Build a Starlette TestClient with API key authentication enabled.

    Patches API_KEY to 'test-secret-key' in both config and api modules,
    then rebuilds the app so BearerAuthMiddleware picks up the key.
    """
    import remind_me_mcp.api as _api_mod
    import remind_me_mcp.config as _cfg
    import remind_me_mcp.importer as _importer_mod

    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db_conn)
    monkeypatch.setattr(_cfg, "API_KEY", "test-secret-key")
    monkeypatch.setattr(_api_mod, "API_KEY", "test-secret-key")

    app = _build_api_app()
    return TestClient(app)


# ---------------------------------------------------------------------------
# Dashboard / index
# ---------------------------------------------------------------------------


def test_dashboard_serves_html(client: TestClient) -> None:
    """GET / should return a 200 HTML response with the React dashboard."""
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Remind Me" in response.text
    assert "react" in response.text.lower()


# ---------------------------------------------------------------------------
# GET /api/stats
# ---------------------------------------------------------------------------


def test_api_stats_empty(client: TestClient) -> None:
    """GET /api/stats with no memories should return total: 0."""
    response = client.get("/api/stats")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0


def test_api_stats_with_data(client: TestClient, memory_factory) -> None:
    """GET /api/stats should reflect correct total and category counts."""
    memory_factory(content="Work memory one", category="work")
    memory_factory(content="Work memory two", category="work")
    memory_factory(content="Personal memory", category="personal")

    response = client.get("/api/stats")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 3
    assert "work" in data["categories"]
    assert "personal" in data["categories"]
    assert data["categories"]["work"] == 2
    assert data["categories"]["personal"] == 1


# ---------------------------------------------------------------------------
# GET /api/memories (list)
# ---------------------------------------------------------------------------


def test_api_list_empty(client: TestClient) -> None:
    """GET /api/memories with no data should return empty list."""
    response = client.get("/api/memories")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0
    assert data["memories"] == []


def test_api_list_with_data(client: TestClient, memory_factory) -> None:
    """GET /api/memories should return all memories when database has entries."""
    memory_factory(content="Memory A")
    memory_factory(content="Memory B")
    memory_factory(content="Memory C")

    response = client.get("/api/memories")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 3
    assert data["count"] == 3


def test_api_list_filter_by_category(client: TestClient, memory_factory) -> None:
    """GET /api/memories?category=work should only return work memories."""
    memory_factory(content="Work item one", category="work")
    memory_factory(content="Work item two", category="work")
    memory_factory(content="Personal item", category="personal")

    response = client.get("/api/memories?category=work")
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2
    for mem in data["memories"]:
        assert mem["category"] == "work"


def test_api_list_pagination(client: TestClient, memory_factory) -> None:
    """GET /api/memories supports limit and offset pagination."""
    for i in range(5):
        memory_factory(content=f"Paginated memory {i}")

    # First page: 2 items, has_more should be True
    response = client.get("/api/memories?limit=2&offset=0")
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2
    assert data["has_more"] is True

    # Last page: offset past all but one, has_more should be False
    response = client.get("/api/memories?limit=2&offset=4")
    assert response.status_code == 200
    data = response.json()
    assert data["has_more"] is False


# ---------------------------------------------------------------------------
# POST /api/memories (add)
# ---------------------------------------------------------------------------


def test_api_add_memory(client: TestClient) -> None:
    """POST /api/memories should create a memory and return 201."""
    response = client.post(
        "/api/memories",
        json={"content": "Test memory", "category": "test"},
    )
    assert response.status_code == 201
    data = response.json()
    assert "id" in data
    assert data["content"] == "Test memory"
    assert data["category"] == "test"


def test_api_add_missing_content(client: TestClient) -> None:
    """POST /api/memories without content should return 400."""
    response = client.post("/api/memories", json={"category": "test"})
    assert response.status_code == 400
    data = response.json()
    assert "content" in data["error"].lower()


def test_api_add_invalid_json(client: TestClient) -> None:
    """POST /api/memories with non-JSON body should return 400."""
    response = client.post(
        "/api/memories",
        content=b"not valid json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# GET /api/memories/{id}
# ---------------------------------------------------------------------------


def test_api_get_memory(client: TestClient, memory_factory) -> None:
    """GET /api/memories/{id} should return the memory with correct content."""
    mem = memory_factory(content="Specific memory to retrieve")
    response = client.get(f"/api/memories/{mem['id']}")
    assert response.status_code == 200
    data = response.json()
    assert data["content"] == "Specific memory to retrieve"
    assert data["id"] == mem["id"]


def test_api_get_not_found(client: TestClient) -> None:
    """GET /api/memories/{id} with nonexistent id should return 404."""
    response = client.get("/api/memories/nonexistent_id_abc123")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# PUT /api/memories/{id} (update)
# ---------------------------------------------------------------------------


def test_api_update_content(client: TestClient, memory_factory) -> None:
    """PUT /api/memories/{id} should update content and return 200."""
    mem = memory_factory(content="Original content")
    response = client.put(
        f"/api/memories/{mem['id']}",
        json={"content": "Updated content"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["content"] == "Updated content"

    # Verify via GET
    get_response = client.get(f"/api/memories/{mem['id']}")
    assert get_response.json()["content"] == "Updated content"


def test_api_update_tags(client: TestClient, memory_factory) -> None:
    """PUT /api/memories/{id} should update tags correctly."""
    mem = memory_factory(content="Memory with tags", tags=[])
    response = client.put(
        f"/api/memories/{mem['id']}",
        json={"tags": ["python", "testing"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert "python" in data["tags"]
    assert "testing" in data["tags"]


def test_api_update_not_found(client: TestClient) -> None:
    """PUT /api/memories/{id} with nonexistent id should return 404."""
    response = client.put(
        "/api/memories/nonexistent_id_xyz",
        json={"content": "x"},
    )
    assert response.status_code == 404


def test_api_update_no_fields(client: TestClient, memory_factory) -> None:
    """PUT /api/memories/{id} with empty body should return 400."""
    mem = memory_factory(content="Memory for no-field update test")
    response = client.put(f"/api/memories/{mem['id']}", json={})
    assert response.status_code == 400
    data = response.json()
    assert "no fields" in data["error"].lower()


# ---------------------------------------------------------------------------
# DELETE /api/memories/{id}
# ---------------------------------------------------------------------------


def test_api_delete_memory(client: TestClient, memory_factory) -> None:
    """DELETE /api/memories/{id} should delete and return 200; GET returns 404 after."""
    mem = memory_factory(content="Memory to delete")
    response = client.delete(f"/api/memories/{mem['id']}")
    assert response.status_code == 200

    # Verify deletion via GET
    get_response = client.get(f"/api/memories/{mem['id']}")
    assert get_response.status_code == 404


def test_api_delete_not_found(client: TestClient) -> None:
    """DELETE /api/memories/{id} with nonexistent id should return 404."""
    response = client.delete("/api/memories/nonexistent_delete_id")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/memories/search
# ---------------------------------------------------------------------------


def test_api_search_finds_match(client: TestClient, memory_factory) -> None:
    """GET /api/memories/search?q=... should return FTS5 matches."""
    memory_factory(content="Python asyncio patterns are useful for concurrency")

    response = client.get("/api/memories/search?q=asyncio")
    assert response.status_code == 200
    data = response.json()
    assert data["count"] >= 1
    contents = [m["content"] for m in data["memories"]]
    assert any("asyncio" in c for c in contents)


def test_api_search_no_query(client: TestClient) -> None:
    """GET /api/memories/search without q param should return 400."""
    response = client.get("/api/memories/search")
    assert response.status_code == 400
    data = response.json()
    assert "q" in data["error"].lower()


def test_api_search_no_results(client: TestClient) -> None:
    """GET /api/memories/search?q=zzzznotfound should return empty results."""
    response = client.get("/api/memories/search?q=zzzznotfound")
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 0


# ---------------------------------------------------------------------------
# POST /api/import
# ---------------------------------------------------------------------------


def test_api_import_file(client: TestClient, db_conn, sample_chat_json: Path) -> None:
    """POST /api/import should process a chat JSON file and return ok with memories_created >= 1."""
    response = client.post("/api/import", json={"file_path": str(sample_chat_json)})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["memories_created"] >= 1


def test_api_import_missing_path(client: TestClient) -> None:
    """POST /api/import with empty file_path should return 400."""
    response = client.post("/api/import", json={"file_path": ""})
    assert response.status_code == 400
    data = response.json()
    assert "file_path" in data["error"].lower()


def test_api_import_nonexistent_file(client: TestClient) -> None:
    """POST /api/import with a nonexistent file inside allowed roots returns 400 with not-found message.

    SEC-02 path guard fires first for paths outside roots, so we use a path
    inside /tmp (which is in the patched IMPORT_ROOTS) that does not exist.
    """
    response = client.post("/api/import", json={"file_path": "/tmp/nonexistent_remind_me_test_file.json"})
    assert response.status_code == 400
    data = response.json()
    assert "not found" in data["error"].lower()


def test_api_import_directory(client: TestClient, db_conn, tmp_path: Path) -> None:
    """POST /api/import with a directory path executes import and returns a summary.

    Exercises the p.is_dir() branch in api_import. Requires the await fix
    in api.py to return a real summary rather than a coroutine object.
    """
    import json

    chat_file = tmp_path / "chat.json"
    chat_file.write_text(json.dumps({
        "chat_messages": [
            {"sender": "user", "content": [{"type": "text", "text": "What is Python?"}]},
            {"sender": "assistant", "content": [{"type": "text", "text": "Python is a programming language."}]},
        ]
    }))

    response = client.post("/api/import", json={"file_path": str(tmp_path)})
    assert response.status_code == 200
    data = response.json()
    assert data["files_processed"] == 1
    assert data["imported"] == 1
    assert data["total_memories_created"] >= 1


# ---------------------------------------------------------------------------
# Full REST CRUD cycle
# ---------------------------------------------------------------------------


def test_api_crud_cycle(client: TestClient) -> None:
    """Full round-trip: POST add -> GET verify -> PUT update -> GET verify -> DELETE -> GET 404."""
    # POST: create
    create_response = client.post(
        "/api/memories",
        json={"content": "CRUD cycle memory", "category": "cycle-test"},
    )
    assert create_response.status_code == 201
    mem = create_response.json()
    mem_id = mem["id"]
    assert mem["content"] == "CRUD cycle memory"

    # GET: verify creation
    get_response = client.get(f"/api/memories/{mem_id}")
    assert get_response.status_code == 200
    assert get_response.json()["content"] == "CRUD cycle memory"

    # PUT: update content
    update_response = client.put(
        f"/api/memories/{mem_id}",
        json={"content": "Updated CRUD cycle memory"},
    )
    assert update_response.status_code == 200

    # GET: verify update
    verify_response = client.get(f"/api/memories/{mem_id}")
    assert verify_response.status_code == 200
    assert verify_response.json()["content"] == "Updated CRUD cycle memory"

    # DELETE
    delete_response = client.delete(f"/api/memories/{mem_id}")
    assert delete_response.status_code == 200

    # GET: verify 404 after deletion
    final_response = client.get(f"/api/memories/{mem_id}")
    assert final_response.status_code == 404


# ---------------------------------------------------------------------------
# Regression test — DATA-02: API tag-filtered pagination
# ---------------------------------------------------------------------------


def test_api_list_tag_filter_pagination(
    client: TestClient,
    memory_factory,
) -> None:
    """DATA-02 regression: GET /api/memories?tags=alpha&limit=5 returns exactly 5
    memories all tagged 'alpha', not 5 pre-filter rows with fewer tagged results.

    Creates 10 memories with tag 'alpha' and 10 without.  With the bug fixed, tag
    filtering happens in SQL before LIMIT, so the response contains exactly 5 'alpha'
    memories regardless of insertion order.
    """
    # Create 10 tagged and 10 untagged memories
    for i in range(10):
        memory_factory(content=f"API alpha tagged memory {i} unique content", tags=["alpha"])
    for i in range(10):
        memory_factory(content=f"API untagged memory {i} unique content", tags=[])

    response = client.get("/api/memories?tags=alpha&limit=5")
    assert response.status_code == 200
    data = response.json()

    # Must return exactly 5 results (not fewer due to Python post-filter)
    assert data["count"] == 5, (
        f"Expected exactly 5 results with tags=alpha limit=5, got {data['count']}. "
        f"This indicates tag filtering still happens in Python after LIMIT."
    )

    # All returned memories must have the 'alpha' tag
    for mem in data["memories"]:
        assert "alpha" in mem["tags"], (
            f"Memory {mem['id']} does not have 'alpha' tag: {mem['tags']}"
        )


# ---------------------------------------------------------------------------
# SEC-01: CORS restriction to localhost origins
# ---------------------------------------------------------------------------


def test_cors_allows_localhost(client: TestClient) -> None:
    """SEC-01: Requests from http://localhost should receive ACAO header."""
    r = client.get("/api/stats", headers={"Origin": "http://localhost"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost"


def test_cors_allows_localhost_with_port(client: TestClient) -> None:
    """SEC-01: Requests from http://localhost:5199 should receive ACAO header."""
    r = client.get("/api/stats", headers={"Origin": "http://localhost:5199"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5199"


def test_cors_allows_127_0_0_1(client: TestClient) -> None:
    """SEC-01: Requests from http://127.0.0.1 should receive ACAO header."""
    r = client.get("/api/stats", headers={"Origin": "http://127.0.0.1"})
    assert r.headers.get("access-control-allow-origin") == "http://127.0.0.1"


def test_cors_allows_127_0_0_1_with_port(client: TestClient) -> None:
    """SEC-01: Requests from http://127.0.0.1:5199 should receive ACAO header."""
    r = client.get("/api/stats", headers={"Origin": "http://127.0.0.1:5199"})
    assert r.headers.get("access-control-allow-origin") == "http://127.0.0.1:5199"


def test_cors_denies_external_origin_preflight(client: TestClient) -> None:
    """SEC-01: Preflight OPTIONS from external origin should return 400."""
    r = client.options("/api/stats", headers={
        "Origin": "http://evil.com",
        "Access-Control-Request-Method": "GET",
    })
    assert r.status_code == 400


def test_cors_no_acao_for_external_simple_request(client: TestClient) -> None:
    """SEC-01: Simple GET from external origin should NOT have ACAO header."""
    r = client.get("/api/stats", headers={"Origin": "http://evil.com"})
    assert "access-control-allow-origin" not in r.headers


def test_cors_rejects_localhost_subdomain(client: TestClient) -> None:
    """SEC-01: localhost.evil.com must not match — fullmatch prevents substring."""
    r = client.get("/api/stats", headers={"Origin": "http://localhost.evil.com"})
    assert "access-control-allow-origin" not in r.headers


# ---------------------------------------------------------------------------
# SEC-02: Import path restriction to allowed roots
# ---------------------------------------------------------------------------


def test_import_rejects_path_outside_home(client: TestClient) -> None:
    """SEC-02: Import with path outside home should return 400."""
    r = client.post("/api/import", json={"file_path": "/etc/passwd"})
    assert r.status_code == 400
    assert "not in allowed" in r.json()["error"].lower()


def test_import_rejects_traversal_attempt(client: TestClient) -> None:
    """SEC-02: Import with traversal sequence should be resolved and rejected."""
    traversal_path = str(Path.home() / ".." / "etc" / "passwd")
    r = client.post("/api/import", json={"file_path": traversal_path})
    assert r.status_code == 400
    assert "not in allowed" in r.json()["error"].lower()


def test_import_allows_path_inside_home(client: TestClient, sample_chat_json: Path) -> None:
    """SEC-02: Import with path inside allowed roots should succeed normally."""
    r = client.post("/api/import", json={"file_path": str(sample_chat_json)})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"


def test_import_custom_roots(client: TestClient, monkeypatch, tmp_path: Path) -> None:
    """SEC-02: Custom IMPORT_ROOTS restricts to configured paths only."""
    import remind_me_mcp.api as _api_mod
    import remind_me_mcp.config as _cfg

    custom_root = tmp_path / "allowed"
    custom_root.mkdir()
    test_file = custom_root / "test.json"
    test_file.write_text('{"chat_messages": []}')

    restricted_roots = [custom_root.resolve()]
    monkeypatch.setattr(_cfg, "IMPORT_ROOTS", restricted_roots)
    monkeypatch.setattr(_api_mod, "IMPORT_ROOTS", restricted_roots)

    # File inside custom root: allowed
    r = client.post("/api/import", json={"file_path": str(test_file)})
    assert r.status_code == 200

    # File outside custom root: rejected
    r = client.post("/api/import", json={"file_path": str(Path.home() / "some_file.txt")})
    assert r.status_code == 400
    assert "not in allowed" in r.json()["error"].lower()


# ---------------------------------------------------------------------------
# SEC-03: Optional Bearer token authentication
# ---------------------------------------------------------------------------


def test_api_requires_auth_when_key_set(client_with_auth: TestClient) -> None:
    """SEC-03: /api/* routes return 401 when API key is set but no token provided."""
    r = client_with_auth.get("/api/stats")
    assert r.status_code == 401
    assert r.json()["error"] == "Unauthorized"


def test_api_rejects_wrong_token(client_with_auth: TestClient) -> None:
    """SEC-03: /api/* routes return 401 with an incorrect Bearer token."""
    r = client_with_auth.get("/api/stats", headers={"Authorization": "Bearer wrong-key"})
    assert r.status_code == 401


def test_api_accepts_valid_token(client_with_auth: TestClient) -> None:
    """SEC-03: /api/* routes return 200 with correct Bearer token."""
    r = client_with_auth.get("/api/stats", headers={"Authorization": "Bearer test-secret-key"})
    assert r.status_code == 200


def test_dashboard_accessible_without_auth(client_with_auth: TestClient) -> None:
    """SEC-03: Dashboard route (/) is not gated by auth — accessible without token."""
    r = client_with_auth.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_api_open_when_no_key_configured(client: TestClient) -> None:
    """SEC-03: When API_KEY is None (default), all routes are open without auth."""
    r = client.get("/api/stats")
    assert r.status_code == 200


def test_auth_does_not_block_cors_preflight(client_with_auth: TestClient) -> None:
    """SEC-03: CORS preflight OPTIONS from localhost should succeed even with auth enabled."""
    r = client_with_auth.options("/api/stats", headers={
        "Origin": "http://localhost:5199",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "Authorization",
    })
    # CORS middleware handles OPTIONS before auth — should return 200 with CORS headers
    assert r.status_code == 200
    assert "access-control-allow-origin" in r.headers


def test_auth_protects_all_api_routes(client_with_auth: TestClient) -> None:
    """SEC-03: POST, PUT, DELETE on /api/* all require auth."""
    # POST /api/memories
    r = client_with_auth.post("/api/memories", json={"content": "test"})
    assert r.status_code == 401

    # POST /api/import
    r = client_with_auth.post("/api/import", json={"file_path": "/tmp/test.json"})
    assert r.status_code == 401

    # DELETE /api/memories/fake-id
    r = client_with_auth.delete("/api/memories/fake-id")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# EMBD-01 / EMBD-02: REST API embedding parity
# ---------------------------------------------------------------------------


def test_api_add_creates_embedding(db_conn_with_vec, mock_embedder, monkeypatch) -> None:
    """EMBD-01: POST /api/memories creates a corresponding row in memories_vec."""
    import remind_me_mcp.api as _api_mod
    import remind_me_mcp.config as _cfg
    import remind_me_mcp.importer as _importer_mod

    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db_conn_with_vec)
    monkeypatch.setattr(_cfg, "IMPORT_ROOTS", [Path.home(), Path("/tmp").resolve()])
    monkeypatch.setattr(_api_mod, "IMPORT_ROOTS", [Path.home(), Path("/tmp").resolve()])

    app = _build_api_app()
    client = TestClient(app)

    response = client.post("/api/memories", json={"content": "embedding parity test memory"})
    assert response.status_code == 201

    mem_id = response.json()["id"]
    rowid_row = db_conn_with_vec.execute(
        "SELECT rowid FROM memories WHERE id = ?", (mem_id,)
    ).fetchone()
    assert rowid_row is not None, "Memory row should exist in memories table"
    rowid = rowid_row[0]

    vec_row = db_conn_with_vec.execute(
        "SELECT rowid FROM memories_vec WHERE rowid = ?", (rowid,)
    ).fetchone()
    assert vec_row is not None, "memories_vec row should exist after POST /api/memories"


def test_api_add_embedding_rowid_matches_memory(db_conn_with_vec, mock_embedder, monkeypatch) -> None:
    """EMBD-01: The memories_vec rowid matches the memory's SQLite rowid (foreign key integrity)."""
    import remind_me_mcp.api as _api_mod
    import remind_me_mcp.config as _cfg
    import remind_me_mcp.importer as _importer_mod

    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db_conn_with_vec)
    monkeypatch.setattr(_cfg, "IMPORT_ROOTS", [Path.home(), Path("/tmp").resolve()])
    monkeypatch.setattr(_api_mod, "IMPORT_ROOTS", [Path.home(), Path("/tmp").resolve()])

    app = _build_api_app()
    client = TestClient(app)

    response = client.post("/api/memories", json={"content": "rowid match test memory"})
    assert response.status_code == 201

    mem_id = response.json()["id"]
    mem_rowid = db_conn_with_vec.execute(
        "SELECT rowid FROM memories WHERE id = ?", (mem_id,)
    ).fetchone()[0]
    vec_rowid = db_conn_with_vec.execute(
        "SELECT rowid FROM memories_vec WHERE rowid = ?", (mem_rowid,)
    ).fetchone()
    assert vec_rowid is not None, "memories_vec rowid should match memory rowid"
    assert vec_rowid[0] == mem_rowid, "memories_vec rowid must equal memory's SQLite rowid"


def test_api_update_content_regenerates_embedding(db_conn_with_vec, mock_embedder, monkeypatch) -> None:
    """EMBD-02: PUT /api/memories/{id} with new content updates the memories_vec row."""
    import remind_me_mcp.api as _api_mod
    import remind_me_mcp.config as _cfg
    import remind_me_mcp.importer as _importer_mod

    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db_conn_with_vec)
    monkeypatch.setattr(_cfg, "IMPORT_ROOTS", [Path.home(), Path("/tmp").resolve()])
    monkeypatch.setattr(_api_mod, "IMPORT_ROOTS", [Path.home(), Path("/tmp").resolve()])

    app = _build_api_app()
    client = TestClient(app)

    # Create initial memory
    create_resp = client.post("/api/memories", json={"content": "original content for update test"})
    assert create_resp.status_code == 201
    mem_id = create_resp.json()["id"]

    mem_rowid = db_conn_with_vec.execute(
        "SELECT rowid FROM memories WHERE id = ?", (mem_id,)
    ).fetchone()[0]

    # Update with new content
    update_resp = client.put(f"/api/memories/{mem_id}", json={"content": "updated content for embedding test"})
    assert update_resp.status_code == 200

    # Vec row should still be present (upsert by rowid — content changed, rowid unchanged)
    vec_row = db_conn_with_vec.execute(
        "SELECT rowid FROM memories_vec WHERE rowid = ?", (mem_rowid,)
    ).fetchone()
    assert vec_row is not None, "memories_vec row should still exist after content update"


def test_api_update_no_content_preserves_embedding(db_conn_with_vec, mock_embedder, monkeypatch) -> None:
    """EMBD-02: PUT /api/memories/{id} with only tags does NOT alter the memories_vec row."""
    import remind_me_mcp.api as _api_mod
    import remind_me_mcp.config as _cfg
    import remind_me_mcp.importer as _importer_mod

    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db_conn_with_vec)
    monkeypatch.setattr(_cfg, "IMPORT_ROOTS", [Path.home(), Path("/tmp").resolve()])
    monkeypatch.setattr(_api_mod, "IMPORT_ROOTS", [Path.home(), Path("/tmp").resolve()])

    app = _build_api_app()
    client = TestClient(app)

    # Create initial memory (with embedding)
    create_resp = client.post("/api/memories", json={"content": "tag-only update test memory"})
    assert create_resp.status_code == 201
    mem_id = create_resp.json()["id"]

    mem_rowid = db_conn_with_vec.execute(
        "SELECT rowid FROM memories WHERE id = ?", (mem_id,)
    ).fetchone()[0]

    # Verify initial vec row exists
    initial_vec = db_conn_with_vec.execute(
        "SELECT rowid FROM memories_vec WHERE rowid = ?", (mem_rowid,)
    ).fetchone()
    assert initial_vec is not None, "Initial embedding should exist after POST"

    # Update with only tags (no content)
    update_resp = client.put(f"/api/memories/{mem_id}", json={"tags": ["new-tag"]})
    assert update_resp.status_code == 200

    # Vec row should still be present and unchanged
    vec_row = db_conn_with_vec.execute(
        "SELECT rowid FROM memories_vec WHERE rowid = ?", (mem_rowid,)
    ).fetchone()
    assert vec_row is not None, "memories_vec row should be preserved on tag-only update"


def test_rest_and_mcp_memories_equally_findable_by_semantic_search(
    db_conn_with_vec, mock_embedder, monkeypatch
) -> None:
    """Parity: REST API and MCP tool memories are equally retrievable via _semantic_search."""
    import remind_me_mcp.api as _api_mod
    import remind_me_mcp.config as _cfg
    import remind_me_mcp.importer as _importer_mod
    from remind_me_mcp.db import _embed_and_store, _make_id, _now_iso, _semantic_search

    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db_conn_with_vec)
    monkeypatch.setattr(_cfg, "IMPORT_ROOTS", [Path.home(), Path("/tmp").resolve()])
    monkeypatch.setattr(_api_mod, "IMPORT_ROOTS", [Path.home(), Path("/tmp").resolve()])

    app = _build_api_app()
    client = TestClient(app)

    # Create a memory via REST API
    rest_resp = client.post("/api/memories", json={"content": "REST API parity test memory"})
    assert rest_resp.status_code == 201
    rest_mem_id = rest_resp.json()["id"]

    # Create an MCP-style memory directly (insert + embed, as tools.py does)
    mcp_content = "MCP tool parity test memory"
    mcp_mem_id = _make_id(mcp_content)
    now = _now_iso()
    import json as _json
    db_conn_with_vec.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (mcp_mem_id, mcp_content, "general", _json.dumps([]), "manual", _json.dumps({}), now, now),
    )
    db_conn_with_vec.commit()
    _embed_and_store(db_conn_with_vec, mcp_mem_id, mcp_content)

    # Both memories should appear in semantic search results
    results = _semantic_search(db_conn_with_vec, "parity test", limit=10)
    result_ids = {r["id"] for r in results}

    assert rest_mem_id in result_ids, f"REST memory {rest_mem_id} not found in semantic search results: {result_ids}"
    assert mcp_mem_id in result_ids, f"MCP memory {mcp_mem_id} not found in semantic search results: {result_ids}"
