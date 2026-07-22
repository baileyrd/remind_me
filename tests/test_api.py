"""
Integration tests for all Starlette HTTP API routes in remind_me_mcp.api.

Each test exercises a real route handler against the in-memory SQLite database
provided by the db_conn fixture. The Starlette TestClient handles async route
handlers synchronously — no async test code is needed.
"""

from __future__ import annotations

import sys
import tempfile
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
# GET /api/vitality
# ---------------------------------------------------------------------------


def test_api_vitality_empty(client: TestClient) -> None:
    """GET /api/vitality with no memories should return zeroed-out counts."""
    response = client.get("/api/vitality")
    assert response.status_code == 200
    data = response.json()
    assert data["total_memories"] == 0
    assert data["active_count"] == 0
    assert data["dormant_count"] == 0
    assert data["vault_health_score"] == "0%"
    assert set(data["vitality_buckets"]) == {
        "0.00-0.05",
        "0.05-0.25",
        "0.25-0.50",
        "0.50-0.75",
        "0.75+",
    }


def test_api_vitality_with_data(client: TestClient, memory_factory) -> None:
    """GET /api/vitality reflects fresh memories as active, in the top vitality bucket."""
    memory_factory(content="Fresh memory one")
    memory_factory(content="Fresh memory two")

    response = client.get("/api/vitality")
    assert response.status_code == 200
    data = response.json()
    assert data["total_memories"] == 2
    assert data["active_count"] == 2
    assert data["dormant_count"] == 0
    assert data["vitality_buckets"]["0.75+"] == 2


def test_api_vitality_matches_decay_distribution_by_memory_type(
    client: TestClient, memory_factory
) -> None:
    memory_factory(content="A decision memory", memory_type="decision")
    memory_factory(content="An action item", memory_type="action_item")

    response = client.get("/api/vitality")
    data = response.json()
    assert data["decay_distribution"]["decision"] == 1
    assert data["decay_distribution"]["action_item"] == 1


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
# POST /api/memories/bulk/delete (issue #16)
# ---------------------------------------------------------------------------


def test_api_bulk_delete_multiple(client: TestClient, memory_factory) -> None:
    mem1 = memory_factory(content="bulk delete target one")
    mem2 = memory_factory(content="bulk delete target two")

    response = client.post("/api/memories/bulk/delete", json={"ids": [mem1["id"], mem2["id"]]})
    assert response.status_code == 200
    data = response.json()
    assert set(data["deleted"]) == {mem1["id"], mem2["id"]}
    assert data["not_found"] == []

    assert client.get(f"/api/memories/{mem1['id']}").status_code == 404
    assert client.get(f"/api/memories/{mem2['id']}").status_code == 404


def test_api_bulk_delete_partial_not_found(client: TestClient, memory_factory) -> None:
    mem = memory_factory(content="exists for bulk delete")
    response = client.post(
        "/api/memories/bulk/delete", json={"ids": [mem["id"], "nonexistent-id"]}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["deleted"] == [mem["id"]]
    assert data["not_found"] == ["nonexistent-id"]


def test_api_bulk_delete_empty_ids_rejected(client: TestClient) -> None:
    response = client.post("/api/memories/bulk/delete", json={"ids": []})
    assert response.status_code == 400


def test_api_bulk_delete_missing_ids_rejected(client: TestClient) -> None:
    response = client.post("/api/memories/bulk/delete", json={})
    assert response.status_code == 400


def test_api_bulk_delete_too_many_ids_rejected(client: TestClient) -> None:
    response = client.post("/api/memories/bulk/delete", json={"ids": [f"id{i}" for i in range(201)]})
    assert response.status_code == 400


def test_api_bulk_delete_non_string_id_rejected(client: TestClient) -> None:
    response = client.post("/api/memories/bulk/delete", json={"ids": ["ok-id", 123]})
    assert response.status_code == 400


def test_api_bulk_delete_soft_deletes_when_sync_enabled(
    client: TestClient, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors single-delete: soft delete (tombstone) when sync is configured."""
    import remind_me_mcp.api as api_mod

    monkeypatch.setattr(api_mod, "SYNC_ENABLED", True)
    mem = memory_factory(content="bulk soft delete target")

    response = client.post("/api/memories/bulk/delete", json={"ids": [mem["id"]]})
    assert response.status_code == 200
    assert response.json()["deleted"] == [mem["id"]]

    # Still present in the DB (tombstoned), just excluded from normal reads.
    from remind_me_mcp.db import _get_db
    row = _get_db().execute(
        "SELECT deleted_at FROM memories WHERE id = ?", (mem["id"],)
    ).fetchone()
    assert row["deleted_at"] is not None


# ---------------------------------------------------------------------------
# POST /api/memories/bulk/tag (issue #16)
# ---------------------------------------------------------------------------


def test_api_bulk_tag_add_mode(client: TestClient, memory_factory) -> None:
    mem1 = memory_factory(content="tag target one", tags=["existing"])
    mem2 = memory_factory(content="tag target two", tags=[])

    response = client.post(
        "/api/memories/bulk/tag",
        json={"ids": [mem1["id"], mem2["id"]], "tags": ["new-tag"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert set(data["updated"]) == {mem1["id"], mem2["id"]}

    r1 = client.get(f"/api/memories/{mem1['id']}").json()
    r2 = client.get(f"/api/memories/{mem2['id']}").json()
    assert r1["tags"] == ["existing", "new-tag"]
    assert r2["tags"] == ["new-tag"]


def test_api_bulk_tag_set_mode_replaces_wholesale(client: TestClient, memory_factory) -> None:
    mem = memory_factory(content="tag set target", tags=["old-a", "old-b"])
    response = client.post(
        "/api/memories/bulk/tag",
        json={"ids": [mem["id"]], "tags": ["replacement"], "mode": "set"},
    )
    assert response.status_code == 200
    row = client.get(f"/api/memories/{mem['id']}").json()
    assert row["tags"] == ["replacement"]


def test_api_bulk_tag_remove_mode(client: TestClient, memory_factory) -> None:
    mem = memory_factory(content="tag remove target", tags=["keep", "drop"])
    response = client.post(
        "/api/memories/bulk/tag",
        json={"ids": [mem["id"]], "tags": ["drop"], "mode": "remove"},
    )
    assert response.status_code == 200
    row = client.get(f"/api/memories/{mem['id']}").json()
    assert row["tags"] == ["keep"]


def test_api_bulk_tag_add_deduplicates(client: TestClient, memory_factory) -> None:
    mem = memory_factory(content="tag dedup target", tags=["shared"])
    response = client.post(
        "/api/memories/bulk/tag",
        json={"ids": [mem["id"]], "tags": ["shared"]},
    )
    assert response.status_code == 200
    row = client.get(f"/api/memories/{mem['id']}").json()
    assert row["tags"] == ["shared"]


def test_api_bulk_tag_partial_not_found(client: TestClient, memory_factory) -> None:
    mem = memory_factory(content="tag exists target")
    response = client.post(
        "/api/memories/bulk/tag",
        json={"ids": [mem["id"], "nonexistent-id"], "tags": ["x"]},
    )
    data = response.json()
    assert data["updated"] == [mem["id"]]
    assert data["not_found"] == ["nonexistent-id"]


def test_api_bulk_tag_invalid_mode_rejected(client: TestClient, memory_factory) -> None:
    mem = memory_factory(content="tag invalid mode target")
    response = client.post(
        "/api/memories/bulk/tag",
        json={"ids": [mem["id"]], "tags": ["x"], "mode": "bogus"},
    )
    assert response.status_code == 400


def test_api_bulk_tag_empty_tags_rejected(client: TestClient, memory_factory) -> None:
    mem = memory_factory(content="tag empty target")
    response = client.post("/api/memories/bulk/tag", json={"ids": [mem["id"]], "tags": []})
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/memories/bulk/reclassify (issue #16)
# ---------------------------------------------------------------------------


def test_api_bulk_reclassify_applies_type_and_decay_rate(client: TestClient, memory_factory) -> None:
    mem = memory_factory(content="reclassify target", memory_type="unclassified")
    response = client.post(
        "/api/memories/bulk/reclassify",
        json={"classifications": [{"memory_id": mem["id"], "memory_type": "decision"}]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["updated"] == 1
    assert data["not_found"] == []
    assert data["total"] == 1

    from remind_me_mcp.vitality import DECAY_RATES
    row = client.get(f"/api/memories/{mem['id']}").json()
    assert row["memory_type"] == "decision"
    assert row["decay_rate"] == DECAY_RATES["decision"]


def test_api_bulk_reclassify_multiple_different_types(client: TestClient, memory_factory) -> None:
    mem1 = memory_factory(content="reclassify multi one")
    mem2 = memory_factory(content="reclassify multi two")
    response = client.post(
        "/api/memories/bulk/reclassify",
        json={
            "classifications": [
                {"memory_id": mem1["id"], "memory_type": "fact"},
                {"memory_id": mem2["id"], "memory_type": "blocker"},
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["updated"] == 2

    r1 = client.get(f"/api/memories/{mem1['id']}").json()
    r2 = client.get(f"/api/memories/{mem2['id']}").json()
    assert r1["memory_type"] == "fact"
    assert r2["memory_type"] == "blocker"


def test_api_bulk_reclassify_partial_not_found(client: TestClient, memory_factory) -> None:
    mem = memory_factory(content="reclassify exists target")
    response = client.post(
        "/api/memories/bulk/reclassify",
        json={
            "classifications": [
                {"memory_id": mem["id"], "memory_type": "insight"},
                {"memory_id": "nonexistent-id", "memory_type": "fact"},
            ]
        },
    )
    data = response.json()
    assert data["updated"] == 1
    assert data["not_found"] == ["nonexistent-id"]
    assert data["total"] == 2


def test_api_bulk_reclassify_empty_rejected(client: TestClient) -> None:
    response = client.post("/api/memories/bulk/reclassify", json={"classifications": []})
    assert response.status_code == 400


def test_api_bulk_reclassify_malformed_entry_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/memories/bulk/reclassify",
        json={"classifications": [{"memory_id": "abc"}]},  # missing memory_type
    )
    assert response.status_code == 400


def test_api_bulk_reclassify_requires_auth(client_with_auth: TestClient, memory_factory) -> None:
    """Bulk routes are covered by the same /api/ auth gate as everything else."""
    response = client_with_auth.post(
        "/api/memories/bulk/reclassify",
        json={"classifications": [{"memory_id": "abc", "memory_type": "fact"}]},
    )
    assert response.status_code == 401


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


def test_api_search_pagination(client: TestClient, memory_factory) -> None:
    """GET /api/memories/search supports limit/offset pagination (issue #16)."""
    for i in range(5):
        memory_factory(content=f"paginated search term entry {i}")

    response = client.get("/api/memories/search?q=paginated&limit=2&offset=0")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 5
    assert data["count"] == 2
    assert data["offset"] == 0
    assert data["limit"] == 2
    assert data["has_more"] is True

    response = client.get("/api/memories/search?q=paginated&limit=2&offset=4")
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["has_more"] is False


def test_api_search_pagination_no_overlap_across_pages(client: TestClient, memory_factory) -> None:
    """Consecutive pages return disjoint results (a real offset, not just a cap)."""
    for i in range(4):
        memory_factory(content=f"pageset unique entry number {i}")

    page1 = client.get("/api/memories/search?q=pageset&limit=2&offset=0").json()
    page2 = client.get("/api/memories/search?q=pageset&limit=2&offset=2").json()
    ids1 = {m["id"] for m in page1["memories"]}
    ids2 = {m["id"] for m in page2["memories"]}
    assert ids1.isdisjoint(ids2)
    assert len(ids1 | ids2) == 4


def test_api_search_entity_not_found_includes_pagination_fields(client: TestClient) -> None:
    """The entity-not-found early return still carries the pagination shape."""
    response = client.get("/api/memories/search", params={"q": "entity:NoSuchEntity"})
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0
    assert data["has_more"] is False
    assert "offset" in data
    assert "limit" in data


def test_api_search_default_pagination_fields_present(client: TestClient, memory_factory) -> None:
    """Every search response carries the standard pagination envelope."""
    memory_factory(content="a simple searchable memory")
    response = client.get("/api/memories/search?q=searchable")
    data = response.json()
    assert {"total", "count", "offset", "limit", "has_more", "memories"} <= data.keys()


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
    inside the system temp dir (which is in the patched IMPORT_ROOTS, see
    conftest.py) that does not exist. Built from tempfile.gettempdir() rather
    than a hardcoded "/tmp" so it resolves inside IMPORT_ROOTS on Windows too.
    """
    missing = Path(tempfile.gettempdir()) / "nonexistent_remind_me_test_file.json"
    response = client.post("/api/import", json={"file_path": str(missing)})
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


def _seed_api_graph(db_conn, memory_factory) -> dict:
    """One memory linked to one entity (FT-06 API tests)."""
    from remind_me_mcp.db import _link_memory_entity, _upsert_entity

    mem = memory_factory(content="Graph endpoint memory")
    eid = _upsert_entity(db_conn, "Bailey Robertson", kind="person", aliases=["Bailey"])
    _link_memory_entity(db_conn, mem["id"], eid)
    db_conn.commit()
    return mem


def test_api_export_includes_graph_by_default(client: TestClient, db_conn, memory_factory) -> None:
    """FT-06: GET /api/export appends entity/memory_entity records by default."""
    mem = _seed_api_graph(db_conn, memory_factory)
    records = client.get("/api/export").json()
    entities = [r for r in records if r.get("record_type") == "entity"]
    links = [r for r in records if r.get("record_type") == "memory_entity"]
    assert len(entities) == 1
    assert entities[0]["name"] == "Bailey Robertson"
    assert entities[0]["aliases"] == ["Bailey"]
    assert len(links) == 1
    assert links[0]["memory_id"] == mem["id"]
    # Memory records are unchanged: first record, no record_type, role marker.
    assert "record_type" not in records[0]
    assert records[0]["role"] == "assistant"


def test_api_export_include_graph_false(client: TestClient, db_conn, memory_factory) -> None:
    """FT-06: include_graph=false produces a memories-only export."""
    _seed_api_graph(db_conn, memory_factory)
    records = client.get("/api/export?include_graph=false").json()
    assert len(records) == 1
    assert all("record_type" not in r for r in records)


def test_api_export_import_graph_round_trip(
    client: TestClient, db_conn, memory_factory, tmp_path: Path
) -> None:
    """End-to-end FT-06: export -> wipe graph -> POST /api/import restores it
    (links re-attach because the memories kept their original ids)."""
    mem = _seed_api_graph(db_conn, memory_factory)
    backup = tmp_path / "http_graph_round_trip.json"
    backup.write_text(client.get("/api/export").text)

    db_conn.execute("DELETE FROM memory_entities")
    db_conn.execute("DELETE FROM entities")
    db_conn.commit()

    response = client.post("/api/import", json={"file_path": str(backup)})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["entities_restored"] == 1
    assert data["links_restored"] == 1
    assert data["links_skipped_dangling"] == 0
    row = db_conn.execute(
        "SELECT entity_id FROM memory_entities WHERE memory_id = ?", (mem["id"],)
    ).fetchone()
    assert row is not None


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
    if sys.platform != "win32":
        # POSIX mode bits aren't meaningful on Windows — see test_oauth.py's
        # test_state_file_permissions for the full rationale.
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


# ---------------------------------------------------------------------------
# FT-04 part 2: GET /api/entity and entity: search syntax parity
# ---------------------------------------------------------------------------


def _api_mention(db_conn, memory_id: str, name: str, kind=None, aliases=None) -> str:
    """Upsert an entity, link it to *memory_id*, commit, return the entity id."""
    from remind_me_mcp.db import _link_memory_entity, _upsert_entity

    eid = _upsert_entity(db_conn, name, kind, aliases)
    _link_memory_entity(db_conn, memory_id, eid)
    db_conn.commit()
    return eid


def test_api_entity_lookup(client: TestClient, db_conn, memory_factory) -> None:
    """GET /api/entity?name= returns the entity, its facts, and linked memories."""
    mem = memory_factory(content="Tailscale config notes")
    _api_mention(db_conn, mem["id"], "Tailscale", kind="tool", aliases=["ts"])
    memory_factory(
        content="remind_me syncs over Tailscale",
        subject="remind_me", predicate="syncs over", object="Tailscale",
    )

    response = client.get("/api/entity", params={"name": "Tailscale"})
    assert response.status_code == 200
    data = response.json()
    assert data["entity"]["name"] == "Tailscale"
    assert data["entity"]["kind"] == "tool"
    assert data["entity"]["aliases"] == ["ts"]
    assert [m["id"] for m in data["memories"]] == [mem["id"]]
    assert data["total_linked_memories"] == 1
    assert [f["content"] for f in data["facts"]] == ["remind_me syncs over Tailscale"]


def test_api_entity_lookup_by_alias(client: TestClient, db_conn, memory_factory) -> None:
    mem = memory_factory(content="Bailey's notes")
    _api_mention(db_conn, mem["id"], "Bailey Robertson", aliases=["BR"])

    response = client.get("/api/entity", params={"name": "br"})
    assert response.status_code == 200
    assert response.json()["entity"]["name"] == "Bailey Robertson"


def test_api_entity_not_found(client: TestClient, db_conn) -> None:
    response = client.get("/api/entity", params={"name": "Ghost"})
    assert response.status_code == 404
    assert "No entity found" in response.json()["error"]


def test_api_entity_missing_name(client: TestClient, db_conn) -> None:
    response = client.get("/api/entity")
    assert response.status_code == 400
    assert "name" in response.json()["error"]


def test_api_entity_requires_auth(client_with_auth: TestClient) -> None:
    """GET /api/entity sits under /api/ so bearer auth gates it (SE-05)."""
    response = client_with_auth.get("/api/entity", params={"name": "x"})
    assert response.status_code == 401


def test_api_search_entity_only_lists_linked_memories(
    client: TestClient, db_conn, memory_factory
) -> None:
    """q='entity:NAME' with no free text lists the entity's memories."""
    linked = memory_factory(content="Tailscale handles the mesh")
    memory_factory(content="unrelated memory")
    stale = memory_factory(content="stale mention", superseded_by="newer")
    eid = _api_mention(db_conn, linked["id"], "Tailscale")
    _api_mention(db_conn, stale["id"], "Tailscale")
    assert eid

    response = client.get("/api/memories/search", params={"q": "entity:Tailscale"})
    assert response.status_code == 200
    data = response.json()
    assert [m["id"] for m in data["memories"]] == [linked["id"]]


def test_api_search_entity_with_free_text(
    client: TestClient, db_conn, memory_factory
) -> None:
    """entity: filter composes with the FTS remainder."""
    match = memory_factory(content="Tailscale mesh networking guide")
    other = memory_factory(content="mesh networking with something else")
    _api_mention(db_conn, match["id"], "Tailscale")

    response = client.get(
        "/api/memories/search", params={"q": 'entity:"Tailscale" mesh'}
    )
    assert response.status_code == 200
    ids = [m["id"] for m in response.json()["memories"]]
    assert ids == [match["id"]]
    assert other["id"] not in ids


def test_api_search_entity_unresolved_returns_empty_with_message(
    client: TestClient, db_conn, memory_factory
) -> None:
    memory_factory(content="Ghost stories are fun")
    response = client.get("/api/memories/search", params={"q": "entity:Ghost"})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 0
    assert data["memories"] == []
    assert "No entity found" in data["message"]


# ---------------------------------------------------------------------------
# Wiki (FT-08) — read-only REST surface
# ---------------------------------------------------------------------------


@pytest.fixture()
def wiki_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point config.WIKI_DIR at a fresh temp directory (mirrors test_wiki.py)."""
    import remind_me_mcp.config as _cfg

    d = tmp_path / "wiki"
    monkeypatch.setattr(_cfg, "WIKI_DIR", d)
    return d


def test_api_wiki_pages_empty(client: TestClient, db_conn, wiki_dir: Path) -> None:
    r = client.get("/api/wiki")
    assert r.status_code == 200
    assert r.json() == {"count": 0, "pages": []}


def test_api_wiki_pages_lists_catalogue(client: TestClient, db_conn, wiki_dir: Path) -> None:
    from remind_me_mcp import wiki

    wiki.write_page("Tailscale", "A mesh VPN built on WireGuard.")
    wiki.write_page("Postgres", "A relational database.")

    r = client.get("/api/wiki")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 2
    titles = {p["title"] for p in data["pages"]}
    assert titles == {"Tailscale", "Postgres"}
    # Sorted title, case-insensitive, matching wiki.list_pages().
    assert [p["title"] for p in data["pages"]] == ["Postgres", "Tailscale"]


def test_api_wiki_page_read_with_links_and_backlinks(
    client: TestClient, db_conn, wiki_dir: Path
) -> None:
    from remind_me_mcp import wiki

    wiki.write_page("Networking", "Overview. See [[Tailscale]] for the VPN.")
    wiki.write_page("Tailscale", "A mesh VPN. Part of [[Networking]].")

    r = client.get("/api/wiki/tailscale")
    assert r.status_code == 200
    data = r.json()
    assert data["title"] == "Tailscale"
    assert data["slug"] == "tailscale"
    assert "A mesh VPN" in data["content"]
    assert [ln["title"] for ln in data["links"]] == ["Networking"]
    assert [bl["title"] for bl in data["backlinks"]] == ["Networking"]


def test_api_wiki_page_accepts_title_not_just_slug(
    client: TestClient, db_conn, wiki_dir: Path
) -> None:
    """The path segment is resolved the same way as remind_me_wiki_read: any
    casing/punctuation variant of the title slugifies to the same page."""
    from remind_me_mcp import wiki

    wiki.write_page("VLAN Setup", "Notes.")
    r = client.get("/api/wiki/VLAN%20Setup")
    assert r.status_code == 200
    assert r.json()["slug"] == "vlan-setup"


def test_api_wiki_page_not_found(client: TestClient, db_conn, wiki_dir: Path) -> None:
    r = client.get("/api/wiki/does-not-exist")
    assert r.status_code == 404
    assert "not found" in r.json()["error"].lower()


def test_api_wiki_search_finds_match(client: TestClient, db_conn, wiki_dir: Path) -> None:
    from remind_me_mcp import wiki

    wiki.write_page("Tailscale", "A mesh VPN built on WireGuard.")
    wiki.write_page("Postgres", "A relational database.")

    r = client.get("/api/wiki/search", params={"q": "WireGuard"})
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    assert data["results"][0]["title"] == "Tailscale"


def test_api_wiki_search_no_query(client: TestClient, wiki_dir: Path) -> None:
    r = client.get("/api/wiki/search")
    assert r.status_code == 400
    assert "Missing 'q'" in r.json()["error"]


def test_api_wiki_search_no_results(client: TestClient, db_conn, wiki_dir: Path) -> None:
    from remind_me_mcp import wiki

    wiki.write_page("Tailscale", "A mesh VPN.")
    r = client.get("/api/wiki/search", params={"q": "nonexistentxyz"})
    assert r.status_code == 200
    assert r.json() == {"count": 0, "results": []}


def test_api_wiki_load_concatenates_pages(client: TestClient, db_conn, wiki_dir: Path) -> None:
    from remind_me_mcp import wiki

    wiki.write_page("A", "First page body.")
    wiki.write_page("B", "Second page body.")

    r = client.get("/api/wiki/load")
    assert r.status_code == 200
    data = r.json()
    assert data["pages_included"] == 2
    assert data["pages_omitted"] == 0
    assert "First page body." in data["content"]
    assert "Second page body." in data["content"]


def test_api_wiki_load_respects_token_budget(client: TestClient, db_conn, wiki_dir: Path) -> None:
    from remind_me_mcp import wiki

    wiki.write_page("Page One", "x" * 400)
    wiki.write_page("Page Two", "y" * 400)

    r = client.get("/api/wiki/load", params={"token_budget": 120, "include_index": "false"})
    assert r.status_code == 200
    data = r.json()
    assert data["pages_included"] == 1
    assert data["pages_omitted"] == 1


def test_api_wiki_load_zero_budget_is_unlimited(
    client: TestClient, db_conn, wiki_dir: Path
) -> None:
    from remind_me_mcp import wiki

    wiki.write_page("Page One", "x" * 400)
    wiki.write_page("Page Two", "y" * 400)

    r = client.get("/api/wiki/load", params={"token_budget": 0})
    assert r.status_code == 200
    assert r.json()["pages_omitted"] == 0


def test_api_wiki_load_invalid_token_budget(client: TestClient, wiki_dir: Path) -> None:
    r = client.get("/api/wiki/load", params={"token_budget": "abc"})
    assert r.status_code == 400


def test_api_wiki_status_reports_pages_and_pending_compile(
    client: TestClient, db_conn, wiki_dir: Path, memory_factory
) -> None:
    from remind_me_mcp import wiki

    wiki.write_page("Tailscale", "A mesh VPN.")
    memory_factory(content="A raw memory not yet folded into the wiki")

    r = client.get("/api/wiki/status")
    assert r.status_code == 200
    data = r.json()
    assert data["pages"] == 1
    assert data["pending_compile"] == 1


def test_api_wiki_endpoints_require_auth(client_with_auth: TestClient, wiki_dir: Path) -> None:
    """SE-01: the wiki routes are gated by the same /api/ bearer middleware."""
    assert client_with_auth.get("/api/wiki").status_code == 401
    assert client_with_auth.get("/api/wiki/search", params={"q": "x"}).status_code == 401
    assert client_with_auth.get("/api/wiki/load").status_code == 401
    assert client_with_auth.get("/api/wiki/status").status_code == 401
    assert client_with_auth.get("/api/wiki/some-page").status_code == 401


# ---------------------------------------------------------------------------
# Tombstone propagation (gap #11) — soft vs hard delete, read-path exclusion
# ---------------------------------------------------------------------------


def test_api_delete_hard_deletes_when_sync_disabled(
    client: TestClient, db_conn, memory_factory
) -> None:
    """Default (no sync configured): DELETE truly removes the row, exactly
    as before this feature."""
    mem = memory_factory(content="hard delete via REST")
    response = client.delete(f"/api/memories/{mem['id']}")
    assert response.status_code == 200

    row = db_conn.execute("SELECT * FROM memories WHERE id = ?", (mem["id"],)).fetchone()
    assert row is None


def test_api_delete_soft_deletes_when_sync_enabled(
    client: TestClient, db_conn, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With sync configured: DELETE tombstones the row instead of removing
    it, so the deletion can propagate."""
    import remind_me_mcp.api as api_mod

    monkeypatch.setattr(api_mod, "SYNC_ENABLED", True)

    mem = memory_factory(content="soft delete via REST")
    response = client.delete(f"/api/memories/{mem['id']}")
    assert response.status_code == 200

    row = db_conn.execute("SELECT * FROM memories WHERE id = ?", (mem["id"],)).fetchone()
    assert row is not None
    assert row["deleted_at"] is not None

    # But it's gone from every normal read.
    assert client.get(f"/api/memories/{mem['id']}").status_code == 404


def test_api_delete_already_soft_deleted_returns_404(
    client: TestClient, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import remind_me_mcp.api as api_mod

    monkeypatch.setattr(api_mod, "SYNC_ENABLED", True)
    mem = memory_factory(content="delete twice via REST")
    assert client.delete(f"/api/memories/{mem['id']}").status_code == 200
    assert client.delete(f"/api/memories/{mem['id']}").status_code == 404


def test_api_list_excludes_soft_deleted_memories(
    client: TestClient, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import remind_me_mcp.api as api_mod

    monkeypatch.setattr(api_mod, "SYNC_ENABLED", True)
    mem = memory_factory(content="soft deleted, should not list")
    memory_factory(content="kept memory")
    client.delete(f"/api/memories/{mem['id']}")

    r = client.get("/api/memories")
    ids = {m["id"] for m in r.json()["memories"]}
    assert mem["id"] not in ids
    assert r.json()["total"] == 1


def test_api_search_excludes_soft_deleted_memories(
    client: TestClient, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import remind_me_mcp.api as api_mod

    monkeypatch.setattr(api_mod, "SYNC_ENABLED", True)
    mem = memory_factory(content="unique quokka widget memory")
    client.delete(f"/api/memories/{mem['id']}")

    r = client.get("/api/memories/search", params={"q": "quokka widget"})
    assert r.status_code == 200
    ids = {m["id"] for m in r.json()["memories"]}
    assert mem["id"] not in ids


def test_api_stats_excludes_soft_deleted_memories_from_total(
    client: TestClient, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import remind_me_mcp.api as api_mod

    monkeypatch.setattr(api_mod, "SYNC_ENABLED", True)
    mem = memory_factory(content="soft deleted, should not count")
    memory_factory(content="kept memory")
    client.delete(f"/api/memories/{mem['id']}")

    r = client.get("/api/stats")
    assert r.json()["total"] == 1


def test_api_update_excludes_soft_deleted_memories(
    client: TestClient, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import remind_me_mcp.api as api_mod

    monkeypatch.setattr(api_mod, "SYNC_ENABLED", True)
    mem = memory_factory(content="deleted then PATCHed?")
    client.delete(f"/api/memories/{mem['id']}")

    r = client.patch(f"/api/memories/{mem['id']}", json={"content": "resurrected"})
    assert r.status_code == 404
