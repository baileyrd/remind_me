"""
Tests for remind_me_mcp.consolidation — pure-function clustering, canonical
selection, merge logic, and integration tests for the consolidation MCP tool.

Covers requirements HYGN-01 through HYGN-05.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import numpy as np
import pytest
from pydantic import ValidationError

if TYPE_CHECKING:
    import sqlite3

from remind_me_mcp.consolidation import find_clusters, merge_cluster, pick_canonical
from remind_me_mcp.db import _make_id, _now_iso
from remind_me_mcp.models import ConsolidateInput

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_embedding(vector: np.ndarray) -> bytes:
    """Convert a numpy float32 vector to raw bytes (sqlite-vec storage format)."""
    vec = vector.astype(np.float32)
    norm = np.linalg.norm(vec)
    if norm > 1e-9:
        vec = vec / norm
    return vec.tobytes()


def _unit_vector(dim: int = 384, index: int = 0) -> np.ndarray:
    """Return a unit vector with 1.0 at the given index, 0 elsewhere."""
    vec = np.zeros(dim, dtype=np.float32)
    vec[index] = 1.0
    return vec


# ---------------------------------------------------------------------------
# find_clusters tests (HYGN-01)
# ---------------------------------------------------------------------------


class TestFindClusters:
    """Tests for the find_clusters function."""

    def test_cluster_above_threshold(self) -> None:
        """Two memories with identical embeddings (similarity 1.0) cluster at threshold 0.85."""
        vec = _unit_vector(384, index=0)
        emb_bytes = _make_embedding(vec)

        memories = [
            {"id": "mem-1", "content": "A", "vitality": 0.9, "access_count": 5, "accessed_at": "2026-01-01T00:00:00Z", "tags": []},
            {"id": "mem-2", "content": "B", "vitality": 0.8, "access_count": 3, "accessed_at": "2026-01-02T00:00:00Z", "tags": []},
        ]
        embeddings = {"mem-1": emb_bytes, "mem-2": emb_bytes}

        clusters = find_clusters(memories, embeddings, similarity_threshold=0.85)

        assert len(clusters) == 1
        assert len(clusters[0]) == 2
        # Sorted by vitality DESC
        assert clusters[0][0]["id"] == "mem-1"
        assert clusters[0][1]["id"] == "mem-2"

    def test_no_cluster_below_threshold(self) -> None:
        """Two memories with orthogonal embeddings (similarity 0.0) are NOT clustered."""
        vec_a = _unit_vector(384, index=0)
        vec_b = _unit_vector(384, index=1)

        memories = [
            {"id": "mem-1", "content": "A", "vitality": 0.9, "access_count": 5, "accessed_at": "2026-01-01T00:00:00Z", "tags": []},
            {"id": "mem-2", "content": "B", "vitality": 0.8, "access_count": 3, "accessed_at": "2026-01-02T00:00:00Z", "tags": []},
        ]
        embeddings = {
            "mem-1": _make_embedding(vec_a),
            "mem-2": _make_embedding(vec_b),
        }

        clusters = find_clusters(memories, embeddings, similarity_threshold=0.85)

        assert len(clusters) == 0

    def test_cluster_with_768_dim_embeddings(self) -> None:
        """Non-384-dim backends (e.g. nomic-embed-text, 768) cluster correctly (DI-06)."""
        vec = _unit_vector(768, index=0)
        emb_bytes = _make_embedding(vec)

        memories = [
            {"id": "mem-1", "content": "A", "vitality": 0.9, "access_count": 5, "accessed_at": "2026-01-01T00:00:00Z", "tags": []},
            {"id": "mem-2", "content": "B", "vitality": 0.8, "access_count": 3, "accessed_at": "2026-01-02T00:00:00Z", "tags": []},
        ]
        embeddings = {"mem-1": emb_bytes, "mem-2": emb_bytes}

        clusters = find_clusters(memories, embeddings, similarity_threshold=0.85)

        assert len(clusters) == 1
        assert len(clusters[0]) == 2

    def test_bytes_to_vector_infers_dim_from_blob_length(self) -> None:
        """_bytes_to_vector infers the dimension from the byte length (DI-06)."""
        from remind_me_mcp.consolidation import _bytes_to_vector

        for dim in (384, 768, 1024):
            vec = _bytes_to_vector(_unit_vector(dim, index=0).tobytes())
            assert vec.shape == (dim,)
            assert vec[0] == 1.0

    def test_bytes_to_vector_rejects_partial_float(self) -> None:
        """A blob whose length isn't a multiple of 4 raises ValueError."""
        from remind_me_mcp.consolidation import _bytes_to_vector

        with pytest.raises(ValueError):
            _bytes_to_vector(b"\x00\x00\x00")

    def test_single_memory_no_cluster(self) -> None:
        """A single memory returns no clusters (clusters must have 2+ members)."""
        vec = _unit_vector(384, index=0)
        memories = [
            {"id": "mem-1", "content": "A", "vitality": 0.9, "access_count": 5, "accessed_at": "2026-01-01T00:00:00Z", "tags": []},
        ]
        embeddings = {"mem-1": _make_embedding(vec)}

        clusters = find_clusters(memories, embeddings, similarity_threshold=0.85)

        assert len(clusters) == 0

    def test_transitive_clustering(self) -> None:
        """If A~B and B~C both above threshold, {A,B,C} form one cluster via Union-Find."""
        # Create vectors where A~B and B~C are similar but A~C may not be directly
        dim = 384
        base = np.random.default_rng(42).standard_normal(dim).astype(np.float32)
        base /= np.linalg.norm(base)

        # Very small perturbation to stay above 0.85 similarity
        rng = np.random.default_rng(99)
        noise1 = rng.standard_normal(dim).astype(np.float32) * 0.01
        noise2 = rng.standard_normal(dim).astype(np.float32) * 0.01

        vec_a = base.copy()
        vec_b = base + noise1
        vec_b /= np.linalg.norm(vec_b)
        vec_c = vec_b + noise2
        vec_c /= np.linalg.norm(vec_c)

        memories = [
            {"id": "mem-a", "content": "A", "vitality": 0.9, "access_count": 5, "accessed_at": "2026-01-01T00:00:00Z", "tags": []},
            {"id": "mem-b", "content": "B", "vitality": 0.7, "access_count": 3, "accessed_at": "2026-01-02T00:00:00Z", "tags": []},
            {"id": "mem-c", "content": "C", "vitality": 0.5, "access_count": 1, "accessed_at": "2026-01-03T00:00:00Z", "tags": []},
        ]
        embeddings = {
            "mem-a": _make_embedding(vec_a),
            "mem-b": _make_embedding(vec_b),
            "mem-c": _make_embedding(vec_c),
        }

        clusters = find_clusters(memories, embeddings, similarity_threshold=0.85)

        assert len(clusters) == 1
        assert len(clusters[0]) == 3

    def test_max_candidates_caps_pool_size(self, caplog: pytest.LogCaptureFixture) -> None:
        """Issue #55: a pool larger than max_candidates is truncated (logged,
        not silent) rather than pairwise-comparing everything."""
        vec = _unit_vector(384, index=0)
        emb_bytes = _make_embedding(vec)
        memories = [
            {"id": f"mem-{i}", "content": "x", "vitality": 1.0, "access_count": 0,
             "accessed_at": "2026-01-01T00:00:00Z", "tags": []}
            for i in range(10)
        ]
        embeddings = {m["id"]: emb_bytes for m in memories}

        with caplog.at_level("WARNING", logger="remind_me_mcp.consolidation"):
            clusters = find_clusters(memories, embeddings, similarity_threshold=0.85, max_candidates=5)

        # Only the first 5 candidates are considered -> one cluster of 5, not 10.
        assert len(clusters) == 1
        assert len(clusters[0]) == 5
        assert any("max_candidates" in r.message for r in caplog.records)

    def test_max_candidates_no_truncation_when_under_cap(self) -> None:
        """A pool at or under max_candidates is unaffected."""
        vec = _unit_vector(384, index=0)
        emb_bytes = _make_embedding(vec)
        memories = [
            {"id": f"mem-{i}", "content": "x", "vitality": 1.0, "access_count": 0,
             "accessed_at": "2026-01-01T00:00:00Z", "tags": []}
            for i in range(5)
        ]
        embeddings = {m["id"]: emb_bytes for m in memories}

        clusters = find_clusters(memories, embeddings, similarity_threshold=0.85, max_candidates=5)

        assert len(clusters) == 1
        assert len(clusters[0]) == 5

    def test_vectorized_threshold_matches_pairwise_semantics_at_scale(self) -> None:
        """The vectorized upper-triangle comparison must produce identical
        clustering to naive pairwise comparison for a larger, mixed pool
        (a handful of near-duplicate groups plus unrelated singletons)."""
        dim = 384
        rng = np.random.default_rng(7)

        def _group(base_index: int, count: int, noise_scale: float = 0.01) -> list[np.ndarray]:
            base = _unit_vector(dim, index=base_index)
            vecs = [base]
            for _ in range(count - 1):
                noisy = base + rng.standard_normal(dim).astype(np.float32) * noise_scale
                noisy /= np.linalg.norm(noisy)
                vecs.append(noisy)
            return vecs

        groups = [_group(0, 4), _group(50, 3), _group(100, 2)]
        singletons = [_unit_vector(dim, index=i) for i in (150, 151, 152)]

        memories: list[dict] = []
        embeddings: dict[str, bytes] = {}
        mid = 0
        for group in groups + [[v] for v in singletons]:
            for vec in group:
                m_id = f"mem-{mid}"
                memories.append({
                    "id": m_id, "content": "x", "vitality": 1.0 - mid * 0.001,
                    "access_count": 0, "accessed_at": "2026-01-01T00:00:00Z", "tags": [],
                })
                embeddings[m_id] = _make_embedding(vec)
                mid += 1

        clusters = find_clusters(memories, embeddings, similarity_threshold=0.85)

        cluster_sizes = sorted(len(c) for c in clusters)
        assert cluster_sizes == [2, 3, 4]  # the three groups; singletons excluded


# ---------------------------------------------------------------------------
# pick_canonical tests (HYGN-02)
# ---------------------------------------------------------------------------


class TestPickCanonical:
    """Tests for the pick_canonical function."""

    def test_pick_canonical_highest_vitality(self) -> None:
        """pick_canonical selects the memory with the highest vitality score."""
        cluster = [
            {"id": "mem-1", "vitality": 0.5, "access_count": 1, "accessed_at": "2026-01-01T00:00:00Z"},
            {"id": "mem-2", "vitality": 0.9, "access_count": 2, "accessed_at": "2026-01-01T00:00:00Z"},
            {"id": "mem-3", "vitality": 0.3, "access_count": 1, "accessed_at": "2026-01-01T00:00:00Z"},
        ]

        canonical = pick_canonical(cluster)

        assert canonical["id"] == "mem-2"

    def test_pick_canonical_tiebreak_accessed_at(self) -> None:
        """When vitality ties, pick_canonical selects the most recently accessed memory."""
        cluster = [
            {"id": "mem-1", "vitality": 0.8, "access_count": 2, "accessed_at": "2026-01-01T00:00:00Z"},
            {"id": "mem-2", "vitality": 0.8, "access_count": 1, "accessed_at": "2026-01-05T00:00:00Z"},
            {"id": "mem-3", "vitality": 0.8, "access_count": 3, "accessed_at": "2026-01-03T00:00:00Z"},
        ]

        canonical = pick_canonical(cluster)

        assert canonical["id"] == "mem-2"


# ---------------------------------------------------------------------------
# merge_cluster tests (HYGN-03, HYGN-04)
# ---------------------------------------------------------------------------


class TestMergeCluster:
    """Tests for the merge_cluster function."""

    def test_merge_cluster_content(self) -> None:
        """merge_cluster deduplicates identical lines and appends unique content from members."""
        canonical = {"id": "c1", "content": "line one\nline two", "access_count": 3, "tags": []}
        members = [
            {"id": "m1", "content": "line two\nline three", "access_count": 1, "tags": []},
            {"id": "m2", "content": "line one\nline four", "access_count": 2, "tags": []},
        ]

        result = merge_cluster(canonical, members)

        lines = result["merged_content"].split("\n")
        assert "line one" in lines
        assert "line two" in lines
        assert "line three" in lines
        assert "line four" in lines
        # No duplicates
        assert lines.count("line one") == 1
        assert lines.count("line two") == 1

    def test_merge_cluster_access_count(self) -> None:
        """merge_cluster returns total_access_count as sum of canonical + all members."""
        canonical = {"id": "c1", "content": "A", "access_count": 10, "tags": []}
        members = [
            {"id": "m1", "content": "B", "access_count": 5, "tags": []},
            {"id": "m2", "content": "C", "access_count": 3, "tags": []},
        ]

        result = merge_cluster(canonical, members)

        assert result["total_access_count"] == 18

    def test_merge_cluster_superseded_ids(self) -> None:
        """merge_cluster returns list of all non-canonical member IDs."""
        canonical = {"id": "c1", "content": "A", "access_count": 1, "tags": []}
        members = [
            {"id": "m1", "content": "B", "access_count": 1, "tags": []},
            {"id": "m2", "content": "C", "access_count": 1, "tags": []},
        ]

        result = merge_cluster(canonical, members)

        assert result["superseded_ids"] == ["m1", "m2"]

    def test_merge_cluster_tags(self) -> None:
        """merge_cluster merges tags from all members using dict.fromkeys deduplication."""
        canonical = {"id": "c1", "content": "A", "access_count": 1, "tags": ["python", "work"]}
        members = [
            {"id": "m1", "content": "B", "access_count": 1, "tags": ["work", "ai"]},
            {"id": "m2", "content": "C", "access_count": 1, "tags": ["python", "ml"]},
        ]

        result = merge_cluster(canonical, members)

        # Order-preserving dedup: canonical tags first, then member tags in order
        assert result["merged_tags"] == ["python", "work", "ai", "ml"]

    def test_merge_cluster_with_summary_replaces_content(self) -> None:
        """Issue #55: a supplied summary becomes merged_content verbatim,
        replacing the raw line-union entirely."""
        canonical = {"id": "c1", "content": "line one\nline two", "access_count": 3, "tags": []}
        members = [
            {"id": "m1", "content": "line three\nline four", "access_count": 1, "tags": []},
        ]

        result = merge_cluster(canonical, members, summary="A concise consolidated summary.")

        assert result["merged_content"] == "A concise consolidated summary."

    def test_merge_cluster_with_summary_still_sums_access_and_tags(self) -> None:
        """Summary only replaces content -- access_count/tags/superseded_ids unaffected."""
        canonical = {"id": "c1", "content": "A", "access_count": 10, "tags": ["python"]}
        members = [
            {"id": "m1", "content": "B", "access_count": 5, "tags": ["work"]},
        ]

        result = merge_cluster(canonical, members, summary="Summary text")

        assert result["total_access_count"] == 15
        assert result["merged_tags"] == ["python", "work"]
        assert result["superseded_ids"] == ["m1"]

    def test_merge_cluster_without_summary_is_unchanged(self) -> None:
        """Regression guard: omitting summary preserves the exact pre-#55 union behavior."""
        canonical = {"id": "c1", "content": "line one", "access_count": 1, "tags": []}
        members = [{"id": "m1", "content": "line two", "access_count": 1, "tags": []}]

        result = merge_cluster(canonical, members)

        assert set(result["merged_content"].split("\n")) == {"line one", "line two"}


# ---------------------------------------------------------------------------
# ConsolidateInput model tests (HYGN-05)
# ---------------------------------------------------------------------------


class TestConsolidateInput:
    """Tests for the ConsolidateInput pydantic model."""

    def test_dry_run_default_true(self) -> None:
        """ConsolidateInput defaults dry_run=True."""
        inp = ConsolidateInput()

        assert inp.dry_run is True

    def test_similarity_threshold_bounds(self) -> None:
        """ConsolidateInput rejects threshold < 0.5 or > 1.0."""
        with pytest.raises(ValidationError):
            ConsolidateInput(similarity_threshold=0.3)

        with pytest.raises(ValidationError):
            ConsolidateInput(similarity_threshold=1.1)

        # Edge cases should be accepted
        valid_low = ConsolidateInput(similarity_threshold=0.5)
        assert valid_low.similarity_threshold == 0.5

        valid_high = ConsolidateInput(similarity_threshold=1.0)
        assert valid_high.similarity_threshold == 1.0

    def test_consolidate_input_limit(self) -> None:
        """ConsolidateInput accepts limit between 10 and 5000, default 500."""
        inp = ConsolidateInput()
        assert inp.limit == 500

        with pytest.raises(ValidationError):
            ConsolidateInput(limit=5)

        with pytest.raises(ValidationError):
            ConsolidateInput(limit=6000)

        valid = ConsolidateInput(limit=100)
        assert valid.limit == 100

    def test_summaries_defaults_to_none(self) -> None:
        """Issue #55: summaries is optional -- dry_run callers don't need it."""
        inp = ConsolidateInput()
        assert inp.summaries is None

    def test_summaries_accepts_mapping(self) -> None:
        inp = ConsolidateInput(dry_run=False, summaries={"canon-1": "a summary"})
        assert inp.summaries == {"canon-1": "a summary"}


# ---------------------------------------------------------------------------
# Integration tests — consolidation tool handler (Phase 14 Plan 02)
# ---------------------------------------------------------------------------

# Monotonic salt so _insert_memory_with_vec generates unique ids even for
# identical content (see note in the helper).
_vec_helper_counter = 0


def _insert_memory_with_vec(
    db: sqlite3.Connection,
    mock_embedder: object,
    *,
    memory_id: str | None = None,
    content: str = "Test memory",
    category: str = "general",
    tags: list[str] | None = None,
    vitality: float = 1.0,
    access_count: int = 0,
    decay_rate: float = 0.10,
    base_weight: float = 1.0,
    status: str = "active",
    superseded_by: str | None = None,
) -> str:
    """Insert a memory row AND its embedding into the test database.

    Uses mock_embedder.embed_one to generate deterministic embeddings and stores
    them in memories_vec. Returns the memory ID.

    Args:
        db: Test database connection with sqlite-vec loaded.
        mock_embedder: FakeEmbedder instance for deterministic embeddings.
        memory_id: Optional explicit ID; generated from content if not provided.
        content: Memory content text.
        category: Memory category.
        tags: Tag list (default empty).
        vitality: Vitality score.
        access_count: Access count.
        decay_rate: Decay rate.
        base_weight: Base weight.
        status: Memory status ('active' or 'dormant').
        superseded_by: ID of canonical memory if this memory is superseded.

    Returns:
        The memory ID string.
    """
    if tags is None:
        tags = []
    if memory_id is None:
        # _make_id is timestamp-salted and collides for same-content calls within
        # one clock tick (coarse on Windows); add a counter so duplicates differ.
        global _vec_helper_counter
        _vec_helper_counter += 1
        memory_id = _make_id(f"{content}|{_vec_helper_counter}")
    now = _now_iso()

    db.execute(
        """INSERT INTO memories (
            id, content, category, tags, source, metadata,
            created_at, updated_at, accessed_at,
            vitality, access_count, decay_rate, base_weight,
            status, memory_type, superseded_by
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            memory_id, content, category, json.dumps(tags),
            "manual", "{}",
            now, now, now,
            vitality, access_count, decay_rate, base_weight,
            status, "unclassified", superseded_by,
        ),
    )

    # Store embedding as a single chunk vector, mapped via vec_chunks (the
    # post-v8 layout the consolidate query reads). chunk_ix=0 mirrors how the
    # migration backfills legacy 1:1 vectors.
    emb_bytes = mock_embedder.embed_one(content)  # type: ignore[union-attr]
    rowid = db.execute(
        "SELECT rowid FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()[0]
    cur = db.execute(
        "INSERT INTO memories_vec(embedding) VALUES (?)", (emb_bytes,)
    )
    db.execute(
        "INSERT INTO vec_chunks(vec_rowid, memory_rowid, chunk_ix) VALUES (?, ?, ?)",
        (cur.lastrowid, rowid, 0),
    )

    db.commit()
    return memory_id


class TestConsolidateToolIntegration:
    """Integration tests for the remind_me_consolidate tool handler.

    These tests use db_conn_with_vec (real sqlite-vec) and mock_embedder
    to test the full tool handler path including DB reads and writes.
    """

    @pytest.mark.asyncio
    async def test_consolidate_tool_dry_run(
        self, db_conn_with_vec: sqlite3.Connection, mock_embedder: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dry run reports 1 cluster for 2 identical memories without modifying DB."""
        import remind_me_mcp.tools as _tools_mod

        monkeypatch.setattr(_tools_mod, "_embed_and_store", lambda mid, c: True)

        # 2 identical content (same embedding), 1 different
        id_a = _insert_memory_with_vec(db_conn_with_vec, mock_embedder, content="duplicate content here", vitality=0.9, access_count=5)
        id_b = _insert_memory_with_vec(db_conn_with_vec, mock_embedder, content="duplicate content here", vitality=0.7, access_count=3)
        _insert_memory_with_vec(db_conn_with_vec, mock_embedder, content="completely different text about something else entirely")

        from remind_me_mcp.tools import remind_me_consolidate

        result_str = await remind_me_consolidate(ConsolidateInput(dry_run=True, similarity_threshold=0.5))
        result = json.loads(result_str)

        assert result["clusters_found"] >= 1
        assert result["dry_run"] is True

        # Verify DB was NOT modified: no superseded_by set
        row_a = db_conn_with_vec.execute("SELECT superseded_by FROM memories WHERE id = ?", (id_a,)).fetchone()
        row_b = db_conn_with_vec.execute("SELECT superseded_by FROM memories WHERE id = ?", (id_b,)).fetchone()
        assert row_a["superseded_by"] is None
        assert row_b["superseded_by"] is None

    @pytest.mark.asyncio
    async def test_consolidate_tool_auto_merge(
        self, db_conn_with_vec: sqlite3.Connection, mock_embedder: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Auto-merge updates canonical, sets superseded_by, and sums access_count."""
        import remind_me_mcp.tools as _tools_mod

        monkeypatch.setattr(_tools_mod, "_embed_and_store", lambda mid, c: True)

        id_high = _insert_memory_with_vec(
            db_conn_with_vec, mock_embedder,
            content="important fact about Python",
            vitality=0.9, access_count=5, decay_rate=0.05, base_weight=1.0,
        )
        id_low = _insert_memory_with_vec(
            db_conn_with_vec, mock_embedder,
            content="important fact about Python",
            vitality=0.3, access_count=3,
        )

        from remind_me_mcp.tools import remind_me_consolidate

        result_str = await remind_me_consolidate(
            ConsolidateInput(
                dry_run=False,
                similarity_threshold=0.5,
                summaries={id_high: "consolidated: important fact about Python"},
            )
        )
        result = json.loads(result_str)

        assert result["clusters_merged"] >= 1
        assert result["dry_run"] is False
        assert result["skipped_no_summary"] == []

        # Higher vitality memory should be canonical
        assert id_high in result["canonical_ids"]

        # Canonical access_count = 5 + 3 = 8, content replaced by the summary
        canonical_row = db_conn_with_vec.execute(
            "SELECT access_count, content FROM memories WHERE id = ?", (id_high,)
        ).fetchone()
        assert canonical_row["access_count"] == 8
        assert canonical_row["content"] == "consolidated: important fact about Python"

        # Lower vitality memory should have superseded_by set
        member_row = db_conn_with_vec.execute(
            "SELECT superseded_by FROM memories WHERE id = ?", (id_low,)
        ).fetchone()
        assert member_row["superseded_by"] == id_high

    @pytest.mark.asyncio
    async def test_consolidate_tool_auto_merge_skips_cluster_without_summary(
        self, db_conn_with_vec: sqlite3.Connection, mock_embedder: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A found cluster with no matching summaries entry is skipped, not
        merged with a raw concatenation (issue #55 — the whole point)."""
        import remind_me_mcp.tools as _tools_mod

        monkeypatch.setattr(_tools_mod, "_embed_and_store", lambda mid, c: True)

        id_high = _insert_memory_with_vec(
            db_conn_with_vec, mock_embedder,
            content="important fact about Python",
            vitality=0.9, access_count=5,
        )
        id_low = _insert_memory_with_vec(
            db_conn_with_vec, mock_embedder,
            content="important fact about Python",
            vitality=0.3, access_count=3,
        )

        from remind_me_mcp.tools import remind_me_consolidate

        result_str = await remind_me_consolidate(
            ConsolidateInput(dry_run=False, similarity_threshold=0.5)
        )
        result = json.loads(result_str)

        assert result["clusters_found"] >= 1
        assert result["clusters_merged"] == 0
        assert id_high in result["skipped_no_summary"]

        # Neither memory should have been touched.
        row_low = db_conn_with_vec.execute(
            "SELECT superseded_by FROM memories WHERE id = ?", (id_low,)
        ).fetchone()
        assert row_low["superseded_by"] is None
        row_high = db_conn_with_vec.execute(
            "SELECT content, access_count FROM memories WHERE id = ?", (id_high,)
        ).fetchone()
        assert row_high["content"] == "important fact about Python"
        assert row_high["access_count"] == 5

    @pytest.mark.asyncio
    async def test_consolidate_tool_category_filter(
        self, db_conn_with_vec: sqlite3.Connection, mock_embedder: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Category filter limits consolidation to the specified category only."""
        import remind_me_mcp.tools as _tools_mod

        monkeypatch.setattr(_tools_mod, "_embed_and_store", lambda mid, c: True)

        # 2 identical in "facts" category
        _insert_memory_with_vec(db_conn_with_vec, mock_embedder, content="shared knowledge base", category="facts")
        _insert_memory_with_vec(db_conn_with_vec, mock_embedder, content="shared knowledge base", category="facts")

        # 2 identical in "notes" category
        _insert_memory_with_vec(db_conn_with_vec, mock_embedder, content="project meeting notes", category="notes")
        _insert_memory_with_vec(db_conn_with_vec, mock_embedder, content="project meeting notes", category="notes")

        from remind_me_mcp.tools import remind_me_consolidate

        result_str = await remind_me_consolidate(ConsolidateInput(dry_run=True, similarity_threshold=0.5, category="facts"))
        result = json.loads(result_str)

        # Should find cluster(s) only from "facts"
        assert result["clusters_found"] >= 1
        for cluster in result["clusters"]:
            # All IDs in the cluster should be from "facts" category
            all_ids = [cluster["canonical"]["id"]] + [m["id"] for m in cluster["members"]]
            for mid in all_ids:
                row = db_conn_with_vec.execute("SELECT category FROM memories WHERE id = ?", (mid,)).fetchone()
                assert row["category"] == "facts"

    @pytest.mark.asyncio
    async def test_consolidate_tool_skips_superseded(
        self, db_conn_with_vec: sqlite3.Connection, mock_embedder: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Memories with superseded_by set are excluded from consolidation."""
        import remind_me_mcp.tools as _tools_mod

        monkeypatch.setattr(_tools_mod, "_embed_and_store", lambda mid, c: True)

        # 2 identical memories, but one already superseded
        _insert_memory_with_vec(db_conn_with_vec, mock_embedder, content="already merged content")
        _insert_memory_with_vec(
            db_conn_with_vec, mock_embedder,
            content="already merged content",
            superseded_by="some-other-id",
        )

        from remind_me_mcp.tools import remind_me_consolidate

        result_str = await remind_me_consolidate(ConsolidateInput(dry_run=True, similarity_threshold=0.5))
        result = json.loads(result_str)

        # Only 1 eligible memory => no clusters possible
        assert result["clusters_found"] == 0

    @pytest.mark.asyncio
    async def test_consolidate_tool_no_clusters(
        self, db_conn_with_vec: sqlite3.Connection, mock_embedder: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dissimilar memories produce no clusters at high threshold."""
        import remind_me_mcp.tools as _tools_mod

        monkeypatch.setattr(_tools_mod, "_embed_and_store", lambda mid, c: True)

        # Very different content => different embeddings
        _insert_memory_with_vec(db_conn_with_vec, mock_embedder, content="alpha bravo charlie delta")
        _insert_memory_with_vec(db_conn_with_vec, mock_embedder, content="zulu yankee xray whiskey")

        from remind_me_mcp.tools import remind_me_consolidate

        result_str = await remind_me_consolidate(ConsolidateInput(dry_run=True, similarity_threshold=0.99))
        result = json.loads(result_str)

        assert result["clusters_found"] == 0
