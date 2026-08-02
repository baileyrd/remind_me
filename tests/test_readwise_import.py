"""Tests for remind_me_mcp.readwise_import — the "readwise" import connector
(FT-20, issue #182).

readwise_import.py has no optional dependency (stdlib json only), unlike
pdf_import.py/image_import.py, so unlike test_pdf_import.py/test_image_import.py
there is nothing here to skip -- these tests always run.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

import pytest

from remind_me_mcp.importer import IMPORT_KINDS, import_chat_file, import_content
from remind_me_mcp.readwise_import import (
    READWISE_FORMAT_ERROR,
    _extract_results,
    _highlight_content,
    _highlight_metadata,
    _readwise_connector,
)

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


def _make_export(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a Readwise export shaped exactly like the real, documented
    Export API response (GET /api/v2/export/ -- see readwise.io/api_deets):
    a {"results": [...]} object, not a bare array."""
    return {"count": len(entries), "nextPageCursor": None, "results": entries}


def _sample_export() -> dict[str, Any]:
    """Two books, three highlights total: one with a note, one without, one
    with tags/location/url -- covers every field readwise_import.py reads."""
    return _make_export(
        [
            {
                "user_book_id": 111,
                "title": "Atomic Habits",
                "author": "James Clear",
                "category": "books",
                "source_url": "https://example.com/atomic-habits",
                "highlights": [
                    {
                        "id": 1001,
                        "text": "You do not rise to the level of your goals.",
                        "note": "Reminds me of the OKR postmortem.",
                        "location": 245,
                        "location_type": "location",
                        "highlighted_at": "2026-01-05T10:00:00Z",
                        "url": None,
                        "tags": [{"id": 1, "name": "habits"}, {"id": 2, "name": "systems"}],
                    },
                    {
                        "id": 1002,
                        "text": "Every action you take is a vote for the type of person you wish to become.",
                        "note": "",
                        "location": 310,
                        "location_type": "location",
                        "highlighted_at": "2026-01-06T11:30:00Z",
                    },
                ],
            },
            {
                "user_book_id": 222,
                "title": "Deep Work Article",
                "author": "Cal Newport",
                "category": "articles",
                "source_url": "https://example.com/deep-work",
                "highlights": [
                    {
                        "id": 2001,
                        "text": "UniqueUltravioletMarker: attention residue lingers after a task switch.",
                        "note": None,
                        "location": None,
                        "url": "https://readwise.io/open/2001",
                    },
                ],
            },
        ]
    )


def _write_export(tmp_path: Path, data: dict[str, Any], name: str = "readwise.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


# ---------------------------------------------------------------------------
# _extract_results — top-level shape handling
# ---------------------------------------------------------------------------


def test_extract_results_accepts_documented_results_shape() -> None:
    data = _make_export([{"title": "A", "highlights": []}])
    assert _extract_results(data) == [{"title": "A", "highlights": []}]


def test_extract_results_accepts_bare_array_for_convenience() -> None:
    entries = [{"title": "A", "highlights": []}]
    assert _extract_results(entries) == entries


def test_extract_results_rejects_unrecognized_shape() -> None:
    with pytest.raises(RuntimeError, match=re.escape(READWISE_FORMAT_ERROR)):
        _extract_results({"messages": []})  # chat-shaped, not readwise-shaped


def test_extract_results_rejects_scalar() -> None:
    with pytest.raises(RuntimeError, match=re.escape(READWISE_FORMAT_ERROR)):
        _extract_results("just a string")


# ---------------------------------------------------------------------------
# _highlight_content — note appended, never discarded
# ---------------------------------------------------------------------------


def test_highlight_content_appends_note_when_present() -> None:
    content = _highlight_content("The highlight text.", "My personal note.")
    assert content == "The highlight text.\n\nNote: My personal note."


def test_highlight_content_omits_note_section_when_absent() -> None:
    assert _highlight_content("The highlight text.", None) == "The highlight text."
    assert _highlight_content("The highlight text.", "") == "The highlight text."
    assert _highlight_content("The highlight text.", "   ") == "The highlight text."


def test_highlight_content_strips_whitespace() -> None:
    assert _highlight_content("  padded  ", "  noted  ") == "padded\n\nNote: noted"


# ---------------------------------------------------------------------------
# _highlight_metadata — book/article context + highlight provenance
# ---------------------------------------------------------------------------


def test_highlight_metadata_includes_book_and_highlight_fields() -> None:
    entry = {
        "user_book_id": 111,
        "title": "Atomic Habits",
        "author": "James Clear",
        "category": "books",
        "source_url": "https://example.com/atomic-habits",
    }
    highlight = {
        "id": 1001,
        "location": 245,
        "location_type": "location",
        "highlighted_at": "2026-01-05T10:00:00Z",
        "url": "https://readwise.io/open/1001",
        "tags": [{"id": 1, "name": "habits"}, {"id": 2, "name": "systems"}],
    }
    meta = _highlight_metadata(entry, highlight)
    assert meta == {
        "readwise_title": "Atomic Habits",
        "readwise_author": "James Clear",
        "readwise_category": "books",
        "readwise_source_url": "https://example.com/atomic-habits",
        "readwise_book_id": 111,
        "readwise_location": 245,
        "readwise_location_type": "location",
        "readwise_highlighted_at": "2026-01-05T10:00:00Z",
        "readwise_highlight_id": 1001,
        "readwise_url": "https://readwise.io/open/1001",
        "readwise_tags": ["habits", "systems"],
    }


def test_highlight_metadata_omits_absent_fields_rather_than_nulling_them() -> None:
    """Sparse output (mirrors pdf_import's bare {"page": N}) -- a missing
    field is absent from metadata, not present with a None/null value."""
    meta = _highlight_metadata({"title": "T"}, {"id": 1})
    assert meta == {"readwise_title": "T", "readwise_highlight_id": 1}
    assert "readwise_author" not in meta
    assert "readwise_tags" not in meta


# ---------------------------------------------------------------------------
# _readwise_connector — one memory per highlight, malformed-entry tolerance
# ---------------------------------------------------------------------------


def _run_connector(data: Any, max_length: int = 10000) -> tuple[list[tuple[str, dict]], int]:
    return _readwise_connector(
        json.dumps(data), {"suffix": ".json", "extract_mode": "assistant_messages", "max_length": max_length, "raw_bytes": b""}
    )


def test_connector_yields_one_chunk_per_highlight() -> None:
    parsed, raw_entries = _run_connector(_sample_export())
    assert raw_entries == 3
    assert len(parsed) == 3
    contents = [c for c, _m in parsed]
    assert contents[0] == (
        "You do not rise to the level of your goals.\n\n"
        "Note: Reminds me of the OKR postmortem."
    )
    assert contents[1] == "Every action you take is a vote for the type of person you wish to become."
    assert "UniqueUltravioletMarker" in contents[2]
    assert "Note:" not in contents[2]  # no note on this one


def test_connector_attaches_book_context_per_highlight() -> None:
    parsed, _raw = _run_connector(_sample_export())
    metas = [m for _c, m in parsed]
    assert metas[0]["readwise_title"] == "Atomic Habits"
    assert metas[0]["readwise_author"] == "James Clear"
    assert metas[0]["readwise_category"] == "books"
    assert metas[0]["readwise_tags"] == ["habits", "systems"]
    assert metas[2]["readwise_title"] == "Deep Work Article"
    assert metas[2]["readwise_category"] == "articles"
    assert metas[2]["readwise_url"] == "https://readwise.io/open/2001"


def test_connector_skips_entry_with_no_highlights_array() -> None:
    data = _make_export([{"title": "Empty Book"}, {"title": "Real Book", "highlights": [{"text": "keep me"}]}])
    parsed, raw_entries = _run_connector(data)
    assert raw_entries == 1
    assert parsed[0][0] == "keep me"


def test_connector_skips_non_object_entries_and_highlights() -> None:
    data = _make_export(["not an object", {"title": "Real Book", "highlights": ["also not an object", {"text": "keep me"}]}])
    parsed, raw_entries = _run_connector(data)
    assert raw_entries == 1
    assert parsed[0][0] == "keep me"


def test_connector_skips_highlights_with_no_text() -> None:
    data = _make_export([{"title": "Book", "highlights": [{"note": "orphan note, no text"}, {"text": "  "}, {"text": "keep me"}]}])
    parsed, raw_entries = _run_connector(data)
    assert raw_entries == 1
    assert parsed[0][0] == "keep me"


def test_connector_splits_long_highlight_via_shared_chunk_text() -> None:
    long_text = " ".join(["word"] * 50)  # well over max_length=50
    data = _make_export([{"title": "Book", "highlights": [{"text": long_text}]}])
    parsed, raw_entries = _run_connector(data, max_length=50)
    assert raw_entries == 1  # one highlight...
    assert len(parsed) > 1  # ...split into multiple chunks
    assert all(len(c) <= 50 for c, _m in parsed)
    # Every sub-chunk of the same highlight still carries the same metadata
    # (mirrors pdf_import's every-sub-chunk-keeps-the-page-number precedent).
    assert all(m.get("readwise_title") == "Book" for _c, m in parsed)


def test_connector_raises_clear_error_for_malformed_top_level_shape() -> None:
    with pytest.raises(RuntimeError, match=re.escape(READWISE_FORMAT_ERROR)):
        _run_connector({"not": "a readwise export"})


def test_connector_raises_clear_error_for_invalid_json() -> None:
    with pytest.raises(RuntimeError, match="Could not parse Readwise export as JSON"):
        _readwise_connector(
            "{not valid json", {"suffix": ".json", "extract_mode": "assistant_messages", "max_length": 10000, "raw_bytes": b""}
        )


# ---------------------------------------------------------------------------
# Connector registration
# ---------------------------------------------------------------------------


def test_readwise_registered_as_connector() -> None:
    import remind_me_mcp.importer as _importer_mod

    assert "readwise" in _importer_mod._CONNECTORS
    assert _importer_mod._CONNECTORS["readwise"] is _readwise_connector


def test_readwise_is_a_valid_import_chat_file_kind() -> None:
    """Unlike 'dbs'/'mempalace' (discovery-only), 'readwise' IS reachable
    through import_chat_file/import_directory."""
    assert "readwise" in IMPORT_KINDS


# ---------------------------------------------------------------------------
# kind validation (FT-20 additions to _validate_kind_and_suffix)
# ---------------------------------------------------------------------------


def test_readwise_kind_forced_on_non_json_suffix_is_rejected(db_conn: sqlite3.Connection) -> None:
    result = import_content(b"whatever", "f.txt", "test", [], "assistant_messages", 10000, kind="readwise")
    assert result["status"] == "error"
    assert "readwise import requires a .json file" in result["reason"]


# ---------------------------------------------------------------------------
# kind=auto routing decision: readwise is NEVER auto-detected (documented
# tradeoff -- a Readwise export stays a plain .json file and .json always
# routes to chat under kind=auto, exactly as it did before this connector
# existed).
# ---------------------------------------------------------------------------


def test_readwise_export_via_kind_auto_routes_as_chat_not_readwise(
    db_conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    export_file = _write_export(tmp_path, _sample_export())

    result = import_chat_file(str(export_file), "", [], "assistant_messages", 10000)  # kind=auto (default)

    assert result["status"] == "ok"
    assert result["kind"] == "chat"
    # A Readwise export has no role/content/messages/chat_messages shape, so
    # the chat parser correctly (if uselessly) extracts nothing -- proof
    # kind=auto genuinely tried the chat path rather than silently detecting
    # readwise on its own.
    assert result["memories_created"] == 0


def test_readwise_export_requires_explicit_kind_to_import_correctly(
    db_conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    export_file = _write_export(tmp_path, _sample_export())

    result = import_chat_file(str(export_file), "", [], "assistant_messages", 10000, kind="readwise")

    assert result["status"] == "ok"
    assert result["kind"] == "readwise"
    assert result["memories_created"] == 3


# ---------------------------------------------------------------------------
# Full pipeline: storage shape, dedup, search round-trip
# ---------------------------------------------------------------------------


def test_import_chat_file_readwise_stores_category_source_and_metadata(
    db_conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    export_file = _write_export(tmp_path, _sample_export())

    result = import_chat_file(str(export_file), "", [], "assistant_messages", 10000, kind="readwise")
    assert result["status"] == "ok"

    rows = db_conn.execute(
        "SELECT content, category, source, metadata, doc_id, chunk_index "
        "FROM memories WHERE source = 'readwise_import' ORDER BY chunk_index"
    ).fetchall()
    assert len(rows) == 3
    assert all(r["category"] == "readwise" for r in rows)
    assert [r["chunk_index"] for r in rows] == [0, 1, 2]
    assert all(r["doc_id"] == result["import_id"] for r in rows)

    metas = [json.loads(r["metadata"]) for r in rows]
    assert metas[0]["readwise_title"] == "Atomic Habits"
    assert metas[0]["import_id"] == result["import_id"]  # shared _ingest_parsed metadata, same as every other kind
    assert metas[2]["readwise_title"] == "Deep Work Article"


def test_import_chat_file_readwise_dedups_by_hash(
    db_conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    export_file = _write_export(tmp_path, _sample_export())

    first = import_chat_file(str(export_file), "", [], "assistant_messages", 10000, kind="readwise")
    assert first["status"] == "ok"
    assert first["memories_created"] == 3

    second = import_chat_file(str(export_file), "", [], "assistant_messages", 10000, kind="readwise")
    assert second["status"] == "skipped"
    assert second["import_id"] == first["import_id"]

    count = db_conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"]
    assert count == 3  # the re-import created nothing new


async def test_readwise_import_is_searchable_round_trip(
    db_conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    """An imported highlight's text (and its note) participate in FTS5
    search like any other connector's output, via remind_me_search."""
    from remind_me_mcp.models import MemorySearchInput, ResponseFormat
    from remind_me_mcp.tools import memory_search

    export_file = _write_export(tmp_path, _sample_export())
    result = import_chat_file(str(export_file), "", [], "assistant_messages", 10000, kind="readwise")
    assert result["status"] == "ok"

    search_result = await memory_search(
        MemorySearchInput(query="UniqueUltravioletMarker", response_format=ResponseFormat.JSON)
    )
    payload = json.loads(search_result)
    assert payload["returned"] >= 1
    assert any("UniqueUltravioletMarker" in m["content"] for m in payload["memories"])


# ---------------------------------------------------------------------------
# Malformed export surfaces a clear error, not a crash
# ---------------------------------------------------------------------------


def test_import_chat_file_malformed_readwise_shape_raises_actionable_error(
    db_conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    """A well-formed JSON file that isn't Readwise-shaped (e.g. someone
    pointed kind='readwise' at a plain chat export) raises the connector's
    RuntimeError with an actionable message, mirroring how a missing pdf/image
    extra propagates (see test_pdf_import.py's equivalent test) -- not a bare
    AttributeError/TypeError traceback from treating the wrong shape as a
    list of book entries."""
    bad_file = tmp_path / "not_readwise.json"
    bad_file.write_text(json.dumps({"messages": [{"role": "user", "content": "hi"}]}))

    with pytest.raises(RuntimeError, match=re.escape(READWISE_FORMAT_ERROR)):
        import_chat_file(str(bad_file), "", [], "assistant_messages", 10000, kind="readwise")


async def test_admin_tool_surfaces_malformed_readwise_shape_as_clean_error(
    db_conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    """remind_me_import_chat (tools/admin.py) catches the connector's
    RuntimeError and returns a clean {"status": "error"} response, not a
    raised exception -- mirrors test_pdf_import.py's equivalent coverage for
    pdf_import.py's missing-dependency RuntimeError."""
    from remind_me_mcp.models import ChatImportInput, ImportKind
    from remind_me_mcp.tools import memory_import_chat

    bad_file = tmp_path / "not_readwise2.json"
    bad_file.write_text(json.dumps({"totally": "unrelated shape"}))

    result_str = await memory_import_chat(
        ChatImportInput(file_path=str(bad_file), kind=ImportKind.READWISE)
    )
    result = json.loads(result_str)
    assert result["status"] == "error"
    assert READWISE_FORMAT_ERROR in result["error"]
