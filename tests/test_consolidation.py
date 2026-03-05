"""
Tests for remind_me_mcp.consolidation — pure-function clustering, canonical
selection, and merge logic for vault hygiene.

Covers requirements HYGN-01 through HYGN-05.
"""

from __future__ import annotations

import numpy as np
import pytest
from pydantic import ValidationError

from remind_me_mcp.consolidation import find_clusters, merge_cluster, pick_canonical
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

        # Small perturbation
        rng = np.random.default_rng(99)
        noise1 = rng.standard_normal(dim).astype(np.float32) * 0.05
        noise2 = rng.standard_normal(dim).astype(np.float32) * 0.05

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
