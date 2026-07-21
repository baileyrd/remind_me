"""
Unit tests for remind_me_mcp.models — Pydantic input model validation.

Covers all 9 input models and the ResponseFormat enum.  Each model's
required fields, field constraints, custom validators, and default values
are tested with both valid and invalid inputs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from remind_me_mcp.models import (
    AutoCaptureInput,
    BulkImportDirInput,
    ChatImportInput,
    DbsImportInput,
    MemoryAddInput,
    MemoryDeleteInput,
    MemoryListInput,
    MemorySearchInput,
    MemoryUpdateInput,
    ResponseFormat,
)

# ---------------------------------------------------------------------------
# ResponseFormat enum
# ---------------------------------------------------------------------------


def test_response_format_values() -> None:
    """ResponseFormat enum has MARKDOWN='markdown' and JSON='json' values."""
    assert ResponseFormat.MARKDOWN == "markdown"
    assert ResponseFormat.JSON == "json"


# ---------------------------------------------------------------------------
# MemoryAddInput
# ---------------------------------------------------------------------------


def test_memory_add_valid() -> None:
    """Minimal valid input (content only) creates model with correct defaults."""
    m = MemoryAddInput(content="A quick note")
    assert m.content == "A quick note"
    assert m.category == "general"
    assert m.tags == []
    assert m.source == "manual"
    assert m.metadata == {}


def test_memory_add_full() -> None:
    """All fields specified successfully creates the model."""
    m = MemoryAddInput(
        content="Full note",
        category="work",
        tags=["python", "async"],
        source="observation",
        metadata={"project": "remind-me"},
    )
    assert m.category == "work"
    assert m.tags == ["python", "async"]
    assert m.metadata == {"project": "remind-me"}


def test_memory_add_empty_content_rejected() -> None:
    """Empty string content raises ValidationError (min_length=1)."""
    with pytest.raises(ValidationError) as exc_info:
        MemoryAddInput(content="")
    assert "content" in str(exc_info.value).lower()


def test_memory_add_extra_fields_rejected() -> None:
    """Unknown field raises ValidationError (extra='forbid')."""
    with pytest.raises(ValidationError):
        MemoryAddInput(content="Note", unknown_field="value")


def test_memory_add_strips_whitespace() -> None:
    """Content with leading/trailing whitespace is stripped."""
    m = MemoryAddInput(content="  hello  ")
    assert m.content == "hello"


# ---------------------------------------------------------------------------
# MemorySearchInput
# ---------------------------------------------------------------------------


def test_search_valid_minimal() -> None:
    """Query-only input creates model with correct defaults."""
    m = MemorySearchInput(query="python async")
    assert m.query == "python async"
    assert m.category is None
    assert m.tags is None
    assert m.limit == 20
    assert m.response_format == ResponseFormat.MARKDOWN


def test_search_with_all_filters() -> None:
    """All filter fields accepted correctly."""
    m = MemorySearchInput(
        query="search term",
        category="work",
        tags=["python"],
        limit=50,
        response_format=ResponseFormat.JSON,
    )
    assert m.limit == 50
    assert m.response_format == ResponseFormat.JSON


def test_search_empty_query_rejected() -> None:
    """Empty string query raises ValidationError (min_length=1)."""
    with pytest.raises(ValidationError):
        MemorySearchInput(query="")


def test_search_limit_bounds() -> None:
    """Limit below 1 and above 100 are both rejected."""
    with pytest.raises(ValidationError):
        MemorySearchInput(query="test", limit=0)
    with pytest.raises(ValidationError):
        MemorySearchInput(query="test", limit=101)


# ---------------------------------------------------------------------------
# MemoryListInput
# ---------------------------------------------------------------------------


def test_list_defaults() -> None:
    """All defaults are applied correctly on empty input."""
    m = MemoryListInput()
    assert m.category is None
    assert m.tags is None
    assert m.source is None
    assert m.limit == 20
    assert m.offset == 0
    assert m.response_format == ResponseFormat.MARKDOWN


def test_list_with_filters() -> None:
    """Category, source, tags, offset, and limit all accepted."""
    m = MemoryListInput(
        category="work",
        source="chat_import",
        tags=["important"],
        offset=10,
        limit=5,
    )
    assert m.category == "work"
    assert m.source == "chat_import"
    assert m.offset == 10
    assert m.limit == 5


# ---------------------------------------------------------------------------
# MemoryUpdateInput
# ---------------------------------------------------------------------------


def test_update_content_only() -> None:
    """memory_id and content only — valid minimal update."""
    m = MemoryUpdateInput(memory_id="abc123", content="Updated content")
    assert m.memory_id == "abc123"
    assert m.content == "Updated content"


def test_update_requires_memory_id() -> None:
    """Missing memory_id raises ValidationError."""
    with pytest.raises(ValidationError):
        MemoryUpdateInput(content="Some content")


# ---------------------------------------------------------------------------
# MemoryDeleteInput
# ---------------------------------------------------------------------------


def test_delete_valid() -> None:
    """Valid memory_id creates model successfully."""
    m = MemoryDeleteInput(memory_id="abc123def456")
    assert m.memory_id == "abc123def456"


def test_delete_empty_id_rejected() -> None:
    """Empty string memory_id raises ValidationError (min_length=1)."""
    with pytest.raises(ValidationError):
        MemoryDeleteInput(memory_id="")


# ---------------------------------------------------------------------------
# ChatImportInput
# ---------------------------------------------------------------------------


def test_chat_import_valid(tmp_path: Path) -> None:
    """Real .json file path is accepted by the validator."""
    f = tmp_path / "export.json"
    f.write_text('{"messages": []}')
    m = ChatImportInput(file_path=str(f))
    assert Path(m.file_path).exists()


def test_chat_import_nonexistent_path(tmp_path: Path) -> None:
    """Path to a missing file (inside allowed roots) raises 'File not found'.

    SE-02: the containment check fires before the existence check, so the
    missing file must live inside IMPORT_ROOTS to exercise this branch.
    """
    with pytest.raises(ValidationError) as exc_info:
        ChatImportInput(file_path=str(tmp_path / "missing_file.json"))
    assert "File not found" in str(exc_info.value)


def test_chat_import_rejects_path_outside_import_roots() -> None:
    """SE-02: a file outside IMPORT_ROOTS is rejected before any existence check.

    /etc/passwd exists, but it is outside the allowed roots ($HOME and the
    temp dir in tests) — the MCP import model must reject it just like the
    HTTP /api/import route does.
    """
    with pytest.raises(ValidationError) as exc_info:
        ChatImportInput(file_path="/etc/passwd")
    assert "not in allowed import roots" in str(exc_info.value)


def test_chat_import_rejects_traversal_outside_roots() -> None:
    """SE-02: traversal sequences are resolved before the containment check."""
    traversal = str(Path.home() / ".." / ".." / "etc" / "passwd")
    with pytest.raises(ValidationError) as exc_info:
        ChatImportInput(file_path=traversal)
    assert "not in allowed import roots" in str(exc_info.value)


def test_chat_import_respects_custom_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SE-02: restricting IMPORT_ROOTS rejects files outside the configured root."""
    import remind_me_mcp.config as _cfg

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    inside = allowed / "ok.json"
    inside.write_text("{}")
    outside = tmp_path / "outside.json"
    outside.write_text("{}")

    monkeypatch.setattr(_cfg, "IMPORT_ROOTS", [allowed.resolve()])

    assert ChatImportInput(file_path=str(inside)).file_path == str(inside.resolve())
    with pytest.raises(ValidationError) as exc_info:
        ChatImportInput(file_path=str(outside))
    assert "not in allowed import roots" in str(exc_info.value)


def test_chat_import_unsupported_extension(tmp_path: Path) -> None:
    """A .csv file raises ValidationError with 'Unsupported file type'."""
    f = tmp_path / "data.csv"
    f.write_text("col1,col2")
    with pytest.raises(ValidationError) as exc_info:
        ChatImportInput(file_path=str(f))
    assert "Unsupported file type" in str(exc_info.value)


def test_chat_import_defaults() -> None:
    """Default values for category, extract_mode, and max_length are correct."""
    # Need a real file to pass validation
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp.write(b'{"messages": []}')
        tmp_path = tmp.name
    try:
        m = ChatImportInput(file_path=tmp_path)
        assert m.category == "chat_import"
        assert m.extract_mode == "assistant_messages"
        assert m.max_length == 10000
    finally:
        Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# BulkImportDirInput
# ---------------------------------------------------------------------------


def test_bulk_import_valid(tmp_path: Path) -> None:
    """Existing directory path validates successfully."""
    m = BulkImportDirInput(directory=str(tmp_path))
    assert Path(m.directory).is_dir()


def test_bulk_import_nonexistent_dir(tmp_path: Path) -> None:
    """Missing directory (inside allowed roots) raises 'Directory not found'.

    SE-02: the containment check fires first, so the missing directory must
    live inside IMPORT_ROOTS to exercise the existence branch.
    """
    with pytest.raises(ValidationError) as exc_info:
        BulkImportDirInput(directory=str(tmp_path / "missing_subdir"))
    assert "Directory not found" in str(exc_info.value)


def test_bulk_import_rejects_dir_outside_import_roots() -> None:
    """SE-02: a directory outside IMPORT_ROOTS is rejected (parity with /api/import)."""
    with pytest.raises(ValidationError) as exc_info:
        BulkImportDirInput(directory="/etc")
    assert "not in allowed import roots" in str(exc_info.value)


def test_bulk_import_respects_custom_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SE-02: restricting IMPORT_ROOTS rejects directories outside the configured root."""
    import remind_me_mcp.config as _cfg

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    monkeypatch.setattr(_cfg, "IMPORT_ROOTS", [allowed.resolve()])

    assert BulkImportDirInput(directory=str(allowed)).directory == str(allowed.resolve())
    with pytest.raises(ValidationError) as exc_info:
        BulkImportDirInput(directory=str(outside))
    assert "not in allowed import roots" in str(exc_info.value)


# ---------------------------------------------------------------------------
# DbsImportInput
# ---------------------------------------------------------------------------


def test_dbs_import_valid(tmp_path: Path) -> None:
    """Real file path is accepted by the validator."""
    f = tmp_path / "dbs.sqlite3"
    f.write_text("")
    m = DbsImportInput(db_path=str(f))
    assert Path(m.db_path).exists()


def test_dbs_import_nonexistent_path(tmp_path: Path) -> None:
    """Path to a missing file (inside allowed roots) raises 'File not found'.

    SE-02: the containment check fires before the existence check, so the
    missing file must live inside IMPORT_ROOTS to exercise this branch.
    """
    with pytest.raises(ValidationError) as exc_info:
        DbsImportInput(db_path=str(tmp_path / "missing.sqlite3"))
    assert "File not found" in str(exc_info.value)


def test_dbs_import_rejects_path_outside_import_roots() -> None:
    """SE-02: a file outside IMPORT_ROOTS is rejected before any existence check.

    Previously missing on DbsImportInput entirely -- _open_dbs_db only
    checked existence, so a caller could point db_path at any readable
    file anywhere on disk.
    """
    with pytest.raises(ValidationError) as exc_info:
        DbsImportInput(db_path="/etc/passwd")
    assert "not in allowed import roots" in str(exc_info.value)


def test_dbs_import_rejects_traversal_outside_roots() -> None:
    """SE-02: traversal sequences are resolved before the containment check."""
    traversal = str(Path.home() / ".." / ".." / "etc" / "passwd")
    with pytest.raises(ValidationError) as exc_info:
        DbsImportInput(db_path=traversal)
    assert "not in allowed import roots" in str(exc_info.value)


def test_dbs_import_respects_custom_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SE-02: restricting IMPORT_ROOTS rejects files outside the configured root."""
    import remind_me_mcp.config as _cfg

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    inside = allowed / "dbs.sqlite3"
    inside.write_text("")
    outside = tmp_path / "dbs.sqlite3"
    outside.write_text("")

    monkeypatch.setattr(_cfg, "IMPORT_ROOTS", [allowed.resolve()])

    assert DbsImportInput(db_path=str(inside)).db_path == str(inside.resolve())
    with pytest.raises(ValidationError) as exc_info:
        DbsImportInput(db_path=str(outside))
    assert "not in allowed import roots" in str(exc_info.value)


def test_dbs_import_defaults(tmp_path: Path) -> None:
    f = tmp_path / "dbs.sqlite3"
    f.write_text("")
    m = DbsImportInput(db_path=str(f))
    assert m.source == ""
    assert m.item_type == ""
    assert m.limit == 500
    assert m.offset == 0
    assert m.tags == []
    assert m.dry_run is False


# ---------------------------------------------------------------------------
# AutoCaptureInput
# ---------------------------------------------------------------------------


def test_auto_capture_valid() -> None:
    """conversation + summary required; rest defaults are correct."""
    m = AutoCaptureInput(
        conversation="Human: Hi\nAssistant: Hello",
        summary="Short greeting exchange",
    )
    assert m.conversation == "Human: Hi\nAssistant: Hello"
    assert m.summary == "Short greeting exchange"
    assert m.title == ""
    assert m.tags == []
    assert m.category == "conversation"
    assert m.metadata == {}


def test_auto_capture_empty_conversation() -> None:
    """Empty conversation raises ValidationError (min_length=1)."""
    with pytest.raises(ValidationError):
        AutoCaptureInput(conversation="", summary="Some summary")


def test_auto_capture_empty_summary() -> None:
    """Empty summary raises ValidationError (min_length=1)."""
    with pytest.raises(ValidationError):
        AutoCaptureInput(conversation="Human: Hi\nAssistant: Hello", summary="")
