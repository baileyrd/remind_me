"""
Tests for remind_me_mcp.exporter and the remind_me_export_memories tool (FT-01).

Covers record collection (all columns, category/tag filters), JSON vs JSONL
rendering, file writes, the inline-size guard, export-root path validation in
ExportInput, and the round-trip guarantee: an exported file re-imports into a
fresh database with every memory's content preserved.

FT-06: also covers the entity-graph export (record_type-tagged entity /
memory_entity records, the include_graph opt-out, filter scoping) and the
import-side restore (alias union-merge, dangling-link skipping and counts).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from remind_me_mcp.db import (
    _ensure_schema,
    _entity_id,
    _link_memory_entity,
    _upsert_entity,
)
from remind_me_mcp.exporter import (
    collect_export_records,
    collect_graph_records,
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
# Entity-graph export and restore (FT-06)
# ---------------------------------------------------------------------------


def _seed_graph(db: sqlite3.Connection, memory_factory) -> tuple[dict, dict]:
    """Two memories linked to two entities, plus one unlinked entity.

    Returns the two memory dicts (the first in category 'keep').
    """
    mem_a = memory_factory(content="Bailey ships remind_me", category="keep")
    mem_b = memory_factory(content="Unrelated note", category="drop")
    eid_person = _upsert_entity(db, "Bailey Robertson", kind="person", aliases=["Bailey"])
    eid_project = _upsert_entity(db, "remind_me", kind="project")
    _upsert_entity(db, "Lonely Entity", kind="tool")  # no links
    _link_memory_entity(db, mem_a["id"], eid_person)
    _link_memory_entity(db, mem_a["id"], eid_project)
    _link_memory_entity(db, mem_b["id"], eid_project)
    db.commit()
    return mem_a, mem_b


def test_graph_records_follow_memories_in_json(db_conn: sqlite3.Connection, memory_factory) -> None:
    """Default export appends record_type-tagged graph records after memories."""
    mem_a, mem_b = _seed_graph(db_conn, memory_factory)
    records = collect_export_records()

    memories = [r for r in records if "record_type" not in r]
    entities = [r for r in records if r.get("record_type") == "entity"]
    links = [r for r in records if r.get("record_type") == "memory_entity"]
    assert len(memories) == 2
    assert len(entities) == 3  # unlinked entities are part of a full backup
    assert len(links) == 3

    # Memory records come first and are unchanged (importer-compatible).
    assert all("record_type" not in r for r in records[:2])
    assert records[0]["role"] == "assistant"

    person = next(e for e in entities if e["name"] == "Bailey Robertson")
    assert person["id"] == _entity_id("Bailey Robertson")
    assert person["kind"] == "person"
    assert person["aliases"] == ["Bailey"]  # deserialized, like tags/metadata
    assert "created_at" in person and "updated_at" in person

    assert {(li["memory_id"], li["entity_id"]) for li in links} == {
        (mem_a["id"], _entity_id("Bailey Robertson")),
        (mem_a["id"], _entity_id("remind_me")),
        (mem_b["id"], _entity_id("remind_me")),
    }
    assert all(li["created_at"] for li in links)


def test_graph_records_in_jsonl_file_export(db_conn: sqlite3.Connection, memory_factory, tmp_path: Path) -> None:
    """JSONL file exports carry the graph records, one per line."""
    _seed_graph(db_conn, memory_factory)
    dest = tmp_path / "graph_backup.jsonl"
    result = export_memories(format="jsonl", file_path=str(dest))
    assert result["status"] == "ok"
    assert result["exported"] == 2  # memory count only
    assert result["entities"] == 3
    assert result["links"] == 3

    lines = [json.loads(line) for line in dest.read_text().splitlines() if line.strip()]
    assert len(lines) == 8
    assert sum(1 for rec in lines if rec.get("record_type") == "entity") == 3
    assert sum(1 for rec in lines if rec.get("record_type") == "memory_entity") == 3


def test_include_graph_false_excludes_graph(db_conn: sqlite3.Connection, memory_factory) -> None:
    """The opt-out produces a memories-only export with no graph keys."""
    _seed_graph(db_conn, memory_factory)
    records = collect_export_records(include_graph=False)
    assert len(records) == 2
    assert all("record_type" not in r for r in records)

    result = export_memories(format="json", include_graph=False)
    assert result["exported"] == 2
    assert "entities" not in result
    assert "links" not in result
    assert all("record_type" not in r for r in json.loads(result["content"]))


def test_filtered_export_scopes_graph_to_exported_memories(db_conn: sqlite3.Connection, memory_factory) -> None:
    """Category/tag filters keep only links of exported memories and the
    entities those links reference (unlinked entities are dropped too)."""
    mem_a, _mem_b = _seed_graph(db_conn, memory_factory)
    records = collect_export_records(category="keep")

    memories = [r for r in records if "record_type" not in r]
    entities = [r for r in records if r.get("record_type") == "entity"]
    links = [r for r in records if r.get("record_type") == "memory_entity"]
    assert [m["content"] for m in memories] == ["Bailey ships remind_me"]
    assert {e["name"] for e in entities} == {"Bailey Robertson", "remind_me"}
    assert all(li["memory_id"] == mem_a["id"] for li in links)
    assert len(links) == 2


def test_collect_graph_records_orders_entities_before_links(db_conn: sqlite3.Connection, memory_factory) -> None:
    """Entities precede links so a sequential restore sees link endpoints."""
    _seed_graph(db_conn, memory_factory)
    kinds = [r["record_type"] for r in collect_graph_records()]
    assert kinds == ["entity"] * 3 + ["memory_entity"] * 3


def test_inline_limit_counts_graph_records(db_conn: sqlite3.Connection, memory_factory) -> None:
    """The inline cap is about payload size: graph records count against it."""
    _seed_graph(db_conn, memory_factory)
    # 2 memories alone fit, but 2 + 6 graph records exceed the cap of 5.
    result = export_memories(format="json", inline_max=5)
    assert result["status"] == "error"
    assert "file_path" in result["error"]
    # The opt-out brings the export back under the cap.
    assert export_memories(format="json", inline_max=5, include_graph=False)["status"] == "ok"


@pytest.mark.parametrize("fmt", ["json", "jsonl"])
def test_restore_graph_into_db_with_original_memories(
    db_conn: sqlite3.Connection, memory_factory, tmp_path: Path, fmt: str
) -> None:
    """export -> wipe graph -> re-import restores entities AND links, because
    the referenced memories still exist under their original ids."""
    _seed_graph(db_conn, memory_factory)
    original_links = {
        (r["memory_id"], r["entity_id"])
        for r in db_conn.execute("SELECT memory_id, entity_id FROM memory_entities").fetchall()
    }
    dest = tmp_path / f"graph_restore.{fmt}"
    assert export_memories(format=fmt, file_path=str(dest))["status"] == "ok"

    db_conn.execute("DELETE FROM memory_entities")
    db_conn.execute("DELETE FROM entities")
    db_conn.commit()

    result = import_chat_file(
        file_path=str(dest),
        category="restored",
        tags=[],
        extract_mode="assistant_messages",
        max_length=10000,
    )
    assert result["status"] == "ok"
    assert result["entities_restored"] == 3
    assert result["links_restored"] == 3
    assert result["links_skipped_dangling"] == 0

    restored_links = {
        (r["memory_id"], r["entity_id"])
        for r in db_conn.execute("SELECT memory_id, entity_id FROM memory_entities").fetchall()
    }
    assert restored_links == original_links
    person = db_conn.execute(
        "SELECT * FROM entities WHERE id = ?", (_entity_id("Bailey Robertson"),)
    ).fetchone()
    assert person is not None
    assert person["kind"] == "person"
    assert json.loads(person["aliases"]) == ["Bailey"]


def test_fresh_db_import_skips_dangling_links(
    db_conn: sqlite3.Connection, fresh_db: sqlite3.Connection, memory_factory, tmp_path: Path
) -> None:
    """A fresh-DB re-import assigns NEW memory ids, so every link is dangling:
    entities restore, links are skipped and counted, nothing is stored as junk."""
    _seed_graph(db_conn, memory_factory)
    dest = tmp_path / "fresh_restore.json"
    assert export_memories(format="json", file_path=str(dest))["status"] == "ok"

    result = import_chat_file(
        file_path=str(dest),
        category="restored",
        tags=[],
        extract_mode="assistant_messages",
        max_length=10000,
    )
    assert result["status"] == "ok"
    assert result["memories_created"] == 2  # graph records never become memories
    assert result["entities_restored"] == 3
    assert result["links_restored"] == 0
    assert result["links_skipped_dangling"] == 3

    assert fresh_db.execute("SELECT COUNT(*) FROM memory_entities").fetchone()[0] == 0
    assert fresh_db.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 3
    # No memory was created from an entity record's fields.
    contents = {r["content"] for r in fresh_db.execute("SELECT content FROM memories").fetchall()}
    assert contents == {"Bailey ships remind_me", "Unrelated note"}


def test_restore_union_merges_aliases_into_existing_entity(
    db_conn: sqlite3.Connection, fresh_db: sqlite3.Connection, memory_factory, tmp_path: Path
) -> None:
    """Restoring over an existing entity union-merges aliases (existing first,
    like sync) and fills in a missing kind instead of clobbering."""
    memory_factory(content="Alias merge memory")
    _upsert_entity(db_conn, "Bailey Robertson", kind="person", aliases=["Bailey", "B-Rob"])
    db_conn.commit()
    dest = tmp_path / "alias_merge.json"
    assert export_memories(format="json", file_path=str(dest))["status"] == "ok"

    _upsert_entity(fresh_db, "Bailey Robertson", aliases=["bdog"])
    fresh_db.commit()

    result = import_chat_file(
        file_path=str(dest),
        category="restored",
        tags=[],
        extract_mode="assistant_messages",
        max_length=10000,
    )
    assert result["status"] == "ok"
    assert result["entities_restored"] == 1

    row = fresh_db.execute(
        "SELECT * FROM entities WHERE id = ?", (_entity_id("Bailey Robertson"),)
    ).fetchone()
    assert json.loads(row["aliases"]) == ["bdog", "Bailey", "B-Rob"]
    assert row["kind"] == "person"  # filled in, since it was locally missing


def test_import_without_graph_records_reports_no_graph_keys(
    db_conn: sqlite3.Connection, fresh_db: sqlite3.Connection, memory_factory, tmp_path: Path
) -> None:
    """A memories-only export imports exactly as before FT-06 — the result
    carries no graph-restore keys (backward-compatible output)."""
    memory_factory(content="Plain export memory")
    dest = tmp_path / "plain.json"
    assert export_memories(format="json", file_path=str(dest), include_graph=False)["status"] == "ok"

    result = import_chat_file(
        file_path=str(dest),
        category="restored",
        tags=[],
        extract_mode="assistant_messages",
        max_length=10000,
    )
    assert result["status"] == "ok"
    assert result["memories_created"] == 1
    assert "entities_restored" not in result
    assert "links_restored" not in result


async def test_tool_export_include_graph_flag(db_conn: sqlite3.Connection, memory_factory) -> None:
    """The MCP tool exposes include_graph: default on, opt-out honored."""
    _seed_graph(db_conn, memory_factory)

    result = json.loads(await memory_export(ExportInput()))
    assert result["entities"] == 3
    assert result["links"] == 3
    records = json.loads(result["content"])
    assert sum(1 for r in records if "record_type" in r) == 6

    result = json.loads(await memory_export(ExportInput(include_graph=False)))
    assert "entities" not in result
    assert all("record_type" not in r for r in json.loads(result["content"]))


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
