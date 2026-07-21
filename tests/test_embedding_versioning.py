"""
Tests for embedding-model versioning + auto-clear-on-mismatch (issue #18).

embedding_mismatch_info/_mark_embedding_meta_current are pure metadata-table
operations and use the plain db_conn fixture; anything touching
memories_vec/vec_chunks (the actual clearing) needs db_conn_with_vec (real
sqlite-vec loaded), mirroring tests/test_ann_index.py's precedent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from remind_me_mcp import ann_index
from remind_me_mcp.db import (
    _SCHEMA_VERSION,
    EMBEDDING_BACKEND,
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    _embed_and_store_rows,
    _mark_embedding_meta_current,
    _now_iso,
    _reconcile_embedding_meta,
    embedding_mismatch_info,
)

if TYPE_CHECKING:
    import sqlite3

    from tests.conftest import FakeEmbedder


def _vec(seed: int, dim: int = EMBEDDING_DIM) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(dim).astype(np.float32)
    return v / np.linalg.norm(v)


def _set_meta(db: sqlite3.Connection, *, model: str, dim: str, backend: str) -> None:
    now = _now_iso()
    for key, value in (("model", model), ("dim", dim), ("backend", backend)):
        db.execute(
            "INSERT INTO embedding_meta (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, now),
        )
    db.commit()


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migration_creates_embedding_meta_table(db_conn: sqlite3.Connection) -> None:
    tables = {
        r[0]
        for r in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert "embedding_meta" in tables


def test_schema_version_is_18() -> None:
    assert _SCHEMA_VERSION == 18


# ---------------------------------------------------------------------------
# embedding_mismatch_info
# ---------------------------------------------------------------------------


def test_mismatch_info_none_when_no_meta_recorded(db_conn: sqlite3.Connection) -> None:
    """A fresh store (or one that predates this feature) has nothing to compare."""
    assert embedding_mismatch_info(db_conn) is None


def test_mismatch_info_none_when_meta_matches_config(db_conn: sqlite3.Connection) -> None:
    _set_meta(db_conn, model=EMBEDDING_MODEL, dim=str(EMBEDDING_DIM), backend=EMBEDDING_BACKEND)

    assert embedding_mismatch_info(db_conn) is None


def test_mismatch_info_detects_model_change(db_conn: sqlite3.Connection) -> None:
    _set_meta(db_conn, model="some-other-model", dim=str(EMBEDDING_DIM), backend=EMBEDDING_BACKEND)

    info = embedding_mismatch_info(db_conn)

    assert info is not None
    assert info["stored_model"] == "some-other-model"
    assert info["current_model"] == EMBEDDING_MODEL


def test_mismatch_info_detects_dim_change(db_conn: sqlite3.Connection) -> None:
    _set_meta(db_conn, model=EMBEDDING_MODEL, dim="768", backend=EMBEDDING_BACKEND)

    info = embedding_mismatch_info(db_conn)

    assert info is not None
    assert info["stored_dim"] == "768"
    assert info["current_dim"] == str(EMBEDDING_DIM)


def test_mismatch_info_detects_backend_change(db_conn: sqlite3.Connection) -> None:
    _set_meta(db_conn, model=EMBEDDING_MODEL, dim=str(EMBEDDING_DIM), backend="ollama")

    info = embedding_mismatch_info(db_conn)

    assert info is not None
    assert info["stored_backend"] == "ollama"
    assert info["current_backend"] == EMBEDDING_BACKEND


def test_mismatch_info_handles_missing_table_gracefully(db_conn: sqlite3.Connection) -> None:
    """A pre-v18 database (table doesn't exist yet) reports no mismatch rather than raising."""
    db_conn.execute("DROP TABLE embedding_meta")
    db_conn.commit()

    assert embedding_mismatch_info(db_conn) is None


# ---------------------------------------------------------------------------
# _reconcile_embedding_meta
# ---------------------------------------------------------------------------


def test_reconcile_noop_when_no_meta_recorded(db_conn_with_vec: sqlite3.Connection) -> None:
    db_conn_with_vec.execute(
        "INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)", (1, _vec(1).tobytes())
    )
    db_conn_with_vec.commit()

    _reconcile_embedding_meta(db_conn_with_vec)

    count = db_conn_with_vec.execute("SELECT COUNT(*) FROM memories_vec").fetchone()[0]
    assert count == 1


def test_reconcile_noop_when_meta_matches(db_conn_with_vec: sqlite3.Connection) -> None:
    _set_meta(
        db_conn_with_vec, model=EMBEDDING_MODEL, dim=str(EMBEDDING_DIM), backend=EMBEDDING_BACKEND
    )
    db_conn_with_vec.execute(
        "INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)", (1, _vec(1).tobytes())
    )
    db_conn_with_vec.commit()

    _reconcile_embedding_meta(db_conn_with_vec)

    count = db_conn_with_vec.execute("SELECT COUNT(*) FROM memories_vec").fetchone()[0]
    assert count == 1


def test_reconcile_clears_stale_vectors_on_mismatch(db_conn_with_vec: sqlite3.Connection) -> None:
    _set_meta(db_conn_with_vec, model="old-model", dim=str(EMBEDDING_DIM), backend=EMBEDDING_BACKEND)
    db_conn_with_vec.execute(
        "INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)", (1, _vec(1).tobytes())
    )
    db_conn_with_vec.execute(
        "INSERT INTO vec_chunks(vec_rowid, memory_rowid, chunk_ix) VALUES (1, 1, 0)"
    )
    db_conn_with_vec.commit()

    _reconcile_embedding_meta(db_conn_with_vec)

    vec_count = db_conn_with_vec.execute("SELECT COUNT(*) FROM memories_vec").fetchone()[0]
    chunk_count = db_conn_with_vec.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0]
    assert vec_count == 0
    assert chunk_count == 0


def test_reconcile_leaves_meta_stale_for_next_check(db_conn_with_vec: sqlite3.Connection) -> None:
    """Meta is deliberately NOT updated by reconcile -- only a real re-embed clears the flag."""
    _set_meta(db_conn_with_vec, model="old-model", dim=str(EMBEDDING_DIM), backend=EMBEDDING_BACKEND)

    _reconcile_embedding_meta(db_conn_with_vec)

    info = embedding_mismatch_info(db_conn_with_vec)
    assert info is not None
    assert info["stored_model"] == "old-model"


def test_reconcile_recreates_a_usable_memories_vec_table(db_conn_with_vec: sqlite3.Connection) -> None:
    _set_meta(db_conn_with_vec, model="old-model", dim=str(EMBEDDING_DIM), backend=EMBEDDING_BACKEND)

    _reconcile_embedding_meta(db_conn_with_vec)

    # Must still be insertable afterward -- proves it was recreated, not just dropped.
    db_conn_with_vec.execute(
        "INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)", (1, _vec(1).tobytes())
    )
    db_conn_with_vec.commit()
    count = db_conn_with_vec.execute("SELECT COUNT(*) FROM memories_vec").fetchone()[0]
    assert count == 1


def test_reconcile_invalidates_the_ann_index(db_conn_with_vec: sqlite3.Connection) -> None:
    db_conn_with_vec.execute(
        "INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)", (1, _vec(1).tobytes())
    )
    db_conn_with_vec.commit()
    idx = ann_index.get_index(db_conn_with_vec)
    assert idx is not None and len(idx) == 1
    ann_index.save_index()
    assert ann_index._index_path().exists()

    _set_meta(db_conn_with_vec, model="old-model", dim=str(EMBEDDING_DIM), backend=EMBEDDING_BACKEND)
    _reconcile_embedding_meta(db_conn_with_vec)

    assert not ann_index._index_path().exists()
    status = ann_index.status()
    assert status["size"] == 0  # rebuilt from the now-empty memories_vec


# ---------------------------------------------------------------------------
# _mark_embedding_meta_current
# ---------------------------------------------------------------------------


def test_mark_current_writes_all_three_keys(db_conn: sqlite3.Connection) -> None:
    _mark_embedding_meta_current(db_conn)

    rows = {r[0]: r[1] for r in db_conn.execute("SELECT key, value FROM embedding_meta").fetchall()}
    assert rows["model"] == EMBEDDING_MODEL
    assert rows["dim"] == str(EMBEDDING_DIM)
    assert rows["backend"] == EMBEDDING_BACKEND


def test_mark_current_overwrites_stale_values(db_conn: sqlite3.Connection) -> None:
    _set_meta(db_conn, model="old-model", dim="768", backend="ollama")

    _mark_embedding_meta_current(db_conn)

    assert embedding_mismatch_info(db_conn) is None


def test_mark_current_is_idempotent(db_conn: sqlite3.Connection) -> None:
    _mark_embedding_meta_current(db_conn)
    _mark_embedding_meta_current(db_conn)

    count = db_conn.execute("SELECT COUNT(*) FROM embedding_meta").fetchone()[0]
    assert count == 3


# ---------------------------------------------------------------------------
# Integration: embedding writes mark meta current
# ---------------------------------------------------------------------------


def test_embed_and_store_rows_marks_meta_current(
    db_conn_with_vec: sqlite3.Connection, mock_embedder: FakeEmbedder
) -> None:
    now = _now_iso()
    db_conn_with_vec.execute(
        "INSERT INTO memories (id, content, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("mem-1", "Some content to embed", now, now),
    )
    db_conn_with_vec.commit()
    rowid = db_conn_with_vec.execute(
        "SELECT rowid FROM memories WHERE id = ?", ("mem-1",)
    ).fetchone()[0]

    stored = _embed_and_store_rows([(rowid, "Some content to embed")])

    assert stored == 1
    assert embedding_mismatch_info(db_conn_with_vec) is None


def test_mismatch_then_reembed_cycle_clears_the_flag(
    db_conn_with_vec: sqlite3.Connection, mock_embedder: FakeEmbedder
) -> None:
    """Full cycle: stale meta -> reconcile clears vectors -> re-embed clears the mismatch flag."""
    now = _now_iso()
    db_conn_with_vec.execute(
        "INSERT INTO memories (id, content, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("mem-1", "Some content to embed", now, now),
    )
    db_conn_with_vec.commit()
    rowid = db_conn_with_vec.execute(
        "SELECT rowid FROM memories WHERE id = ?", ("mem-1",)
    ).fetchone()[0]
    db_conn_with_vec.execute(
        "INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)", (999, _vec(1).tobytes())
    )
    db_conn_with_vec.commit()
    _set_meta(db_conn_with_vec, model="old-model", dim=str(EMBEDDING_DIM), backend=EMBEDDING_BACKEND)

    _reconcile_embedding_meta(db_conn_with_vec)
    assert embedding_mismatch_info(db_conn_with_vec) is not None  # still flagged

    stored = _embed_and_store_rows([(rowid, "Some content to embed")])

    assert stored == 1
    assert embedding_mismatch_info(db_conn_with_vec) is None  # flag cleared by the re-embed
