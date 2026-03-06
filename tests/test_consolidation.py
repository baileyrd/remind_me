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


# ---------------------------------------------------------------------------
# Integration tests — consolidation tool handler (Phase 14 Plan 02)
# ---------------------------------------------------------------------------


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
        memory_id = _make_id(content)
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

    # Store embedding in memories_vec
    emb_bytes = mock_embedder.embed_one(content)  # type: ignore[union-attr]
    rowid = db.execute(
        "SELECT rowid FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()[0]
    db.execute(
        "INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)",
        (rowid, emb_bytes),
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

        result_str = await remind_me_consolidate(ConsolidateInput(dry_run=False, similarity_threshold=0.5))
        result = json.loads(result_str)

        assert result["clusters_merged"] >= 1
        assert result["dry_run"] is False

        # Higher vitality memory should be canonical
        assert id_high in result["canonical_ids"]

        # Canonical access_count = 5 + 3 = 8
        canonical_row = db_conn_with_vec.execute(
            "SELECT access_count, content FROM memories WHERE id = ?", (id_high,)
        ).fetchone()
        assert canonical_row["access_count"] == 8

        # Lower vitality memory should have superseded_by set
        member_row = db_conn_with_vec.execute(
            "SELECT superseded_by FROM memories WHERE id = ?", (id_low,)
        ).fetchone()
        assert member_row["superseded_by"] == id_high

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
