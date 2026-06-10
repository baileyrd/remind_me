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

    API_KEY is set to the explicit "disabled" opt-out so the open-access
    behavior is exercised (SE-01: auth is otherwise on by default with an
    auto-generated key). IMPORT_ROOTS containment relies on the session
    fixture, which allows $HOME and the system temp dir.
    """
    import remind_me_mcp.config as _cfg
    import remind_me_mcp.importer as _importer_mod

    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db_conn)
    monkeypatch.setattr(_cfg, "API_KEY", "disabled")

    app = _build_api_app()
    return TestClient(app)


@pytest.fixture()
def client_with_auth(db_conn, monkeypatch):
    """Build a Starlette TestClient with API key authentication enabled.

    Patches API_KEY to 'test-secret-key' in the config module (the api module
    resolves the key through config.resolve_api_key at app build time), then
    rebuilds the app so BearerAuthMiddleware picks up the key.
    """
    import remind_me_mcp.config as _cfg
    import remind_me_mcp.importer as _importer_mod

    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db_conn)
    monkeypatch.setattr(_cfg, "API_KEY", "test-secret-key")

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


def test_api_search_category_filter_applies_before_limit(
    client: TestClient, memory_factory
) -> None:
    """api_search pushes the category filter into SQL before LIMIT (DI-03)."""
    memory_factory(content="falcon falcon falcon field notes", category="noise")
    memory_factory(content="a falcon fact", category="birds")

    response = client.get("/api/memories/search?q=falcon&limit=1&category=birds")
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["memories"][0]["content"] == "a falcon fact"


def test_api_search_tag_filter_applies_before_limit(
    client: TestClient, memory_factory
) -> None:
    """api_search pushes the tag filter into SQL before LIMIT (DI-03)."""
    memory_factory(content="otter otter otter river survey", tags=["noise"])
    memory_factory(content="one otter spotted", tags=["river", "mammal"])

    response = client.get("/api/memories/search?q=otter&limit=1&tags=river,mammal")
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["memories"][0]["content"] == "one otter spotted"


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


def test_api_import_document_markdown(client: TestClient, db_conn, tmp_path: Path) -> None:
    """POST /api/import auto-detects a notes markdown file as a document (FT-02).

    Each heading section becomes its own memory with the heading recorded in
    metadata and source set to 'document_import' — parity with the MCP tool.
    """
    import json

    notes = tmp_path / "notes.md"
    notes.write_text("# Garden\n\n## Tomatoes\nNeed full sun and weekly feeding.\n")

    response = client.post("/api/import", json={"file_path": str(notes)})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["kind"] == "document"
    assert data["memories_created"] == 1

    row = db_conn.execute("SELECT source, metadata FROM memories").fetchone()
    assert row["source"] == "document_import"
    assert json.loads(row["metadata"])["section"] == "Garden > Tomatoes"


def test_api_import_explicit_kind_forwarded(client: TestClient, db_conn, tmp_path: Path) -> None:
    """POST /api/import forwards an explicit kind to the importer (FT-02)."""
    chatish = tmp_path / "chatish.md"
    chatish.write_text("## Assistant\nThis looks like a chat export.\n")

    response = client.post(
        "/api/import", json={"file_path": str(chatish), "kind": "document"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["kind"] == "document"


def test_api_import_invalid_kind(client: TestClient, tmp_path: Path) -> None:
    """POST /api/import with an unknown kind returns 400 (FT-02)."""
    notes = tmp_path / "notes.md"
    notes.write_text("Some notes.")

    response = client.post(
        "/api/import", json={"file_path": str(notes), "kind": "banana"}
    )
    assert response.status_code == 400
    assert "invalid kind" in response.json()["error"].lower()


# ---------------------------------------------------------------------------
# GET /api/export (FT-01)
# ---------------------------------------------------------------------------


def test_api_export_empty(client: TestClient) -> None:
    """GET /api/export on an empty store returns an empty JSON array."""
    response = client.get("/api/export")
    assert response.status_code == 200
    assert "application/json" in response.headers["content-type"]
    assert response.json() == []


def test_api_export_json_inline(client: TestClient, memory_factory) -> None:
    """GET /api/export returns full records including lifecycle columns."""
    memory_factory(content="Export endpoint memory", category="work", tags=["x"])
    response = client.get("/api/export")
    assert response.status_code == 200
    records = response.json()
    assert len(records) == 1
    rec = records[0]
    assert rec["content"] == "Export endpoint memory"
    assert rec["category"] == "work"
    assert rec["tags"] == ["x"]
    assert rec["role"] == "assistant"
    assert "id" in rec
    assert "created_at" in rec
    assert "superseded_by" in rec


def test_api_export_jsonl(client: TestClient, memory_factory) -> None:
    """GET /api/export?format=jsonl streams one record per line as NDJSON."""
    import json as _json

    memory_factory(content="JSONL export one")
    memory_factory(content="JSONL export two")
    response = client.get("/api/export?format=jsonl")
    assert response.status_code == 200
    assert "application/x-ndjson" in response.headers["content-type"]
    lines = [line for line in response.text.splitlines() if line.strip()]
    assert len(lines) == 2
    assert {_json.loads(line)["content"] for line in lines} == {
        "JSONL export one",
        "JSONL export two",
    }


def test_api_export_invalid_format(client: TestClient) -> None:
    """GET /api/export?format=xml is rejected with 400."""
    response = client.get("/api/export?format=xml")
    assert response.status_code == 400
    assert "format" in response.json()["error"].lower()


def test_api_export_category_and_tag_filters(client: TestClient, memory_factory) -> None:
    """Category and tag filters narrow the export."""
    memory_factory(content="Keep me", category="keep", tags=["a", "b"])
    memory_factory(content="Wrong category", category="drop", tags=["a", "b"])
    memory_factory(content="Missing tag", category="keep", tags=["a"])

    response = client.get("/api/export?category=keep&tags=a,b")
    assert response.status_code == 200
    records = response.json()
    assert [r["content"] for r in records] == ["Keep me"]


def test_api_export_to_file(client: TestClient, memory_factory, tmp_path: Path) -> None:
    """GET /api/export?file_path=... writes the file and returns a summary."""
    memory_factory(content="File endpoint export memory")
    dest = tmp_path / "api_backup.json"
    response = client.get(f"/api/export?file_path={dest}")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["exported"] == 1
    assert dest.exists()


def test_api_export_rejects_path_outside_roots(client: TestClient) -> None:
    """FT-01 mirrors SE-02: export destinations outside EXPORT_ROOTS are 400."""
    response = client.get("/api/export?file_path=/etc/exfiltrated.json")
    assert response.status_code == 400
    assert "not in allowed export roots" in response.json()["error"].lower()


def test_api_export_rejects_missing_parent(client: TestClient, tmp_path: Path) -> None:
    """A destination in a nonexistent directory is rejected with 400."""
    dest = tmp_path / "missing" / "backup.json"
    response = client.get(f"/api/export?file_path={dest}")
    assert response.status_code == 400
    assert "parent directory" in response.json()["error"].lower()


def test_api_export_requires_auth(client_with_auth: TestClient) -> None:
    """SEC-03: /api/export is gated by bearer auth like every /api/* route."""
    assert client_with_auth.get("/api/export").status_code == 401
    r = client_with_auth.get(
        "/api/export", headers={"Authorization": "Bearer test-secret-key"}
    )
    assert r.status_code == 200


def test_api_export_import_round_trip(client: TestClient, memory_factory, tmp_path: Path) -> None:
    """End-to-end: GET /api/export -> save -> POST /api/import re-creates content."""
    memory_factory(content="HTTP round trip memory")
    export_text = client.get("/api/export").text
    backup = tmp_path / "http_round_trip.json"
    backup.write_text(export_text)

    response = client.post("/api/import", json={"file_path": str(backup)})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["memories_created"] == 1


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
    import remind_me_mcp.config as _cfg

    custom_root = tmp_path / "allowed"
    custom_root.mkdir()
    test_file = custom_root / "test.json"
    test_file.write_text('{"chat_messages": []}')

    restricted_roots = [custom_root.resolve()]
    monkeypatch.setattr(_cfg, "IMPORT_ROOTS", restricted_roots)

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


def test_api_open_when_auth_explicitly_disabled(client: TestClient) -> None:
    """SE-01: REMIND_ME_API_KEY=disabled is the explicit opt-out — routes are open."""
    r = client.get("/api/stats")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# SE-01: auth on by default — auto-generated, persisted API key
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_key_dir(monkeypatch, tmp_path: Path) -> Path:
    """Point MEMORY_DIR at a fresh per-test dir and clear any env API_KEY."""
    import remind_me_mcp.config as _cfg

    monkeypatch.setattr(_cfg, "API_KEY", None)
    monkeypatch.setattr(_cfg, "MEMORY_DIR", tmp_path)
    return tmp_path


def test_default_app_generates_and_persists_api_key(db_conn, isolated_key_dir: Path) -> None:
    """SE-01: with no env key, building the app generates a key file (0600) and enforces auth."""
    app = _build_api_app()
    test_client = TestClient(app)

    key_file = isolated_key_dir / "api_key"
    assert key_file.is_file(), "API key must be auto-generated and persisted on first run"
    assert (key_file.stat().st_mode & 0o777) == 0o600, "key file must be private (0600)"
    key = key_file.read_text().strip()
    assert len(key) >= 32

    # No token -> 401; persisted token -> 200
    assert test_client.get("/api/stats").status_code == 401
    r = test_client.get("/api/stats", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200


def test_default_app_reuses_persisted_api_key(db_conn, isolated_key_dir: Path) -> None:
    """SE-01: subsequent app builds reuse the persisted key instead of regenerating."""
    from remind_me_mcp.config import resolve_api_key

    first = resolve_api_key()
    second = resolve_api_key()
    assert first == second
    assert (isolated_key_dir / "api_key").read_text().strip() == first


def test_env_api_key_wins_over_key_file(db_conn, isolated_key_dir: Path, monkeypatch) -> None:
    """SE-01: an explicit REMIND_ME_API_KEY beats the persisted key file."""
    import remind_me_mcp.config as _cfg

    (isolated_key_dir / "api_key").write_text("file-key\n")
    monkeypatch.setattr(_cfg, "API_KEY", "env-key")

    app = _build_api_app()
    test_client = TestClient(app)
    assert test_client.get("/api/stats", headers={"Authorization": "Bearer env-key"}).status_code == 200
    assert test_client.get("/api/stats", headers={"Authorization": "Bearer file-key"}).status_code == 401


def test_disabled_opt_out_creates_no_key_file(db_conn, isolated_key_dir: Path, monkeypatch) -> None:
    """SE-01: REMIND_ME_API_KEY=disabled opens the API and writes no key file."""
    import remind_me_mcp.config as _cfg

    monkeypatch.setattr(_cfg, "API_KEY", "Disabled")  # case-insensitive

    app = _build_api_app()
    test_client = TestClient(app)
    assert test_client.get("/api/stats").status_code == 200
    assert not (isolated_key_dir / "api_key").exists()


# ---------------------------------------------------------------------------
# SE-01: CSRF hardening — mutating routes require a JSON Content-Type (415)
# ---------------------------------------------------------------------------


def test_post_rejects_non_json_content_type(client: TestClient) -> None:
    """SE-01: cross-origin 'simple' POST bodies (text/plain, form) are rejected with 415."""
    for content_type in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
        r = client.post(
            "/api/memories",
            content=b'{"content": "csrf attempt"}',
            headers={"content-type": content_type},
        )
        assert r.status_code == 415, f"{content_type} must be rejected"
        assert "application/json" in r.json()["error"]


def test_post_rejects_missing_content_type(client: TestClient) -> None:
    """SE-01: a mutating request with no JSON Content-Type at all is rejected with 415."""
    r = client.request("POST", "/api/import", content=b'{"file_path": "/etc/passwd"}')
    assert r.status_code == 415


def test_put_rejects_non_json_content_type(client: TestClient, memory_factory) -> None:
    """SE-01: PUT with a form Content-Type is rejected with 415."""
    mem = memory_factory(content="content-type guard memory")
    r = client.put(
        f"/api/memories/{mem['id']}",
        content=b'{"content": "x"}',
        headers={"content-type": "text/plain"},
    )
    assert r.status_code == 415


def test_json_content_type_with_charset_accepted(client: TestClient) -> None:
    """SE-01: 'application/json; charset=utf-8' passes the Content-Type gate."""
    r = client.post(
        "/api/memories",
        content=b'{"content": "charset ok"}',
        headers={"content-type": "application/json; charset=utf-8"},
    )
    assert r.status_code == 201


def test_get_and_delete_unaffected_by_content_type_gate(client: TestClient, memory_factory) -> None:
    """SE-01: GET and DELETE (no body, preflighted anyway) bypass the JSON gate."""
    mem = memory_factory(content="delete without content type")
    assert client.get("/api/stats").status_code == 200
    assert client.delete(f"/api/memories/{mem['id']}").status_code == 200


# ---------------------------------------------------------------------------
# SE-04: unauthenticated /health liveness route
# ---------------------------------------------------------------------------


def test_health_route_open_without_auth(client_with_auth: TestClient) -> None:
    """SE-04: /health returns 200 with no token even when auth is enabled."""
    r = client_with_auth.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_health_route_reveals_no_data(client: TestClient, memory_factory) -> None:
    """SE-04: /health is a pure liveness probe — no memory data in the body."""
    memory_factory(content="secret memory content")
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


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
    import remind_me_mcp.config as _cfg
    import remind_me_mcp.importer as _importer_mod

    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db_conn_with_vec)
    monkeypatch.setattr(_cfg, "API_KEY", "disabled")

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

    vec_count = db_conn_with_vec.execute(
        "SELECT COUNT(*) FROM vec_chunks WHERE memory_rowid = ?", (rowid,)
    ).fetchone()[0]
    assert vec_count >= 1, "chunk vectors should exist after POST /api/memories"


def test_api_add_creates_chunk_vectors(db_conn_with_vec, mock_embedder, monkeypatch) -> None:
    """EMBD-01: Adding a memory links its chunk vectors to the memory via vec_chunks."""
    import remind_me_mcp.config as _cfg
    import remind_me_mcp.importer as _importer_mod

    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db_conn_with_vec)
    monkeypatch.setattr(_cfg, "API_KEY", "disabled")

    app = _build_api_app()
    client = TestClient(app)

    response = client.post("/api/memories", json={"content": "rowid match test memory"})
    assert response.status_code == 201

    mem_id = response.json()["id"]
    mem_rowid = db_conn_with_vec.execute(
        "SELECT rowid FROM memories WHERE id = ?", (mem_id,)
    ).fetchone()[0]
    # Every chunk vector for this memory is mapped through vec_chunks to a real
    # memories_vec row (no dangling references).
    chunk_vecs = db_conn_with_vec.execute(
        """SELECT mv.rowid FROM vec_chunks vc
           JOIN memories_vec mv ON mv.rowid = vc.vec_rowid
           WHERE vc.memory_rowid = ?""",
        (mem_rowid,),
    ).fetchall()
    assert len(chunk_vecs) >= 1, "memory should own at least one chunk vector"


def test_api_update_content_regenerates_embedding(db_conn_with_vec, mock_embedder, monkeypatch) -> None:
    """EMBD-02: PUT /api/memories/{id} with new content updates the memories_vec row."""
    import remind_me_mcp.config as _cfg
    import remind_me_mcp.importer as _importer_mod

    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db_conn_with_vec)
    monkeypatch.setattr(_cfg, "API_KEY", "disabled")

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

    # The memory should still own chunk vectors after re-embedding its new content
    # (old chunks are replaced; the parent link in vec_chunks persists).
    chunk_count = db_conn_with_vec.execute(
        "SELECT COUNT(*) FROM vec_chunks WHERE memory_rowid = ?", (mem_rowid,)
    ).fetchone()[0]
    assert chunk_count >= 1, "memory should still have chunk vectors after content update"


def test_api_update_no_content_preserves_embedding(db_conn_with_vec, mock_embedder, monkeypatch) -> None:
    """EMBD-02: PUT /api/memories/{id} with only tags does NOT alter the memories_vec row."""
    import remind_me_mcp.config as _cfg
    import remind_me_mcp.importer as _importer_mod

    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db_conn_with_vec)
    monkeypatch.setattr(_cfg, "API_KEY", "disabled")

    app = _build_api_app()
    client = TestClient(app)

    # Create initial memory (with embedding)
    create_resp = client.post("/api/memories", json={"content": "tag-only update test memory"})
    assert create_resp.status_code == 201
    mem_id = create_resp.json()["id"]

    mem_rowid = db_conn_with_vec.execute(
        "SELECT rowid FROM memories WHERE id = ?", (mem_id,)
    ).fetchone()[0]

    # Verify initial chunk vectors exist
    initial_vecs = db_conn_with_vec.execute(
        "SELECT vec_rowid FROM vec_chunks WHERE memory_rowid = ? ORDER BY chunk_ix", (mem_rowid,)
    ).fetchall()
    assert initial_vecs, "Initial embedding should exist after POST"

    # Update with only tags (no content)
    update_resp = client.put(f"/api/memories/{mem_id}", json={"tags": ["new-tag"]})
    assert update_resp.status_code == 200

    # Chunk vectors should be preserved and unchanged (no re-embed on tag-only update)
    after_vecs = db_conn_with_vec.execute(
        "SELECT vec_rowid FROM vec_chunks WHERE memory_rowid = ? ORDER BY chunk_ix", (mem_rowid,)
    ).fetchall()
    assert after_vecs == initial_vecs, "chunk vectors should be unchanged on tag-only update"


def test_rest_and_mcp_memories_equally_findable_by_semantic_search(
    db_conn_with_vec, mock_embedder, monkeypatch
) -> None:
    """Parity: REST API and MCP tool memories are equally retrievable via _semantic_search."""
    import remind_me_mcp.config as _cfg
    import remind_me_mcp.importer as _importer_mod
    from remind_me_mcp.db import _embed_and_store, _make_id, _now_iso, _semantic_search

    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db_conn_with_vec)
    monkeypatch.setattr(_cfg, "API_KEY", "disabled")

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
    _embed_and_store(mcp_mem_id, mcp_content)

    # Both memories should appear in semantic search results
    results = _semantic_search("parity test", limit=10)
    result_ids = {r["id"] for r in results}

    assert rest_mem_id in result_ids, f"REST memory {rest_mem_id} not found in semantic search results: {result_ids}"
    assert mcp_mem_id in result_ids, f"MCP memory {mcp_mem_id} not found in semantic search results: {result_ids}"


# ---------------------------------------------------------------------------
# Branch coverage — targeted tests for uncovered api.py lines (Phase 09-02)
# ---------------------------------------------------------------------------


def test_api_stats_malformed_tags(client: TestClient, db_conn) -> None:
    """GET /api/stats silently skips memories with malformed JSON in the tags column.

    Covers api.py: the except (json.JSONDecodeError, TypeError) branch inside the
    tag aggregation loop in api_stats. Also covers the loop body (all_tags increment)
    by inserting a memory with valid tags alongside the malformed one.
    """
    import json as _json

    now = "2026-01-01T00:00:00Z"
    # Insert a memory with malformed tags (triggers the except branch)
    db_conn.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("malformed-tags-id", "Malformed tags memory", "general", "NOT_VALID_JSON", "manual", _json.dumps({}), now, now),
    )
    # Insert a memory with valid tags (covers the loop body — all_tags increment)
    db_conn.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("valid-tags-id", "Valid tags memory", "general", _json.dumps(["python", "testing"]), "manual", _json.dumps({}), now, now),
    )
    db_conn.commit()

    response = client.get("/api/stats")
    assert response.status_code == 200
    data = response.json()
    # Should not crash — malformed tags row is silently skipped
    assert "total" in data
    assert data["total"] >= 2
    # Valid tags memory should appear in tag counts
    assert "python" in data["tags"]


def test_api_list_filter_by_source(client: TestClient) -> None:
    """GET /api/memories?source=manual filters results by source field.

    Covers api.py lines 168-169: the 'if src := params.get("source")' branch
    in api_list that adds source filtering to the SQL WHERE clause.
    """
    # Create memories with different sources
    create_manual = client.post(
        "/api/memories",
        json={"content": "Manual source memory for filter test", "source": "manual"},
    )
    assert create_manual.status_code == 201

    response = client.get("/api/memories?source=manual")
    assert response.status_code == 200
    data = response.json()
    assert data["count"] >= 1
    for mem in data["memories"]:
        assert mem["source"] == "manual"


def test_api_search_with_category_filter(client: TestClient) -> None:
    """GET /api/memories/search?q=term&category=cat returns only the matching category.

    Covers api.py lines 219-220: the 'if cat := params.get("category")' branch
    that post-filters FTS5 results by category.
    """
    client.post("/api/memories", json={"content": "category filter alpha keyword", "category": "alpha"})
    client.post("/api/memories", json={"content": "category filter beta keyword", "category": "beta"})

    response = client.get("/api/memories/search?q=keyword&category=alpha")
    assert response.status_code == 200
    data = response.json()
    assert data["count"] >= 1
    for mem in data["memories"]:
        assert mem["category"] == "alpha"


def test_api_search_with_tag_filter(client: TestClient) -> None:
    """GET /api/memories/search?q=term&tags=tag filters search results by tag.

    Covers api.py lines 225-228: the 'if tag_param := params.get("tags")' branch
    that post-filters FTS5 results to only include memories containing the tag.
    """
    client.post("/api/memories", json={"content": "tagged search target unique xyz", "tags": ["special-tag"]})
    client.post("/api/memories", json={"content": "untagged search target unique xyz", "tags": []})

    response = client.get("/api/memories/search?q=xyz&tags=special-tag")
    assert response.status_code == 200
    data = response.json()
    assert data["count"] >= 1
    for mem in data["memories"]:
        assert "special-tag" in mem["tags"]


def test_api_update_invalid_json_body(client: TestClient, memory_factory) -> None:
    """PUT /api/memories/{id} with non-JSON body returns 400.

    Covers api.py: the except (json.JSONDecodeError, TypeError, ValueError) branch
    in api_update that handles malformed request bodies.
    """
    mem = memory_factory(content="Memory for invalid-JSON update test")
    response = client.put(
        f"/api/memories/{mem['id']}",
        content=b"not valid json body",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    data = response.json()
    assert "invalid json" in data["error"].lower()


def test_api_search_fts_error(client: TestClient) -> None:
    """GET /api/memories/search with a malformed FTS5 query returns 400 search error.

    Covers api.py: the except _sqlite3.OperationalError branch in api_search
    that handles queries which FTS5 cannot parse (e.g., unmatched quotes).
    """
    # FTS5 raises OperationalError on syntactically invalid queries like unmatched quotes
    response = client.get('/api/memories/search?q="unclosed+quote')
    assert response.status_code == 400
    data = response.json()
    assert "search error" in data["error"].lower()


def test_api_import_invalid_json_body(client: TestClient) -> None:
    """POST /api/import with non-JSON body returns 400.

    Covers api.py: the except (json.JSONDecodeError, TypeError, ValueError) branch
    in api_import that handles malformed request bodies.
    """
    response = client.post(
        "/api/import",
        content=b"not valid json body",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    data = response.json()
    assert "invalid json" in data["error"].lower()


def test_api_import_raises_oserror(client: TestClient, tmp_path: Path, monkeypatch) -> None:
    """POST /api/import logs and returns 400 when import_chat_file raises OSError.

    Covers api.py: the except (FileNotFoundError, OSError, ...) block in api_import
    that handles runtime errors during file import (lines 361-363).
    """
    import remind_me_mcp.api as _api_mod

    # Create a real file inside allowed roots so the path check passes
    chat_file = tmp_path / "broken.json"
    chat_file.write_text("{}")

    # Monkeypatch import_chat_file to raise OSError so we hit the exception handler
    def fake_import(*args, **kwargs):
        raise OSError("simulated read error")

    monkeypatch.setattr(_api_mod, "import_chat_file", fake_import)

    response = client.post("/api/import", json={"file_path": str(chat_file)})
    assert response.status_code == 400
    data = response.json()
    assert "import error" in data["error"].lower()


def test_api_update_metadata_only(client: TestClient, memory_factory) -> None:
    """PUT /api/memories/{id} with only a metadata dict updates metadata and returns 200.

    Covers api.py: the 'if "metadata" in body and body["metadata"] is not None' branch
    inside api_update that appends the metadata SET clause (lines 292-294).
    """
    mem = memory_factory(content="Memory for metadata-only update test")
    response = client.put(
        f"/api/memories/{mem['id']}",
        json={"metadata": {"project": "remind_me", "phase": "09"}},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["metadata"].get("project") == "remind_me"


# ---------------------------------------------------------------------------
# HY-06: garbage query parameters answer 400, not 500
# ---------------------------------------------------------------------------


def test_api_list_invalid_limit_returns_400(client: TestClient) -> None:
    """GET /api/memories?limit=garbage should be a client error, not a crash."""
    response = client.get("/api/memories", params={"limit": "garbage"})
    assert response.status_code == 400
    assert "limit" in response.json()["error"]


def test_api_list_invalid_offset_returns_400(client: TestClient) -> None:
    """GET /api/memories?offset=NaN should be a client error, not a crash."""
    response = client.get("/api/memories", params={"offset": "NaN"})
    assert response.status_code == 400
    assert "offset" in response.json()["error"]


def test_api_search_invalid_limit_returns_400(client: TestClient) -> None:
    """GET /api/memories/search?q=x&limit=zzz should be a client error."""
    response = client.get("/api/memories/search", params={"q": "x", "limit": "zzz"})
    assert response.status_code == 400
    assert "limit" in response.json()["error"]


def test_api_list_blank_limit_uses_default(client: TestClient, memory_factory) -> None:
    """A blank limit parameter falls back to the default instead of erroring."""
    memory_factory(content="Blank limit default test")
    response = client.get("/api/memories", params={"limit": ""})
    assert response.status_code == 200
    assert response.json()["total"] == 1
