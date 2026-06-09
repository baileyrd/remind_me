"""Tests for sliding-window embedding chunking (lever B).

Covers the pure ``chunk_text`` splitter, the multi-vector store/search path
(``_embed_and_store`` + ``_semantic_search`` deduping to parent memories), and
the v7->v8 migration that backfills legacy 1:1 vectors into ``vec_chunks``.
"""

import sqlite3

import pytest

from remind_me_mcp.db import (
    _embed_and_store,
    _make_id,
    _now_iso,
    _semantic_search,
)
from remind_me_mcp.embeddings import chunk_text

# ---------------------------------------------------------------------------
# chunk_text — pure splitter
# ---------------------------------------------------------------------------


def test_chunk_text_short_returns_single_chunk() -> None:
    """Content at or under max_chars is returned unchanged as one chunk."""
    text = "a short memory"
    assert chunk_text(text, max_chars=100, overlap=10) == [text]


def test_chunk_text_blank_returns_empty() -> None:
    """Whitespace-only / empty content yields no chunks."""
    assert chunk_text("   \n  ", max_chars=100, overlap=10) == []
    assert chunk_text("", max_chars=100, overlap=10) == []


def test_chunk_text_long_text_splits_with_full_coverage() -> None:
    """A long document splits into several windows that together keep every word."""
    words = [f"word{i}" for i in range(400)]
    text = " ".join(words)
    chunks = chunk_text(text, max_chars=200, overlap=40, max_chunks=64)

    assert len(chunks) > 1
    assert all(c for c in chunks)  # no empty chunks
    assert all(len(c) <= 200 for c in chunks)
    # No content is lost: every original word appears in some chunk.
    seen = set()
    for c in chunks:
        seen.update(c.split())
    assert seen == set(words)


def test_chunk_text_consecutive_windows_overlap() -> None:
    """Adjacent windows share content so boundary-straddling evidence survives."""
    text = " ".join(f"tok{i}" for i in range(300))
    chunks = chunk_text(text, max_chars=200, overlap=60, max_chunks=64)
    assert len(chunks) >= 2
    # Consecutive chunks share at least one token.
    assert set(chunks[0].split()) & set(chunks[1].split())


def test_chunk_text_respects_max_chunks_cap() -> None:
    """The window count never exceeds max_chunks (tail is dropped)."""
    text = " ".join(f"w{i}" for i in range(2000))
    chunks = chunk_text(text, max_chars=100, overlap=20, max_chunks=3)
    assert len(chunks) == 3


# ---------------------------------------------------------------------------
# Multi-vector store + dedup search
# ---------------------------------------------------------------------------


def _insert_memory(db: sqlite3.Connection, content: str) -> str:
    """Insert a bare memory row and return its id."""
    mem_id = _make_id(content)
    now = _now_iso()
    db.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (mem_id, content, "general", "[]", "manual", "{}", now, now),
    )
    db.commit()
    return mem_id


def test_embed_stores_one_vector_per_chunk(
    db_conn_with_vec: sqlite3.Connection, mock_embedder
) -> None:
    """A long memory is stored as N chunk vectors, all mapped to its rowid."""
    content = " ".join(f"fact{i}" for i in range(600))
    expected = len(chunk_text(content))
    assert expected > 1  # precondition: this content actually chunks

    mem_id = _insert_memory(db_conn_with_vec, content)
    assert _embed_and_store(mem_id, content) is True

    rowid = db_conn_with_vec.execute(
        "SELECT rowid FROM memories WHERE id = ?", (mem_id,)
    ).fetchone()[0]
    n_chunks = db_conn_with_vec.execute(
        "SELECT COUNT(*) FROM vec_chunks WHERE memory_rowid = ?", (rowid,)
    ).fetchone()[0]
    n_vecs = db_conn_with_vec.execute("SELECT COUNT(*) FROM memories_vec").fetchone()[0]
    assert n_chunks == expected
    assert n_vecs == expected


def test_tail_chunk_is_retrievable(
    db_conn_with_vec: sqlite3.Connection, mock_embedder
) -> None:
    """Evidence in the tail of a long memory is found — the truncation fix.

    The deterministic fake embedder maps identical text to identical vectors, so
    querying with a verbatim tail chunk yields distance ~0 *only if that tail was
    actually embedded*. Under the old 256-token / 2000-char truncation it would
    not have been, and this would miss.
    """
    content = " ".join(f"item{i}" for i in range(800))
    chunks = chunk_text(content)
    assert len(chunks) > 2
    tail = chunks[-1]

    mem_id = _insert_memory(db_conn_with_vec, content)
    _embed_and_store(mem_id, content)

    results = _semantic_search(tail, limit=10)
    assert any(r["id"] == mem_id for r in results)
    hit = next(r for r in results if r["id"] == mem_id)
    assert hit["semantic_distance"] == pytest.approx(0.0, abs=1e-4)


def test_search_dedupes_to_one_row_per_memory(
    db_conn_with_vec: sqlite3.Connection, mock_embedder
) -> None:
    """A multi-chunk memory appears at most once in results (best chunk kept)."""
    content = " ".join(f"piece{i}" for i in range(800))
    chunks = chunk_text(content)
    mem_id = _insert_memory(db_conn_with_vec, content)
    _embed_and_store(mem_id, content)

    results = _semantic_search(chunks[0], limit=10)
    ids = [r["id"] for r in results]
    assert ids.count(mem_id) == 1


def test_reembed_replaces_old_chunks(
    db_conn_with_vec: sqlite3.Connection, mock_embedder
) -> None:
    """Re-embedding a memory clears its previous chunk vectors."""
    long_content = " ".join(f"a{i}" for i in range(600))
    mem_id = _insert_memory(db_conn_with_vec, long_content)
    _embed_and_store(mem_id, long_content)
    rowid = db_conn_with_vec.execute(
        "SELECT rowid FROM memories WHERE id = ?", (mem_id,)
    ).fetchone()[0]
    assert (
        db_conn_with_vec.execute(
            "SELECT COUNT(*) FROM vec_chunks WHERE memory_rowid = ?", (rowid,)
        ).fetchone()[0]
        > 1
    )

    # Re-embed with short content -> exactly one chunk, no orphans left behind.
    _embed_and_store(mem_id, "now short")
    assert (
        db_conn_with_vec.execute(
            "SELECT COUNT(*) FROM vec_chunks WHERE memory_rowid = ?", (rowid,)
        ).fetchone()[0]
        == 1
    )
    # No dangling vectors: every memories_vec row is mapped in vec_chunks.
    mapped = db_conn_with_vec.execute(
        """SELECT COUNT(*) FROM memories_vec mv
           LEFT JOIN vec_chunks vc ON vc.vec_rowid = mv.rowid
           WHERE vc.vec_rowid IS NULL"""
    ).fetchone()[0]
    assert mapped == 0


# ---------------------------------------------------------------------------
# Migration v7 -> v8 — backfill legacy 1:1 vectors
# ---------------------------------------------------------------------------


def test_migration_backfills_legacy_vectors(
    db_conn_with_vec: sqlite3.Connection, mock_embedder
) -> None:
    """A pre-v8 DB with a 1:1 memories_vec row is backfilled as chunk_ix=0.

    Simulates the legacy layout (vector keyed by memory rowid, no vec_chunks),
    rewinds user_version to 7, re-runs the migration, and asserts the vector is
    mapped to its parent and still retrievable.
    """
    from remind_me_mcp.db import _migrate_schema

    content = "legacy single-vector memory"
    mem_id = _insert_memory(db_conn_with_vec, content)
    rowid = db_conn_with_vec.execute(
        "SELECT rowid FROM memories WHERE id = ?", (mem_id,)
    ).fetchone()[0]

    # Recreate the old layout: clear the map and store one vector at the memory
    # rowid, exactly as the pre-chunking code did.
    db_conn_with_vec.execute("DELETE FROM vec_chunks")
    db_conn_with_vec.execute("DELETE FROM memories_vec")
    vec = mock_embedder.embed_one(content)
    db_conn_with_vec.execute(
        "INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)", (rowid, vec)
    )
    db_conn_with_vec.execute("PRAGMA user_version = 7")
    db_conn_with_vec.commit()

    _migrate_schema(db_conn_with_vec)

    mapped = db_conn_with_vec.execute(
        "SELECT memory_rowid, chunk_ix FROM vec_chunks WHERE vec_rowid = ?", (rowid,)
    ).fetchone()
    assert mapped is not None
    assert mapped["memory_rowid"] == rowid
    assert mapped["chunk_ix"] == 0

    results = _semantic_search(content, limit=5)
    assert any(r["id"] == mem_id for r in results)
