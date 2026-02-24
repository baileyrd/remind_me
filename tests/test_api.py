"""
Integration tests for all Starlette HTTP API routes in remind_me_mcp.api.

Each test exercises a real route handler against the in-memory SQLite database
provided by the db_conn fixture. The Starlette TestClient handles async route
handlers synchronously — no async test code is needed.
"""

from __future__ import annotations

import json
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
    """
    import remind_me_mcp.importer as _importer_mod

    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db_conn)

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
    """POST /api/import with a nonexistent file path should return 400 with not found message."""
    response = client.post("/api/import", json={"file_path": "/nonexistent/path/file.json"})
    assert response.status_code == 400
    data = response.json()
    assert "not found" in data["error"].lower()


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
