"""
Tests for remind_me_mcp.tools.normalize — ingest-time LLM normalization
(FT-09, Phase 5b): remind_me_normalize_batch / remind_me_normalize_apply.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from remind_me_mcp.models import NormalizationEntry, NormalizeApplyInput, NormalizeBatchInput
from remind_me_mcp.tools.normalize import remind_me_normalize_apply, remind_me_normalize_batch

if TYPE_CHECKING:
    import sqlite3


# ---------------------------------------------------------------------------
# remind_me_normalize_batch
# ---------------------------------------------------------------------------


async def test_normalize_batch_returns_document_and_chat_imports(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    memory_factory(
        content="Raw chat export chunk one.",
        source="chat_import",
        metadata={"filename": "chat.json"},
    )
    memory_factory(
        content="Raw document import chunk one.",
        source="document_import",
        metadata={"filename": "notes.md"},
    )
    memory_factory(content="Manually added memory.", source="manual")

    result = json.loads(await remind_me_normalize_batch(NormalizeBatchInput()))

    assert result["total_unnormalized"] == 2
    sources = {m["source"] for m in result["memories"]}
    assert sources == {"chat_import", "document_import"}
    filenames = {m["filename"] for m in result["memories"]}
    assert filenames == {"chat.json", "notes.md"}


async def test_normalize_batch_excludes_superseded(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    memory_factory(
        content="Superseded chunk.",
        source="document_import",
        superseded_by="some-newer-import",
    )

    result = json.loads(await remind_me_normalize_batch(NormalizeBatchInput()))
    assert result["total_unnormalized"] == 0
    assert result["memories"] == []


async def test_normalize_batch_excludes_already_normalized(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    raw = memory_factory(content="Raw content to normalize.", source="document_import")
    memory_factory(
        content="**Q:** topic\n\nA distilled summary.",
        category="normalized",
        source="normalization",
        metadata={"normalized_from": raw["id"]},
    )

    result = json.loads(await remind_me_normalize_batch(NormalizeBatchInput()))
    assert result["total_unnormalized"] == 0
    assert result["memories"] == []


async def test_normalize_batch_respects_batch_size(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    for i in range(5):
        memory_factory(content=f"Raw chunk number {i}.", source="chat_import")

    result = json.loads(
        await remind_me_normalize_batch(NormalizeBatchInput(batch_size=2))
    )
    assert result["total_unnormalized"] == 5
    assert len(result["memories"]) == 2


# ---------------------------------------------------------------------------
# remind_me_normalize_apply
# ---------------------------------------------------------------------------


async def test_normalize_apply_creates_linked_memory(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    raw = memory_factory(
        content="How the VPN was configured, in verbatim raw form.",
        source="document_import",
        tags=["networking"],
    )

    params = NormalizeApplyInput(
        normalizations=[
            NormalizationEntry(
                memory_id=raw["id"],
                question="How is the VPN configured?",
                summary="Split-tunnel VPN over WireGuard, config in /etc/wireguard.",
                resolution="Confirmed working after reboot.",
                refs=["ticket-123"],
            )
        ]
    )
    result = json.loads(await remind_me_normalize_apply(params))

    assert result["normalized"] == 1
    assert result["errors"] == []
    normalized_id = result["results"][0]["normalized_id"]
    assert result["results"][0]["memory_id"] == raw["id"]

    row = db_conn.execute(
        "SELECT content, category, source, tags, metadata FROM memories WHERE id = ?",
        (normalized_id,),
    ).fetchone()
    assert row is not None
    assert row["category"] == "normalized"
    assert row["source"] == "normalization"
    assert "How is the VPN configured?" in row["content"]
    assert "Split-tunnel VPN" in row["content"]
    assert "Confirmed working after reboot." in row["content"]
    assert json.loads(row["tags"]) == ["networking"]

    metadata = json.loads(row["metadata"])
    assert metadata["normalized_from"] == raw["id"]
    assert metadata["question"] == "How is the VPN configured?"
    assert metadata["resolution"] == "Confirmed working after reboot."
    assert metadata["refs"] == ["ticket-123"]

    # The raw memory is left untouched, not replaced.
    raw_row = db_conn.execute(
        "SELECT content FROM memories WHERE id = ?", (raw["id"],)
    ).fetchone()
    assert raw_row["content"] == raw["content"]


async def test_normalize_apply_without_resolution_omits_it(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    raw = memory_factory(content="Some raw content.", source="chat_import")

    params = NormalizeApplyInput(
        normalizations=[
            NormalizationEntry(
                memory_id=raw["id"],
                question="What is this about?",
                summary="A concise summary with no resolution.",
            )
        ]
    )
    result = json.loads(await remind_me_normalize_apply(params))
    normalized_id = result["results"][0]["normalized_id"]

    row = db_conn.execute(
        "SELECT content, metadata FROM memories WHERE id = ?", (normalized_id,)
    ).fetchone()
    assert "Resolution:" not in row["content"]
    metadata = json.loads(row["metadata"])
    assert "resolution" not in metadata


async def test_normalize_apply_unknown_memory_id_reports_error(
    db_conn: sqlite3.Connection,
) -> None:
    params = NormalizeApplyInput(
        normalizations=[
            NormalizationEntry(
                memory_id="does-not-exist",
                question="Q?",
                summary="Summary.",
            )
        ]
    )
    result = json.loads(await remind_me_normalize_apply(params))
    assert result["normalized"] == 0
    assert result["errors"] == [
        {"memory_id": "does-not-exist", "error": "memory not found"}
    ]


async def test_normalize_apply_reapply_creates_a_new_memory(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """Not idempotent (ids are timestamp-salted, like remind_me_decompose's
    fact ids): re-applying the same normalization creates a second memory,
    but the raw row was already excluded from the batch after the first
    apply, so this only matters if a caller re-applies deliberately."""
    raw = memory_factory(content="Raw content.", source="document_import")
    entry = NormalizationEntry(memory_id=raw["id"], question="Q?", summary="Summary.")

    first = json.loads(await remind_me_normalize_apply(NormalizeApplyInput(normalizations=[entry])))
    second = json.loads(await remind_me_normalize_apply(NormalizeApplyInput(normalizations=[entry])))

    assert first["results"][0]["normalized_id"] != second["results"][0]["normalized_id"]
    count = db_conn.execute(
        "SELECT COUNT(*) AS c FROM memories WHERE category = 'normalized'"
    ).fetchone()["c"]
    assert count == 2


async def test_normalize_apply_removes_row_from_next_batch(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """Applying a normalization makes remind_me_normalize_batch skip the raw row."""
    raw = memory_factory(content="Raw content to normalize.", source="chat_import")

    before = json.loads(await remind_me_normalize_batch(NormalizeBatchInput()))
    assert before["total_unnormalized"] == 1

    await remind_me_normalize_apply(
        NormalizeApplyInput(
            normalizations=[
                NormalizationEntry(memory_id=raw["id"], question="Q?", summary="Summary.")
            ]
        )
    )

    after = json.loads(await remind_me_normalize_batch(NormalizeBatchInput()))
    assert after["total_unnormalized"] == 0


async def test_normalize_apply_extra_field_rejected() -> None:
    with pytest.raises(ValueError):
        NormalizationEntry(
            memory_id="m1", question="Q?", summary="S.", bogus_field="nope"
        )
