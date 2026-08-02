"""
Tests for remind_me_mcp.image_import — the "image" OCR import connector
(FT-19, issue #181).

rapidocr-onnxruntime (the 'image' extra) is optional; every test here skips
gracefully (not fails) when it isn't installed, matching the [semantic]-extra
skip convention already used by test_ann_index.py/test_reranker.py.

Fixture generation: builds a real, OCR-able PNG at test time with Pillow (a
transitive dependency of rapidocr-onnxruntime — see image_import.py's module
docstring for why that library was chosen) rendering a large, high-contrast
word, then actually OCRs it end-to-end with RapidOCR's bundled models (no
network access needed — RapidOCR ships its detection/recognition models
inside the pip package itself) rather than mocking extraction.
"""

from __future__ import annotations

import io
import json
import re
import sys
from typing import TYPE_CHECKING

import pytest

rapidocr = pytest.importorskip(
    "rapidocr_onnxruntime", reason="rapidocr-onnxruntime (the 'image' extra) not installed"
)
PIL = pytest.importorskip("PIL", reason="Pillow (a transitive dependency of the 'image' extra) not installed")

from remind_me_mcp import image_import as _image_mod  # noqa: E402
from remind_me_mcp.image_import import (  # noqa: E402
    IMAGE_EXTRA_INSTALL_MSG,
    _extract_image_text,
    _image_connector,
)
from remind_me_mcp.importer import import_chat_file, import_content  # noqa: E402

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


def _load_test_font(size: int = 60):
    """Best-effort scalable font for rendering OCR-able test text.

    Tries a few common system TrueType paths (present in this project's dev/
    CI Linux images) before falling back to Pillow's built-in default font
    at the requested size (supported since Pillow 10.1).
    """
    from PIL import ImageFont

    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _make_ocr_image(text: str, size: tuple[int, int] = (700, 200)) -> bytes:
    """Render *text* as a large, high-contrast word on a white background PNG."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", size, color="white")
    draw = ImageDraw.Draw(img)
    draw.text((20, 60), text, fill="black", font=_load_test_font(60))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_blank_image(size: tuple[int, int] = (200, 100)) -> bytes:
    from PIL import Image

    img = Image.new("RGB", size, color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# _extract_image_text — real RapidOCR extraction
# ---------------------------------------------------------------------------


def test_extract_image_text_ocrs_real_image() -> None:
    png_bytes = _make_ocr_image("HELLO WORLD")
    text = _extract_image_text(png_bytes)
    assert "HELLO" in text.upper()
    assert "WORLD" in text.upper()


def test_extract_image_text_blank_image_yields_empty_string() -> None:
    png_bytes = _make_blank_image()
    assert _extract_image_text(png_bytes) == ""


def test_extract_image_text_raises_clear_error_for_garbage_bytes() -> None:
    with pytest.raises(RuntimeError, match="Could not OCR image"):
        _extract_image_text(b"not an image at all")


# ---------------------------------------------------------------------------
# _image_connector — whole image as one chunk
# ---------------------------------------------------------------------------


def test_image_connector_returns_single_chunk_with_no_metadata() -> None:
    png_bytes = _make_ocr_image("SINGLECHUNK")
    parsed, raw_entries = _image_connector("", {"raw_bytes": png_bytes})
    assert raw_entries == 1
    assert len(parsed) == 1
    content, meta = parsed[0]
    assert "SINGLECHUNK" in content.upper()
    assert meta == {}


def test_image_connector_no_text_detected_returns_empty() -> None:
    png_bytes = _make_blank_image()
    parsed, raw_entries = _image_connector("", {"raw_bytes": png_bytes})
    assert parsed == []
    assert raw_entries == 0


# ---------------------------------------------------------------------------
# Missing-dependency error path
# ---------------------------------------------------------------------------


def test_get_ocr_engine_missing_dependency_raises_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_image_mod, "_ocr_engine", None)
    monkeypatch.setattr(_image_mod, "_ocr_deps_missing", False)
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", None)
    with pytest.raises(RuntimeError, match=re.escape(IMAGE_EXTRA_INSTALL_MSG)):
        _image_mod._get_ocr_engine()


def test_get_ocr_engine_passes_configured_model_paths_to_rapidocr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REMIND_ME_OCR_*_MODEL_PATH overrides (issue #202) pass straight
    through as RapidOCR() kwargs -- an unset override contributes no kwarg
    at all (see the sibling default test), so this only exercises the
    opt-in path."""
    monkeypatch.setattr(_image_mod, "_ocr_engine", None)
    monkeypatch.setattr(_image_mod, "_ocr_deps_missing", False)
    monkeypatch.setattr(_image_mod, "OCR_DET_MODEL_PATH", "/models/en_det.onnx")
    monkeypatch.setattr(_image_mod, "OCR_CLS_MODEL_PATH", None)
    monkeypatch.setattr(_image_mod, "OCR_REC_MODEL_PATH", "/models/en_rec.onnx")

    captured: dict[str, str] = {}

    class FakeRapidOCR:
        def __init__(self, **kwargs: str) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(rapidocr, "RapidOCR", FakeRapidOCR)

    engine = _image_mod._get_ocr_engine()
    assert isinstance(engine, FakeRapidOCR)
    assert captured == {
        "det_model_path": "/models/en_det.onnx",
        "rec_model_path": "/models/en_rec.onnx",
    }


def test_get_ocr_engine_default_config_passes_no_model_path_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset overrides (today's default) call plain RapidOCR() -- no kwargs
    at all, so behavior is byte-for-byte identical to before issue #202."""
    monkeypatch.setattr(_image_mod, "_ocr_engine", None)
    monkeypatch.setattr(_image_mod, "_ocr_deps_missing", False)
    monkeypatch.setattr(_image_mod, "OCR_DET_MODEL_PATH", None)
    monkeypatch.setattr(_image_mod, "OCR_CLS_MODEL_PATH", None)
    monkeypatch.setattr(_image_mod, "OCR_REC_MODEL_PATH", None)

    captured: dict[str, str] = {}

    class FakeRapidOCR:
        def __init__(self, **kwargs: str) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(rapidocr, "RapidOCR", FakeRapidOCR)

    _image_mod._get_ocr_engine()
    assert captured == {}


def test_import_chat_file_missing_image_dependency_raises_actionable_error(
    db_conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    png_bytes = _make_ocr_image("Some content")
    png_file = tmp_path / "needs_extra.png"
    png_file.write_bytes(png_bytes)
    monkeypatch.setattr(_image_mod, "_ocr_engine", None)
    monkeypatch.setattr(_image_mod, "_ocr_deps_missing", False)
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", None)

    with pytest.raises(RuntimeError, match=re.escape(IMAGE_EXTRA_INSTALL_MSG)):
        import_chat_file(str(png_file), "", [], "assistant_messages", 10000)


async def test_admin_tool_surfaces_missing_image_dependency_as_clean_error(
    db_conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from remind_me_mcp.models import ChatImportInput
    from remind_me_mcp.tools import memory_import_chat

    png_bytes = _make_ocr_image("Some content")
    png_file = tmp_path / "needs_extra2.png"
    png_file.write_bytes(png_bytes)
    monkeypatch.setattr(_image_mod, "_ocr_engine", None)
    monkeypatch.setattr(_image_mod, "_ocr_deps_missing", False)
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", None)

    result_str = await memory_import_chat(ChatImportInput(file_path=str(png_file)))
    result = json.loads(result_str)
    assert result["status"] == "error"
    assert IMAGE_EXTRA_INSTALL_MSG in result["error"]


# ---------------------------------------------------------------------------
# kind validation (FT-19 additions to _validate_kind_and_suffix)
# ---------------------------------------------------------------------------


def test_image_kind_forced_on_non_image_suffix_is_rejected(db_conn: sqlite3.Connection) -> None:
    result = import_content(b"whatever", "f.txt", "test", [], "assistant_messages", 10000, kind="image")
    assert result["status"] == "error"
    assert "image import requires one of" in result["reason"]


def test_chat_kind_forced_on_image_suffix_is_rejected(db_conn: sqlite3.Connection) -> None:
    png_bytes = _make_ocr_image("content")
    result = import_content(png_bytes, "f.png", "test", [], "assistant_messages", 10000, kind="chat")
    assert result["status"] == "error"
    assert "must use kind='image' or 'auto'" in result["reason"]


# ---------------------------------------------------------------------------
# Full pipeline: hash dedup, kind=auto routing, storage shape, search
# ---------------------------------------------------------------------------


def test_import_chat_file_auto_routes_image_and_stores_single_memory(
    db_conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    png_bytes = _make_ocr_image("ZQXMARKERWORD")
    png_file = tmp_path / "photo.png"
    png_file.write_bytes(png_bytes)

    result = import_chat_file(str(png_file), "", [], "assistant_messages", 10000)  # kind=auto (default)
    assert result["status"] == "ok"
    assert result["kind"] == "image"
    assert result["memories_created"] == 1

    row = db_conn.execute(
        "SELECT content, category, source, doc_id, chunk_index FROM memories WHERE source = 'image_import'"
    ).fetchone()
    assert row is not None
    assert row["category"] == "image"
    assert row["doc_id"] == result["import_id"]
    assert row["chunk_index"] == 0
    assert "ZQXMARKERWORD" in row["content"].upper()


def test_import_chat_file_image_dedups_by_hash(
    db_conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    png_bytes = _make_ocr_image("DEDUPWORD")
    png_file = tmp_path / "dedup.png"
    png_file.write_bytes(png_bytes)

    first = import_chat_file(str(png_file), "", [], "assistant_messages", 10000)
    assert first["status"] == "ok"

    second = import_chat_file(str(png_file), "", [], "assistant_messages", 10000)
    assert second["status"] == "skipped"
    assert second["import_id"] == first["import_id"]


def test_import_chat_file_jpeg_extension_also_routes_to_image(
    db_conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    from PIL import Image

    png_bytes = _make_ocr_image("JPEGROUTEWORD")
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    jpg_file = tmp_path / "photo.jpg"
    img.save(jpg_file, format="JPEG", quality=95)

    result = import_chat_file(str(jpg_file), "", [], "assistant_messages", 10000)
    assert result["status"] == "ok"
    assert result["kind"] == "image"


async def test_image_import_is_searchable_round_trip(
    db_conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    """Imported OCR content participates in FTS5 dedup/search like any other
    connector's output — round-tripped through remind_me_search."""
    from remind_me_mcp.models import MemorySearchInput, ResponseFormat
    from remind_me_mcp.tools import memory_search

    png_bytes = _make_ocr_image("ZEBRAOCRTERM")
    png_file = tmp_path / "searchable.png"
    png_file.write_bytes(png_bytes)

    result = import_chat_file(str(png_file), "", [], "assistant_messages", 10000)
    assert result["status"] == "ok"

    search_result = await memory_search(
        MemorySearchInput(query="ZEBRAOCRTERM", response_format=ResponseFormat.JSON)
    )
    payload = json.loads(search_result)
    assert payload["returned"] >= 1
    assert any("ZEBRAOCRTERM" in m["content"].upper() for m in payload["memories"])
