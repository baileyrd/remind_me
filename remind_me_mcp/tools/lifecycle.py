"""
remind_me_mcp.tools.lifecycle — vitality report / reclassify / consolidate handlers.

Patchable shared state and cross-module helpers are looked up through the
``remind_me_mcp.tools`` package namespace (``_pkg.<name>``) at call time so
monkeypatching ``remind_me_mcp.tools.<name>`` keeps working (HY-02).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from remind_me_mcp import tools as _pkg
from remind_me_mcp.consolidation import find_clusters, merge_cluster, pick_canonical
from remind_me_mcp.db import _now_iso, _row_to_dict
from remind_me_mcp.models import (
    ConsolidateInput,
    ReclassifyBatchInput,
    ReclassifyInput,
    ResponseFormat,
    VitalityReportInput,
)
from remind_me_mcp.server import mcp
from remind_me_mcp.tools._shared import _maybe_update_notice
from remind_me_mcp.vitality import (
    DECAY_RATES,
    compute_vitality,
    effective_vitality,
    get_effective_decay_rate,
    is_dormant,
)

# ---------------------------------------------------------------------------
# Vitality report tool (Phase 11 Plan 03)
# ---------------------------------------------------------------------------


@mcp.tool(
    name="remind_me_vitality_report",
    annotations={
        "title": "Vitality Report",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_vitality_report(params: VitalityReportInput) -> str:
    """Generate a vault health report with vitality metrics, dormant counts, and decay distribution.

    Provides an overview of the memory vault's health including active/dormant
    counts, average vitality, vitality distribution across buckets, and a
    breakdown by memory type.

    Args:
        params: Report options including response format.

    Returns:
        str: Vault health report in the requested format (JSON or markdown).
    """
    db = _pkg._get_db()

    # Effective (read-time) vitality per memory — the stored column is a
    # stale at-access snapshot (DI-04).
    rows = db.execute("SELECT * FROM memories").fetchall()
    total = len(rows)
    vitalities = [effective_vitality(_row_to_dict(r)) for r in rows]

    # Core counts (dormancy from effective vitality, not the stored status)
    dormant_count = sum(1 for v in vitalities if is_dormant(v))
    active_count = total - dormant_count

    # Average vitality
    avg_vitality = round(sum(vitalities) / total, 2) if total > 0 else 0.0

    # Decay distribution by memory_type
    type_rows = db.execute(
        "SELECT memory_type, COUNT(*) as cnt FROM memories GROUP BY memory_type"
    ).fetchall()
    decay_distribution = {r["memory_type"]: r["cnt"] for r in type_rows}

    # Vitality buckets. The top bucket is open-ended: accessed memories exceed
    # 1.0 (one access -> sqrt(2) ~= 1.41), so a closed bucket would lose them
    # and the counts wouldn't sum to the total (DI-04).
    bucket_ranges = [
        ("0.00-0.05", 0.0, 0.05),
        ("0.05-0.25", 0.05, 0.25),
        ("0.25-0.50", 0.25, 0.50),
        ("0.50-0.75", 0.50, 0.75),
    ]
    top_bucket = "0.75+"
    vitality_buckets: dict[str, int] = {label: 0 for label, _, _ in bucket_ranges}
    vitality_buckets[top_bucket] = 0
    for v in vitalities:
        for label, low, high in bucket_ranges:
            if low <= v < high:
                vitality_buckets[label] += 1
                break
        else:
            vitality_buckets[top_bucket] += 1

    # Vault health score
    health_pct = round(active_count / total * 100) if total > 0 else 0
    vault_health_score = f"{health_pct}%"

    data = {
        "total_memories": total,
        "active_count": active_count,
        "dormant_count": dormant_count,
        "average_vitality": avg_vitality,
        "vault_health_score": vault_health_score,
        "decay_distribution": decay_distribution,
        "vitality_buckets": vitality_buckets,
    }

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(data, indent=2, default=str)

    # Markdown format
    lines = [
        "## Vault Vitality Report",
        "",
        f"**Total memories:** {total}",
        f"**Active:** {active_count}",
        f"**Dormant:** {dormant_count}",
        f"**Vault health:** {vault_health_score}",
        f"**Average vitality:** {avg_vitality:.2f}",
        "",
        "### Vitality Distribution",
        "",
    ]
    for label, count in vitality_buckets.items():
        bar = "#" * min(count, 40)
        lines.append(f"  {label}: {bar} ({count})")
    lines.append("")
    lines.append("### Memory Type Distribution")
    lines.append("")
    for mtype, count in sorted(decay_distribution.items()):
        lines.append(f"- **{mtype}**: {count}")

    return _maybe_update_notice("\n".join(lines))


# ---------------------------------------------------------------------------
# Classification tools (Phase 11 Plan 02)
# ---------------------------------------------------------------------------


@mcp.tool(
    name="remind_me_reclassify",
    annotations={
        "title": "Reclassify Memories",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_reclassify(params: ReclassifyInput) -> str:
    """Apply memory type classifications to one or more memories.

    For each classification, sets the memory's memory_type and updates its
    decay_rate to match the per-category rate from the DECAY_RATES table.
    Classification is idempotent -- reclassifying a memory overwrites its
    previous type and decay rate.

    Args:
        params: List of {memory_id, memory_type} classification pairs.

    Returns:
        JSON string with updated count, not_found IDs, and total processed.
    """
    db = _pkg._get_db()
    now = _now_iso()
    updated = 0
    not_found: list[str] = []

    for classification in params.classifications:
        row = db.execute(
            "SELECT id FROM memories WHERE id = ?",
            (classification.memory_id,),
        ).fetchone()

        if row is None:
            not_found.append(classification.memory_id)
            continue

        decay_rate = DECAY_RATES.get(classification.memory_type, 0.10)
        db.execute(
            "UPDATE memories SET memory_type = ?, decay_rate = ?, updated_at = ? WHERE id = ?",
            (classification.memory_type, decay_rate, now, classification.memory_id),
        )
        updated += 1

    db.commit()

    result = {
        "updated": updated,
        "not_found": not_found,
        "total": len(params.classifications),
    }
    return json.dumps(result)


@mcp.tool(
    name="remind_me_reclassify_batch",
    annotations={
        "title": "Get Unclassified Memories",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_reclassify_batch(params: ReclassifyBatchInput) -> str:
    """Fetch a batch of unclassified memories for Claude to classify.

    Returns memories with memory_type='unclassified' so Claude can review
    their content and call remind_me_reclassify with appropriate types.
    Each memory includes its id, a content snippet (first 500 chars),
    category, and tags.

    Args:
        params: Batch size (default 20, max 100).

    Returns:
        JSON string with memories array and total_unclassified count.
    """
    db = _pkg._get_db()

    # Get total count of unclassified memories
    total_row = db.execute(
        "SELECT COUNT(*) as cnt FROM memories WHERE memory_type = 'unclassified'",
    ).fetchone()
    total_unclassified = total_row["cnt"]

    # Fetch batch
    rows = db.execute(
        "SELECT id, substr(content, 1, 500) as content_snippet, category, tags "
        "FROM memories WHERE memory_type = 'unclassified' LIMIT ?",
        (params.batch_size,),
    ).fetchall()

    memories = []
    for row in rows:
        tags = row["tags"]
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except (json.JSONDecodeError, TypeError):
                tags = []
        memories.append({
            "id": row["id"],
            "content_snippet": row["content_snippet"],
            "category": row["category"],
            "tags": tags,
        })

    result = {
        "memories": memories,
        "total_unclassified": total_unclassified,
    }
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Consolidation tools (Phase 14 Plan 02)
# ---------------------------------------------------------------------------


@mcp.tool(
    name="remind_me_consolidate",
    annotations={
        "title": "Consolidate Duplicate Memories",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def remind_me_consolidate(params: ConsolidateInput) -> str:
    """Find clusters of semantically similar memories and optionally merge them.

    In dry_run mode (default), reports clusters with canonical and member details
    without modifying data. In auto-merge mode (dry_run=False), merges content
    into the canonical memory, sets superseded_by on members, sums access counts,
    recalculates vitality, and re-embeds the canonical.

    Only active, non-superseded memories are considered for consolidation.

    Args:
        params: Consolidation parameters (similarity_threshold, dry_run, category, limit).

    Returns:
        JSON string with cluster report (dry_run) or merge results (auto-merge).
    """
    db = _pkg._get_db()

    # Build query to fetch active, non-superseded memories with embeddings
    sql = """
        SELECT m.id, m.content, m.vitality, m.access_count, m.accessed_at,
               m.category, m.tags, m.memory_type, m.decay_rate, m.base_weight,
               mv.embedding
        FROM memories m
        JOIN vec_chunks vc ON vc.memory_rowid = m.rowid AND vc.chunk_ix = 0
        JOIN memories_vec mv ON mv.rowid = vc.vec_rowid
        WHERE m.status = 'active'
          AND m.superseded_by IS NULL
          AND m.deleted_at IS NULL
    """
    query_params: list[Any] = []

    if params.category is not None:
        sql += "  AND m.category = ?\n"
        query_params.append(params.category)

    sql += "  LIMIT ?"
    query_params.append(params.limit)

    rows = db.execute(sql, query_params).fetchall()

    if not rows:
        return json.dumps({"clusters_found": 0, "message": "No eligible memories found"})

    # Build memories list and embeddings dict
    memories: list[dict[str, Any]] = []
    embeddings: dict[str, bytes] = {}

    for row in rows:
        raw_tags = row["tags"]
        if isinstance(raw_tags, str):
            try:
                tags = json.loads(raw_tags)
            except (json.JSONDecodeError, TypeError):
                tags = []
        else:
            tags = raw_tags if raw_tags else []

        mem = {
            "id": row["id"],
            "content": row["content"],
            "vitality": row["vitality"],
            "access_count": row["access_count"],
            "accessed_at": row["accessed_at"],
            "category": row["category"],
            "tags": tags,
            "memory_type": row["memory_type"],
            "decay_rate": row["decay_rate"],
            "base_weight": row["base_weight"],
        }
        memories.append(mem)
        embeddings[row["id"]] = bytes(row["embedding"])

    # Find clusters
    clusters = find_clusters(memories, embeddings, params.similarity_threshold)

    if not clusters:
        return json.dumps({"clusters_found": 0, "message": "No similar memories found above threshold"})

    # --- Dry run mode ---
    if params.dry_run:
        cluster_reports: list[dict[str, Any]] = []

        import numpy as np

        for cluster in clusters:
            canonical = pick_canonical(cluster)
            members = [m for m in cluster if m["id"] != canonical["id"]]

            # Compute similarity between canonical and each member for display
            canonical_emb = embeddings[canonical["id"]]
            dim = len(canonical_emb) // 4  # float32 = 4 bytes
            canonical_vec = np.frombuffer(canonical_emb, dtype=np.float32).reshape(dim)

            member_reports: list[dict[str, Any]] = []
            for member in members:
                member_emb = embeddings[member["id"]]
                member_vec = np.frombuffer(member_emb, dtype=np.float32).reshape(dim)
                similarity = float(np.dot(canonical_vec, member_vec))

                member_reports.append({
                    "id": member["id"],
                    "content": member["content"][:200],
                    "vitality": member["vitality"],
                    "similarity": round(similarity, 4),
                })

            cluster_reports.append({
                "canonical": {
                    "id": canonical["id"],
                    "content": canonical["content"][:200],
                    "vitality": canonical["vitality"],
                },
                "members": member_reports,
                "cluster_size": len(cluster),
            })

        result = {
            "clusters_found": len(clusters),
            "dry_run": True,
            "clusters": cluster_reports,
        }
        return json.dumps(result, indent=2)

    # --- Auto-merge mode ---
    now = _now_iso()
    total_superseded = 0
    canonical_ids: list[str] = []

    for cluster in clusters:
        canonical = pick_canonical(cluster)
        members = [m for m in cluster if m["id"] != canonical["id"]]

        merged = merge_cluster(canonical, members)

        # Update canonical: content, access_count, tags, updated_at
        db.execute(
            """UPDATE memories
               SET content = ?, access_count = ?, tags = ?, updated_at = ?
               WHERE id = ?""",
            (
                merged["merged_content"],
                merged["total_access_count"],
                json.dumps(merged["merged_tags"]),
                now,
                canonical["id"],
            ),
        )

        # Recalculate vitality for canonical with new access_count
        effective_rate = get_effective_decay_rate(
            canonical["decay_rate"], merged["total_access_count"]
        )
        new_vitality = compute_vitality(
            base_weight=canonical["base_weight"],
            access_count=merged["total_access_count"],
            decay_rate=effective_rate,
            days_since_last_access=0.0,
        )

        db.execute(
            """UPDATE memories
               SET vitality = ?, status = 'active'
               WHERE id = ?""",
            (new_vitality, canonical["id"]),
        )

        # Set superseded_by on each member
        for member in members:
            db.execute(
                """UPDATE memories
                   SET superseded_by = ?, updated_at = ?
                   WHERE id = ?""",
                (canonical["id"], now, member["id"]),
            )

        total_superseded += len(members)
        canonical_ids.append(canonical["id"])

        # Fire-and-forget re-embed canonical with merged content (reference
        # held in _background_tasks, PF-04)
        _pkg._spawn_task(
            asyncio.to_thread(_pkg._embed_and_store, canonical["id"], merged["merged_content"])
        )

    db.commit()

    result = {
        "clusters_merged": len(clusters),
        "memories_superseded": total_superseded,
        "canonical_ids": canonical_ids,
        "dry_run": False,
    }
    return json.dumps(result, indent=2)
