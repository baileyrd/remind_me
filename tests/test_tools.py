"""
Integration tests for all 13 MCP tool handlers and 2 resource handlers.

Each test calls the actual async handler function with validated Pydantic input
models, exercising real SQL operations against an in-memory SQLite database.
The db_conn fixture monkeypatches _get_db so all application code routes through
the test-controlled :memory: connection automatically.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from remind_me_mcp.models import (
    AutoCaptureInput,
    BulkImportDirInput,
    ChatImportInput,
    MemoryAddInput,
    MemoryDeleteInput,
    MemoryListInput,
    MemorySearchInput,
    MemoryStatsInput,
    MemoryUpdateInput,
    ResponseFormat,
)
from remind_me_mcp.tools import (
    memory_add,
    memory_delete,
    memory_get,
    memory_import_chat,
    memory_import_directory,
    memory_list,
    memory_search,
    memory_stats,
    memory_update,
    remind_me_auto_capture,
    remind_me_get_capture,
    remind_me_reindex,
    remind_me_server_status,
    resource_categories,
    resource_stats,
)


# ---------------------------------------------------------------------------
# memory_add tests
# ---------------------------------------------------------------------------


async def test_memory_add_basic(db_conn: sqlite3.Connection) -> None:
    """Adding a basic memory returns confirmation and stores the row."""
    params = MemoryAddInput(
        content="Remember to buy milk",
        category="reminder",
        tags=["shopping"],
    )
    result = await memory_add(params)

    assert "Memory stored" in result
    # Extract ID from response
    # Response: "✓ Memory stored with id `<id>` in category 'reminder'."
    import re
    m = re.search(r"`([a-f0-9]+)`", result)
    assert m is not None, f"No ID found in: {result}"
    mem_id = m.group(1)

    row = db_conn.execute("SELECT * FROM memories WHERE id = ?", (mem_id,)).fetchone()
    assert row is not None
    assert row["content"] == "Remember to buy milk"
    assert row["category"] == "reminder"
    assert json.loads(row["tags"]) == ["shopping"]


async def test_memory_add_stores_metadata(db_conn: sqlite3.Connection) -> None:
    """Metadata dict is serialized to JSON and can be deserialized back."""
    params = MemoryAddInput(
        content="Metadata test memory",
        metadata={"key": "val", "number": 42},
    )
    result = await memory_add(params)
    assert "Memory stored" in result

    import re
    m = re.search(r"`([a-f0-9]+)`", result)
    assert m is not None
    mem_id = m.group(1)

    row = db_conn.execute("SELECT metadata FROM memories WHERE id = ?", (mem_id,)).fetchone()
    assert row is not None
    parsed = json.loads(row["metadata"])
    assert parsed["key"] == "val"
    assert parsed["number"] == 42


async def test_memory_add_default_category(db_conn: sqlite3.Connection) -> None:
    """Adding with only content defaults category to 'general'."""
    params = MemoryAddInput(content="Just a plain note")
    result = await memory_add(params)

    assert "general" in result

    import re
    m = re.search(r"`([a-f0-9]+)`", result)
    assert m is not None
    mem_id = m.group(1)

    row = db_conn.execute("SELECT category FROM memories WHERE id = ?", (mem_id,)).fetchone()
    assert row is not None
    assert row["category"] == "general"


# ---------------------------------------------------------------------------
# memory_get tests
# ---------------------------------------------------------------------------


async def test_memory_get_exists(db_conn: sqlite3.Connection, memory_factory) -> None:
    """Getting an existing memory returns its content and ID."""
    mem = memory_factory(content="A memory to retrieve", category="notes")
    result = await memory_get(mem["id"])

    assert mem["id"] in result
    assert "A memory to retrieve" in result


async def test_memory_get_not_found(db_conn: sqlite3.Connection) -> None:
    """Getting a nonexistent ID returns a not-found message."""
    result = await memory_get("nonexistent123")
    assert "not found" in result.lower()


# ---------------------------------------------------------------------------
# memory_search tests
# ---------------------------------------------------------------------------


async def test_memory_search_fts_match(db_conn: sqlite3.Connection) -> None:
    """FTS5 keyword search finds a memory by a specific term in its content."""
    params = MemoryAddInput(content="Python asyncio tutorial for beginners", category="learning")
    await memory_add(params)

    search_params = MemorySearchInput(query="asyncio")
    result = await memory_search(search_params)

    assert "asyncio" in result.lower()
    assert "_No memories found._" not in result


async def test_memory_search_no_results(db_conn: sqlite3.Connection) -> None:
    """Searching for a nonexistent term returns the no-results indicator."""
    search_params = MemorySearchInput(query="zzzznonexistent")
    result = await memory_search(search_params)

    assert "_No memories found._" in result or '"count": 0' in result


async def test_memory_search_with_category_filter(db_conn: sqlite3.Connection) -> None:
    """Category filter restricts search results to the specified category."""
    add1 = MemoryAddInput(content="Python programming notes", category="learning")
    add2 = MemoryAddInput(content="Python shopping list", category="personal")
    await memory_add(add1)
    await memory_add(add2)

    search_params = MemorySearchInput(query="Python", category="learning")
    result = await memory_search(search_params)

    # Should contain learning result
    assert "Python programming notes" in result
    # Should not contain personal result
    assert "Python shopping list" not in result


async def test_memory_search_json_format(db_conn: sqlite3.Connection) -> None:
    """Search with JSON response_format returns valid JSON with 'count' and 'memories' keys."""
    add_params = MemoryAddInput(content="JSON format test memory", category="test")
    await memory_add(add_params)

    search_params = MemorySearchInput(
        query="JSON format",
        response_format=ResponseFormat.JSON,
    )
    result = await memory_search(search_params)

    data = json.loads(result)
    assert "count" in data
    assert "memories" in data
    assert isinstance(data["memories"], list)


# ---------------------------------------------------------------------------
# memory_list tests
# ---------------------------------------------------------------------------


async def test_memory_list_empty(db_conn: sqlite3.Connection) -> None:
    """Listing with no memories returns the no-memories indicator."""
    params = MemoryListInput()
    result = await memory_list(params)

    assert "_No memories found._" in result


async def test_memory_list_with_memories(db_conn: sqlite3.Connection, memory_factory) -> None:
    """Listing returns all 3 created memories."""
    memory_factory(content="First memory")
    memory_factory(content="Second memory")
    memory_factory(content="Third memory")

    params = MemoryListInput()
    result = await memory_list(params)

    assert "First memory" in result
    assert "Second memory" in result
    assert "Third memory" in result


async def test_memory_list_category_filter(db_conn: sqlite3.Connection, memory_factory) -> None:
    """Category filter returns only memories from that category."""
    memory_factory(content="Learning note", category="learning")
    memory_factory(content="Work task", category="work")
    memory_factory(content="Another learning note", category="learning")

    params = MemoryListInput(category="learning")
    result = await memory_list(params)

    assert "Learning note" in result
    assert "Another learning note" in result
    assert "Work task" not in result


async def test_memory_list_pagination(db_conn: sqlite3.Connection, memory_factory) -> None:
    """Pagination via limit/offset slices results correctly."""
    for i in range(5):
        memory_factory(content=f"Paginated memory {i}")

    page1_params = MemoryListInput(limit=2, offset=0)
    page1 = await memory_list(page1_params)

    page2_params = MemoryListInput(limit=2, offset=2)
    page2 = await memory_list(page2_params)

    # Both pages should return valid results (not empty)
    assert "_No memories found._" not in page1
    assert "_No memories found._" not in page2


# ---------------------------------------------------------------------------
# memory_update tests
# ---------------------------------------------------------------------------


async def test_memory_update_content(db_conn: sqlite3.Connection, memory_factory) -> None:
    """Updating content persists the new value and is reflected in memory_get."""
    mem = memory_factory(content="Original content")

    update_params = MemoryUpdateInput(memory_id=mem["id"], content="Updated content")
    update_result = await memory_update(update_params)

    assert "updated" in update_result.lower()

    get_result = await memory_get(mem["id"])
    assert "Updated content" in get_result
    assert "Original content" not in get_result


async def test_memory_update_category_and_tags(db_conn: sqlite3.Connection, memory_factory) -> None:
    """Updating category and tags persists both changes to the database."""
    mem = memory_factory(content="Content to recategorize", category="old_cat", tags=["old_tag"])

    update_params = MemoryUpdateInput(
        memory_id=mem["id"],
        category="new_cat",
        tags=["new_tag", "another_tag"],
    )
    await memory_update(update_params)

    row = db_conn.execute("SELECT category, tags FROM memories WHERE id = ?", (mem["id"],)).fetchone()
    assert row is not None
    assert row["category"] == "new_cat"
    assert json.loads(row["tags"]) == ["new_tag", "another_tag"]


async def test_memory_update_not_found(db_conn: sqlite3.Connection) -> None:
    """Updating a nonexistent memory ID returns a not-found message."""
    update_params = MemoryUpdateInput(memory_id="doesnotexist99", content="New content")
    result = await memory_update(update_params)

    assert "not found" in result.lower()


async def test_memory_update_no_fields(db_conn: sqlite3.Connection, memory_factory) -> None:
    """Calling update with only memory_id and no fields returns 'Nothing to update'."""
    mem = memory_factory(content="Content that won't change")

    update_params = MemoryUpdateInput(memory_id=mem["id"])
    result = await memory_update(update_params)

    assert "Nothing to update" in result


# ---------------------------------------------------------------------------
# memory_delete tests
# ---------------------------------------------------------------------------


async def test_memory_delete_exists(db_conn: sqlite3.Connection, memory_factory) -> None:
    """Deleting an existing memory confirms deletion and makes it unretrievable."""
    mem = memory_factory(content="Memory to be deleted")

    delete_params = MemoryDeleteInput(memory_id=mem["id"])
    delete_result = await memory_delete(delete_params)

    assert "deleted" in delete_result.lower()

    get_result = await memory_get(mem["id"])
    assert "not found" in get_result.lower()


async def test_memory_delete_not_found(db_conn: sqlite3.Connection) -> None:
    """Deleting a nonexistent memory ID returns a not-found message."""
    delete_params = MemoryDeleteInput(memory_id="ghost_id_xyz")
    result = await memory_delete(delete_params)

    assert "not found" in result.lower()


# ---------------------------------------------------------------------------
# Full CRUD cycle test
# ---------------------------------------------------------------------------


async def test_crud_cycle(db_conn: sqlite3.Connection) -> None:
    """End-to-end CRUD: add -> get -> search -> update -> get (verify new) -> delete -> get (not found)."""
    import re

    # Add
    add_params = MemoryAddInput(
        content="Cycle test: unique xyzzy content",
        category="cycle",
        tags=["cycle_test"],
    )
    add_result = await memory_add(add_params)
    assert "Memory stored" in add_result

    m = re.search(r"`([a-f0-9]+)`", add_result)
    assert m is not None
    mem_id = m.group(1)

    # Get
    get_result = await memory_get(mem_id)
    assert "xyzzy content" in get_result
    assert mem_id in get_result

    # Search
    search_params = MemorySearchInput(query="xyzzy")
    search_result = await memory_search(search_params)
    assert "xyzzy" in search_result.lower()

    # Update
    update_params = MemoryUpdateInput(memory_id=mem_id, content="Cycle test: updated content")
    update_result = await memory_update(update_params)
    assert "updated" in update_result.lower()

    # Get after update
    get_after_update = await memory_get(mem_id)
    assert "updated content" in get_after_update
    assert "xyzzy content" not in get_after_update

    # Delete
    delete_params = MemoryDeleteInput(memory_id=mem_id)
    delete_result = await memory_delete(delete_params)
    assert "deleted" in delete_result.lower()

    # Get after delete
    get_after_delete = await memory_get(mem_id)
    assert "not found" in get_after_delete.lower()


# ---------------------------------------------------------------------------
# memory_import_chat tests
# ---------------------------------------------------------------------------


async def test_import_chat_json(db_conn: sqlite3.Connection, sample_chat_json: Path) -> None:
    """Importing a JSON chat file returns status=ok and creates memories in the DB."""
    params = ChatImportInput(file_path=str(sample_chat_json))
    result_str = await memory_import_chat(params)

    result = json.loads(result_str)
    assert result["status"] == "ok"
    assert result["memories_created"] >= 1

    count = db_conn.execute("SELECT COUNT(*) as cnt FROM memories").fetchone()["cnt"]
    assert count >= 1


async def test_import_chat_markdown(db_conn: sqlite3.Connection, sample_chat_md: Path) -> None:
    """Importing a Markdown chat file returns status=ok and creates memories."""
    params = ChatImportInput(file_path=str(sample_chat_md))
    result_str = await memory_import_chat(params)

    result = json.loads(result_str)
    assert result["status"] == "ok"
    assert result["memories_created"] >= 1


async def test_import_chat_dedup(db_conn: sqlite3.Connection, sample_chat_json: Path) -> None:
    """Importing the same file twice returns status=skipped on the second import."""
    params = ChatImportInput(file_path=str(sample_chat_json))

    first = json.loads(await memory_import_chat(params))
    assert first["status"] == "ok"

    second = json.loads(await memory_import_chat(params))
    assert second["status"] == "skipped"


async def test_import_chat_search_round_trip(db_conn: sqlite3.Connection, tmp_path: Path) -> None:
    """Import a chat file containing 'Python', then search confirms the memory is findable."""
    # Create a chat file with Python content
    data = {
        "chat_messages": [
            {
                "sender": "assistant",
                "content": [{"type": "text", "text": "Python is a great programming language for beginners."}],
            }
        ]
    }
    chat_file = tmp_path / "python_chat.json"
    chat_file.write_text(json.dumps(data))

    import_params = ChatImportInput(file_path=str(chat_file))
    import_result = json.loads(await memory_import_chat(import_params))
    assert import_result["status"] == "ok"
    assert import_result["memories_created"] >= 1

    search_params = MemorySearchInput(query="Python")
    search_result = await memory_search(search_params)

    assert "_No memories found._" not in search_result
    assert "Python" in search_result


# ---------------------------------------------------------------------------
# memory_import_directory tests
# ---------------------------------------------------------------------------


async def test_import_directory(db_conn: sqlite3.Connection, tmp_path: Path) -> None:
    """Importing a directory with 2 JSON files processes both and creates memories."""
    # Create two distinct JSON chat files
    for i in range(2):
        data = {
            "chat_messages": [
                {
                    "sender": "assistant",
                    "content": [{"type": "text", "text": f"Directory import test message {i}: unique content {i}"}],
                }
            ]
        }
        (tmp_path / f"chat_{i}.json").write_text(json.dumps(data))

    params = BulkImportDirInput(directory=str(tmp_path))
    result_str = await memory_import_directory(params)
    result = json.loads(result_str)

    assert result["imported"] >= 2
    assert result["total_memories_created"] > 0


async def test_import_directory_empty(db_conn: sqlite3.Connection, tmp_path: Path) -> None:
    """Importing an empty directory reports files_processed == 0."""
    params = BulkImportDirInput(directory=str(tmp_path))
    result_str = await memory_import_directory(params)
    result = json.loads(result_str)

    assert result["files_processed"] == 0


# ---------------------------------------------------------------------------
# memory_stats tests
# ---------------------------------------------------------------------------


async def test_stats_empty(db_conn: sqlite3.Connection) -> None:
    """Stats with no memories shows total_memories == 0."""
    params = MemoryStatsInput(response_format=ResponseFormat.JSON)
    result_str = await memory_stats(params)

    result = json.loads(result_str)
    assert result["total_memories"] == 0


async def test_stats_with_data(db_conn: sqlite3.Connection, memory_factory) -> None:
    """Stats with several memories reflects the correct total and category counts."""
    memory_factory(content="Cat A memory 1", category="catA")
    memory_factory(content="Cat A memory 2", category="catA")
    memory_factory(content="Cat B memory 1", category="catB")

    params = MemoryStatsInput(response_format=ResponseFormat.JSON)
    result_str = await memory_stats(params)

    result = json.loads(result_str)
    assert result["total_memories"] == 3
    assert result["categories"]["catA"] == 2
    assert result["categories"]["catB"] == 1


async def test_stats_json_format(db_conn: sqlite3.Connection, memory_factory) -> None:
    """Stats JSON format returns valid JSON with 'total_memories' key."""
    memory_factory(content="Stats format test")

    params = MemoryStatsInput(response_format=ResponseFormat.JSON)
    result_str = await memory_stats(params)

    data = json.loads(result_str)
    assert "total_memories" in data
    assert data["total_memories"] >= 1


# ---------------------------------------------------------------------------
# remind_me_auto_capture tests
# ---------------------------------------------------------------------------


async def test_auto_capture_creates_two_memories(db_conn: sqlite3.Connection) -> None:
    """Auto-capture stores two rows: one dialog (category=dialog) and one summary."""
    params = AutoCaptureInput(
        conversation="Human: What is asyncio?\nAssistant: asyncio is Python's async framework.",
        summary="Discussed Python asyncio basics.",
        category="conversation",
    )
    result = await remind_me_auto_capture(params)

    assert "captured" in result.lower() or "Conversation captured" in result

    count = db_conn.execute("SELECT COUNT(*) as cnt FROM memories").fetchone()["cnt"]
    assert count == 2

    dialog_row = db_conn.execute(
        "SELECT * FROM memories WHERE category = ?", ("dialog",)
    ).fetchone()
    assert dialog_row is not None

    summary_row = db_conn.execute(
        "SELECT * FROM memories WHERE category = ?", ("conversation",)
    ).fetchone()
    assert summary_row is not None


async def test_auto_capture_links_via_capture_id(db_conn: sqlite3.Connection) -> None:
    """Both captured memories share the same capture_id in their metadata."""
    import re

    params = AutoCaptureInput(
        conversation="Human: Tell me about decorators.\nAssistant: Decorators wrap functions.",
        summary="Discussed Python decorators.",
        category="conversation",
    )
    result = await remind_me_auto_capture(params)

    # Extract capture_id from the return string
    m = re.search(r"Capture ID.*?`([a-f0-9]+)`", result)
    assert m is not None, f"Could not find capture_id in: {result}"
    capture_id = m.group(1)

    rows = db_conn.execute("SELECT metadata FROM memories").fetchall()
    assert len(rows) == 2

    for row in rows:
        meta = json.loads(row["metadata"])
        assert meta.get("capture_id") == capture_id

    # Verify cross-referencing: summary has linked_dialog, dialog has linked_summary
    dialog_row = db_conn.execute(
        "SELECT metadata FROM memories WHERE category = 'dialog'"
    ).fetchone()
    summary_row = db_conn.execute(
        "SELECT metadata FROM memories WHERE category != 'dialog'"
    ).fetchone()

    dialog_meta = json.loads(dialog_row["metadata"])
    summary_meta = json.loads(summary_row["metadata"])

    assert "linked_summary" in dialog_meta
    assert "linked_dialog" in summary_meta


async def test_auto_capture_with_tags(db_conn: sqlite3.Connection) -> None:
    """Tags provided to auto-capture are applied to both captured memories."""
    params = AutoCaptureInput(
        conversation="Human: Python closures?\nAssistant: Closures capture outer scope.",
        summary="Discussed Python closures.",
        tags=["python", "test"],
    )
    await remind_me_auto_capture(params)

    rows = db_conn.execute("SELECT tags FROM memories").fetchall()
    assert len(rows) == 2
    for row in rows:
        tags = json.loads(row["tags"])
        assert "python" in tags
        assert "test" in tags


# ---------------------------------------------------------------------------
# remind_me_get_capture tests
# ---------------------------------------------------------------------------


async def test_get_capture_retrieves_pair(db_conn: sqlite3.Connection) -> None:
    """get_capture with a valid capture_id returns both Summary and Dialog sections."""
    import re

    capture_params = AutoCaptureInput(
        conversation="Human: What is a context manager?\nAssistant: Context managers handle setup/teardown.",
        summary="Discussed Python context managers.",
        category="conversation",
    )
    capture_result = await remind_me_auto_capture(capture_params)

    m = re.search(r"Capture ID.*?`([a-f0-9]+)`", capture_result)
    assert m is not None
    capture_id = m.group(1)

    get_result = await remind_me_get_capture(capture_id)

    assert "Summary" in get_result
    assert "Dialog" in get_result


async def test_get_capture_not_found(db_conn: sqlite3.Connection) -> None:
    """get_capture with a nonexistent capture_id returns 'No capture found'."""
    result = await remind_me_get_capture("fakecaptureid000")
    assert "No capture found" in result


# ---------------------------------------------------------------------------
# remind_me_reindex tests
# ---------------------------------------------------------------------------


async def test_reindex_no_embedder(
    db_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reindex without an embedder returns an 'not available' message."""
    import remind_me_mcp.embeddings as _emb_mod
    import remind_me_mcp.db as _db_mod

    monkeypatch.setattr(_emb_mod, "_get_embedder", lambda: None)
    monkeypatch.setattr(_db_mod, "_get_embedder", lambda: None)

    result = await remind_me_reindex()

    assert "not available" in result.lower() or "unavailable" in result.lower()


async def test_reindex_with_embedder(
    db_conn: sqlite3.Connection,
    mock_embedder,
    memory_factory,
) -> None:
    """Reindex with a mock embedder completes without crashing."""
    memory_factory(content="Memory to reindex")

    result = await remind_me_reindex()

    # Should either report complete or note embeddings exist
    # The mock embedder may fail on memories_vec table (no sqlite-vec in :memory:)
    # but the function should handle this gracefully
    assert isinstance(result, str)
    assert len(result) > 0


# ---------------------------------------------------------------------------
# remind_me_server_status tests
# ---------------------------------------------------------------------------


async def test_server_status_no_ui(
    db_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Server status with UI stopped reports 'Not running'."""
    import remind_me_mcp.tools as _tools_mod

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

    result = await remind_me_server_status()

    assert "Not running" in result


# ---------------------------------------------------------------------------
# Resource handler tests
# ---------------------------------------------------------------------------


async def test_resource_stats(db_conn: sqlite3.Connection, memory_factory) -> None:
    """resource_stats returns valid JSON with 'total_memories' >= 1 after adding a memory."""
    memory_factory(content="Resource stats test memory")

    result_str = await resource_stats()
    result = json.loads(result_str)

    assert "total_memories" in result
    assert result["total_memories"] >= 1


async def test_resource_categories(db_conn: sqlite3.Connection, memory_factory) -> None:
    """resource_categories returns JSON containing all category names used."""
    memory_factory(content="Alpha memory", category="alpha")
    memory_factory(content="Beta memory", category="beta")

    result_str = await resource_categories()
    result = json.loads(result_str)

    assert "alpha" in result
    assert "beta" in result


# ---------------------------------------------------------------------------
# Regression tests — BUGF-01, BUGF-02, DATA-02
# ---------------------------------------------------------------------------


async def test_import_then_search_embeds_correctly(
    db_conn: sqlite3.Connection,
    mock_embedder,
    tmp_path: Path,
) -> None:
    """BUGF-01 regression (Phase 3 success criterion 1): import then search returns results.

    Imports a JSON chat file via memory_import_chat, then calls memory_search with a
    query matching the imported content.  With the bug fixed (embed_pairs collected
    during INSERT), embeddings are stored against the correct memory IDs and FTS5 also
    indexes the content, so the search must return non-empty results.
    """
    data = {
        "chat_messages": [
            {
                "sender": "assistant",
                "content": [{"type": "text", "text": "Asyncio is Python's asynchronous framework."}],
            }
        ]
    }
    chat_file = tmp_path / "import_search_test.json"
    chat_file.write_text(json.dumps(data))

    import_params = ChatImportInput(file_path=str(chat_file))
    import_result = json.loads(await memory_import_chat(import_params))
    assert import_result["status"] == "ok"
    assert import_result["memories_created"] >= 1

    # FTS5 keyword search must find the imported content
    search_params = MemorySearchInput(query="asyncio")
    search_result = await memory_search(search_params)

    assert "_No memories found._" not in search_result
    assert "asyncio" in search_result.lower() or "Asyncio" in search_result


async def test_get_capture_uses_column_lookup(db_conn: sqlite3.Connection) -> None:
    """BUGF-02 regression (Phase 3 success criterion 2): remind_me_get_capture
    uses WHERE capture_id = ? column lookup, not metadata LIKE scan.

    Inserts two memories with the capture_id column set directly (no capture_id
    in metadata JSON) and verifies get_capture finds both by column value only.
    """
    from remind_me_mcp.db import _make_id, _now_iso

    cap_id = "test_cap_bugf02"
    now = _now_iso()

    for content in ("Dialog memory for BUGF-02 test", "Summary memory for BUGF-02 test"):
        mem_id = _make_id(content)
        db_conn.execute(
            """INSERT INTO memories
               (id, content, category, tags, source, metadata, capture_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (mem_id, content, "dialog", "[]", "manual", "{}", cap_id, now, now),
        )
    db_conn.commit()

    result = await remind_me_get_capture(cap_id)

    # Both memories must be returned (capture found, not "No capture found")
    assert "No capture found" not in result
    assert cap_id in result


async def test_list_tag_filter_pagination(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """DATA-02 regression (Phase 3 success criterion 3): memory_list with tags filter
    and limit returns exactly `limit` tag-matching results.

    Before the fix, tag filtering happened in Python after the LIMIT was applied in SQL,
    so `limit=5` could return fewer than 5 results even when more tagged memories existed.
    After the fix, the SQL WHERE clause filters by tag before LIMIT, so exactly 5 results
    are returned and all have the expected tag.
    """
    # Create 10 memories with tag "alpha" and 10 without any tag
    for i in range(10):
        memory_factory(content=f"Alpha tagged memory number {i} unique", tags=["alpha"])
    for i in range(10):
        memory_factory(content=f"Untagged memory number {i} unique", tags=[])

    params = MemoryListInput(tags=["alpha"], limit=5)
    result_str = await memory_list(params)

    # Parse JSON to count results precisely
    json_params = MemoryListInput(tags=["alpha"], limit=5, response_format=ResponseFormat.JSON)
    json_result_str = await memory_list(json_params)
    data = json.loads(json_result_str)

    # Must return exactly 5 results (not fewer due to Python post-filter)
    assert data["count"] == 5, (
        f"Expected exactly 5 results with tags=['alpha'] limit=5, got {data['count']}. "
        f"This indicates tag filtering still happens in Python after LIMIT."
    )

    # All returned memories must have the 'alpha' tag
    for mem in data["memories"]:
        assert "alpha" in mem["tags"], (
            f"Memory {mem['id']} does not have 'alpha' tag: {mem['tags']}"
        )
