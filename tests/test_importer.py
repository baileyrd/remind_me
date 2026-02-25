"""
Unit tests for remind_me_mcp.importer pure functions.

All tests are synchronous and exercise functions directly without any MCP
server overhead. The functions under test have no I/O side-effects except
_file_hash which reads from disk (tmp_path used for isolation).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from remind_me_mcp.importer import (
    _chunk_text,
    _extract_messages_from_json,
    _file_hash,
    _filter_messages,
    _parse_markdown_chat,
    import_chat_file,
)

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    import pytest

# ---------------------------------------------------------------------------
# _chunk_text
# ---------------------------------------------------------------------------


def test_chunk_text_short_text_no_split() -> None:
    """Text shorter than max_len is returned as a single-element list."""
    text = "Hello, world!"
    result = _chunk_text(text, max_len=100)
    assert result == [text]


def test_chunk_text_splits_at_paragraph() -> None:
    """Text with double newline prefers the paragraph boundary for splitting."""
    first = "A" * 80
    second = "B" * 80
    text = first + "\n\n" + second
    result = _chunk_text(text, max_len=100)
    # Should have split somewhere — more than one chunk
    assert len(result) > 1
    # All content is preserved
    joined = " ".join(result)
    assert "A" * 10 in joined
    assert "B" * 10 in joined


def test_chunk_text_splits_at_newline() -> None:
    """Text with single newline (no double) splits at newline boundary."""
    first = "A" * 80
    second = "B" * 80
    text = first + "\n" + second
    result = _chunk_text(text, max_len=100)
    assert len(result) > 1


def test_chunk_text_splits_at_sentence() -> None:
    """Text with '. ' but no newlines splits at sentence boundary."""
    # Build text that exceeds max_len with sentence break inside
    first = "A" * 60 + ". "
    second = "B" * 60
    text = first + second
    result = _chunk_text(text, max_len=80)
    assert len(result) > 1


def test_chunk_text_hard_split() -> None:
    """Text with no natural boundary hard-splits at max_len."""
    text = "A" * 200  # no whitespace or punctuation
    result = _chunk_text(text, max_len=50)
    assert len(result) > 1
    for chunk in result:
        assert len(chunk) <= 50


def test_chunk_text_all_chunks_nonempty() -> None:
    """No empty strings appear in the output list."""
    text = "Hello world\n\nFoo bar\n\nBaz qux"
    result = _chunk_text(text, max_len=15)
    assert all(chunk != "" for chunk in result)
    assert all(chunk.strip() != "" for chunk in result)


def test_chunk_text_preserves_all_content() -> None:
    """Joining all chunks recovers the full content (whitespace may differ)."""
    text = "Word " * 100  # 500 chars
    result = _chunk_text(text, max_len=80)
    # Every word must appear in the joined output
    joined = "".join(result)
    original_words = text.split()
    for word in original_words:
        assert word in joined


# ---------------------------------------------------------------------------
# _extract_messages_from_json
# ---------------------------------------------------------------------------


def test_extract_claude_export_format() -> None:
    """Claude export dict with chat_messages + content as [{type, text}] blocks."""
    data = {
        "chat_messages": [
            {"sender": "human", "content": [{"type": "text", "text": "Hello?"}]},
            {"sender": "assistant", "content": [{"type": "text", "text": "Hi there!"}]},
        ]
    }
    result = _extract_messages_from_json(data, "all_messages")
    assert len(result) == 2
    assert result[0]["role"] == "human"
    assert result[0]["content"] == "Hello?"
    assert result[1]["role"] == "assistant"
    assert result[1]["content"] == "Hi there!"


def test_extract_standard_role_content_list() -> None:
    """List of {role, content} dicts extracts all messages."""
    data = [
        {"role": "user", "content": "What is Python?"},
        {"role": "assistant", "content": "A programming language."},
    ]
    result = _extract_messages_from_json(data, "all_messages")
    assert len(result) == 2
    assert result[0]["role"] == "user"
    assert result[1]["content"] == "A programming language."


def test_extract_dict_with_messages_key() -> None:
    """Dict with a 'messages' key delegates to the inner list."""
    data = {
        "messages": [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "World"},
        ]
    }
    result = _extract_messages_from_json(data, "all_messages")
    assert len(result) == 2


def test_extract_list_of_conversations() -> None:
    """List of dicts each with 'chat_messages' key extracts from all conversations."""
    data = [
        {"chat_messages": [{"sender": "human", "content": "Conv 1 msg"}]},
        {"chat_messages": [{"sender": "assistant", "content": "Conv 2 msg"}]},
    ]
    result = _extract_messages_from_json(data, "all_messages")
    assert len(result) == 2
    contents = [m["content"] for m in result]
    assert "Conv 1 msg" in contents
    assert "Conv 2 msg" in contents


def test_extract_empty_content_skipped() -> None:
    """Messages with empty or whitespace-only content are excluded."""
    data = [
        {"role": "user", "content": "  "},
        {"role": "user", "content": ""},
        {"role": "assistant", "content": "This should appear."},
    ]
    result = _extract_messages_from_json(data, "all_messages")
    assert len(result) == 1
    assert result[0]["content"] == "This should appear."


def test_extract_string_content() -> None:
    """Content as a plain string (not a list) is handled correctly."""
    data = [
        {"role": "user", "content": "Plain string content"},
    ]
    result = _extract_messages_from_json(data, "all_messages")
    assert len(result) == 1
    assert result[0]["content"] == "Plain string content"


# ---------------------------------------------------------------------------
# _filter_messages
# ---------------------------------------------------------------------------


_SAMPLE_MESSAGES = [
    {"role": "user", "content": "User question"},
    {"role": "human", "content": "Human question"},
    {"role": "assistant", "content": "Assistant answer"},
    {"role": "bot", "content": "Bot answer"},
    {"role": "summary_author", "content": "Summary content"},
]


def test_filter_assistant_messages() -> None:
    """Mode 'assistant_messages' returns only assistant/bot content."""
    result = _filter_messages(_SAMPLE_MESSAGES, "assistant_messages")
    assert result == ["Assistant answer", "Bot answer"]


def test_filter_user_messages() -> None:
    """Mode 'user_messages' returns only user/human content."""
    result = _filter_messages(_SAMPLE_MESSAGES, "user_messages")
    assert result == ["User question", "Human question"]


def test_filter_all_messages() -> None:
    """Mode 'all_messages' includes role prefix '[role] content'."""
    result = _filter_messages(_SAMPLE_MESSAGES, "all_messages")
    assert len(result) == len(_SAMPLE_MESSAGES)
    assert "[user] User question" in result
    assert "[assistant] Assistant answer" in result


def test_filter_conversations() -> None:
    """Mode 'conversations' joins all messages into one string."""
    result = _filter_messages(_SAMPLE_MESSAGES, "conversations")
    assert len(result) == 1
    assert "User question" in result[0]
    assert "Assistant answer" in result[0]


def test_filter_summaries() -> None:
    """Mode 'summaries' returns only messages whose role contains 'summary'."""
    result = _filter_messages(_SAMPLE_MESSAGES, "summaries")
    assert result == ["Summary content"]


def test_filter_default() -> None:
    """Unknown mode returns all content without any role prefix."""
    result = _filter_messages(_SAMPLE_MESSAGES, "unknown_mode")
    assert len(result) == len(_SAMPLE_MESSAGES)
    # No role prefix — just raw content
    for item in result:
        assert not item.startswith("[")


# ---------------------------------------------------------------------------
# _parse_markdown_chat
# ---------------------------------------------------------------------------


def test_parse_markdown_with_headers() -> None:
    """'## Human' / '## Assistant' Markdown headers are parsed correctly."""
    text = "## Human\nWhat is Python?\n\n## Assistant\nPython is a programming language.\n"
    result = _parse_markdown_chat(text, "all_messages")
    assert len(result) == 2
    assert any("What is Python?" in r for r in result)
    assert any("Python is a programming language." in r for r in result)


def test_parse_markdown_bold_prefix() -> None:
    """'**User:**' / '**Assistant:**' bold prefix format is parsed."""
    text = "**User:**\nHello there\n\n**Assistant:**\nHi back\n"
    result = _parse_markdown_chat(text, "all_messages")
    assert len(result) == 2
    assert any("Hello there" in r for r in result)
    assert any("Hi back" in r for r in result)


def test_parse_markdown_no_structure() -> None:
    """Plain text with no role markers is returned as a single entry."""
    text = "This is just plain text with no role markers at all."
    result = _parse_markdown_chat(text, "all_messages")
    assert result == [text]


def test_parse_markdown_empty() -> None:
    """Empty string returns empty list."""
    result = _parse_markdown_chat("", "all_messages")
    assert result == []


# ---------------------------------------------------------------------------
# _file_hash
# ---------------------------------------------------------------------------


def test_file_hash_deterministic(tmp_path: Path) -> None:
    """Same file content produces the same hash on repeated calls."""
    f = tmp_path / "test.txt"
    f.write_bytes(b"deterministic content")
    hash1 = _file_hash(str(f))
    hash2 = _file_hash(str(f))
    assert hash1 == hash2


def test_file_hash_different_content(tmp_path: Path) -> None:
    """Different file content produces a different hash."""
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_bytes(b"content A")
    f2.write_bytes(b"content B")
    assert _file_hash(str(f1)) != _file_hash(str(f2))


def test_file_hash_returns_16_chars(tmp_path: Path) -> None:
    """Hash is always exactly 16 hex characters."""
    f = tmp_path / "test.txt"
    f.write_bytes(b"any content here")
    result = _file_hash(str(f))
    assert len(result) == 16
    assert all(c in "0123456789abcdef" for c in result)


# ---------------------------------------------------------------------------
# Regression test — BUGF-01: import embedding ID matches INSERT ID
# ---------------------------------------------------------------------------


def test_import_embed_id_matches_insert_id(
    db_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """BUGF-01 regression: _embed_and_store is called with the exact mem_id
    that was used in the INSERT statement.

    Before the fix, the embedding loop recomputed a different ID via
    ``hashlib.sha256(f"{chunk}{now}").hexdigest()[:12]``, causing a mismatch
    between the memories row id and the embedding rowid.  After the fix, both
    loops iterate the same (mem_id, chunk) pairs collected during INSERT.
    """
    import remind_me_mcp.importer as _importer_mod

    # Spy: record every (memory_id, content) pair passed to _embed_and_store
    embedded_ids: list[str] = []

    def fake_embed_and_store(db, memory_id: str, content: str) -> None:
        embedded_ids.append(memory_id)

    monkeypatch.setattr(_importer_mod, "_embed_and_store", fake_embed_and_store)

    # Write a small chat JSON file with two assistant messages
    data = {
        "chat_messages": [
            {
                "sender": "assistant",
                "content": [{"type": "text", "text": "First unique content for BUGF-01 test."}],
            },
            {
                "sender": "assistant",
                "content": [{"type": "text", "text": "Second unique content for BUGF-01 test."}],
            },
        ]
    }
    chat_file = tmp_path / "bugf01_test.json"
    chat_file.write_text(json.dumps(data))

    result = import_chat_file(
        file_path=str(chat_file),
        category="test",
        tags=[],
        extract_mode="assistant_messages",
        max_length=10000,
    )
    assert result["status"] == "ok"
    assert result["memories_created"] >= 1

    # Every ID passed to _embed_and_store must exist in the memories table
    assert len(embedded_ids) >= 1, "Expected at least one embedding call"
    for mem_id in embedded_ids:
        row = db_conn.execute(
            "SELECT id FROM memories WHERE id = ?", (mem_id,)
        ).fetchone()
        assert row is not None, (
            f"_embed_and_store was called with mem_id={mem_id!r} but no matching "
            f"row exists in memories — this indicates the BUGF-01 ID mismatch bug."
        )


# ---------------------------------------------------------------------------
# Branch coverage — targeted tests for uncovered importer.py lines (Phase 09-02)
# ---------------------------------------------------------------------------


def test_import_jsonl_format(
    db_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """import_chat_file processes .jsonl files by iterating over newline-delimited JSON objects.

    Covers importer.py lines 279-289: the 'elif suffix in (".jsonl",)' branch
    that reads each line, parses it as JSON, extracts messages, and accumulates contents.
    """
    import remind_me_mcp.importer as _importer_mod

    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db_conn)
    monkeypatch.setattr(_importer_mod, "_embed_and_store", lambda db, mem_id, content: None)

    jsonl_file = tmp_path / "chat.jsonl"
    # Each JSONL line is a conversation object with chat_messages
    jsonl_file.write_text(
        '{"chat_messages": [{"sender": "assistant", "content": "Answer from first line"}]}\n'
        '{"chat_messages": [{"sender": "assistant", "content": "Answer from second line"}]}\n'
        "\n"  # blank line should be skipped
    )

    result = import_chat_file(
        file_path=str(jsonl_file),
        category="test",
        tags=[],
        extract_mode="assistant_messages",
        max_length=10000,
    )
    assert result["status"] == "ok"
    assert result["raw_entries"] >= 1


def test_import_multi_conversation_json(
    db_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """import_chat_file handles JSON arrays where each element is a conversation object.

    Covers importer.py lines 272-274: the 'if isinstance(data, list) and data and
    isinstance(data[0], dict) and ("chat_messages" in data[0] or "messages" in data[0])'
    branch that processes lists of conversation dicts.
    """
    import remind_me_mcp.importer as _importer_mod

    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db_conn)
    monkeypatch.setattr(_importer_mod, "_embed_and_store", lambda db, mem_id, content: None)

    multi_conv = [
        {"chat_messages": [{"sender": "assistant", "content": "First conversation answer"}]},
        {"chat_messages": [{"sender": "assistant", "content": "Second conversation answer"}]},
    ]
    json_file = tmp_path / "multi_conv.json"
    json_file.write_text(json.dumps(multi_conv))

    result = import_chat_file(
        file_path=str(json_file),
        category="test",
        tags=[],
        extract_mode="assistant_messages",
        max_length=10000,
    )
    assert result["status"] == "ok"
    assert result["raw_entries"] >= 2


def test_import_unsupported_format(
    db_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """import_chat_file returns a status='error' dict for unsupported file extensions.

    Covers importer.py line 262: the early-return guard checking suffix against
    the set of supported extensions (.json, .jsonl, .md, .markdown, .txt).
    """
    import remind_me_mcp.importer as _importer_mod

    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db_conn)

    xyz_file = tmp_path / "chat.xyz"
    xyz_file.write_text("some unsupported format content")

    result = import_chat_file(
        file_path=str(xyz_file),
        category="test",
        tags=[],
        extract_mode="assistant_messages",
        max_length=10000,
    )
    assert result["status"] == "error"
    assert "unsupported" in result["reason"].lower()


def test_import_jsonl_with_malformed_line(
    db_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """import_chat_file skips malformed JSONL lines without crashing.

    Covers importer.py lines 287-289: the except json.JSONDecodeError handler
    inside the JSONL loop that logs and continues past bad lines.
    """
    import remind_me_mcp.importer as _importer_mod

    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db_conn)
    monkeypatch.setattr(_importer_mod, "_embed_and_store", lambda db, mem_id, content: None)

    jsonl_file = tmp_path / "mixed.jsonl"
    jsonl_file.write_text(
        '{"chat_messages": [{"sender": "assistant", "content": "Valid line"}]}\n'
        "THIS IS NOT VALID JSON !!!\n"  # malformed line — should be skipped
    )

    result = import_chat_file(
        file_path=str(jsonl_file),
        category="test",
        tags=[],
        extract_mode="assistant_messages",
        max_length=10000,
    )
    # Import should succeed despite the malformed line
    assert result["status"] == "ok"
    # Only the valid line contributes
    assert result["raw_entries"] >= 1


def test_extract_messages_unrecognized_data() -> None:
    """_extract_messages_from_json returns an empty list for unrecognized data shapes.

    Covers importer.py line 155: the fallthrough 'return messages' for data that is
    not a list, not a dict with chat_messages, and not a dict with messages.
    """
    from remind_me_mcp.importer import _extract_messages_from_json

    # A plain string is not a recognizable conversation format
    result = _extract_messages_from_json("just a string", "all_messages")
    assert result == []

    # A number is not recognizable either
    result = _extract_messages_from_json(42, "all_messages")
    assert result == []


def test_extract_messages_chat_messages_string_blocks() -> None:
    """_extract_messages_from_json handles content blocks that are raw strings (not dicts).

    Covers importer.py lines 121-122: the 'elif isinstance(block, str)' branch
    inside the chat_messages content-list loop.
    """
    from remind_me_mcp.importer import _extract_messages_from_json

    data = {
        "chat_messages": [
            {
                "sender": "assistant",
                # content as list with raw string elements (not {type, text} dicts)
                "content": ["Hello from assistant as a raw string"],
            }
        ]
    }
    result = _extract_messages_from_json(data, "all_messages")
    assert len(result) == 1
    assert "Hello from assistant as a raw string" in result[0]["content"]


def test_extract_messages_non_string_content_field() -> None:
    """_extract_messages_from_json converts non-string, non-list content to str.

    Covers importer.py line 127: the 'else: content = str(content_field)' fallthrough
    when content_field is neither a list nor a string (e.g., an integer or None-ish value).
    """
    from remind_me_mcp.importer import _extract_messages_from_json

    data = {
        "chat_messages": [
            {
                "sender": "assistant",
                "content": 12345,  # integer — neither list nor str
            }
        ]
    }
    result = _extract_messages_from_json(data, "all_messages")
    assert len(result) == 1
    assert "12345" in result[0]["content"]


def test_extract_messages_list_content_in_role_list() -> None:
    """_extract_messages_from_json handles messages whose content field is a list.

    Covers importer.py lines 142-146: the 'if isinstance(content, list)' branch
    inside the standard {role, content} list path.
    """
    from remind_me_mcp.importer import _extract_messages_from_json

    data = [
        {
            "role": "assistant",
            # content as list of {text} dicts (not inside chat_messages)
            "content": [{"text": "Part one"}, {"text": "Part two"}],
        }
    ]
    result = _extract_messages_from_json(data, "all_messages")
    assert len(result) == 1
    assert "Part one" in result[0]["content"]


def test_filter_conversations_empty_messages() -> None:
    """_filter_messages with mode='conversations' and empty list returns empty list.

    Covers importer.py line 180: the 'return []' branch in conversations mode
    when the messages list is empty.
    """
    result = _filter_messages([], "conversations")
    assert result == []
