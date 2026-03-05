"""
remind_me_mcp.consolidation -- Pure-function clustering and merge logic for vault hygiene.

This module implements the core consolidation algorithm as testable pure functions
with no database interaction. It finds clusters of similar memories using cosine
similarity, selects canonical representatives, and merges duplicate content.

Key concepts:
  - **Clustering**: Groups memories whose pairwise cosine similarity exceeds a threshold,
    using Union-Find for transitive closure.
  - **Canonical selection**: The highest-vitality memory in each cluster becomes the
    canonical representative (tiebreak by most recent accessed_at).
  - **Merging**: Combines content from cluster members into the canonical memory,
    deduplicating lines and summing access counts.
"""

from __future__ import annotations

import struct
from typing import Any

import numpy as np


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _bytes_to_vector(raw: bytes, dim: int = 384) -> np.ndarray:
    """Convert raw float32 bytes to a numpy vector.

    Args:
        raw: Raw bytes containing float32 values.
        dim: Expected dimensionality of the vector.

    Returns:
        A 1-D float32 numpy array of length ``dim``.

    Raises:
        ValueError: If byte length does not match expected dimension.
    """
    expected = dim * 4  # float32 = 4 bytes
    if len(raw) != expected:
        raise ValueError(f"Expected {expected} bytes for dim={dim}, got {len(raw)}")
    return np.array(struct.unpack(f"{dim}f", raw), dtype=np.float32)


class _UnionFind:
    """Disjoint-set (Union-Find) data structure with path compression and union by rank.

    Used to transitively cluster indices where pairwise similarity exceeds
    the threshold.
    """

    def __init__(self, n: int) -> None:
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        """Find the root of element x with path compression."""
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]

    def union(self, x: int, y: int) -> None:
        """Unite the sets containing x and y using union by rank."""
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return
        if self.rank[rx] < self.rank[ry]:
            rx, ry = ry, rx
        self.parent[ry] = rx
        if self.rank[rx] == self.rank[ry]:
            self.rank[rx] += 1


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def find_clusters(
    memories: list[dict[str, Any]],
    embeddings: dict[str, bytes],
    similarity_threshold: float = 0.85,
) -> list[list[dict[str, Any]]]:
    """Find clusters of similar memories based on cosine similarity.

    Converts embedding bytes to numpy vectors, builds a cosine similarity
    matrix, and uses Union-Find to group indices where similarity >= threshold.
    Only clusters with 2+ members are returned, each sorted by vitality DESC.

    Args:
        memories: List of memory dicts, each with at least an ``id`` and ``vitality`` key.
        embeddings: Mapping of memory ID to raw float32 embedding bytes.
        similarity_threshold: Minimum cosine similarity to cluster memories together.

    Returns:
        A list of clusters, where each cluster is a list of memory dicts
        sorted by vitality descending. Only clusters with 2+ members are included.
    """
    n = len(memories)
    if n < 2:
        return []

    # Build matrix of L2-normalized vectors
    vectors = np.stack(
        [_bytes_to_vector(embeddings[m["id"]]) for m in memories],
        axis=0,
    )

    # Cosine similarity matrix (vectors are already L2-normalized)
    sim_matrix = vectors @ vectors.T

    # Union-Find clustering
    uf = _UnionFind(n)
    for i in range(n):
        for j in range(i + 1, n):
            if sim_matrix[i, j] >= similarity_threshold:
                uf.union(i, j)

    # Group by root
    groups: dict[int, list[int]] = {}
    for i in range(n):
        root = uf.find(i)
        groups.setdefault(root, []).append(i)

    # Filter to 2+ members, sort each by vitality DESC
    clusters: list[list[dict[str, Any]]] = []
    for indices in groups.values():
        if len(indices) >= 2:
            cluster = [memories[i] for i in indices]
            cluster.sort(key=lambda m: m.get("vitality", 0.0), reverse=True)
            clusters.append(cluster)

    return clusters


def pick_canonical(cluster: list[dict[str, Any]]) -> dict[str, Any]:
    """Select the canonical (representative) memory from a cluster.

    Returns the memory with the highest vitality score. When vitality ties,
    selects the most recently accessed memory (by ``accessed_at``).

    Args:
        cluster: A list of memory dicts, each with ``vitality`` and ``accessed_at`` keys.

    Returns:
        The canonical memory dict.

    Raises:
        ValueError: If the cluster is empty.
    """
    if not cluster:
        raise ValueError("Cannot pick canonical from an empty cluster")

    return max(
        cluster,
        key=lambda m: (m.get("vitality", 0.0), m.get("accessed_at", "")),
    )


def merge_cluster(
    canonical: dict[str, Any],
    members: list[dict[str, Any]],
) -> dict[str, Any]:
    """Merge cluster members into the canonical memory.

    Produces merged content (deduplicated lines), summed access counts,
    list of superseded IDs, and merged tags.

    Args:
        canonical: The canonical memory dict (highest vitality in the cluster).
        members: List of non-canonical member dicts to merge into canonical.

    Returns:
        A dict with keys:
          - ``merged_content``: Deduplicated content lines, canonical first.
          - ``total_access_count``: Sum of all access counts.
          - ``superseded_ids``: List of member IDs that will be superseded.
          - ``merged_tags``: Order-preserving deduplicated tags from all memories.
    """
    # Merge content: canonical lines first, then unique lines from members
    seen_lines: dict[str, None] = {}
    for line in canonical.get("content", "").split("\n"):
        seen_lines[line] = None

    for member in members:
        for line in member.get("content", "").split("\n"):
            if line not in seen_lines:
                seen_lines[line] = None

    merged_content = "\n".join(seen_lines)

    # Sum access counts
    total_access_count = canonical.get("access_count", 0) + sum(
        m.get("access_count", 0) for m in members
    )

    # Collect superseded IDs
    superseded_ids = [m["id"] for m in members]

    # Merge tags with order-preserving deduplication
    all_tags: list[str] = list(canonical.get("tags", []))
    for member in members:
        all_tags.extend(member.get("tags", []))
    merged_tags = list(dict.fromkeys(all_tags))

    return {
        "merged_content": merged_content,
        "total_access_count": total_access_count,
        "superseded_ids": superseded_ids,
        "merged_tags": merged_tags,
    }


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "find_clusters",
    "merge_cluster",
    "pick_canonical",
]
