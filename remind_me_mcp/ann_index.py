"""
remind_me_mcp.ann_index — Optional HNSW ANN index for semantic search
(capability review gap #10, 2026-07-21).

`_semantic_search` (db.py) runs a brute-force scan over every chunk vector in
`memories_vec` via sqlite-vec's `vec0` MATCH operator — correct, but O(n) per
query. That's invisible at typical single-user scale but degrades as a store
grows into the tens of thousands of chunks. This module adds an optional
HNSW approximate-nearest-neighbor index (via the `usearch` package, the
`ann` extra) that `_semantic_search` consults first once the corpus is large
enough to benefit (`config.ANN_MIN_CHUNKS`); below that, or whenever
`usearch` isn't installed, callers fall back to the existing exact
brute-force scan unchanged — the same graceful-degradation posture as
embeddings/reranking/OTEL elsewhere in this codebase.

The index is keyed by `vec_chunks.vec_rowid` (the same id already used to
join chunk vectors back to their parent memory), held in memory for the life
of the process, mutated incrementally as chunks are added/removed, and
persisted to disk (``ann_index.usearch`` next to the DB file) on clean
shutdown. A missing, corrupt, or size-mismatched index file triggers a full
rebuild from `memories_vec` on next use — self-healing, mirroring
`db._prune_orphan_chunks`.

Every public function here is best-effort and never raises: a failure
degrades to "ANN unavailable for this process" (logged once) rather than
breaking search. Callers only need to handle `None`/no-op returns.

Distance metric is squared L2 (`usearch`'s ``l2sq``), reported as plain L2
(``sqrt(l2sq)``) to match sqlite-vec's own `vec0` distance convention (see
db.py's `_semantic_search`), so a `semantic_distance` value means the same
thing regardless of which path served the query.
"""

from __future__ import annotations

import contextlib
import logging
import math
import threading
from typing import TYPE_CHECKING, Any

from remind_me_mcp import config

if TYPE_CHECKING:
    import sqlite3

log = logging.getLogger("remind_me_mcp.ann_index")

_usearch_mod: Any = None
_usearch_checked = False


def _usearch() -> Any | None:
    """Import usearch.index lazily, caching the (un)availability check."""
    global _usearch_mod, _usearch_checked
    if not _usearch_checked:
        _usearch_checked = True
        try:
            import usearch.index as mod
        except ImportError as e:
            log.debug(
                "usearch not installed: %s (ANN index disabled, using brute-force scan)", e
            )
        else:
            _usearch_mod = mod
    return _usearch_mod


_lock = threading.Lock()
_index: Any | None = None
_index_failed = False
"""Sticky for the life of the process once a build/load attempt errors, so a
structurally broken index (e.g. a dimension mismatch from a changed
embedding model) isn't retried on every single search. A fresh process
(after fixing the underlying cause) gets a clean attempt."""


def _index_path() -> Any:
    return config.MEMORY_DIR / "ann_index.usearch"


def _new_index() -> Any:
    mod = _usearch()
    assert mod is not None
    return mod.Index(ndim=config.EMBEDDING_DIM, metric="l2sq", dtype="f32")


def _build_from_db(db: sqlite3.Connection) -> Any:
    """Construct a fresh index from every row currently in memories_vec."""
    import numpy as np

    idx = _new_index()
    rows = db.execute("SELECT rowid, embedding FROM memories_vec").fetchall()
    if rows:
        keys = np.array([r[0] for r in rows], dtype=np.int64)
        vecs = np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
        idx.add(keys, vecs)
    return idx


def get_index(db: sqlite3.Connection) -> Any | None:
    """Return the process-wide ANN index, building or loading it on first use.

    Returns None when `usearch` isn't installed, `memories_vec` doesn't exist
    (sqlite-vec not loaded), or a previous build/load attempt failed this
    process (sticky — see `_index_failed`).
    """
    global _index, _index_failed
    if _usearch() is None or _index_failed:
        return None
    with _lock:
        if _index is not None:
            return _index
        try:
            path = _index_path()
            if path.exists():
                idx = _new_index()
                idx.load(str(path))
                (count,) = db.execute("SELECT COUNT(*) FROM memories_vec").fetchone()
                if len(idx) != count:
                    log.info(
                        "ANN index stale (%d indexed vs %d in memories_vec) — rebuilding",
                        len(idx),
                        count,
                    )
                    idx = _build_from_db(db)
            else:
                idx = _build_from_db(db)
        except Exception:
            log.warning(
                "Failed to build/load ANN index — falling back to brute-force scan for this process",
                exc_info=True,
            )
            _index_failed = True
            return None
        _index = idx
        return _index


def add_vector(db: sqlite3.Connection, vec_rowid: int, vector: bytes) -> None:
    """Add one chunk vector to the ANN index. No-op if ANN is unavailable.

    Call only after the corresponding SQL insert has committed — ANN
    mutations aren't part of the SQL transaction and can't be rolled back
    with it (see db._embed_and_store_rows).
    """
    idx = get_index(db)
    if idx is None:
        return
    import numpy as np

    with _lock:
        try:
            if vec_rowid in idx:
                idx.remove(vec_rowid)
            idx.add(
                np.array([vec_rowid], dtype=np.int64),
                np.frombuffer(vector, dtype=np.float32).reshape(1, -1),
            )
        except Exception:
            log.warning("Failed to add vector %d to ANN index", vec_rowid, exc_info=True)


def remove_vector(db: sqlite3.Connection, vec_rowid: int) -> None:
    """Remove one chunk vector from the ANN index. No-op if ANN is unavailable.

    Call only after the corresponding SQL delete has committed (see
    db._delete_chunks / db._prune_orphan_chunks).
    """
    idx = get_index(db)
    if idx is None:
        return
    with _lock:
        try:
            if vec_rowid in idx:
                idx.remove(vec_rowid)
        except Exception:
            log.warning("Failed to remove vector %d from ANN index", vec_rowid, exc_info=True)


def search(db: sqlite3.Connection, query_vector: bytes, k: int) -> list[tuple[int, float]] | None:
    """Return up to *k* ``(vec_rowid, l2_distance)`` pairs nearest *query_vector*.

    Returns None when ANN is unavailable, the index is empty, or the search
    itself fails — callers should treat None as "fall back to brute force,"
    not as "no results."
    """
    idx = get_index(db)
    if idx is None or len(idx) == 0:
        return None
    import numpy as np

    with _lock:
        try:
            matches = idx.search(np.frombuffer(query_vector, dtype=np.float32), k)
        except Exception:
            log.warning("ANN search failed — falling back to brute-force scan", exc_info=True)
            return None
    return [
        (int(key), math.sqrt(max(float(dist), 0.0)))
        for key, dist in zip(matches.keys, matches.distances, strict=True)
    ]


def save_index() -> None:
    """Persist the in-memory ANN index to disk. Called at server shutdown."""
    with _lock:
        if _index is None:
            return
        try:
            _index.save(str(_index_path()))
        except Exception:
            log.warning("Failed to save ANN index to disk", exc_info=True)


def rebuild_index(db: sqlite3.Connection) -> int:
    """Force a full rebuild from memories_vec. Returns the vector count (0 if
    ANN is unavailable)."""
    global _index, _index_failed
    if _usearch() is None:
        return 0
    with _lock:
        try:
            idx = _build_from_db(db)
        except Exception:
            log.warning("Failed to rebuild ANN index", exc_info=True)
            _index_failed = True
            return 0
        _index = idx
        _index_failed = False
        return len(idx)


def status() -> dict[str, Any]:
    """Small status summary for remind_me_server_status."""
    return {
        "available": _usearch() is not None,
        "loaded": _index is not None,
        "size": len(_index) if _index is not None else 0,
        "min_chunks_threshold": config.ANN_MIN_CHUNKS,
    }


def invalidate_index(db: sqlite3.Connection) -> None:
    """Discard the in-memory index and its persisted file, then rebuild from `db`.

    Used when the embedding model/dimension changes (issue #18): existing
    vectors — and any on-disk index built from them — are no longer valid
    for the newly configured model, so both must be dropped rather than
    silently kept around until a stale-size check happens to catch it.
    `db` is typically empty of vectors right after the caller clears
    `memories_vec`, so this mostly resets state to "correctly-dimensioned
    and empty" rather than eagerly repopulating; `rebuild_index` doesn't
    hurt either way since it just reads whatever's in `memories_vec`.
    """
    global _index, _index_failed
    with _lock:
        _index = None
        _index_failed = False
    with contextlib.suppress(OSError):
        _index_path().unlink(missing_ok=True)
    rebuild_index(db)


def reset_for_tests() -> None:
    """Clear all cached state. Test-only — production code never needs this."""
    global _index, _index_failed
    with _lock:
        _index = None
        _index_failed = False


__all__ = [
    "get_index",
    "add_vector",
    "remove_vector",
    "invalidate_index",
    "search",
    "save_index",
    "rebuild_index",
    "status",
    "reset_for_tests",
]
