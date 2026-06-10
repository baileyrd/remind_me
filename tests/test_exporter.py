"""
Tests for remind_me_mcp.exporter and the remind_me_export_memories tool (FT-01).

Covers record collection (all columns, category/tag filters), JSON vs JSONL
rendering, file writes, the inline-size guard, export-root path validation in
ExportInput, and the round-trip guarantee: an exported file re-imports into a
fresh database with every memory's content preserved.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from remind_me_mcp.db import _ensure_schema
from remind_me_mcp.exporter import (
    collect_export_records,
    export_memories,
    render_export,
)
from remind_me_mcp.importer import import_chat_file
from remind_me_mcp.models import ExportFormat, ExportInput
from remind_me_mcp.tools import memory_export

# ---------------------------------------------------------------------------
# collect_export_records
# ---------------------------------------------------------------------------


def test_collect_records_includes_all_memory_columns(db_conn: sqlite3.Connection, memory_factory) -> None:
    """Every memories-table column appears in the export record (full backup)."""
    memory_factory(
        content="Full column export test",
        category="work",
        tags=["alpha", "beta"],
        metadata={"project": "remind_me"},
    )
    records = collect_export_records()
    assert len(records) == 1
    rec = records[0]

    # Core columns
    for key in ("id", "content", "category", "tags", "source", "metadata", "created_at", "updated_at"):
        assert key in rec, f"missing column {key!r}"
    # Lifecycle / structured-memory columns from later schema versions
    for key in ("vitality", "status", "memory_type", "superseded_by", "capture_id", "subject"):
        assert key in rec, f"missing lifecycle column {key!r}"

    # JSON columns are deserialized; importer-compat role marker is present
    assert rec["tags"] == ["alpha", "beta"]
    assert rec["metadata"] == {"project": "remind_me"}
    assert rec["role"] == "assistant"
    assert rec["content"] == "Full column export test"
    # Embedding vectors are never part of a record
    assert "embedding" not in rec


def test_collect_records_category_filter(db_conn: sqlite3.Connection, memory_factory) -> None:
    """Only memories in the requested category are exported."""
    memory_factory(content="Work note", category="work")
    memory_factory(content="Personal note", category="personal")

    records = collect_export_records(category="work")
    assert len(records) == 1
    assert records[0]["content"] == "Work note"


def test_collect_records_tag_filter_requires_all_tags(db_conn: sqlite3.Connection, memory_factory) -> None:
    """Tag filtering uses ALL-of semantics, matching list/search behavior."""
    memory_factory(content="Both tags", tags=["python", "work"])
    memory_factory(content="One tag only", tags=["python"])
    memory_factory(content="No tags", tags=[])

    records = collect_export_records(tags=["python", "work"])
    assert [r["content"] for r in records] == ["Both tags"]

    records = collect_export_records(tags=["python"])
    assert {r["content"] for r in records} == {"Both tags", "One tag only"}


def test_collect_records_empty_db(db_conn: sqlite3.Connection) -> None:
    """An empty database exports an empty record list."""
    assert collect_export_records() == []


# ---------------------------------------------------------------------------
# render_export
# ---------------------------------------------------------------------------


def test_render_json_is_parseable_array(db_conn: sqlite3.Connection, memory_factory) -> None:
    """JSON rendering produces a parseable array of records."""
    memory_factory(content="First")
    memory_factory(content="Second")
    payload = render_export(collect_export_records(), "json")
    parsed = json.loads(payload)
    assert isinstance(parsed, list)
    assert {r["content"] for r in parsed} == {"First", "Second"}


def test_render_jsonl_one_record_per_line(db_conn: sqlite3.Connection, memory_factory) -> None:
    """JSONL rendering writes exactly one JSON object per line."""
    memory_factory(content="Line one")
    memory_factory(content="Line two")
    payload = render_export(collect_export_records(), "jsonl")
    lines = [line for line in payload.splitlines() if line.strip()]
    assert len(lines) == 2
    for line in lines:
        rec = json.loads(line)
        assert "content" in rec
        assert rec["role"] == "assistant"


def test_render_unknown_format_raises(db_conn: sqlite3.Connection) -> None:
    """An unsupported format raises ValueError."""
    with pytest.raises(ValueError, match="Unsupported export format"):
        render_export([], "xml")


# ---------------------------------------------------------------------------
# export_memories — inline and file modes
# ---------------------------------------------------------------------------


def test_export_inline_returns_content(db_conn: sqlite3.Connection, memory_factory) -> None:
    """Without file_path the rendered payload is returned inline."""
    memory_factory(content="Inline export memory")
    result = export_memories(format="json")
    assert result["status"] == "ok"
    assert result["exported"] == 1
    assert result["format"] == "json"
    parsed = json.loads(result["content"])
    assert parsed[0]["content"] == "Inline export memory"


def test_export_inline_limit_exceeded(db_conn: sqlite3.Connection, memory_factory) -> None:
    """Inline exports above inline_max return an error pointing at file_path."""
    for i in range(3):
        memory_factory(content=f"Inline limit memory {i}")
    result = export_memories(format="json", inline_max=2)
    assert result["status"] == "error"
    assert "file_path" in result["error"]


def test_export_to_file_json(db_conn: sqlite3.Connection, memory_factory, tmp_path: Path) -> None:
    """File export writes the payload and returns a summary."""
    memory_factory(content="File export memory")
    dest = tmp_path / "backup.json"
    result = export_memories(format="json", file_path=str(dest))
    assert result["status"] == "ok"
    assert result["exported"] == 1
    assert result["file"] == str(dest)
    assert result["bytes"] == len(dest.read_bytes())
    assert json.loads(dest.read_text())[0]["content"] == "File export memory"


def test_export_to_file_jsonl(db_conn: sqlite3.Connection, memory_factory, tmp_path: Path) -> None:
    """JSONL file export writes one record per line."""
    memory_factory(content="JSONL one")
    memory_factory(content="JSONL two")
    dest = tmp_path / "backup.jsonl"
    result = export_memories(format="jsonl", file_path=str(dest))
    assert result["status"] == "ok"
    assert result["exported"] == 2
    lines = [line for line in dest.read_text().splitlines() if line.strip()]
    assert len(lines) == 2


# ---------------------------------------------------------------------------
# Round-trip: export -> import into a fresh database
# ---------------------------------------------------------------------------


@pytest.fixture()
def fresh_db(monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    """A second, empty in-memory database wired into the importer.

    Re-patches remind_me_mcp.importer._get_db (the db_conn fixture pointed it
    at the source database) so import_chat_file writes into this fresh DB,
    simulating migration to a new machine.
    """
    import remind_me_mcp.importer as _importer_mod

    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    _ensure_schema(db)
    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db)
    yield db
    db.close()


def _round_trip(db_conn: sqlite3.Connection, fresh_db: sqlite3.Connection, tmp_path: Path, fmt: str) -> None:
    """Export db_conn to a file in *fmt*, import it into fresh_db, compare content."""
    original = {
        row["content"]
        for row in db_conn.execute("SELECT content FROM memories").fetchall()
    }
    dest = tmp_path / f"backup.{fmt}"
    result = export_memories(format=fmt, file_path=str(dest))
    assert result["status"] == "ok"
    assert result["exported"] == len(original)

    import_result = import_chat_file(
        file_path=str(dest),
        category="restored",
        tags=[],
        extract_mode="assistant_messages",
        max_length=10000,
    )
    assert import_result["status"] == "ok"
    assert import_result["memories_created"] == len(original)

    restored = {
        row["content"]
        for row in fresh_db.execute("SELECT content FROM memories").fetchall()
    }
    assert restored == original


def test_round_trip_json(db_conn: sqlite3.Connection, fresh_db: sqlite3.Connection, memory_factory, tmp_path: Path) -> None:
    """add -> export (json) -> import into a fresh DB preserves all content."""
    memory_factory(content="Round trip fact one", category="fact", tags=["rt"])
    memory_factory(content="Round trip preference two", category="preference")
    memory_factory(content="Round trip note three", metadata={"k": "v"})
    _round_trip(db_conn, fresh_db, tmp_path, "json")


def test_round_trip_jsonl(db_conn: sqlite3.Connection, fresh_db: sqlite3.Connection, memory_factory, tmp_path: Path) -> None:
    """add -> export (jsonl) -> import into a fresh DB preserves all content."""
    memory_factory(content="JSONL round trip alpha")
    memory_factory(content="JSONL round trip beta")
    _round_trip(db_conn, fresh_db, tmp_path, "jsonl")


# ---------------------------------------------------------------------------
# ExportInput path validation (mirrors SE-02)
# ---------------------------------------------------------------------------


def test_export_input_rejects_path_outside_roots() -> None:
    """A destination outside EXPORT_ROOTS is rejected by the input model."""
    with pytest.raises(ValidationError) as exc_info:
        ExportInput(file_path="/etc/exfiltrated.json")
    assert "not in allowed export roots" in str(exc_info.value)


def test_export_input_rejects_traversal_attempt() -> None:
    """Traversal sequences are resolved before the containment check."""
    traversal = str(Path.home() / ".." / ".." / "etc" / "evil.json")
    with pytest.raises(ValidationError) as exc_info:
        ExportInput(file_path=traversal)
    assert "not in allowed export roots" in str(exc_info.value)


def test_export_input_rejects_directory_destination(tmp_path: Path) -> None:
    """A directory destination is rejected — exports go to a file."""
    with pytest.raises(ValidationError) as exc_info:
        ExportInput(file_path=str(tmp_path))
    assert "directory" in str(exc_info.value)


def test_export_input_rejects_missing_parent(tmp_path: Path) -> None:
    """A destination in a nonexistent directory is rejected."""
    with pytest.raises(ValidationError) as exc_info:
        ExportInput(file_path=str(tmp_path / "missing" / "backup.json"))
    assert "Parent directory not found" in str(exc_info.value)


def test_export_input_accepts_path_inside_roots(tmp_path: Path) -> None:
    """A valid destination inside EXPORT_ROOTS resolves and is accepted."""
    params = ExportInput(file_path=str(tmp_path / "backup.json"))
    assert params.file_path == str((tmp_path / "backup.json").resolve())


def test_export_input_rejects_invalid_format() -> None:
    """Formats other than json/jsonl are rejected by the enum field."""
    with pytest.raises(ValidationError):
        ExportInput(format="xml")


def test_export_input_defaults() -> None:
    """Defaults: json format, no filters, inline (no file_path)."""
    params = ExportInput()
    assert params.format == ExportFormat.JSON
    assert params.category is None
    assert params.tags is None
    assert params.file_path is None


# ---------------------------------------------------------------------------
# remind_me_export_memories MCP tool handler
# ---------------------------------------------------------------------------


async def test_tool_export_inline(db_conn: sqlite3.Connection, memory_factory) -> None:
    """The tool returns an inline JSON export for small vaults."""
    memory_factory(content="Tool inline export memory", category="work")
    result = json.loads(await memory_export(ExportInput()))
    assert result["status"] == "ok"
    assert result["exported"] == 1
    records = json.loads(result["content"])
    assert records[0]["content"] == "Tool inline export memory"
    assert records[0]["category"] == "work"


async def test_tool_export_to_file(db_conn: sqlite3.Connection, memory_factory, tmp_path: Path) -> None:
    """The tool writes the export file and reports a summary."""
    memory_factory(content="Tool file export memory")
    dest = tmp_path / "tool_backup.jsonl"
    result = json.loads(
        await memory_export(ExportInput(format="jsonl", file_path=str(dest)))
    )
    assert result["status"] == "ok"
    assert result["exported"] == 1
    assert dest.exists()
    assert json.loads(dest.read_text().splitlines()[0])["content"] == "Tool file export memory"


async def test_tool_export_category_filter(db_conn: sqlite3.Connection, memory_factory) -> None:
    """The tool applies category filters to the export."""
    memory_factory(content="Filtered in", category="keep")
    memory_factory(content="Filtered out", category="drop")
    result = json.loads(await memory_export(ExportInput(category="keep")))
    records = json.loads(result["content"])
    assert [r["content"] for r in records] == ["Filtered in"]


async def test_tool_export_inline_limit(
    db_conn: sqlite3.Connection, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Above the inline limit the tool returns an error suggesting file_path."""
    import remind_me_mcp.tools.admin as _admin_mod

    monkeypatch.setattr(_admin_mod, "EXPORT_INLINE_MAX", 1)
    memory_factory(content="Limit memory one")
    memory_factory(content="Limit memory two")
    result = json.loads(await memory_export(ExportInput()))
    assert result["status"] == "error"
    assert "file_path" in result["error"]
