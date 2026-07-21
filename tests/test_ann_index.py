"""
Tests for remind_me_mcp.ann_index — the optional HNSW ANN index for semantic
search (capability review gap #10).

Uses db_conn_with_vec (real sqlite-vec loaded) since ann_index's functions
take a live connection to read memories_vec / vec_chunks. The `usearch`
package is an optional extra (like sqlite-vec itself), so every test here
skips gracefully via pytest.importorskip when it isn't installed.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

usearch = pytest.importorskip("usearch", reason="usearch (the 'ann' extra) not installed")

from remind_me_mcp import ann_index  # noqa: E402


def _vec(seed: int, dim: int = 384) -> np.ndarray:
    """A deterministic, L2-normalised vector matching memories_vec's fixed
    384-dim schema (db_conn_with_vec creates the real vec0 table at that
    width, so test vectors must match it regardless of config.EMBEDDING_DIM)."""
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(dim).astype(np.float32)
    return v / np.linalg.norm(v)


# ---------------------------------------------------------------------------
# Graceful degradation when usearch is unavailable
# ---------------------------------------------------------------------------


def test_all_operations_are_noop_when_usearch_unavailable(monkeypatch, db_conn_with_vec) -> None:
    """Every public function degrades to a harmless no-op/None when usearch
    can't be imported — mirrors the embeddings/reranking/OTEL pattern."""
    monkeypatch.setattr(ann_index, "_usearch", lambda: None)

    assert ann_index.get_index(db_conn_with_vec) is None
    assert ann_index.search(db_conn_with_vec, _vec(1).tobytes(), 5) is None
    assert ann_index.rebuild_index(db_conn_with_vec) == 0
    # add_vector/remove_vector/save_index must not raise.
    ann_index.add_vector(db_conn_with_vec, 1, _vec(1).tobytes())
    ann_index.remove_vector(db_conn_with_vec, 1)
    ann_index.save_index()

    status = ann_index.status()
    assert status["available"] is False
    assert status["loaded"] is False
    assert status["size"] == 0


# ---------------------------------------------------------------------------
# Building / loading
# ---------------------------------------------------------------------------


def test_get_index_builds_from_existing_memories_vec_rows(db_conn_with_vec) -> None:
    """A fresh index is built from whatever is already in memories_vec."""
    for i in range(3):
        db_conn_with_vec.execute(
            "INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)",
            (i + 1, _vec(i).tobytes()),
        )
    db_conn_with_vec.commit()

    idx = ann_index.get_index(db_conn_with_vec)
    assert idx is not None
    assert len(idx) == 3


def test_get_index_on_empty_table_returns_empty_index(db_conn_with_vec) -> None:
    idx = ann_index.get_index(db_conn_with_vec)
    assert idx is not None
    assert len(idx) == 0


def test_get_index_caches_across_calls(db_conn_with_vec) -> None:
    """The same process-wide index instance is reused, not rebuilt every call."""
    first = ann_index.get_index(db_conn_with_vec)
    second = ann_index.get_index(db_conn_with_vec)
    assert first is second


# ---------------------------------------------------------------------------
# add_vector / remove_vector / search
# ---------------------------------------------------------------------------


def test_add_vector_then_search_finds_it(db_conn_with_vec) -> None:
    v = _vec(42)
    ann_index.add_vector(db_conn_with_vec, 100, v.tobytes())

    hits = ann_index.search(db_conn_with_vec, v.tobytes(), 5)
    assert hits is not None
    keys = [k for k, _ in hits]
    assert 100 in keys
    distance = dict(hits)[100]
    assert distance == pytest.approx(0.0, abs=1e-4)


def test_search_returns_none_when_index_empty(db_conn_with_vec) -> None:
    assert ann_index.search(db_conn_with_vec, _vec(1).tobytes(), 5) is None


def test_search_distance_matches_plain_l2_not_squared(db_conn_with_vec) -> None:
    """ann_index reports plain L2 (sqrt of usearch's l2sq) to match sqlite-vec's
    own vec0 distance convention — not squared L2."""
    a = np.zeros(384, dtype=np.float32)
    a[0] = 1.0
    b = np.zeros(384, dtype=np.float32)
    b[1] = 1.0
    ann_index.add_vector(db_conn_with_vec, 1, a.tobytes())
    ann_index.add_vector(db_conn_with_vec, 2, b.tobytes())

    hits = ann_index.search(db_conn_with_vec, a.tobytes(), 2)
    assert hits is not None
    by_key = dict(hits)
    assert by_key[1] == pytest.approx(0.0, abs=1e-5)
    assert by_key[2] == pytest.approx(math.sqrt(2), abs=1e-4)


def test_remove_vector_excludes_it_from_search(db_conn_with_vec) -> None:
    v = _vec(7)
    ann_index.add_vector(db_conn_with_vec, 5, v.tobytes())
    ann_index.remove_vector(db_conn_with_vec, 5)

    hits = ann_index.search(db_conn_with_vec, v.tobytes(), 5)
    assert hits is None or 5 not in dict(hits)


def test_add_vector_twice_replaces_not_duplicates(db_conn_with_vec) -> None:
    """Re-adding the same key (a re-embed) replaces rather than duplicating."""
    ann_index.add_vector(db_conn_with_vec, 9, _vec(1).tobytes())
    ann_index.add_vector(db_conn_with_vec, 9, _vec(2).tobytes())
    idx = ann_index.get_index(db_conn_with_vec)
    assert idx is not None
    assert len(idx) == 1


# ---------------------------------------------------------------------------
# Persistence: save / reload / staleness self-healing
# ---------------------------------------------------------------------------


def test_save_and_reload_round_trips(db_conn_with_vec) -> None:
    # add_vector only mutates the in-memory ANN index — mirror it with real
    # memories_vec rows too, since get_index()'s staleness check (below and
    # in production) compares against the real table's row count.
    for i in (1, 2):
        db_conn_with_vec.execute(
            "INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)", (i, _vec(i).tobytes())
        )
    db_conn_with_vec.commit()
    ann_index.add_vector(db_conn_with_vec, 1, _vec(1).tobytes())
    ann_index.add_vector(db_conn_with_vec, 2, _vec(2).tobytes())
    ann_index.save_index()

    ann_index.reset_for_tests()  # clear in-memory cache only, disk file stays
    idx = ann_index.get_index(db_conn_with_vec)
    assert idx is not None
    assert len(idx) == 2


def test_stale_disk_index_triggers_rebuild(db_conn_with_vec) -> None:
    """If memories_vec has moved on since the index was saved (e.g. a row was
    added by another process/run), the count mismatch triggers a rebuild
    instead of silently searching a stale index."""
    db_conn_with_vec.execute(
        "INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)", (1, _vec(1).tobytes())
    )
    db_conn_with_vec.commit()
    ann_index.add_vector(db_conn_with_vec, 1, _vec(1).tobytes())
    ann_index.save_index()
    ann_index.reset_for_tests()

    # Simulate a row that appeared after the index was saved, bypassing
    # add_vector entirely (as a crash-before-save scenario would).
    db_conn_with_vec.execute(
        "INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)", (2, _vec(2).tobytes())
    )
    db_conn_with_vec.commit()

    idx = ann_index.get_index(db_conn_with_vec)
    assert idx is not None
    assert len(idx) == 2  # rebuilt from memories_vec, not loaded stale from disk


# ---------------------------------------------------------------------------
# rebuild_index / status
# ---------------------------------------------------------------------------


def test_rebuild_index_reflects_current_memories_vec(db_conn_with_vec) -> None:
    for i in range(1, 5):
        db_conn_with_vec.execute(
            "INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)", (i, _vec(i).tobytes())
        )
    db_conn_with_vec.commit()

    count = ann_index.rebuild_index(db_conn_with_vec)
    assert count == 4


def test_status_reports_size_and_threshold(db_conn_with_vec, monkeypatch: pytest.MonkeyPatch) -> None:
    import remind_me_mcp.config as _cfg

    monkeypatch.setattr(_cfg, "ANN_MIN_CHUNKS", 123)
    ann_index.add_vector(db_conn_with_vec, 1, _vec(1).tobytes())

    status = ann_index.status()
    assert status["available"] is True
    assert status["loaded"] is True
    assert status["size"] == 1
    assert status["min_chunks_threshold"] == 123
