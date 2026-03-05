# Phase 14: Vault Hygiene - Research

**Researched:** 2026-03-05
**Domain:** Semantic similarity clustering, memory consolidation, sqlite-vec vector operations
**Confidence:** HIGH

## Summary

Phase 14 implements a `remind_me_consolidate` tool that clusters semantically similar memories and optionally merges them. The project already has all the infrastructure needed: sqlite-vec for vector similarity search, vitality scores for picking canonical records, and `superseded_by` columns for tracking replaced memories. No new dependencies are required.

The core algorithm is: (1) fetch active, non-superseded memory embeddings, (2) compute pairwise cosine similarity, (3) cluster memories above a configurable threshold, (4) in dry_run mode report clusters, (5) in auto-merge mode pick the highest-vitality member as canonical, merge content, sum access_count, and set superseded_by on non-canonical members.

**Primary recommendation:** Build a single `remind_me_consolidate` tool with `dry_run` (default True), `similarity_threshold` (default 0.85), and optional `category`/`tags` scope filters. Keep the clustering logic in a new `consolidation.py` pure-function module (matching the `retrieval.py` and `vitality.py` pattern). Use the existing `_semantic_search` infrastructure and `memories_vec` table for similarity computation.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| HYGN-01 | remind_me_consolidate clusters semantically similar memories above configurable similarity threshold | sqlite-vec provides cosine distance via vec0 virtual table; threshold is 1 - distance for L2-normalized vectors |
| HYGN-02 | Consolidation supports dry_run mode that reports clusters without modifying data | Tool parameter with default=True; return cluster report as JSON |
| HYGN-03 | Auto-merge mode merges cluster content into highest-vitality canonical record | vitality column already exists; UPDATE canonical content, set superseded_by on others |
| HYGN-04 | Superseded memories get superseded_by set to canonical ID (not deleted) | superseded_by TEXT column already exists from Phase 13 migration v6->v7 |
| HYGN-05 | Canonical record inherits summed access_count from all merged members | Simple SQL: sum access_count from all cluster members, UPDATE canonical |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| sqlite-vec | >=0.1.0 | Vector similarity search | Already in use; provides cosine distance queries |
| sqlite3 | stdlib | Database operations | Already the project's data layer |
| numpy | >=1.24.0 | Vector operations for batch similarity | Already a dependency for embeddings |
| pydantic | >=2.0.0 | Input model validation | Already used for all tool inputs |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| (none needed) | - | - | All dependencies already present |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Batch pairwise similarity in Python | SQL-only distance queries | Python gives more control over clustering; SQL would need N queries |
| Union-Find clustering | Simple greedy clustering | Union-Find handles transitive similarity better for large vaults |

**Installation:**
```bash
# No new dependencies needed - all already installed
```

## Architecture Patterns

### Recommended Project Structure
```
remind_me_mcp/
├── consolidation.py    # NEW: Pure-function clustering + merge logic
├── tools.py            # Add remind_me_consolidate handler
├── models.py           # Add ConsolidateInput model
├── db.py               # No changes needed (embeddings infrastructure exists)
├── vitality.py         # No changes (read vitality for canonical selection)
└── embeddings.py       # No changes (use existing embedder)
```

### Pattern 1: Pure-Function Module (match retrieval.py/vitality.py)
**What:** Keep clustering and merge logic in `consolidation.py` as pure functions that accept data and return results. Tool handler in `tools.py` does DB wiring.
**When to use:** Always -- this is the established project pattern.
**Example:**
```python
# consolidation.py

def find_clusters(
    memories: list[dict],
    embeddings: dict[str, bytes],
    similarity_threshold: float = 0.85,
) -> list[list[dict]]:
    """Cluster memories by semantic similarity using pairwise cosine distance.

    Args:
        memories: List of memory dicts (must include 'id', 'vitality', 'content').
        embeddings: Map of memory_id -> raw embedding bytes (float32).
        similarity_threshold: Minimum cosine similarity to cluster together.

    Returns:
        List of clusters, each a list of memory dicts sorted by vitality DESC.
        Only clusters with 2+ members are returned.
    """

def pick_canonical(cluster: list[dict]) -> dict:
    """Select the highest-vitality memory as canonical record.

    Returns the memory dict with the highest vitality score.
    Ties broken by most recent accessed_at.
    """

def merge_cluster(
    canonical: dict,
    members: list[dict],
) -> dict:
    """Produce merged content and summed access_count for the canonical record.

    Returns dict with: merged_content, total_access_count, superseded_ids.
    """
```

### Pattern 2: Pairwise Cosine Similarity via numpy
**What:** Convert embedding bytes to numpy arrays, compute cosine similarity matrix, threshold to find clusters.
**When to use:** For batch similarity computation -- more efficient than N individual sqlite-vec queries.
**Example:**
```python
import numpy as np

def _bytes_to_vec(raw: bytes, dim: int = 384) -> np.ndarray:
    """Convert raw float32 bytes to numpy vector."""
    return np.frombuffer(raw, dtype=np.float32)

def _cosine_similarity_matrix(vectors: np.ndarray) -> np.ndarray:
    """Compute pairwise cosine similarity. Vectors assumed L2-normalized."""
    # For L2-normalized vectors, cosine similarity = dot product
    return vectors @ vectors.T
```

### Pattern 3: Union-Find Clustering
**What:** Use Union-Find (disjoint set) to group memories transitively -- if A~B and B~C, then {A,B,C} form one cluster even if A and C are below threshold.
**When to use:** When transitive clustering is desired (recommended for vault hygiene).
**Example:**
```python
def _union_find_cluster(
    ids: list[str],
    sim_matrix: np.ndarray,
    threshold: float,
) -> list[list[int]]:
    """Group indices using Union-Find based on similarity threshold."""
    parent = list(range(len(ids)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            if sim_matrix[i, j] >= threshold:
                union(i, j)

    # Collect clusters
    from collections import defaultdict
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(ids)):
        groups[find(i)].append(i)

    return [indices for indices in groups.values() if len(indices) > 1]
```

### Pattern 4: Content Merging Strategy
**What:** Merge content from cluster members into the canonical record. Use a structured format that preserves information.
**When to use:** In auto-merge mode.
**Example:**
```python
def _merge_content(canonical_content: str, member_contents: list[str]) -> str:
    """Merge member content into canonical, deduplicating identical lines."""
    # For simple memories (atomic facts), the canonical content is usually
    # sufficient. Append unique additional context from members.
    all_lines = set()
    result_lines = []

    for line in canonical_content.strip().split('\n'):
        stripped = line.strip()
        if stripped and stripped not in all_lines:
            all_lines.add(stripped)
            result_lines.append(line)

    for content in member_contents:
        for line in content.strip().split('\n'):
            stripped = line.strip()
            if stripped and stripped not in all_lines:
                all_lines.add(stripped)
                result_lines.append(line)

    return '\n'.join(result_lines)
```

### Anti-Patterns to Avoid
- **Deleting superseded memories:** Requirements explicitly say set superseded_by, never delete. Existing search already filters `WHERE superseded_by IS NULL`.
- **Modifying embeddings in-place during merge:** Re-embed the canonical record's merged content after merge, not during.
- **Running consolidation on dormant memories:** Only consolidate active, non-superseded memories to avoid re-processing.
- **Blocking on embedding generation:** Use fire-and-forget `asyncio.create_task(asyncio.to_thread(...))` pattern (established in decompose tool).

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Cosine similarity | Custom distance function | numpy dot product (vectors are L2-normalized) | Numerically stable, fast, single line |
| Embedding lookup | Custom SQL per memory | Batch SELECT from memories_vec JOIN memories | One query instead of N |
| Supersession tracking | Custom status column | Existing `superseded_by` column | Already implemented in Phase 13 |
| Cluster canonical selection | Custom ranking | Sort by `vitality` DESC, break ties by `accessed_at` DESC | vitality already encodes access patterns |

**Key insight:** This phase is mostly wiring together existing infrastructure (embeddings, vitality, superseded_by). The only genuinely new logic is the clustering algorithm and the content merge strategy.

## Common Pitfalls

### Pitfall 1: Quadratic Memory for Large Vaults
**What goes wrong:** Computing a full NxN similarity matrix for thousands of memories exhausts RAM.
**Why it happens:** Pairwise comparison is O(N^2) in both time and space.
**How to avoid:** Scope consolidation with category/tags filters, or process in batches (e.g., 500 memories at a time). The tool should accept `category` and `limit` parameters to constrain scope.
**Warning signs:** Memory errors or extreme slowness on vaults with >1000 active memories.

### Pitfall 2: Threshold Too Low Creates Mega-Clusters
**What goes wrong:** With transitive clustering at threshold 0.7, nearly everything clusters together.
**Why it happens:** Union-Find transitivity chains: A~B at 0.72, B~C at 0.71, C~D at 0.70... creates one massive cluster.
**How to avoid:** Default threshold 0.85 is conservative. Document that lower values create larger, potentially inappropriate clusters. In dry_run, show cluster sizes so users can adjust.
**Warning signs:** Dry run returns a single cluster with dozens of members.

### Pitfall 3: Stale Embeddings After Content Update
**What goes wrong:** Merged canonical record has new content but old embedding.
**Why it happens:** Forgetting to re-embed after content merge.
**How to avoid:** Always call `_embed_and_store(canonical_id, merged_content)` after updating canonical content.
**Warning signs:** Search returns the canonical record for the OLD content's queries but not the merged content.

### Pitfall 4: Race Condition with Concurrent Access
**What goes wrong:** Another tool modifies a memory while consolidation is running.
**Why it happens:** SQLite WAL mode allows concurrent reads but serial writes.
**How to avoid:** Wrap the merge operation in a single transaction. Read -> compute -> write all in one commit. SQLite's busy_timeout (5000ms) handles brief contention.
**Warning signs:** IntegrityError or OperationalError during merge.

### Pitfall 5: Forgetting to Sum access_count on Canonical
**What goes wrong:** Canonical record loses access history from merged members.
**Why it happens:** Only updating superseded_by on members without transferring their access_count.
**How to avoid:** HYGN-05 explicitly requires summing. `canonical.access_count += sum(m.access_count for m in members)`.
**Warning signs:** Canonical record has lower access_count than expected.

## Code Examples

### Fetching All Embeddings for Active Memories
```python
# In consolidation tool handler (tools.py)
db = _get_db()

# Get active, non-superseded memories with their embeddings
rows = db.execute("""
    SELECT m.id, m.content, m.vitality, m.access_count, m.accessed_at,
           m.category, m.tags, m.memory_type, m.decay_rate, m.base_weight,
           mv.embedding
    FROM memories m
    JOIN memories_vec mv ON mv.rowid = m.rowid
    WHERE m.status = 'active'
      AND m.superseded_by IS NULL
    ORDER BY m.id
""").fetchall()
```

### Building the Similarity Matrix
```python
import numpy as np
from remind_me_mcp.config import EMBEDDING_DIM

ids = [row["id"] for row in rows]
vectors = np.array([
    np.frombuffer(row["embedding"], dtype=np.float32)
    for row in rows
])

# Cosine similarity (vectors are already L2-normalized)
sim_matrix = vectors @ vectors.T
```

### Executing the Merge
```python
now = _now_iso()

for cluster in clusters:
    canonical = pick_canonical(cluster)
    members = [m for m in cluster if m["id"] != canonical["id"]]

    merged = merge_cluster(canonical, members)

    # Update canonical: merged content + summed access_count
    db.execute("""
        UPDATE memories
        SET content = ?, access_count = ?, updated_at = ?
        WHERE id = ?
    """, (merged["merged_content"], merged["total_access_count"], now, canonical["id"]))

    # Set superseded_by on all non-canonical members
    for member in members:
        db.execute("""
            UPDATE memories SET superseded_by = ?, updated_at = ? WHERE id = ?
        """, (canonical["id"], now, member["id"]))

    # Re-embed canonical with merged content
    asyncio.create_task(
        asyncio.to_thread(_embed_and_store, canonical["id"], merged["merged_content"])
    )

db.commit()
```

### ConsolidateInput Model Pattern
```python
class ConsolidateInput(BaseModel):
    """Input for the remind_me_consolidate tool."""

    model_config = ConfigDict(extra="forbid")

    similarity_threshold: float = Field(
        default=0.85,
        ge=0.5,
        le=1.0,
        description="Minimum cosine similarity to cluster memories together. Higher = stricter.",
    )
    dry_run: bool = Field(
        default=True,
        description="If True, report clusters without modifying data. Set False to auto-merge.",
    )
    category: str | None = Field(
        default=None,
        description="Limit consolidation to this category",
    )
    limit: int = Field(
        default=500,
        ge=10,
        le=5000,
        description="Maximum memories to consider (prevents runaway on large vaults)",
    )
```

### Dry Run Response Format
```python
{
    "mode": "dry_run",
    "clusters_found": 3,
    "total_memories_in_clusters": 8,
    "similarity_threshold": 0.85,
    "clusters": [
        {
            "size": 3,
            "canonical": {"id": "abc123", "content": "...", "vitality": 0.95},
            "members": [
                {"id": "def456", "content": "...", "vitality": 0.72, "similarity": 0.91},
                {"id": "ghi789", "content": "...", "vitality": 0.45, "similarity": 0.87},
            ]
        }
    ]
}
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Manual memory cleanup | Semantic clustering + auto-merge | Phase 14 (new) | Automated vault maintenance |
| Hard delete duplicates | superseded_by soft-link | Phase 13 | Preserves data lineage |

**Deprecated/outdated:**
- None applicable -- this is a new feature built on stable infrastructure.

## Open Questions

1. **Content merge strategy for long memories**
   - What we know: Short atomic facts merge well by deduplication
   - What's unclear: How to merge long-form captures (conversations, multi-paragraph notes)
   - Recommendation: For auto-merge, append unique content from members below the canonical content with a separator line. Keep it simple; users can refine via `remind_me_update`.

2. **Should tags be merged from cluster members into canonical?**
   - What we know: Requirements don't specify tag handling
   - What's unclear: Whether canonical should inherit all member tags
   - Recommendation: Yes, merge tags with deduplication (using existing `dict.fromkeys` pattern). This preserves searchability.

3. **Should vitality be recalculated after merge?**
   - What we know: access_count is summed into canonical (HYGN-05)
   - What's unclear: Whether vitality should be recomputed from the new access_count
   - Recommendation: Yes, recompute vitality after summing access_count using `compute_vitality()`. The canonical record becomes more vital because it represents more accesses.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest (via pyproject.toml) |
| Config file | pyproject.toml [tool.pytest.ini_options] if exists, else defaults |
| Quick run command | `python -m pytest tests/test_consolidation.py -x -q` |
| Full suite command | `python -m pytest tests/ -x -q` |

### Phase Requirements to Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| HYGN-01 | Clustering above similarity threshold | unit | `python -m pytest tests/test_consolidation.py::test_cluster_above_threshold -x` | Wave 0 |
| HYGN-01 | Memories below threshold not clustered | unit | `python -m pytest tests/test_consolidation.py::test_no_cluster_below_threshold -x` | Wave 0 |
| HYGN-02 | dry_run reports clusters without modifying DB | unit | `python -m pytest tests/test_consolidation.py::test_dry_run_no_modification -x` | Wave 0 |
| HYGN-03 | Auto-merge picks highest-vitality canonical | unit | `python -m pytest tests/test_consolidation.py::test_canonical_highest_vitality -x` | Wave 0 |
| HYGN-03 | Auto-merge merges content into canonical | unit | `python -m pytest tests/test_consolidation.py::test_merge_content -x` | Wave 0 |
| HYGN-04 | Superseded members get superseded_by set | unit | `python -m pytest tests/test_consolidation.py::test_superseded_by_set -x` | Wave 0 |
| HYGN-05 | Canonical inherits summed access_count | unit | `python -m pytest tests/test_consolidation.py::test_access_count_summed -x` | Wave 0 |

### Sampling Rate
- **Per task commit:** `python -m pytest tests/test_consolidation.py -x -q`
- **Per wave merge:** `python -m pytest tests/ -x -q`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_consolidation.py` -- covers HYGN-01 through HYGN-05
- [ ] Test fixtures need `db_conn_with_vec` + `mock_embedder` combo (both exist in conftest.py)
- [ ] Need memory_factory memories with known-similar embeddings for deterministic clustering tests

## Sources

### Primary (HIGH confidence)
- Project source code: `remind_me_mcp/db.py` -- sqlite-vec integration, `_semantic_search`, `_embed_and_store`
- Project source code: `remind_me_mcp/embeddings.py` -- L2-normalized float32 vectors, 384 dimensions
- Project source code: `remind_me_mcp/vitality.py` -- vitality computation, access recording
- Project source code: `remind_me_mcp/tools.py` -- 19 existing tool handlers, decompose/reclassify patterns
- Project source code: `remind_me_mcp/models.py` -- Pydantic input model patterns
- Project source code: `remind_me_mcp/db.py` migration v6->v7 -- superseded_by column exists
- Project source code: `tests/conftest.py` -- FakeEmbedder, db_conn_with_vec, memory_factory fixtures

### Secondary (MEDIUM confidence)
- numpy documentation -- cosine similarity via dot product for L2-normalized vectors
- sqlite-vec documentation -- vec0 virtual table embedding storage as float32 bytes

### Tertiary (LOW confidence)
- None

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - all dependencies already in project, no new libraries needed
- Architecture: HIGH - follows established project patterns (pure-function module + tool handler)
- Pitfalls: HIGH - well-understood domain (pairwise similarity, clustering); codebase provides clear constraints
- Clustering algorithm: MEDIUM - Union-Find is well-known but threshold tuning may need iteration

**Research date:** 2026-03-05
**Valid until:** 2026-04-05 (stable domain, project-internal patterns)
