"""
Integration tests for all 13 MCP tool handlers and 2 resource handlers.

Each test calls the actual async handler function with validated Pydantic input
models, exercising real SQL operations against an in-memory SQLite database.
The db_conn fixture monkeypatches _get_db so all application code routes through
the test-controlled :memory: connection automatically.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

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
    """Search with JSON response_format returns valid JSON with envelope keys and 'memories'."""
    add_params = MemoryAddInput(content="JSON format test memory", category="test")
    await memory_add(add_params)

    search_params = MemorySearchInput(
        query="JSON format",
        response_format=ResponseFormat.JSON,
    )
    result = await memory_search(search_params)

    data = json.loads(result)
    assert "returned" in data
    assert "total_candidates" in data
    assert "trimmed" in data
    assert "tokens_used" in data
    assert "budget" in data
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


async def test_memory_delete_removes_chunk_vectors(
    db_conn_with_vec: sqlite3.Connection,
    mock_embedder,
) -> None:
    """Deleting a memory removes its chunk vectors from vec_chunks/memories_vec (DI-01)."""
    await memory_add(MemoryAddInput(content="Chunk cleanup test memory"))
    row = db_conn_with_vec.execute("SELECT rowid, id FROM memories").fetchone()

    chunk_count = db_conn_with_vec.execute(
        "SELECT COUNT(*) FROM vec_chunks WHERE memory_rowid = ?", (row["rowid"],)
    ).fetchone()[0]
    assert chunk_count > 0, "precondition: memory was embedded"

    result = await memory_delete(MemoryDeleteInput(memory_id=row["id"]))
    assert "deleted" in result

    assert db_conn_with_vec.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0] == 0
    assert db_conn_with_vec.execute("SELECT COUNT(*) FROM memories_vec").fetchone()[0] == 0


async def test_reindex_prunes_orphaned_chunks_for_reused_rowid(
    db_conn_with_vec: sqlite3.Connection,
    mock_embedder,
) -> None:
    """Reindex prunes vec_chunks rows whose memory no longer exists (DI-01).

    SQLite reuses freed rowids: without pruning, a new memory inherits the
    deleted memory's embedding and reindex skips it forever.
    """
    from remind_me_mcp.db import _make_id, _now_iso, _semantic_search

    await memory_add(MemoryAddInput(content="Old deleted memory about sailing boats"))
    old = db_conn_with_vec.execute("SELECT rowid, id FROM memories").fetchone()

    # Simulate the historical buggy delete: row removed, chunk vectors orphaned.
    db_conn_with_vec.execute("DELETE FROM memories WHERE id = ?", (old["id"],))
    db_conn_with_vec.commit()
    assert db_conn_with_vec.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0] > 0

    # Reindex must prune the orphaned chunk vectors.
    await remind_me_reindex()
    assert db_conn_with_vec.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0] == 0
    assert db_conn_with_vec.execute("SELECT COUNT(*) FROM memories_vec").fetchone()[0] == 0

    # Insert a new memory directly (no embedding) — it reuses the freed rowid.
    new_content = "Brand new memory about quantum chess strategies"
    new_id = _make_id(new_content)
    now = _now_iso()
    db_conn_with_vec.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
           VALUES (?, ?, 'general', '[]', 'manual', '{}', ?, ?)""",
        (new_id, new_content, now, now),
    )
    db_conn_with_vec.commit()
    new_rowid = db_conn_with_vec.execute(
        "SELECT rowid FROM memories WHERE id = ?", (new_id,)
    ).fetchone()[0]
    assert new_rowid == old["rowid"], "precondition: rowid was reused"

    # Without the prune, reindex would have seen this rowid as already embedded
    # and the new memory would keep the deleted memory's embedding forever.
    await remind_me_reindex()
    results = _semantic_search(new_content, limit=1)
    assert results, "new memory should have a real embedding after reindex"
    assert results[0]["id"] == new_id
    assert results[0]["semantic_distance"] < 0.1


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


async def test_import_directory_concurrent(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """Importing a directory with 12 files processes all files correctly via concurrent dispatch."""
    # Create 12 distinct JSON chat files
    for i in range(12):
        data = {
            "chat_messages": [
                {
                    "sender": "assistant",
                    "content": [{"type": "text", "text": f"Concurrent import test file {i}: unique content for concurrency test {i}"}],
                }
            ]
        }
        (tmp_path / f"concurrent_{i}.json").write_text(json.dumps(data))

    params = BulkImportDirInput(directory=str(tmp_path))
    result_str = await memory_import_directory(params)
    result = json.loads(result_str)

    assert result["files_processed"] == 12
    assert result["imported"] == 12
    assert result["errors"] == 0
    assert result["total_memories_created"] >= 12


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


async def test_auto_capture_decomposition_pending(db_conn: sqlite3.Connection) -> None:
    """auto_capture response includes decomposition_pending hint with capture_id."""
    import re

    params = AutoCaptureInput(
        conversation="Human: I prefer dark mode.\nAssistant: Noted, dark mode preference.",
        summary="User prefers dark mode for all interfaces.",
        tags=["preferences"],
    )
    result = await remind_me_auto_capture(params)

    # Extract capture_id from the standard part of the response
    m = re.search(r"Capture ID.*?`([a-f0-9]+)`", result)
    assert m is not None, "Expected capture_id in response"
    capture_id = m.group(1)

    # The response must include decomposition_pending hint
    assert "decomposition_pending" in result
    # The hint must reference the remind_me_decompose tool
    assert "remind_me_decompose" in result
    # The hint must include the capture_id so Claude knows what to pass
    hint_section = result.split("decomposition_pending")[1]
    assert capture_id in hint_section


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
    import remind_me_mcp.db as _db_mod
    import remind_me_mcp.embeddings as _emb_mod

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


async def test_reindex_batches_embed_calls(
    db_conn: sqlite3.Connection,
    mock_embedder,
    memory_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reindex with 40 memories calls embedder.embed() twice (batch 32 + batch 8), not 40 times."""
    from remind_me_mcp.tools import EMBED_BATCH_SIZE

    # Create 40 memories so we get 2 batches (32 + 8)
    for i in range(40):
        memory_factory(content=f"Batch reindex test memory number {i} with unique content")

    # Spy on embed calls to track batch sizes
    call_log: list[list[str]] = []
    original_embed = mock_embedder.embed

    def spy_embed(texts: list[str]):
        call_log.append(list(texts))
        return original_embed(texts)

    monkeypatch.setattr(mock_embedder, "embed", spy_embed)

    result = await remind_me_reindex()

    # Verify batch behavior: 40 items split into 2 batches (32 + 8)
    assert len(call_log) == 2, f"Expected 2 batch calls (32+8), got {len(call_log)}"
    assert len(call_log[0]) == EMBED_BATCH_SIZE, (
        f"First batch should be {EMBED_BATCH_SIZE}, got {len(call_log[0])}"
    )
    assert len(call_log[1]) == 8, f"Second batch should be 8, got {len(call_log[1])}"
    assert "Newly embedded" in result or "complete" in result.lower()


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
# Error path tests — ERRH-01, ERRH-02, ERRH-03
# ---------------------------------------------------------------------------


async def test_memory_get_not_found_message(db_conn: sqlite3.Connection) -> None:
    """Getting a nonexistent memory returns a user-facing 'not found' message."""
    result = await memory_get("definitely_not_there")
    assert "not found" in result.lower()
    # Should mention the ID in the message
    assert "definitely_not_there" in result


async def test_memory_delete_not_found_message(db_conn: sqlite3.Connection) -> None:
    """Deleting a nonexistent memory returns a user-facing 'not found' message."""
    params = MemoryDeleteInput(memory_id="ghost_id_errh")
    result = await memory_delete(params)
    assert "not found" in result.lower()
    assert "ghost_id_errh" in result


async def test_memory_update_not_found_message(db_conn: sqlite3.Connection) -> None:
    """Updating a nonexistent memory returns a user-facing 'not found' message."""
    params = MemoryUpdateInput(memory_id="missing_id_errh", content="whatever")
    result = await memory_update(params)
    assert "not found" in result.lower()
    assert "missing_id_errh" in result


async def test_get_capture_not_found_message(db_conn: sqlite3.Connection) -> None:
    """get_capture with nonexistent capture_id returns a 'No capture found' message."""
    result = await remind_me_get_capture("no_such_capture_errh")
    assert "No capture found" in result


async def test_memory_import_chat_file_not_found(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Importing a file that disappears after validation returns a JSON error."""
    # Create a real file so Pydantic validation passes
    chat_file = tmp_path / "disappears.json"
    chat_file.write_text('{"chat_messages": []}')
    params = ChatImportInput(file_path=str(chat_file))
    # Delete the file before the handler runs to trigger FileNotFoundError
    chat_file.unlink()

    result_str = await memory_import_chat(params)
    result = json.loads(result_str)
    assert result["status"] == "error"
    assert "not found" in result["error"].lower()


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
    await memory_list(params)

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


# ---------------------------------------------------------------------------
# RRF retrieval pipeline integration tests (Phase 10 Plan 02)
# ---------------------------------------------------------------------------


async def test_search_returns_envelope_json(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """Search with JSON format returns envelope metadata keys."""
    memory_factory(content="Envelope test memory alpha", category="test")
    memory_factory(content="Envelope test memory beta", category="test")
    memory_factory(content="Envelope test memory gamma", category="test")

    search_params = MemorySearchInput(
        query="Envelope test memory",
        response_format=ResponseFormat.JSON,
    )
    result = await memory_search(search_params)
    data = json.loads(result)

    # All 5 envelope keys must exist with correct types
    assert isinstance(data["total_candidates"], int)
    assert isinstance(data["returned"], int)
    assert isinstance(data["trimmed"], int)
    assert isinstance(data["tokens_used"], int)
    assert isinstance(data["budget"], int)
    assert isinstance(data["memories"], list)
    assert data["returned"] == len(data["memories"])
    assert data["budget"] == 800  # default token_budget


async def test_search_token_budget_trims(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """With a small token_budget, fewer results are returned and trimmed > 0."""
    # Each memory ~100 tokens (400 chars / 4)
    for i in range(5):
        memory_factory(
            content=f"Budget trim test memory number {i}. " + ("x" * 380),
            category="test",
        )

    search_params = MemorySearchInput(
        query="Budget trim test memory",
        token_budget=250,
        response_format=ResponseFormat.JSON,
    )
    result = await memory_search(search_params)
    data = json.loads(result)

    assert data["returned"] < data["total_candidates"]
    assert data["trimmed"] > 0
    assert data["tokens_used"] <= 250


async def test_search_token_budget_zero_unlimited(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """token_budget=0 returns all matching results (unlimited)."""
    for i in range(5):
        memory_factory(
            content=f"Unlimited budget test memory number {i} with unique content",
            category="test",
        )

    search_params = MemorySearchInput(
        query="Unlimited budget test memory",
        token_budget=0,
        response_format=ResponseFormat.JSON,
    )
    result = await memory_search(search_params)
    data = json.loads(result)

    assert data["returned"] == data["total_candidates"]
    assert data["trimmed"] == 0


async def test_search_envelope_markdown(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """Markdown response includes token/budget info in the envelope summary line."""
    memory_factory(content="Markdown envelope test memory alpha", category="test")
    memory_factory(content="Markdown envelope test memory beta", category="test")
    memory_factory(content="Markdown envelope test memory gamma", category="test")

    search_params = MemorySearchInput(query="Markdown envelope test memory")
    result = await memory_search(search_params)

    assert "tokens" in result.lower()
    assert "budget" in result.lower()
    assert "results" in result.lower()


async def test_search_rrf_ranking_smoke(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """RRF ranking path produces valid envelope response (smoke test)."""
    # Create memories with different timestamps to exercise the recency signal.
    # accessed_at is kept fresh so read-time vitality decay (DI-04) doesn't
    # mark months-old created_at values dormant.
    for i in range(3):
        memory_factory(
            content=f"RRF smoke test content item {i} with searchable text",
            category="test",
            created_at=f"2026-01-0{i + 1}T00:00:00Z",
            accessed_at=_days_ago_iso(i),
        )

    search_params = MemorySearchInput(
        query="RRF smoke test content",
        response_format=ResponseFormat.JSON,
    )
    result = await memory_search(search_params)
    data = json.loads(result)

    # Verify the response is a valid envelope with memories
    assert data["returned"] >= 1
    assert data["total_candidates"] >= 1
    assert isinstance(data["memories"], list)
    assert len(data["memories"]) == data["returned"]

    # HY-05: internal RRF metadata must NOT leak into the JSON payload
    for mem in data["memories"]:
        assert not any(k.startswith("_") for k in mem), (
            f"internal fields leaked into JSON response: {sorted(mem)}"
        )


# ---------------------------------------------------------------------------
# remind_me_reclassify tests (Phase 11 Plan 02)
# ---------------------------------------------------------------------------


async def test_reclassify_updates_memory_type_and_decay(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """remind_me_reclassify accepts [{id, memory_type}] and updates each memory's memory_type and decay_rate."""
    from remind_me_mcp.models import ReclassifyInput
    from remind_me_mcp.tools import remind_me_reclassify
    from remind_me_mcp.vitality import DECAY_RATES

    mem = memory_factory(content="Reclassify test: I prefer dark mode")

    params = ReclassifyInput(
        classifications=[{"memory_id": mem["id"], "memory_type": "preference"}],
    )
    result = await remind_me_reclassify(params)
    data = json.loads(result)

    assert data["updated"] == 1
    assert data["not_found"] == []

    row = db_conn.execute(
        "SELECT memory_type, decay_rate FROM memories WHERE id = ?",
        (mem["id"],),
    ).fetchone()
    assert row["memory_type"] == "preference"
    assert row["decay_rate"] == DECAY_RATES["preference"]


async def test_reclassify_rejects_invalid_memory_type() -> None:
    """remind_me_reclassify rejects invalid memory_type values not in the allowed set."""
    from pydantic import ValidationError

    from remind_me_mcp.models import ReclassifyInput

    with pytest.raises(ValidationError, match="memory_type"):
        ReclassifyInput(
            classifications=[{"memory_id": "abc", "memory_type": "invalid_type"}],
        )


async def test_reclassify_returns_counts(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """remind_me_reclassify returns count of updated, not_found, and total results."""
    from remind_me_mcp.models import ReclassifyInput
    from remind_me_mcp.tools import remind_me_reclassify

    mem1 = memory_factory(content="Reclassify count test A")
    mem2 = memory_factory(content="Reclassify count test B")

    params = ReclassifyInput(
        classifications=[
            {"memory_id": mem1["id"], "memory_type": "fact"},
            {"memory_id": mem2["id"], "memory_type": "decision"},
            {"memory_id": "nonexistent_id", "memory_type": "insight"},
        ],
    )
    result = await remind_me_reclassify(params)
    data = json.loads(result)

    assert data["updated"] == 2
    assert data["not_found"] == ["nonexistent_id"]
    assert data["total"] == 3


async def test_reclassify_sets_correct_decay_rate(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """After reclassification, memory's decay_rate matches DECAY_RATES[memory_type]."""
    from remind_me_mcp.models import ReclassifyInput
    from remind_me_mcp.tools import remind_me_reclassify
    from remind_me_mcp.vitality import DECAY_RATES

    mem = memory_factory(content="Decay rate test memory")

    params = ReclassifyInput(
        classifications=[{"memory_id": mem["id"], "memory_type": "action_item"}],
    )
    await remind_me_reclassify(params)

    row = db_conn.execute(
        "SELECT decay_rate FROM memories WHERE id = ?", (mem["id"],)
    ).fetchone()
    assert row["decay_rate"] == DECAY_RATES["action_item"]
    assert row["decay_rate"] == 0.20


# ---------------------------------------------------------------------------
# remind_me_reclassify_batch tests (Phase 11 Plan 02)
# ---------------------------------------------------------------------------


async def test_reclassify_batch_returns_unclassified(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """remind_me_reclassify_batch returns up to N unclassified memories with id, content_snippet, category, tags."""
    from remind_me_mcp.models import ReclassifyBatchInput
    from remind_me_mcp.tools import remind_me_reclassify_batch

    # Create unclassified memories (memory_type defaults to 'unclassified' from schema)
    for i in range(5):
        memory_factory(content=f"Unclassified memory number {i} for batch test")

    params = ReclassifyBatchInput(batch_size=3)
    result = await remind_me_reclassify_batch(params)
    data = json.loads(result)

    assert len(data["memories"]) == 3
    assert data["total_unclassified"] == 5

    for mem in data["memories"]:
        assert "id" in mem
        assert "content_snippet" in mem
        assert "category" in mem
        assert "tags" in mem


async def test_reclassify_batch_respects_batch_size(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """remind_me_reclassify_batch respects batch_size parameter (default 20)."""
    from remind_me_mcp.models import ReclassifyBatchInput
    from remind_me_mcp.tools import remind_me_reclassify_batch

    for i in range(25):
        memory_factory(content=f"Batch size test memory {i}")

    # Default batch_size=20
    params_default = ReclassifyBatchInput()
    result = await remind_me_reclassify_batch(params_default)
    data = json.loads(result)

    assert len(data["memories"]) == 20
    assert data["total_unclassified"] == 25


async def test_reclassify_batch_empty_when_all_classified(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """remind_me_reclassify_batch returns empty list when no unclassified memories remain."""
    from remind_me_mcp.models import ReclassifyBatchInput, ReclassifyInput
    from remind_me_mcp.tools import remind_me_reclassify, remind_me_reclassify_batch

    mem = memory_factory(content="Already classified memory for empty test")

    # Reclassify it so none remain unclassified
    classify_params = ReclassifyInput(
        classifications=[{"memory_id": mem["id"], "memory_type": "fact"}],
    )
    await remind_me_reclassify(classify_params)

    params = ReclassifyBatchInput()
    result = await remind_me_reclassify_batch(params)
    data = json.loads(result)

    assert data["memories"] == []
    assert data["total_unclassified"] == 0


# ---------------------------------------------------------------------------
# Dormant filtering and vitality in search (Phase 11 Plan 03)
# ---------------------------------------------------------------------------


def _days_ago_iso(days: float) -> str:
    """ISO timestamp *days* in the past (UTC), for staging vitality decay."""
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


async def test_search_excludes_dormant_by_default(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """Memories whose effective vitality has decayed below the floor are excluded by default (DI-04).

    Dormancy is computed at read time from accessed_at, not from the stored
    status/vitality snapshot.
    """
    memory_factory(
        content="Active vitality test memory alpha",
        category="test",
        accessed_at=_days_ago_iso(0),
    )
    # 90 days unaccessed at decay_rate 0.1 -> e^-9 ~= 0.0001 < VITALITY_FLOOR
    memory_factory(
        content="Dormant vitality test memory beta",
        category="test",
        accessed_at=_days_ago_iso(90),
        decay_rate=0.1,
    )

    search_params = MemorySearchInput(query="vitality test memory")
    result = await memory_search(search_params)

    assert "alpha" in result.lower()
    assert "beta" not in result.lower()


async def test_search_include_dormant_shows_all(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """include_dormant=True includes effectively dormant memories in search results."""
    memory_factory(
        content="Active include dormant test gamma",
        category="test",
        accessed_at=_days_ago_iso(0),
    )
    memory_factory(
        content="Dormant include dormant test delta",
        category="test",
        accessed_at=_days_ago_iso(90),
        decay_rate=0.1,
    )

    search_params = MemorySearchInput(
        query="include dormant test",
        include_dormant=True,
        response_format=ResponseFormat.JSON,
    )
    result = await memory_search(search_params)
    data = json.loads(result)

    ids_content = [m["content"] for m in data["memories"]]
    assert any("gamma" in c.lower() for c in ids_content)
    assert any("delta" in c.lower() for c in ids_content)


async def test_search_min_vitality_filter(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """min_vitality=0.5 excludes memories whose effective vitality decayed below 0.5 (DI-04)."""
    memory_factory(
        content="High vitality filter test epsilon",
        category="test",
        accessed_at=_days_ago_iso(0),
    )
    # 15 days at decay_rate 0.1 -> e^-1.5 ~= 0.22 < 0.5
    memory_factory(
        content="Low vitality filter test zeta",
        category="test",
        accessed_at=_days_ago_iso(15),
        decay_rate=0.1,
    )

    search_params = MemorySearchInput(
        query="vitality filter test",
        min_vitality=0.5,
        response_format=ResponseFormat.JSON,
    )
    result = await memory_search(search_params)
    data = json.loads(result)

    contents = [m["content"].lower() for m in data["memories"]]
    assert any("epsilon" in c for c in contents)
    assert not any("zeta" in c for c in contents)


async def test_search_vitality_is_recomputed_at_read_time(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """Returned vitality reflects elapsed-days decay, not the stale stored snapshot (DI-04)."""
    import math

    # Stored snapshot says 1.0, but ~6.93 days at decay_rate 0.1 halves it.
    days = math.log(2) / 0.1
    memory_factory(
        content="Read time decay check memory",
        vitality=1.0,
        decay_rate=0.1,
        accessed_at=_days_ago_iso(days),
    )

    search_params = MemorySearchInput(
        query="read time decay check",
        response_format=ResponseFormat.JSON,
    )
    result = await memory_search(search_params)
    data = json.loads(result)

    assert len(data["memories"]) == 1
    assert data["memories"][0]["vitality"] == pytest.approx(0.5, abs=0.01)


async def test_search_counts_effectively_dormant_in_dormant_excluded(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """dormant_excluded counts read-time dormancy even when stored status is 'active' (DI-04)."""
    memory_factory(
        content="Stale status dormant counter memory",
        status="active",
        vitality=1.0,
        accessed_at=_days_ago_iso(90),
        decay_rate=0.1,
    )

    search_params = MemorySearchInput(
        query="stale status dormant counter",
        response_format=ResponseFormat.JSON,
    )
    result = await memory_search(search_params)
    data = json.loads(result)

    assert data["memories"] == []
    assert data["dormant_excluded"] == 1


async def test_search_record_access_called(
    db_conn: sqlite3.Connection,
    memory_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """record_accesses is called once with all returned search result ids (PF-02)."""
    import remind_me_mcp.tools as _tools_mod

    mem = memory_factory(
        content="Record access test eta unique content",
        category="test",
        status="active",
        vitality=0.8,
    )

    called_ids: list[str] = []
    call_count = {"n": 0}

    def fake_record_accesses(ids: list[str]) -> int:
        call_count["n"] += 1
        called_ids.extend(ids)
        return len(ids)

    monkeypatch.setattr(_tools_mod, "record_accesses", fake_record_accesses)

    # Capture the fire-and-forget task so we can await it deterministically
    # instead of sleeping for an arbitrary duration.
    import asyncio

    created_tasks: list[asyncio.Task] = []
    real_create_task = asyncio.create_task

    def capture_task(coro, **kwargs):
        task = real_create_task(coro, **kwargs)
        created_tasks.append(task)
        return task

    monkeypatch.setattr(asyncio, "create_task", capture_task)

    search_params = MemorySearchInput(query="record access test eta")
    await memory_search(search_params)

    assert created_tasks, "memory_search should schedule a record-access task"
    await asyncio.gather(*created_tasks)

    assert mem["id"] in called_ids
    # PF-02: one batched call per search, not one call per returned memory.
    assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# remind_me_vitality_report tests (Phase 11 Plan 03)
# ---------------------------------------------------------------------------


async def test_vitality_report_basic_counts(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """remind_me_vitality_report returns total_memories, active_count, dormant_count.

    Counts reflect effective (read-time) vitality, not the stored status (DI-04).
    """
    from remind_me_mcp.models import VitalityReportInput
    from remind_me_mcp.tools import remind_me_vitality_report

    memory_factory(content="Active report test 1", accessed_at=_days_ago_iso(0))
    memory_factory(content="Active report test 2", accessed_at=_days_ago_iso(1))
    memory_factory(
        content="Dormant report test 1", accessed_at=_days_ago_iso(90), decay_rate=0.1
    )

    params = VitalityReportInput()
    result = await remind_me_vitality_report(params)
    data = json.loads(result)

    assert data["total_memories"] == 3
    assert data["active_count"] == 2
    assert data["dormant_count"] == 1


async def test_vitality_report_average_vitality(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """Report includes average_vitality across all memories (effective vitality, DI-04)."""
    import math

    from remind_me_mcp.models import VitalityReportInput
    from remind_me_mcp.tools import remind_me_vitality_report

    # Fresh memory -> 1.0; one half-life (ln2/0.1 days) old -> 0.5; avg 0.75.
    memory_factory(content="Avg test 1", accessed_at=_days_ago_iso(0))
    memory_factory(
        content="Avg test 2", accessed_at=_days_ago_iso(math.log(2) / 0.1), decay_rate=0.1
    )

    params = VitalityReportInput()
    result = await remind_me_vitality_report(params)
    data = json.loads(result)

    assert abs(data["average_vitality"] - 0.75) < 0.01


async def test_vitality_report_decay_distribution(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """Report includes decay_distribution (count per memory_type)."""
    from remind_me_mcp.models import VitalityReportInput
    from remind_me_mcp.tools import remind_me_vitality_report

    memory_factory(content="Decision type 1", memory_type="decision", status="active", vitality=0.9)
    memory_factory(content="Decision type 2", memory_type="decision", status="active", vitality=0.8)
    memory_factory(content="Fact type 1", memory_type="fact", status="active", vitality=0.7)

    params = VitalityReportInput()
    result = await remind_me_vitality_report(params)
    data = json.loads(result)

    assert data["decay_distribution"]["decision"] == 2
    assert data["decay_distribution"]["fact"] == 1


async def test_vitality_report_vitality_buckets(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """Report buckets effective vitality, with an open-ended top bucket (DI-04)."""
    import math

    from remind_me_mcp.models import VitalityReportInput
    from remind_me_mcp.tools import remind_me_vitality_report

    def _staged(target: float) -> float:
        """Days ago at decay_rate 0.1 yielding effective vitality *target*."""
        return -math.log(target) / 0.1

    memory_factory(content="Bucket test 1", accessed_at=_days_ago_iso(_staged(0.01)), decay_rate=0.1)  # 0.00-0.05
    memory_factory(content="Bucket test 2", accessed_at=_days_ago_iso(_staged(0.10)), decay_rate=0.1)  # 0.05-0.25
    memory_factory(content="Bucket test 3", accessed_at=_days_ago_iso(_staged(0.30)), decay_rate=0.1)  # 0.25-0.50
    memory_factory(content="Bucket test 4", accessed_at=_days_ago_iso(_staged(0.60)), decay_rate=0.1)  # 0.50-0.75
    memory_factory(content="Bucket test 5", accessed_at=_days_ago_iso(0))                              # 0.75+

    params = VitalityReportInput()
    result = await remind_me_vitality_report(params)
    data = json.loads(result)

    assert data["vitality_buckets"]["0.00-0.05"] == 1
    assert data["vitality_buckets"]["0.05-0.25"] == 1
    assert data["vitality_buckets"]["0.25-0.50"] == 1
    assert data["vitality_buckets"]["0.50-0.75"] == 1
    assert data["vitality_buckets"]["0.75+"] == 1


async def test_vitality_report_top_bucket_counts_accessed_memories(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """Accessed memories (vitality > 1.0, e.g. sqrt(2)~=1.41) land in the open top bucket
    and bucket counts sum to the total (DI-04)."""
    from remind_me_mcp.models import VitalityReportInput
    from remind_me_mcp.tools import remind_me_vitality_report

    # One access just now: effective vitality = sqrt(2) ~= 1.41 > 1.01, which the
    # old closed 0.75-1.01 bucket silently dropped.
    memory_factory(
        content="Boosted bucket memory", access_count=1, accessed_at=_days_ago_iso(0)
    )

    params = VitalityReportInput()
    result = await remind_me_vitality_report(params)
    data = json.loads(result)

    assert data["vitality_buckets"]["0.75+"] == 1
    assert sum(data["vitality_buckets"].values()) == data["total_memories"] == 1


async def test_vitality_report_vault_health_score(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """Report includes vault_health_score (active_count / total as percentage)."""
    from remind_me_mcp.models import VitalityReportInput
    from remind_me_mcp.tools import remind_me_vitality_report

    memory_factory(content="Health test 1", accessed_at=_days_ago_iso(0))
    memory_factory(content="Health test 2", accessed_at=_days_ago_iso(1))
    memory_factory(content="Health test 3", accessed_at=_days_ago_iso(90), decay_rate=0.1)
    memory_factory(content="Health test 4", accessed_at=_days_ago_iso(91), decay_rate=0.1)

    params = VitalityReportInput()
    result = await remind_me_vitality_report(params)
    data = json.loads(result)

    assert data["vault_health_score"] == "50%"


# ---------------------------------------------------------------------------
# Decomposition tests — Task 1 (schema + models)
# ---------------------------------------------------------------------------


async def test_migration_v5_to_v6_adds_source_capture_id(
    db_conn: sqlite3.Connection,
) -> None:
    """Migration adds source_capture_id column to memories table."""
    cols = {
        row[1]
        for row in db_conn.execute("PRAGMA table_info(memories)").fetchall()
    }
    assert "source_capture_id" in cols


async def test_migration_v5_to_v6_creates_index(
    db_conn: sqlite3.Connection,
) -> None:
    """Migration creates idx_memories_source_capture_id index."""
    indexes = {
        row[1]
        for row in db_conn.execute("PRAGMA index_list(memories)").fetchall()
    }
    assert "idx_memories_source_capture_id" in indexes


async def test_schema_version_is_6(db_conn: sqlite3.Connection) -> None:
    """Schema version is at least 6 after migration (later migrations may bump it further)."""
    version = db_conn.execute("PRAGMA user_version").fetchone()[0]
    assert version >= 6


async def test_decompose_input_validates_capture_id() -> None:
    """DecomposeInput requires capture_id with min_length=1."""
    from pydantic import ValidationError

    from remind_me_mcp.models import AtomicFact, DecomposeInput

    # Valid
    inp = DecomposeInput(
        capture_id="abc123",
        facts=[AtomicFact(content="A fact")],
    )
    assert inp.capture_id == "abc123"

    # Empty capture_id should fail
    with pytest.raises(ValidationError):
        DecomposeInput(capture_id="", facts=[AtomicFact(content="A fact")])


async def test_decompose_input_requires_facts() -> None:
    """DecomposeInput requires at least one fact."""
    from pydantic import ValidationError

    from remind_me_mcp.models import DecomposeInput

    with pytest.raises(ValidationError):
        DecomposeInput(capture_id="abc123", facts=[])


async def test_atomic_fact_validates_content() -> None:
    """AtomicFact requires content with min_length=1."""
    from pydantic import ValidationError

    from remind_me_mcp.models import AtomicFact

    fact = AtomicFact(content="Valid content")
    assert fact.content == "Valid content"

    with pytest.raises(ValidationError):
        AtomicFact(content="")


async def test_atomic_fact_validates_memory_type() -> None:
    """AtomicFact.memory_type must be in VALID_MEMORY_TYPES when not None."""
    from pydantic import ValidationError

    from remind_me_mcp.models import AtomicFact

    # None is fine (default)
    fact = AtomicFact(content="A fact")
    assert fact.memory_type is None

    # Valid type
    fact = AtomicFact(content="A fact", memory_type="decision")
    assert fact.memory_type == "decision"

    # Invalid type
    with pytest.raises(ValidationError):
        AtomicFact(content="A fact", memory_type="invalid_type")


async def test_atomic_fact_validates_extra_tags() -> None:
    """AtomicFact.extra_tags defaults to empty list and accepts list of strings."""
    from remind_me_mcp.models import AtomicFact

    fact = AtomicFact(content="A fact")
    assert fact.extra_tags == []

    fact = AtomicFact(content="A fact", extra_tags=["tag1", "tag2"])
    assert fact.extra_tags == ["tag1", "tag2"]


async def test_decompose_batch_input_validates_batch_size() -> None:
    """DecomposeBatchInput validates batch_size (default=20, ge=1, le=100)."""
    from pydantic import ValidationError

    from remind_me_mcp.models import DecomposeBatchInput

    inp = DecomposeBatchInput()
    assert inp.batch_size == 20

    inp = DecomposeBatchInput(batch_size=50)
    assert inp.batch_size == 50

    with pytest.raises(ValidationError):
        DecomposeBatchInput(batch_size=0)

    with pytest.raises(ValidationError):
        DecomposeBatchInput(batch_size=101)


# ---------------------------------------------------------------------------
# Decomposition tests — Task 2 (tool handlers)
# ---------------------------------------------------------------------------


async def test_decompose_creates_one_memory_per_fact(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """remind_me_decompose creates one memory per fact with source_capture_id."""
    from remind_me_mcp.models import AtomicFact, DecomposeInput
    from remind_me_mcp.tools import remind_me_decompose

    memory_factory(
        content="A conversation about Python",
        category="dialog",
        tags=["python", "programming"],
        source="auto_capture",
        capture_id="cap_001",
    )

    params = DecomposeInput(
        capture_id="cap_001",
        facts=[
            AtomicFact(content="Python is dynamically typed"),
            AtomicFact(content="Python supports async/await"),
        ],
    )
    result = await remind_me_decompose(params)
    data = json.loads(result)

    assert data["created"] == 2
    assert len(data["fact_ids"]) == 2
    assert data["capture_id"] == "cap_001"

    # Verify each fact is stored with source_capture_id
    for fact_id in data["fact_ids"]:
        row = db_conn.execute(
            "SELECT source_capture_id, source, category FROM memories WHERE id = ?",
            (fact_id,),
        ).fetchone()
        assert row is not None
        assert row["source_capture_id"] == "cap_001"
        assert row["source"] == "decomposition"
        assert row["category"] == "fact"


async def test_decompose_inherits_parent_tags(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """Decomposed facts inherit tags from the parent capture."""
    from remind_me_mcp.models import AtomicFact, DecomposeInput
    from remind_me_mcp.tools import remind_me_decompose

    memory_factory(
        content="Discussion about databases",
        tags=["database", "sql"],
        capture_id="cap_002",
    )

    params = DecomposeInput(
        capture_id="cap_002",
        facts=[AtomicFact(content="PostgreSQL supports JSONB")],
    )
    result = await remind_me_decompose(params)
    data = json.loads(result)

    row = db_conn.execute(
        "SELECT tags FROM memories WHERE id = ?",
        (data["fact_ids"][0],),
    ).fetchone()
    tags = json.loads(row["tags"])
    assert "database" in tags
    assert "sql" in tags
    assert data["parent_tags_inherited"] == ["database", "sql"]


async def test_decompose_merges_extra_tags(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """Extra_tags are merged with inherited tags (no duplicates)."""
    from remind_me_mcp.models import AtomicFact, DecomposeInput
    from remind_me_mcp.tools import remind_me_decompose

    memory_factory(
        content="Discussion about Python testing",
        tags=["python", "testing"],
        capture_id="cap_003",
    )

    params = DecomposeInput(
        capture_id="cap_003",
        facts=[
            AtomicFact(
                content="pytest is the standard test framework",
                extra_tags=["pytest", "python"],  # 'python' overlaps with parent
            ),
        ],
    )
    result = await remind_me_decompose(params)
    data = json.loads(result)

    row = db_conn.execute(
        "SELECT tags FROM memories WHERE id = ?",
        (data["fact_ids"][0],),
    ).fetchone()
    tags = json.loads(row["tags"])
    assert "python" in tags
    assert "testing" in tags
    assert "pytest" in tags
    # No duplicates
    assert len(tags) == len(set(tags))


async def test_decompose_sets_memory_type_and_decay_rate(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """Decomposed facts get memory_type and decay_rate when specified."""
    from remind_me_mcp.models import AtomicFact, DecomposeInput
    from remind_me_mcp.tools import remind_me_decompose

    memory_factory(content="Decisions discussion", capture_id="cap_004")

    params = DecomposeInput(
        capture_id="cap_004",
        facts=[AtomicFact(content="Use PostgreSQL for prod", memory_type="decision")],
    )
    result = await remind_me_decompose(params)
    data = json.loads(result)

    row = db_conn.execute(
        "SELECT memory_type, decay_rate FROM memories WHERE id = ?",
        (data["fact_ids"][0],),
    ).fetchone()
    assert row["memory_type"] == "decision"
    assert row["decay_rate"] == 0.02  # DECAY_RATES["decision"]


async def test_decompose_defaults_to_unclassified(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """Decomposed facts default to memory_type='unclassified' when not specified."""
    from remind_me_mcp.models import AtomicFact, DecomposeInput
    from remind_me_mcp.tools import remind_me_decompose

    memory_factory(content="General discussion", capture_id="cap_005")

    params = DecomposeInput(
        capture_id="cap_005",
        facts=[AtomicFact(content="Some general fact")],
    )
    result = await remind_me_decompose(params)
    data = json.loads(result)

    row = db_conn.execute(
        "SELECT memory_type, decay_rate FROM memories WHERE id = ?",
        (data["fact_ids"][0],),
    ).fetchone()
    assert row["memory_type"] == "unclassified"
    assert row["decay_rate"] == 0.10  # DECAY_RATES["unclassified"]


async def test_decompose_returns_error_for_missing_capture(
    db_conn: sqlite3.Connection,
) -> None:
    """remind_me_decompose returns error when capture_id does not exist."""
    from remind_me_mcp.models import AtomicFact, DecomposeInput
    from remind_me_mcp.tools import remind_me_decompose

    params = DecomposeInput(
        capture_id="nonexistent_cap",
        facts=[AtomicFact(content="Some fact")],
    )
    result = await remind_me_decompose(params)
    assert "error" in result.lower() or "not found" in result.lower()


async def test_decompose_batch_returns_undecomposed_memories(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """remind_me_decompose_batch returns captures that have not been decomposed."""
    from remind_me_mcp.models import DecomposeBatchInput
    from remind_me_mcp.tools import remind_me_decompose_batch

    # Create a capture that has NOT been decomposed
    memory_factory(
        content="Undecomposed capture content",
        capture_id="cap_batch_001",
        tags=["test"],
        category="dialog",
    )

    # Create a capture that HAS been decomposed (has children)
    memory_factory(
        content="Already decomposed capture",
        capture_id="cap_batch_002",
    )
    # Simulate a decomposed child
    memory_factory(
        content="Child fact from cap_batch_002",
        source="decomposition",
        source_capture_id="cap_batch_002",
    )

    params = DecomposeBatchInput(batch_size=10)
    result = await remind_me_decompose_batch(params)
    data = json.loads(result)

    capture_ids = [m["capture_id"] for m in data["memories"]]
    assert "cap_batch_001" in capture_ids
    assert "cap_batch_002" not in capture_ids


async def test_decompose_batch_returns_correct_fields(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """decompose_batch returns content_snippet, id, category, tags, capture_id."""
    from remind_me_mcp.models import DecomposeBatchInput
    from remind_me_mcp.tools import remind_me_decompose_batch

    memory_factory(
        content="A capture with metadata",
        capture_id="cap_fields_001",
        tags=["field_test"],
        category="conversation",
    )

    params = DecomposeBatchInput(batch_size=10)
    result = await remind_me_decompose_batch(params)
    data = json.loads(result)

    mem = next(m for m in data["memories"] if m["capture_id"] == "cap_fields_001")
    assert "content_snippet" in mem
    assert "id" in mem
    assert "category" in mem
    assert "tags" in mem
    assert "capture_id" in mem


async def test_decompose_batch_returns_total_count(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """decompose_batch returns total_undecomposed count."""
    from remind_me_mcp.models import DecomposeBatchInput
    from remind_me_mcp.tools import remind_me_decompose_batch

    memory_factory(content="Capture 1", capture_id="cap_cnt_001")
    memory_factory(content="Capture 2", capture_id="cap_cnt_002")
    memory_factory(content="Capture 3", capture_id="cap_cnt_003")

    params = DecomposeBatchInput(batch_size=2)
    result = await remind_me_decompose_batch(params)
    data = json.loads(result)

    assert data["total_undecomposed"] == 3
    assert len(data["memories"]) == 2  # limited by batch_size


# ---------------------------------------------------------------------------
# Structured query detection and indexed lookup tests (Phase 13 Plan 01)
# ---------------------------------------------------------------------------


async def test_structured_search_by_subject(db_conn: sqlite3.Connection, memory_factory) -> None:
    """Query 'subject:Bailey' returns memories where subject='Bailey' via indexed lookup."""
    memory_factory(
        content="Bailey prefers dark mode",
        subject="Bailey",
        predicate="prefers",
        object="dark mode",
    )
    memory_factory(
        content="Alice prefers light mode",
        subject="Alice",
        predicate="prefers",
        object="light mode",
    )

    params = MemorySearchInput(query="subject:Bailey", response_format=ResponseFormat.JSON)
    result = await memory_search(params)
    data = json.loads(result)

    assert data["returned"] >= 1
    contents = [m["content"] for m in data["memories"]]
    assert "Bailey prefers dark mode" in contents
    assert "Alice prefers light mode" not in contents


async def test_structured_search_by_subject_and_predicate(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """Query 'subject:Bailey predicate:prefers' returns memories matching both conditions."""
    memory_factory(
        content="Bailey prefers dark mode",
        subject="Bailey",
        predicate="prefers",
        object="dark mode",
    )
    memory_factory(
        content="Bailey uses Python",
        subject="Bailey",
        predicate="uses",
        object="Python",
    )

    params = MemorySearchInput(
        query="subject:Bailey predicate:prefers", response_format=ResponseFormat.JSON
    )
    result = await memory_search(params)
    data = json.loads(result)

    assert data["returned"] >= 1
    contents = [m["content"] for m in data["memories"]]
    assert "Bailey prefers dark mode" in contents
    assert "Bailey uses Python" not in contents


async def test_structured_search_fallback_to_rrf(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """Query with structured prefix but no structured results falls back to FTS/semantic."""
    memory_factory(content="Bailey likes dark mode themes")

    params = MemorySearchInput(
        query="subject:Nonexistent predicate:nothing", response_format=ResponseFormat.JSON
    )
    result = await memory_search(params)
    data = json.loads(result)

    # Should not crash; may return 0 results if FTS doesn't match stripped query
    assert "returned" in data


async def test_structured_normal_query_no_regression(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """Query without subject/predicate prefix goes through normal RRF pipeline."""
    memory_factory(content="Python is a programming language")

    params = MemorySearchInput(query="Python programming", response_format=ResponseFormat.JSON)
    result = await memory_search(params)
    data = json.loads(result)

    assert data["returned"] >= 1
    assert "Python is a programming language" in [m["content"] for m in data["memories"]]


async def test_structured_search_respects_category_filter(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """Structured lookup respects category filter."""
    memory_factory(
        content="Bailey prefers dark mode",
        subject="Bailey",
        predicate="prefers",
        object="dark mode",
        category="preference",
    )
    memory_factory(
        content="Bailey mentioned dark mode",
        subject="Bailey",
        predicate="mentioned",
        object="dark mode",
        category="general",
    )

    params = MemorySearchInput(
        query="subject:Bailey",
        category="preference",
        response_format=ResponseFormat.JSON,
    )
    result = await memory_search(params)
    data = json.loads(result)

    contents = [m["content"] for m in data["memories"]]
    assert "Bailey prefers dark mode" in contents
    assert "Bailey mentioned dark mode" not in contents


async def test_structured_search_respects_dormant_filter(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """Structured lookup respects include_dormant filter (read-time dormancy, DI-04)."""
    memory_factory(
        content="Bailey prefers vim",
        subject="Bailey",
        predicate="prefers",
        object="vim",
        accessed_at=_days_ago_iso(90),
        decay_rate=0.1,
    )
    memory_factory(
        content="Bailey prefers dark mode",
        subject="Bailey",
        predicate="prefers",
        object="dark mode",
        accessed_at=_days_ago_iso(0),
    )

    params = MemorySearchInput(
        query="subject:Bailey predicate:prefers",
        include_dormant=False,
        response_format=ResponseFormat.JSON,
    )
    result = await memory_search(params)
    data = json.loads(result)
    contents = [m["content"] for m in data["memories"]]
    assert "Bailey prefers dark mode" in contents
    assert "Bailey prefers vim" not in contents


async def test_structured_search_respects_min_vitality(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """Structured lookup respects min_vitality filter (effective vitality, DI-04)."""
    memory_factory(
        content="Bailey prefers tabs",
        subject="Bailey",
        predicate="prefers",
        object="tabs",
        accessed_at=_days_ago_iso(15),
        decay_rate=0.1,
    )
    memory_factory(
        content="Bailey prefers dark mode",
        subject="Bailey",
        predicate="prefers",
        object="dark mode",
        accessed_at=_days_ago_iso(0),
    )

    params = MemorySearchInput(
        query="subject:Bailey predicate:prefers",
        min_vitality=0.5,
        response_format=ResponseFormat.JSON,
    )
    result = await memory_search(params)
    data = json.loads(result)
    contents = [m["content"] for m in data["memories"]]
    assert "Bailey prefers dark mode" in contents
    assert "Bailey prefers tabs" not in contents


async def test_structured_search_envelope_metadata(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """Structured results are wrapped in SearchEnvelope with correct metadata."""
    memory_factory(
        content="Bailey prefers dark mode",
        subject="Bailey",
        predicate="prefers",
        object="dark mode",
    )

    params = MemorySearchInput(query="subject:Bailey", response_format=ResponseFormat.JSON)
    result = await memory_search(params)
    data = json.loads(result)

    assert "total_candidates" in data
    assert "returned" in data
    assert "trimmed" in data
    assert "tokens_used" in data
    assert "budget" in data
    assert "memories" in data


async def test_structured_search_excludes_superseded(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """Structured lookup excludes memories where superseded_by IS NOT NULL."""
    memory_factory(
        content="Bailey prefers light mode",
        subject="Bailey",
        predicate="prefers",
        object="light mode",
        superseded_by="newer_id_123",
    )
    memory_factory(
        content="Bailey prefers dark mode",
        subject="Bailey",
        predicate="prefers",
        object="dark mode",
    )

    params = MemorySearchInput(
        query="subject:Bailey predicate:prefers", response_format=ResponseFormat.JSON
    )
    result = await memory_search(params)
    data = json.loads(result)

    contents = [m["content"] for m in data["memories"]]
    assert "Bailey prefers dark mode" in contents
    assert "Bailey prefers light mode" not in contents


async def test_rerank_pool_extends_beyond_limit(
    db_conn: sqlite3.Connection,
    memory_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reranker sees a pool of max(limit, RERANK_TOP_K) candidates so it can
    promote matches beyond the head; the limit applies after reranking (DI-07)."""
    import remind_me_mcp.tools as _tools_mod

    for i in range(6):
        memory_factory(content=f"rerank pool candidate {i} mentions walrus")

    seen: dict = {}

    def fake_rerank(query: str, memories: list[dict]) -> list[dict]:
        seen["order"] = [m["id"] for m in memories]
        return list(reversed(memories))  # promote the tail

    monkeypatch.setattr(_tools_mod, "maybe_rerank", fake_rerank)

    params = MemorySearchInput(
        query="walrus", limit=2, response_format=ResponseFormat.JSON
    )
    result = await memory_search(params)
    data = json.loads(result)

    # The reranker received the whole candidate pool, not just `limit` heads...
    assert len(seen["order"]) == 6
    # ...the limit still applies to the response...
    assert data["returned"] == 2
    # ...and candidates promoted from beyond the old head are returned.
    returned_ids = [m["id"] for m in data["memories"]]
    assert returned_ids == list(reversed(seen["order"]))[:2]


async def test_search_category_filter_applies_before_limit(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """Category filter is pushed into SQL, so matches past the fetch limit are found (DI-03)."""
    # This memory matches the query more strongly (more term hits) but has the
    # wrong category; with limit=1 it used to crowd out the real match before
    # the Python-side filter dropped it.
    memory_factory(content="zebra zebra zebra observation notes", category="noise")
    memory_factory(content="single zebra sighting", category="wildlife")

    params = MemorySearchInput(
        query="zebra", category="wildlife", limit=1, response_format=ResponseFormat.JSON
    )
    result = await memory_search(params)
    data = json.loads(result)

    contents = [m["content"] for m in data["memories"]]
    assert contents == ["single zebra sighting"]


async def test_search_tag_filter_applies_before_limit(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """Tag filter is pushed into SQL, so matches past the fetch limit are found (DI-03)."""
    memory_factory(content="kayak kayak kayak rental brochure", tags=["noise"])
    memory_factory(content="one kayak trip", tags=["water", "sport"])

    params = MemorySearchInput(
        query="kayak",
        tags=["water", "sport"],
        limit=1,
        response_format=ResponseFormat.JSON,
    )
    result = await memory_search(params)
    data = json.loads(result)

    contents = [m["content"] for m in data["memories"]]
    assert contents == ["one kayak trip"]


async def test_semantic_search_category_and_tag_filters_in_sql(
    db_conn_with_vec: sqlite3.Connection, mock_embedder
) -> None:
    """_semantic_search filters category/tags in SQL before the limit (DI-03)."""
    from remind_me_mcp.db import _semantic_search

    await memory_add(
        MemoryAddInput(content="perfectly matching decoy text", category="noise")
    )
    await memory_add(
        MemoryAddInput(
            content="loosely related target text",
            category="target",
            tags=["keep"],
        )
    )

    # The decoy is the nearest neighbour; with limit=1 a post-hoc filter
    # would return nothing.
    results = _semantic_search("perfectly matching decoy text", limit=1, category="target")
    assert [m["category"] for m in results] == ["target"]

    results = _semantic_search("perfectly matching decoy text", limit=1, tags=["keep"])
    assert [m["category"] for m in results] == ["target"]


async def test_fts_search_excludes_superseded(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """The FTS keyword tier excludes superseded memories (DI-02)."""
    canonical = memory_factory(content="Consolidated note about espresso machines")
    memory_factory(
        content="Duplicate note about espresso machines",
        superseded_by=canonical["id"],
    )

    params = MemorySearchInput(
        query="espresso machines", response_format=ResponseFormat.JSON
    )
    result = await memory_search(params)
    data = json.loads(result)

    contents = [m["content"] for m in data["memories"]]
    assert "Consolidated note about espresso machines" in contents
    assert "Duplicate note about espresso machines" not in contents


async def test_semantic_search_excludes_superseded(
    db_conn_with_vec: sqlite3.Connection, mock_embedder
) -> None:
    """The semantic vector tier excludes superseded memories (DI-02)."""
    from remind_me_mcp.db import _semantic_search

    await memory_add(MemoryAddInput(content="Canonical fact about hummingbird wings"))
    await memory_add(MemoryAddInput(content="Old duplicate fact about hummingbird wings"))

    canonical_id = db_conn_with_vec.execute(
        "SELECT id FROM memories WHERE content LIKE 'Canonical%'"
    ).fetchone()[0]
    db_conn_with_vec.execute(
        "UPDATE memories SET superseded_by = ? WHERE content LIKE 'Old duplicate%'",
        (canonical_id,),
    )
    db_conn_with_vec.commit()

    results = _semantic_search("Old duplicate fact about hummingbird wings", limit=10)

    ids = [m["id"] for m in results]
    assert canonical_id in ids
    assert all(m.get("superseded_by") is None for m in results)


# ---------------------------------------------------------------------------
# Debug signals, tier breakdown, dormant_excluded (Phase 13 Plan 02)
# ---------------------------------------------------------------------------


async def test_search_verbose_json_includes_debug_signals(
    db_conn: sqlite3.Connection,
) -> None:
    """Search with verbose=True and response_format=JSON includes debug_signals block per memory."""
    await memory_add(MemoryAddInput(content="Verbose debug test memory"))

    params = MemorySearchInput(
        query="Verbose debug test",
        response_format=ResponseFormat.JSON,
        verbose=True,
    )
    result = await memory_search(params)
    data = json.loads(result)

    assert data["returned"] >= 1
    mem = data["memories"][0]
    assert "debug_signals" in mem
    signals = mem["debug_signals"]
    assert "semantic_rank" in signals
    assert "keyword_rank" in signals
    assert "recency_rank" in signals
    assert "vitality_rank" in signals
    assert "days_old" in signals


async def test_search_verbose_false_no_debug_signals(
    db_conn: sqlite3.Connection,
) -> None:
    """Search with verbose=False (default) does NOT include debug_signals."""
    await memory_add(MemoryAddInput(content="No verbose test memory"))

    params = MemorySearchInput(
        query="No verbose test",
        response_format=ResponseFormat.JSON,
        verbose=False,
    )
    result = await memory_search(params)
    data = json.loads(result)

    assert data["returned"] >= 1
    mem = data["memories"][0]
    assert "debug_signals" not in mem


async def test_search_json_always_includes_tier_breakdown(
    db_conn: sqlite3.Connection,
) -> None:
    """JSON envelope always includes tier_breakdown with keyword/semantic/hybrid counts."""
    await memory_add(MemoryAddInput(content="Tier breakdown test memory"))

    params = MemorySearchInput(
        query="Tier breakdown test",
        response_format=ResponseFormat.JSON,
    )
    result = await memory_search(params)
    data = json.loads(result)

    assert "tier_breakdown" in data
    tb = data["tier_breakdown"]
    assert "keyword" in tb
    assert "semantic" in tb
    assert "hybrid" in tb


async def test_search_json_always_includes_dormant_excluded(
    db_conn: sqlite3.Connection,
) -> None:
    """JSON envelope always includes dormant_excluded count."""
    await memory_add(MemoryAddInput(content="Dormant excluded test memory"))

    params = MemorySearchInput(
        query="Dormant excluded test",
        response_format=ResponseFormat.JSON,
    )
    result = await memory_search(params)
    data = json.loads(result)

    assert "dormant_excluded" in data
    assert isinstance(data["dormant_excluded"], int)


async def test_search_dormant_excluded_count_accurate(
    db_conn: sqlite3.Connection,
    memory_factory,
) -> None:
    """dormant_excluded count matches actual number of dormant memories excluded."""
    # Create 2 active, 1 effectively dormant memory (90 unaccessed days at
    # decay_rate 0.1 -> read-time vitality well below the floor, DI-04)
    memory_factory(content="Active memory alpha")
    memory_factory(content="Active memory beta")
    memory_factory(
        content="Dormant memory gamma", accessed_at=_days_ago_iso(90), decay_rate=0.1
    )

    params = MemorySearchInput(
        query="memory",
        response_format=ResponseFormat.JSON,
        include_dormant=False,
    )
    result = await memory_search(params)
    data = json.loads(result)

    # Dormant memory should be excluded and counted
    assert data["dormant_excluded"] >= 1


async def test_search_markdown_verbose_shows_ranking_info(
    db_conn: sqlite3.Connection,
) -> None:
    """Markdown response with verbose=True shows ranking info per result."""
    await memory_add(MemoryAddInput(content="Markdown verbose ranking info test"))

    params = MemorySearchInput(
        query="Markdown verbose ranking",
        response_format=ResponseFormat.MARKDOWN,
        verbose=True,
    )
    result = await memory_search(params)

    # Should contain per-result ranking info
    assert "Ranks:" in result
    assert "kw=" in result
    assert "sem=" in result
    assert "days old" in result


async def test_search_markdown_always_shows_tier_line(
    db_conn: sqlite3.Connection,
) -> None:
    """Markdown response always includes tier breakdown summary line."""
    await memory_add(MemoryAddInput(content="Markdown tier summary line test"))

    params = MemorySearchInput(
        query="Markdown tier summary",
        response_format=ResponseFormat.MARKDOWN,
    )
    result = await memory_search(params)

    assert "Tiers:" in result
    assert "dormant excluded" in result


# ---------------------------------------------------------------------------
# PF-04: fire-and-forget tasks keep a strong reference until done
# ---------------------------------------------------------------------------


async def test_spawn_task_holds_strong_reference_until_done() -> None:
    """_spawn_task registers the task in the module-level set (so the event
    loop's weak reference is not the only one) and discards it on completion."""
    import asyncio

    from remind_me_mcp.tools import _background_tasks, _spawn_task

    started = asyncio.Event()
    release = asyncio.Event()

    async def waiter() -> str:
        started.set()
        await release.wait()
        return "done"

    task = _spawn_task(waiter())
    await started.wait()
    assert task in _background_tasks  # strong reference held while in flight

    release.set()
    assert await task == "done"
    # The done-callback runs via call_soon; yield once so it executes.
    await asyncio.sleep(0)
    assert task not in _background_tasks  # no leak after completion
