"""
Tests for FT-02 — generic document ingestion.

Covers the document parsing helpers (_split_markdown_sections, _parse_document,
_looks_like_chat_markdown), the kind-aware import_chat_file / import_directory
pipeline (per-section chunking, heading metadata, source/category assignment,
hash dedup), and the ImportKind surface on the MCP input models.

Follows the test_importer.py patterns: db_conn fixture for an isolated
in-memory database, _embed_and_store_rows monkeypatched to a no-op.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from remind_me_mcp.importer import (
    DOCUMENT_CATEGORY,
    DOCUMENT_SOURCE,
    _looks_like_chat_markdown,
    _parse_document,
    _split_markdown_sections,
    import_chat_file,
    import_directory,
)

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


NOTES_MD = """# Projects

## Remind Me
A personal AI memory store built on SQLite with hybrid search.

## Home Lab
Notes about the home lab VLAN configuration and the NAS backup job.
"""

CHAT_MD = "## Human\nWhat is Python?\n\n## Assistant\nPython is a programming language.\n"

PLAIN_NOTES = "Just some plain notes about gardening.\nTomatoes need full sun."


@pytest.fixture()
def no_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable real embedding during import tests (pattern from test_importer.py)."""
    import remind_me_mcp.importer as _importer_mod

    monkeypatch.setattr(_importer_mod, "_embed_and_store_rows", lambda rows: len(rows))


def _import(path: Path, **kwargs) -> dict:
    """Call import_chat_file with test defaults, overridable via kwargs."""
    defaults = {
        "file_path": str(path),
        "category": "chat_import",
        "tags": [],
        "extract_mode": "assistant_messages",
        "max_length": 10000,
        "kind": "auto",
    }
    defaults.update(kwargs)
    return import_chat_file(**defaults)


# ---------------------------------------------------------------------------
# _split_markdown_sections
# ---------------------------------------------------------------------------


def test_split_sections_basic_headings() -> None:
    """Each heading starts a new section; heading-only sections are dropped."""
    sections = _split_markdown_sections(NOTES_MD)
    headings = [h for h, _ in sections]
    # "# Projects" has no body of its own (next line is a sub-heading) — dropped.
    assert headings == ["Projects > Remind Me", "Projects > Home Lab"]
    assert "SQLite" in sections[0][1]
    assert "VLAN" in sections[1][1]


def test_split_sections_preamble_before_first_heading() -> None:
    """Content before the first heading becomes a section with heading None."""
    text = "Intro paragraph.\n\n# First\nBody one.\n"
    sections = _split_markdown_sections(text)
    assert sections[0] == (None, "Intro paragraph.")
    assert sections[1] == ("First", "Body one.")


def test_split_sections_breadcrumb_resets_on_sibling() -> None:
    """A sibling heading at the same level replaces, not extends, the breadcrumb."""
    text = "# A\nbody a\n## B\nbody b\n## C\nbody c\n# D\nbody d\n"
    sections = _split_markdown_sections(text)
    assert [h for h, _ in sections] == ["A", "A > B", "A > C", "D"]


def test_split_sections_ignores_headings_in_code_fences() -> None:
    """'#' lines inside fenced code blocks are not treated as headings."""
    text = "# Real\nbefore\n```\n# not a heading\ncode line\n```\nafter\n"
    sections = _split_markdown_sections(text)
    assert len(sections) == 1
    heading, body = sections[0]
    assert heading == "Real"
    assert "# not a heading" in body
    assert "after" in body


def test_split_sections_no_headings_single_section() -> None:
    """A file without headings yields one section with heading None."""
    sections = _split_markdown_sections(PLAIN_NOTES)
    assert len(sections) == 1
    assert sections[0][0] is None


def test_split_sections_empty_text() -> None:
    """Empty or whitespace-only input yields no sections."""
    assert _split_markdown_sections("") == []
    assert _split_markdown_sections("   \n\n  ") == []


# ---------------------------------------------------------------------------
# _parse_document
# ---------------------------------------------------------------------------


def test_parse_document_markdown_keeps_heading_context() -> None:
    """Markdown chunks carry the heading breadcrumb in content and metadata slot."""
    pairs = _parse_document(NOTES_MD, ".md", max_length=10000)
    assert len(pairs) == 2
    content, heading = pairs[0]
    assert heading == "Projects > Remind Me"
    assert content.startswith("Projects > Remind Me\n\n")
    assert "SQLite" in content


def test_parse_document_long_section_falls_back_to_chunking() -> None:
    """A section longer than max_length splits into multiple chunks, each
    keeping the same heading context (FT-02 fallback chunking)."""
    body = "\n\n".join(f"Paragraph {i} about the project. " + "x" * 60 for i in range(6))
    text = f"## Long Section\n{body}\n"
    pairs = _parse_document(text, ".md", max_length=150)
    assert len(pairs) > 1
    for content, heading in pairs:
        assert heading == "Long Section"
        assert content.startswith("Long Section\n\n")


def test_parse_document_plain_text_paragraph_chunking() -> None:
    """Plain .txt is paragraph/size chunked with no heading metadata."""
    text = "\n\n".join(f"Note paragraph number {i}. " + "y" * 50 for i in range(4))
    pairs = _parse_document(text, ".txt", max_length=120)
    assert len(pairs) > 1
    assert all(heading is None for _, heading in pairs)
    joined = " ".join(content for content, _ in pairs)
    assert "Note paragraph number 3" in joined


# ---------------------------------------------------------------------------
# _looks_like_chat_markdown (auto-detection sniffer)
# ---------------------------------------------------------------------------


def test_chat_markdown_detected_as_chat() -> None:
    """Role-structured markdown is recognised as a chat export."""
    assert _looks_like_chat_markdown(CHAT_MD) is True
    assert _looks_like_chat_markdown("**User:**\nHi\n\n**Assistant:**\nHello\n") is True


def test_notes_markdown_not_detected_as_chat() -> None:
    """Notes markdown and plain text have no role structure."""
    assert _looks_like_chat_markdown(NOTES_MD) is False
    assert _looks_like_chat_markdown(PLAIN_NOTES) is False


# ---------------------------------------------------------------------------
# import_chat_file — document kind end-to-end
# ---------------------------------------------------------------------------


def test_import_markdown_notes_per_section(
    db_conn: sqlite3.Connection, no_embed: None, tmp_path: Path
) -> None:
    """Markdown notes import per-section with heading metadata, document source,
    and the 'document' default category."""
    f = tmp_path / "notes.md"
    f.write_text(NOTES_MD)

    result = _import(f)
    assert result["status"] == "ok"
    assert result["kind"] == "document"
    assert result["memories_created"] == 2
    assert result["raw_entries"] == 2

    rows = db_conn.execute(
        "SELECT content, category, source, metadata FROM memories ORDER BY content"
    ).fetchall()
    assert len(rows) == 2
    sections = set()
    for row in rows:
        assert row["source"] == DOCUMENT_SOURCE
        assert row["category"] == DOCUMENT_CATEGORY
        meta = json.loads(row["metadata"])
        assert meta["filename"] == "notes.md"
        assert meta["import_id"] == result["import_id"]
        sections.add(meta["section"])
    assert sections == {"Projects > Remind Me", "Projects > Home Lab"}


def test_import_document_long_section_chunked(
    db_conn: sqlite3.Connection, no_embed: None, tmp_path: Path
) -> None:
    """A long section becomes several memories, all tagged with its heading."""
    body = "\n\n".join(f"Fact number {i}. " + "z" * 70 for i in range(6))
    f = tmp_path / "long.md"
    f.write_text(f"# Big Topic\n{body}\n")

    result = _import(f, max_length=150)
    assert result["status"] == "ok"
    assert result["kind"] == "document"
    assert result["memories_created"] > 1

    rows = db_conn.execute("SELECT content, metadata FROM memories").fetchall()
    assert len(rows) == result["memories_created"]
    for row in rows:
        assert json.loads(row["metadata"])["section"] == "Big Topic"
        assert row["content"].startswith("Big Topic\n\n")


def test_import_plain_text_document(
    db_conn: sqlite3.Connection, no_embed: None, tmp_path: Path
) -> None:
    """A plain .txt notes file imports as a document with no section metadata."""
    f = tmp_path / "notes.txt"
    f.write_text(PLAIN_NOTES)

    result = _import(f)
    assert result["status"] == "ok"
    assert result["kind"] == "document"
    assert result["memories_created"] == 1

    row = db_conn.execute("SELECT source, category, metadata FROM memories").fetchone()
    assert row["source"] == DOCUMENT_SOURCE
    assert row["category"] == DOCUMENT_CATEGORY
    meta = json.loads(row["metadata"])
    assert "section" not in meta
    assert meta["filename"] == "notes.txt"


def test_import_document_custom_category_honored(
    db_conn: sqlite3.Connection, no_embed: None, tmp_path: Path
) -> None:
    """An explicit non-default category is kept for document imports."""
    f = tmp_path / "research.md"
    f.write_text("# Findings\nThe experiment succeeded.\n")

    result = _import(f, category="research")
    assert result["status"] == "ok"
    row = db_conn.execute("SELECT category FROM memories").fetchone()
    assert row["category"] == "research"


# ---------------------------------------------------------------------------
# Auto-detection routing
# ---------------------------------------------------------------------------


def test_auto_chat_markdown_still_imports_as_chat(
    db_conn: sqlite3.Connection, no_embed: None, tmp_path: Path
) -> None:
    """kind='auto' routes role-structured markdown through the chat parser
    (existing behavior preserved)."""
    f = tmp_path / "chat_export.md"
    f.write_text(CHAT_MD)

    result = _import(f, extract_mode="all_messages")
    assert result["status"] == "ok"
    assert result["kind"] == "chat"
    assert result["memories_created"] == 2

    rows = db_conn.execute("SELECT source, category, metadata FROM memories").fetchall()
    for row in rows:
        assert row["source"] == "chat_import"
        assert row["category"] == "chat_import"
        assert "section" not in json.loads(row["metadata"])


def test_auto_json_always_chat(
    db_conn: sqlite3.Connection, no_embed: None, tmp_path: Path
) -> None:
    """kind='auto' never routes .json through the document parser."""
    f = tmp_path / "export.json"
    f.write_text(json.dumps([{"role": "assistant", "content": "# Heading-looking text"}]))

    result = _import(f)
    assert result["status"] == "ok"
    assert result["kind"] == "chat"


def test_explicit_kind_chat_forces_chat_parser(
    db_conn: sqlite3.Connection, no_embed: None, tmp_path: Path
) -> None:
    """kind='chat' on a notes file keeps the legacy whole-file-as-one-memory
    fallback of the chat markdown parser."""
    f = tmp_path / "notes.md"
    f.write_text(NOTES_MD)

    result = _import(f, kind="chat", extract_mode="all_messages")
    assert result["status"] == "ok"
    assert result["kind"] == "chat"
    assert result["memories_created"] == 1
    row = db_conn.execute("SELECT source FROM memories").fetchone()
    assert row["source"] == "chat_import"


def test_explicit_kind_document_forces_document_parser(
    db_conn: sqlite3.Connection, no_embed: None, tmp_path: Path
) -> None:
    """kind='document' on chat-style markdown bypasses the chat parser."""
    f = tmp_path / "looks_like_chat.md"
    f.write_text(CHAT_MD)

    result = _import(f, kind="document")
    assert result["status"] == "ok"
    assert result["kind"] == "document"
    rows = db_conn.execute("SELECT source FROM memories").fetchall()
    assert rows
    assert all(r["source"] == DOCUMENT_SOURCE for r in rows)


def test_kind_document_rejects_json(
    db_conn: sqlite3.Connection, no_embed: None, tmp_path: Path
) -> None:
    """kind='document' is invalid for .json/.jsonl files."""
    f = tmp_path / "export.json"
    f.write_text("[]")

    result = _import(f, kind="document")
    assert result["status"] == "error"
    assert "document import" in result["reason"]


def test_invalid_kind_rejected(
    db_conn: sqlite3.Connection, no_embed: None, tmp_path: Path
) -> None:
    """An unknown kind value returns a status='error' dict."""
    f = tmp_path / "notes.md"
    f.write_text(NOTES_MD)

    result = _import(f, kind="banana")
    assert result["status"] == "error"
    assert "invalid kind" in result["reason"]


# ---------------------------------------------------------------------------
# Dedup-by-hash
# ---------------------------------------------------------------------------


def test_document_reimport_skipped_by_hash(
    db_conn: sqlite3.Connection, no_embed: None, tmp_path: Path
) -> None:
    """Re-importing the same document content is a no-op (hash dedup)."""
    f = tmp_path / "notes.md"
    f.write_text(NOTES_MD)

    first = _import(f)
    assert first["status"] == "ok"

    second = _import(f)
    assert second["status"] == "skipped"
    assert second["reason"] == "already_imported"
    assert second["import_id"] == first["import_id"]

    count = db_conn.execute("SELECT COUNT(*) AS cnt FROM memories").fetchone()["cnt"]
    assert count == first["memories_created"]


# ---------------------------------------------------------------------------
# Directory import mixing chat exports and notes
# ---------------------------------------------------------------------------


async def test_import_directory_mixed_chat_and_documents(
    db_conn: sqlite3.Connection, no_embed: None, tmp_path: Path
) -> None:
    """A directory with chat exports and notes files routes each file by kind."""
    (tmp_path / "chat.json").write_text(
        json.dumps({"chat_messages": [{"sender": "assistant", "content": "JSON answer."}]})
    )
    (tmp_path / "chat.md").write_text(CHAT_MD)
    (tmp_path / "notes.md").write_text(NOTES_MD)
    (tmp_path / "notes.txt").write_text(PLAIN_NOTES)

    summary = await import_directory(directory=str(tmp_path))
    assert summary["files_processed"] == 4
    assert summary["imported"] == 4
    assert summary["errors"] == 0

    kinds = {d["file"]: d["kind"] for d in summary["details"]}
    assert kinds == {
        "chat.json": "chat",
        "chat.md": "chat",
        "notes.md": "document",
        "notes.txt": "document",
    }

    sources = {
        r["source"]
        for r in db_conn.execute("SELECT DISTINCT source FROM memories").fetchall()
    }
    assert sources == {"chat_import", DOCUMENT_SOURCE}


# ---------------------------------------------------------------------------
# ImportKind model surface + IMPORT_ROOTS enforcement
# ---------------------------------------------------------------------------


def test_chat_import_input_kind_default_and_values(tmp_path: Path) -> None:
    """ChatImportInput defaults kind to 'auto' and accepts explicit kinds."""
    from remind_me_mcp.models import ChatImportInput, ImportKind

    f = tmp_path / "notes.md"
    f.write_text(NOTES_MD)

    assert ChatImportInput(file_path=str(f)).kind == ImportKind.AUTO
    assert ChatImportInput(file_path=str(f), kind="document").kind == ImportKind.DOCUMENT
    assert ChatImportInput(file_path=str(f), kind="chat").kind == ImportKind.CHAT


def test_chat_import_input_rejects_invalid_kind(tmp_path: Path) -> None:
    """An unknown kind value fails model validation."""
    from pydantic import ValidationError

    from remind_me_mcp.models import ChatImportInput

    f = tmp_path / "notes.md"
    f.write_text(NOTES_MD)

    with pytest.raises(ValidationError):
        ChatImportInput(file_path=str(f), kind="banana")


def test_document_import_rejects_path_outside_import_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SE-02 parity: document imports respect IMPORT_ROOTS containment."""
    from pydantic import ValidationError

    import remind_me_mcp.config as _cfg
    from remind_me_mcp.models import BulkImportDirInput, ChatImportInput

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside = outside_dir / "notes.md"
    outside.write_text(NOTES_MD)

    monkeypatch.setattr(_cfg, "IMPORT_ROOTS", [allowed.resolve()])

    with pytest.raises(ValidationError) as exc_info:
        ChatImportInput(file_path=str(outside), kind="document")
    assert "not in allowed import roots" in str(exc_info.value)

    with pytest.raises(ValidationError) as exc_info:
        BulkImportDirInput(directory=str(outside_dir), kind="document")
    assert "not in allowed import roots" in str(exc_info.value)


# ---------------------------------------------------------------------------
# MCP tool surface (memory_import_chat / memory_import_directory)
# ---------------------------------------------------------------------------


async def test_tool_import_document_markdown(
    db_conn: sqlite3.Connection, no_embed: None, tmp_path: Path
) -> None:
    """remind_me_import_chat ingests a notes file as a document via kind=auto."""
    from remind_me_mcp.models import ChatImportInput
    from remind_me_mcp.tools.admin import memory_import_chat

    f = tmp_path / "notes.md"
    f.write_text(NOTES_MD)

    result = json.loads(await memory_import_chat(ChatImportInput(file_path=str(f))))
    assert result["status"] == "ok"
    assert result["kind"] == "document"
    assert result["memories_created"] == 2


async def test_tool_import_directory_kind_param(
    db_conn: sqlite3.Connection, no_embed: None, tmp_path: Path
) -> None:
    """remind_me_import_directory forwards the kind parameter."""
    from remind_me_mcp.models import BulkImportDirInput
    from remind_me_mcp.tools.admin import memory_import_directory

    (tmp_path / "notes.md").write_text(NOTES_MD)

    params = BulkImportDirInput(directory=str(tmp_path), kind="document")
    summary = json.loads(await memory_import_directory(params))
    assert summary["imported"] == 1
    assert summary["details"][0]["kind"] == "document"
