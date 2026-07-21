"""Tests for sliding-window embedding chunking (lever B).

Covers the pure ``chunk_text`` splitter, the multi-vector store/search path
(``_embed_and_store`` + ``_semantic_search`` deduping to parent memories), and
the v7->v8 migration that backfills legacy 1:1 vectors into ``vec_chunks``.
"""

import sqlite3

import pytest

from remind_me_mcp.db import (
    _embed_and_store,
    _embed_and_store_rows,
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


# ---------------------------------------------------------------------------
# PF-05: failed embeds must roll back uncommitted chunk DELETEs
# ---------------------------------------------------------------------------


class _WrongDimEmbedder:
    """Embedder whose vectors don't fit the vec0 table — INSERT fails after
    the old chunks were already DELETEd inside the same transaction."""

    def embed(self, texts, *, role="passage"):
        import numpy as np

        return np.zeros((len(texts), 8), dtype=np.float32)

    def embed_one(self, text, *, role="passage"):
        return self.embed([text], role=role)[0].tobytes()


def test_failed_embed_rolls_back_chunk_deletes(
    db_conn_with_vec: sqlite3.Connection, mock_embedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PF-05: when storing new chunk vectors fails, the in-flight DELETE of
    the memory's existing chunks is rolled back — it must not ride along
    with the next unrelated commit on the same connection."""
    import remind_me_mcp.db as db_mod

    content = "rollback survival test memory with enough text to embed"
    mem_id = _insert_memory(db_conn_with_vec, content)
    assert _embed_and_store(mem_id, content) is True

    rowid = db_conn_with_vec.execute(
        "SELECT rowid FROM memories WHERE id = ?", (mem_id,)
    ).fetchone()[0]
    before = db_conn_with_vec.execute(
        "SELECT COUNT(*) FROM vec_chunks WHERE memory_rowid = ?", (rowid,)
    ).fetchone()[0]
    assert before >= 1

    # Re-embed with an embedder whose vectors can't be INSERTed: the failure
    # hits after _delete_chunks already ran in the same transaction.
    monkeypatch.setattr(db_mod, "_get_embedder", lambda: _WrongDimEmbedder())
    assert _embed_and_store_rows([(rowid, content)]) == 0

    # An unrelated commit on this connection must not sweep the chunk
    # DELETEs along — the original embeddings survive.
    db_conn_with_vec.commit()
    after = db_conn_with_vec.execute(
        "SELECT COUNT(*) FROM vec_chunks WHERE memory_rowid = ?", (rowid,)
    ).fetchone()[0]
    assert after == before


# ---------------------------------------------------------------------------
# Internal batching (issue #16): _embed_and_store_rows must never hand an
# unbounded number of rows to one embed()/transaction, regardless of how
# many rows the caller passes in a single call. This is what fixes sync.py's
# _upsert_records, which hands over its entire pulled batch in one call with
# no batching of its own — the invariant now lives in _embed_and_store_rows
# itself, not in each caller.
# ---------------------------------------------------------------------------


def test_embed_and_store_rows_batches_large_input(
    db_conn_with_vec: sqlite3.Connection, mock_embedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single _embed_and_store_rows call with more than EMBED_BATCH_SIZE
    rows is split into EMBED_BATCH_SIZE-sized (or smaller, for the
    remainder) calls to _embed_and_store_batch — never one unbounded call."""
    import remind_me_mcp.db as db_mod

    monkeypatch.setattr(db_mod, "EMBED_BATCH_SIZE", 4)

    batch_sizes: list[int] = []
    real_batch = db_mod._embed_and_store_batch

    def spy_batch(embedder, rows):
        batch_sizes.append(len(rows))
        return real_batch(embedder, rows)

    monkeypatch.setattr(db_mod, "_embed_and_store_batch", spy_batch)

    rows = []
    for i in range(10):  # 10 rows over a batch size of 4 -> 4, 4, 2
        content = f"batch invariant memory number {i}"
        mem_id = _insert_memory(db_conn_with_vec, content)
        rowid = db_conn_with_vec.execute(
            "SELECT rowid FROM memories WHERE id = ?", (mem_id,)
        ).fetchone()[0]
        rows.append((rowid, content))

    stored = db_mod._embed_and_store_rows(rows)

    assert stored == 10
    assert batch_sizes == [4, 4, 2]
    assert all(n <= 4 for n in batch_sizes)


def test_embed_and_store_rows_single_call_below_batch_size_unaffected(
    db_conn_with_vec: sqlite3.Connection, mock_embedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Input at or under EMBED_BATCH_SIZE still goes through in one batch —
    the internal batching is transparent for the common case."""
    import remind_me_mcp.db as db_mod

    monkeypatch.setattr(db_mod, "EMBED_BATCH_SIZE", 32)

    batch_sizes: list[int] = []
    real_batch = db_mod._embed_and_store_batch

    def spy_batch(embedder, rows):
        batch_sizes.append(len(rows))
        return real_batch(embedder, rows)

    monkeypatch.setattr(db_mod, "_embed_and_store_batch", spy_batch)

    rows = []
    for i in range(3):
        content = f"small batch memory {i}"
        mem_id = _insert_memory(db_conn_with_vec, content)
        rowid = db_conn_with_vec.execute(
            "SELECT rowid FROM memories WHERE id = ?", (mem_id,)
        ).fetchone()[0]
        rows.append((rowid, content))

    stored = db_mod._embed_and_store_rows(rows)

    assert stored == 3
    assert batch_sizes == [3]


def test_sync_style_large_unbatched_pull_is_batched_internally(
    db_conn_with_vec: sqlite3.Connection, mock_embedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for issue #16: sync.py's _upsert_records hands its
    entire pulled batch to _embed_and_store_rows in one call with no
    batching of its own. Simulate that call shape directly (more rows than
    EMBED_BATCH_SIZE, one call) and confirm no single embed() call receives
    more chunks than the batch size allows."""
    import remind_me_mcp.db as db_mod

    monkeypatch.setattr(db_mod, "EMBED_BATCH_SIZE", 5)

    embed_call_sizes: list[int] = []
    real_embed = mock_embedder.embed

    def spy_embed(texts, *, role="passage"):
        embed_call_sizes.append(len(texts))
        return real_embed(texts, role=role)

    monkeypatch.setattr(mock_embedder, "embed", spy_embed)

    rows = []
    for i in range(13):  # a pulled sync batch bigger than one embed batch
        content = f"pulled sync record {i}"
        mem_id = _insert_memory(db_conn_with_vec, content)
        rowid = db_conn_with_vec.execute(
            "SELECT rowid FROM memories WHERE id = ?", (mem_id,)
        ).fetchone()[0]
        rows.append((rowid, content))

    # Mirrors sync.py's _upsert_records: the *entire* pulled batch in one call.
    stored = db_mod._embed_and_store_rows(rows)

    assert stored == 13
    # Each chunk of work handed to embed() must be bounded by EMBED_BATCH_SIZE
    # memories' worth of chunks (1 chunk/memory here, short content) -- never
    # one call spanning the whole 13-row input.
    assert all(n <= 5 for n in embed_call_sizes)
    assert len(embed_call_sizes) > 1


# ---------------------------------------------------------------------------
# ANN index integration (gap #10) — _semantic_search's ANN/brute-force split
# ---------------------------------------------------------------------------

usearch = pytest.importorskip("usearch", reason="usearch (the 'ann' extra) not installed")


def test_semantic_search_uses_ann_path_once_over_threshold(
    db_conn_with_vec: sqlite3.Connection, mock_embedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lowering ANN_MIN_CHUNKS to 0 forces every search through the ANN path
    (verified via a spy), and results still land on the right memory."""
    import remind_me_mcp.db as db_mod
    from remind_me_mcp import ann_index

    monkeypatch.setattr(db_mod, "ANN_MIN_CHUNKS", 0)

    calls = []
    real_search = ann_index.search

    def spy_search(db, query_vector, k):
        calls.append(1)
        return real_search(db, query_vector, k)

    monkeypatch.setattr(ann_index, "search", spy_search)

    content = "the ANN path should find this memory"
    mem_id = _insert_memory(db_conn_with_vec, content)
    _embed_and_store(mem_id, content)

    results = _semantic_search(content, limit=5)
    assert calls, "expected the ANN search path to be consulted"
    assert any(r["id"] == mem_id for r in results)


def test_semantic_search_stays_on_brute_force_below_threshold(
    db_conn_with_vec: sqlite3.Connection, mock_embedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default (large) threshold means a small test corpus never touches
    the ANN path at all — verified via a spy that must not be called."""
    import remind_me_mcp.db as db_mod
    from remind_me_mcp import ann_index

    assert db_mod.ANN_MIN_CHUNKS > 0  # sanity: production default is opt-in-by-scale

    calls = []
    monkeypatch.setattr(ann_index, "search", lambda *a, **k: calls.append(1))

    content = "brute force should handle this small corpus"
    mem_id = _insert_memory(db_conn_with_vec, content)
    _embed_and_store(mem_id, content)

    results = _semantic_search(content, limit=5)
    assert not calls, "ANN path must not be consulted below ANN_MIN_CHUNKS"
    assert any(r["id"] == mem_id for r in results)


def test_semantic_search_falls_back_to_brute_force_when_ann_returns_none(
    db_conn_with_vec: sqlite3.Connection, mock_embedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the ANN path can't serve a result (unavailable/failed), search still
    succeeds via the brute-force fallback — never a silent empty result."""
    import remind_me_mcp.db as db_mod
    from remind_me_mcp import ann_index

    monkeypatch.setattr(db_mod, "ANN_MIN_CHUNKS", 0)
    monkeypatch.setattr(ann_index, "search", lambda *a, **k: None)

    content = "fallback path should still find this memory"
    mem_id = _insert_memory(db_conn_with_vec, content)
    _embed_and_store(mem_id, content)

    results = _semantic_search(content, limit=5)
    assert any(r["id"] == mem_id for r in results)


def test_semantic_search_ann_path_respects_category_and_tag_filters(
    db_conn_with_vec: sqlite3.Connection, mock_embedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Category/tag filters apply identically whether ANN or brute force served
    the KNN — _hydrate_ann_hits mirrors the SQL path's filter semantics."""
    import remind_me_mcp.db as db_mod

    monkeypatch.setattr(db_mod, "ANN_MIN_CHUNKS", 0)

    content_a = "shared topic memory in category alpha"
    content_b = "shared topic memory in category beta"
    mem_a = _make_id(content_a)
    mem_b = _make_id(content_b)
    now = _now_iso()
    db_conn_with_vec.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
           VALUES (?, ?, 'alpha', '[]', 'manual', '{}', ?, ?)""",
        (mem_a, content_a, now, now),
    )
    db_conn_with_vec.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
           VALUES (?, ?, 'beta', '[]', 'manual', '{}', ?, ?)""",
        (mem_b, content_b, now, now),
    )
    db_conn_with_vec.commit()
    _embed_and_store(mem_a, content_a)
    _embed_and_store(mem_b, content_b)

    results = _semantic_search("shared topic memory", limit=10, category="alpha")
    ids = {r["id"] for r in results}
    assert mem_a in ids
    assert mem_b not in ids


def test_semantic_search_ann_path_excludes_superseded_memories(
    db_conn_with_vec: sqlite3.Connection, mock_embedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A superseded memory is excluded from ANN-served results, same as the
    brute-force SQL path's WHERE m.superseded_by IS NULL."""
    import remind_me_mcp.db as db_mod

    monkeypatch.setattr(db_mod, "ANN_MIN_CHUNKS", 0)

    content = "this memory will be marked superseded"
    mem_id = _insert_memory(db_conn_with_vec, content)
    _embed_and_store(mem_id, content)
    db_conn_with_vec.execute(
        "UPDATE memories SET superseded_by = 'someone-else' WHERE id = ?", (mem_id,)
    )
    db_conn_with_vec.commit()

    results = _semantic_search(content, limit=10)
    assert all(r["id"] != mem_id for r in results)


def test_semantic_search_ann_and_brute_force_agree_on_dedup_and_ranking(
    db_conn_with_vec: sqlite3.Connection, mock_embedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same corpus, same query: the ANN path and the brute-force path return
    the same set of memory ids in the same order (exact KNN either way at
    this small scale — usearch's HNSW is exact, not approximate, until the
    corpus is large enough to build a multi-layer graph)."""
    import remind_me_mcp.db as db_mod
    from remind_me_mcp import ann_index

    content = " ".join(f"piece{i}" for i in range(800))  # multi-chunk memory
    chunks = chunk_text(content)
    assert len(chunks) > 1
    mem_id = _insert_memory(db_conn_with_vec, content)
    _embed_and_store(mem_id, content)
    for i in range(4):
        _insert_memory(db_conn_with_vec, f"unrelated noise memory number {i}")
        # (left unembedded — brute force and ANN both only see embedded rows)

    monkeypatch.setattr(db_mod, "ANN_MIN_CHUNKS", 999999)
    brute_force = _semantic_search(chunks[0], limit=10)

    ann_index.reset_for_tests()
    monkeypatch.setattr(db_mod, "ANN_MIN_CHUNKS", 0)
    via_ann = _semantic_search(chunks[0], limit=10)

    assert [r["id"] for r in via_ann] == [r["id"] for r in brute_force]
    for a, b in zip(via_ann, brute_force, strict=True):
        assert a["semantic_distance"] == pytest.approx(b["semantic_distance"], abs=1e-4)
