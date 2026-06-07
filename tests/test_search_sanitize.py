"""
Tests for FTS5 query sanitization in remind_me_search.

Natural-language questions contain punctuation that FTS5 treats as operator
syntax. These tests verify that:
  1. ``_sanitize_fts_query`` turns arbitrary text into a valid OR-of-terms query.
  2. A punctuated NL question now matches via the keyword tier instead of
     silently returning nothing.
  3. Valid FTS5 syntax (explicit OR / phrases) still takes the primary path.
"""

from __future__ import annotations

import json

import pytest

from remind_me_mcp.models import MemorySearchInput, ResponseFormat
from remind_me_mcp.tools import _sanitize_fts_query, memory_search


@pytest.fixture()
def _no_semantic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the embedder off so only the FTS keyword tier contributes."""
    monkeypatch.setattr("remind_me_mcp.embeddings._get_embedder", lambda: None)
    monkeypatch.setattr("remind_me_mcp.db._get_embedder", lambda: None)


def test_sanitize_basic_question():
    assert _sanitize_fts_query("What degree?") == '"What" OR "degree"'


def test_sanitize_strips_punctuation_and_possessives():
    # Apostrophes, commas, and "$" are split out; word tokens survive.
    assert _sanitize_fts_query("shelter's $5 coupon, please") == (
        '"shelter" OR "s" OR "5" OR "coupon" OR "please"'
    )


def test_sanitize_empty_when_no_tokens():
    assert _sanitize_fts_query("???") == ""
    assert _sanitize_fts_query("   ") == ""


def test_sanitize_escapes_embedded_quotes():
    # A literal double quote in a token is doubled per FTS5 string rules.
    assert _sanitize_fts_query('say "hi"') == '"say" OR "hi"'


async def test_nl_question_matches_via_sanitized_fts(db_conn, memory_factory, _no_semantic):
    memory_factory(content="The capstone degree project covered marine biology.")
    memory_factory(content="Unrelated note about weekend grocery shopping.")

    params = MemorySearchInput(
        query="What degree did I graduate with?",
        response_format=ResponseFormat.JSON,
        token_budget=0,
    )
    payload = json.loads(await memory_search(params))

    contents = [m["content"] for m in payload["memories"]]
    assert any("degree project" in c for c in contents)


async def test_raw_question_alone_would_not_match_without_sanitization(db_conn, memory_factory, _no_semantic):
    """Sanity check: the punctuated query is invalid FTS5, so the fix is load-bearing."""
    import sqlite3

    memory_factory(content="The capstone degree project covered marine biology.")
    with pytest.raises(sqlite3.OperationalError):
        db_conn.execute(
            "SELECT m.* FROM memories m JOIN memories_fts fts ON m.rowid = fts.rowid "
            "WHERE memories_fts MATCH ? ORDER BY rank",
            ("What degree did I graduate with?",),
        ).fetchall()


async def test_valid_fts_syntax_still_works(db_conn, memory_factory, _no_semantic):
    memory_factory(content="Notes on biology coursework.")
    memory_factory(content="Notes on chemistry coursework.")

    params = MemorySearchInput(
        query="biology OR chemistry",
        response_format=ResponseFormat.JSON,
        token_budget=0,
    )
    payload = json.loads(await memory_search(params))
    assert len(payload["memories"]) == 2
