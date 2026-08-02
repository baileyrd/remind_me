"""
Tests for remind_me_mcp.pdf_import — the "pdf" import connector (FT-19, issue #181).

pypdf (the 'pdf' extra) is optional; every test here skips gracefully (not
fails) when it isn't installed, matching the [semantic]-extra skip convention
already used by test_ann_index.py/test_reranker.py/test_ollama_embedder.py.

Fixture generation: rather than checking in a binary .pdf, `_make_pdf` below
hand-builds a real, multi-page, text-extractable PDF at test time using
pypdf's own low-level object API (a bare "/Helvetica" Type1 font resource —
one of the 14 standard PDF fonts, no font *file* needed — plus a minimal
`BT ... Tj ET` content stream per page). The resulting bytes round-trip
through pypdf.PdfReader.extract_text() as real text, so extraction tests
exercise the real library end-to-end rather than a mocked stand-in.
"""

from __future__ import annotations

import io
import json
import re
import sys
from typing import TYPE_CHECKING

import pytest

pypdf = pytest.importorskip("pypdf", reason="pypdf (the 'pdf' extra) not installed")

from remind_me_mcp.importer import import_chat_file, import_content  # noqa: E402
from remind_me_mcp.pdf_import import (  # noqa: E402
    PDF_EXTRA_INSTALL_MSG,
    _extract_pages,
    _pdf_connector,
)

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


def _make_pdf(page_texts: list[str]) -> bytes:
    """Hand-build a real, extractable multi-page PDF from *page_texts*.

    One page per string, each drawn with the standard (fontfile-free)
    /Helvetica Type1 font via a minimal content stream.
    """
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = pypdf.PdfWriter()

    font_dict = DictionaryObject()
    font_dict[NameObject("/Type")] = NameObject("/Font")
    font_dict[NameObject("/Subtype")] = NameObject("/Type1")
    font_dict[NameObject("/BaseFont")] = NameObject("/Helvetica")
    font_ref = writer._add_object(font_dict)

    for text in page_texts:
        page = writer.add_blank_page(width=400, height=200)
        # Minimal escaping for these ASCII-only test fixtures.
        safe = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        content = f"BT /F1 14 Tf 10 150 Td ({safe}) Tj ET"
        stream_obj = DecodedStreamObject()
        stream_obj.set_data(content.encode("latin-1"))
        stream_ref = writer._add_object(stream_obj)

        resources = DictionaryObject()
        font_res = DictionaryObject()
        font_res[NameObject("/F1")] = font_ref
        resources[NameObject("/Font")] = font_res

        page[NameObject("/Contents")] = stream_ref
        page[NameObject("/Resources")] = resources

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_long_pdf_page(word: str, repeats: int) -> bytes:
    """A single-page PDF whose extracted text is `word` repeated *repeats*
    times, space-separated — long enough to force _chunk_text to split it."""
    text = " ".join([word] * repeats)
    return _make_pdf([text])


# ---------------------------------------------------------------------------
# _extract_pages — real pypdf extraction
# ---------------------------------------------------------------------------


def test_extract_pages_returns_real_per_page_text() -> None:
    pdf_bytes = _make_pdf(["AlphaMarkerOne content", "BetaMarkerTwo content"])
    pages = _extract_pages(pdf_bytes)
    assert len(pages) == 2
    assert "AlphaMarkerOne" in pages[0]
    assert "BetaMarkerTwo" in pages[1]


def test_extract_pages_blank_page_yields_empty_string() -> None:
    pdf_bytes = _make_pdf([""])
    pages = _extract_pages(pdf_bytes)
    assert len(pages) == 1
    assert pages[0].strip() == ""


def test_extract_pages_raises_clear_error_for_garbage_bytes() -> None:
    with pytest.raises(RuntimeError, match="Could not parse PDF"):
        _extract_pages(b"not a real pdf at all")


# ---------------------------------------------------------------------------
# _pdf_connector — per-page chunking with page metadata
# ---------------------------------------------------------------------------


def test_pdf_connector_chunks_per_page_with_page_metadata() -> None:
    pdf_bytes = _make_pdf(["AlphaMarkerOne content", "BetaMarkerTwo content"])
    parsed, raw_entries = _pdf_connector(
        "", {"suffix": ".pdf", "extract_mode": "assistant_messages",
             "max_length": 10000, "raw_bytes": pdf_bytes}
    )
    assert raw_entries == len(parsed) == 2
    contents = [c for c, _meta in parsed]
    metas = [m for _c, m in parsed]
    assert "AlphaMarkerOne" in contents[0]
    assert "BetaMarkerTwo" in contents[1]
    assert metas == [{"page": 1}, {"page": 2}]


def test_pdf_connector_skips_blank_pages() -> None:
    """A blank page between two text pages produces no chunk of its own, and
    the surviving pages keep their true (not renumbered) page numbers."""
    pdf_bytes = _make_pdf(["First page content", "", "Third page content"])
    parsed, raw_entries = _pdf_connector(
        "", {"suffix": ".pdf", "extract_mode": "assistant_messages",
             "max_length": 10000, "raw_bytes": pdf_bytes}
    )
    assert raw_entries == 2
    pages = [m["page"] for _c, m in parsed]
    assert pages == [1, 3]


def test_pdf_connector_splits_long_page_into_multiple_chunks_same_page_number() -> None:
    """A page whose text exceeds max_length is split by _chunk_text like an
    oversized document section — every sub-chunk still carries that page's
    number (mirrors _parse_document's heading-context precedent)."""
    pdf_bytes = _make_long_pdf_page("word", repeats=50)  # ~250 chars of text
    parsed, raw_entries = _pdf_connector(
        "", {"suffix": ".pdf", "extract_mode": "assistant_messages",
             "max_length": 50, "raw_bytes": pdf_bytes}
    )
    assert len(parsed) > 1
    assert raw_entries == len(parsed)
    assert all(meta == {"page": 1} for _c, meta in parsed)
    assert all(len(c) <= 50 for c, _meta in parsed)


# ---------------------------------------------------------------------------
# Missing-dependency error path
# ---------------------------------------------------------------------------


def test_extract_pages_missing_dependency_raises_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "pypdf", None)
    with pytest.raises(RuntimeError, match=re.escape(PDF_EXTRA_INSTALL_MSG)):
        _extract_pages(b"whatever")


def test_import_chat_file_missing_pdf_dependency_raises_actionable_error(
    db_conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The RuntimeError from a missing 'pdf' extra propagates all the way up
    through _ingest_parsed/import_chat_file with its actionable message —
    tools/admin.py is what turns this into a clean tool response (tested in
    test_admin_import.py-style flows below via the tool function directly)."""
    pdf_bytes = _make_pdf(["Some content"])
    pdf_file = tmp_path / "needs_extra.pdf"
    pdf_file.write_bytes(pdf_bytes)
    monkeypatch.setitem(sys.modules, "pypdf", None)

    with pytest.raises(RuntimeError, match=re.escape(PDF_EXTRA_INSTALL_MSG)):
        import_chat_file(str(pdf_file), "", [], "assistant_messages", 10000)


async def test_admin_tool_surfaces_missing_pdf_dependency_as_clean_error(
    db_conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """remind_me_import_chat (tools/admin.py) catches the RuntimeError and
    returns a clean {"status": "error"} response, not a raised exception."""
    from remind_me_mcp.models import ChatImportInput
    from remind_me_mcp.tools import memory_import_chat

    pdf_bytes = _make_pdf(["Some content"])
    pdf_file = tmp_path / "needs_extra2.pdf"
    pdf_file.write_bytes(pdf_bytes)
    monkeypatch.setitem(sys.modules, "pypdf", None)

    result_str = await memory_import_chat(ChatImportInput(file_path=str(pdf_file)))
    result = json.loads(result_str)
    assert result["status"] == "error"
    assert PDF_EXTRA_INSTALL_MSG in result["error"]


# ---------------------------------------------------------------------------
# kind validation (FT-19 additions to _validate_kind_and_suffix)
# ---------------------------------------------------------------------------


def test_pdf_kind_forced_on_non_pdf_suffix_is_rejected(db_conn: sqlite3.Connection) -> None:
    result = import_content(b"whatever", "f.txt", "test", [], "assistant_messages", 10000, kind="pdf")
    assert result["status"] == "error"
    assert "pdf import requires a .pdf file" in result["reason"]


def test_chat_kind_forced_on_pdf_suffix_is_rejected(db_conn: sqlite3.Connection) -> None:
    pdf_bytes = _make_pdf(["content"])
    result = import_content(pdf_bytes, "f.pdf", "test", [], "assistant_messages", 10000, kind="chat")
    assert result["status"] == "error"
    assert "must use kind='pdf' or 'auto'" in result["reason"]


# ---------------------------------------------------------------------------
# Full pipeline: hash dedup, kind=auto routing, storage shape, search
# ---------------------------------------------------------------------------


def test_import_chat_file_auto_routes_pdf_and_stores_page_metadata(
    db_conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    pdf_bytes = _make_pdf(["AlphaMarkerZQX one", "BetaMarkerZQX two"])
    pdf_file = tmp_path / "notes.pdf"
    pdf_file.write_bytes(pdf_bytes)

    result = import_chat_file(str(pdf_file), "", [], "assistant_messages", 10000)  # kind=auto (default)
    assert result["status"] == "ok"
    assert result["kind"] == "pdf"
    assert result["memories_created"] == 2

    rows = db_conn.execute(
        "SELECT content, category, source, metadata, doc_id, chunk_index "
        "FROM memories WHERE source = 'pdf_import' ORDER BY chunk_index"
    ).fetchall()
    assert len(rows) == 2
    assert all(r["category"] == "pdf" for r in rows)
    assert [r["chunk_index"] for r in rows] == [0, 1]
    assert all(r["doc_id"] == result["import_id"] for r in rows)
    pages = [json.loads(r["metadata"])["page"] for r in rows]
    assert pages == [1, 2]
    assert "AlphaMarkerZQX" in rows[0]["content"]
    assert "BetaMarkerZQX" in rows[1]["content"]


def test_import_chat_file_pdf_dedups_by_hash(
    db_conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    pdf_bytes = _make_pdf(["Dedup content here"])
    pdf_file = tmp_path / "dedup.pdf"
    pdf_file.write_bytes(pdf_bytes)

    first = import_chat_file(str(pdf_file), "", [], "assistant_messages", 10000)
    assert first["status"] == "ok"

    second = import_chat_file(str(pdf_file), "", [], "assistant_messages", 10000)
    assert second["status"] == "skipped"
    assert second["import_id"] == first["import_id"]


async def test_pdf_import_is_searchable_round_trip(
    db_conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    """Imported PDF content participates in FTS5 dedup/search like any other
    connector's output — round-tripped through remind_me_search."""
    from remind_me_mcp.models import MemorySearchInput, ResponseFormat
    from remind_me_mcp.tools import memory_search

    pdf_bytes = _make_pdf(["UniqueZebraTermXQ appears on this page"])
    pdf_file = tmp_path / "searchable.pdf"
    pdf_file.write_bytes(pdf_bytes)

    result = import_chat_file(str(pdf_file), "", [], "assistant_messages", 10000)
    assert result["status"] == "ok"

    search_result = await memory_search(
        MemorySearchInput(query="UniqueZebraTermXQ", response_format=ResponseFormat.JSON)
    )
    payload = json.loads(search_result)
    assert payload["returned"] >= 1
    assert any("UniqueZebraTermXQ" in m["content"] for m in payload["memories"])
