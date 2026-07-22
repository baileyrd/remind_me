"""
Tests for co-retrieval reinforcement (issue #9): memories that appear
together in a search result set build up a bounded, undecayed association
weight (vitality.record_co_retrieval / memory_associations table), surfaced
only as an opt-in remind_me_search expansion section
(_expand_via_co_retrieval / expand_co_retrieval -> related_via_co_retrieval).

Deliberately never feeds back into RRF ranking -- see the docstrings on
record_co_retrieval and _expand_via_co_retrieval for why. These tests cover:

  - _expand_via_co_retrieval (db.py-adjacent, pure query logic)
  - remind_me_search's expand_co_retrieval flag end-to-end, including that
    every search (regardless of the flag) passively reinforces associations
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

    import pytest

from remind_me_mcp.db import _now_iso
from remind_me_mcp.models import MemorySearchInput, ResponseFormat
from remind_me_mcp.tools import _expand_via_co_retrieval, memory_search


def _associate(db: sqlite3.Connection, a: str, b: str, weight: int = 1) -> None:
    """Directly write a memory_associations row (canonical pair order), commit."""
    x, y = sorted((a, b))
    now = _now_iso()
    db.execute(
        "INSERT INTO memory_associations (memory_id_a, memory_id_b, weight, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (x, y, weight, now),
    )
    db.commit()


# ---------------------------------------------------------------------------
# _expand_via_co_retrieval
# ---------------------------------------------------------------------------


def test_expand_via_co_retrieval_empty_when_no_seeds(db_conn: sqlite3.Connection) -> None:
    assert _expand_via_co_retrieval(db_conn, []) == []


def test_expand_via_co_retrieval_empty_when_no_associations(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    seed = memory_factory(content="lonely memory")
    assert _expand_via_co_retrieval(db_conn, [seed]) == []


def test_expand_via_co_retrieval_returns_associated_memory(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    seed = memory_factory(content="seed memory")
    other = memory_factory(content="associated memory")
    _associate(db_conn, seed["id"], other["id"], weight=3)

    related = _expand_via_co_retrieval(db_conn, [seed])

    assert len(related) == 1
    assert related[0]["id"] == other["id"]
    assert related[0]["co_retrieval_weight"] == 3
    assert "content_snippet" in related[0]


def test_expand_via_co_retrieval_orders_by_weight_desc(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    seed = memory_factory(content="seed memory")
    weak = memory_factory(content="weakly associated")
    strong = memory_factory(content="strongly associated")
    _associate(db_conn, seed["id"], weak["id"], weight=1)
    _associate(db_conn, seed["id"], strong["id"], weight=10)

    related = _expand_via_co_retrieval(db_conn, [seed])

    assert [r["id"] for r in related] == [strong["id"], weak["id"]]


def test_expand_via_co_retrieval_excludes_seeds(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """A memory associated with one seed but ALSO present as another seed isn't re-surfaced."""
    seed_a = memory_factory(content="seed alpha")
    seed_b = memory_factory(content="seed beta")
    _associate(db_conn, seed_a["id"], seed_b["id"])

    related = _expand_via_co_retrieval(db_conn, [seed_a, seed_b])

    assert related == []


def test_expand_via_co_retrieval_excludes_superseded(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    seed = memory_factory(content="seed memory")
    superseded = memory_factory(content="stale associate", superseded_by="newer")
    _associate(db_conn, seed["id"], superseded["id"])

    related = _expand_via_co_retrieval(db_conn, [seed])

    assert related == []


def test_expand_via_co_retrieval_respects_cap(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    seed = memory_factory(content="seed memory")
    for i in range(7):
        other = memory_factory(content=f"associate number {i}")
        _associate(db_conn, seed["id"], other["id"])

    related = _expand_via_co_retrieval(db_conn, [seed])

    assert len(related) == 5  # documented cap


def test_expand_via_co_retrieval_dedups_multi_seed_associations(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """The same associated memory linked to two different seeds appears once, highest weight."""
    seed_a = memory_factory(content="seed alpha")
    seed_b = memory_factory(content="seed beta")
    other = memory_factory(content="shared associate")
    _associate(db_conn, seed_a["id"], other["id"], weight=2)
    _associate(db_conn, seed_b["id"], other["id"], weight=9)

    related = _expand_via_co_retrieval(db_conn, [seed_a, seed_b])

    assert len(related) == 1
    assert related[0]["id"] == other["id"]


# ---------------------------------------------------------------------------
# remind_me_search integration: expand_co_retrieval flag
# ---------------------------------------------------------------------------


async def test_search_co_retrieval_off_by_default(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    seed = memory_factory(content="zephyr expedition notes")
    other = memory_factory(content="unrelated other memory")
    _associate(db_conn, seed["id"], other["id"])

    data = json.loads(await memory_search(MemorySearchInput(
        query="zephyr expedition", response_format=ResponseFormat.JSON,
    )))

    assert "related_via_co_retrieval" not in data


async def test_search_co_retrieval_surfaces_association_without_disturbing_ranking(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    seed = memory_factory(content="zephyr expedition notes")
    associate = memory_factory(content="basecamp logistics for the trip")
    _associate(db_conn, seed["id"], associate["id"], weight=5)

    baseline = json.loads(await memory_search(MemorySearchInput(
        query="zephyr expedition", response_format=ResponseFormat.JSON,
    )))
    data = json.loads(await memory_search(MemorySearchInput(
        query="zephyr expedition", response_format=ResponseFormat.JSON,
        expand_co_retrieval=True,
    )))

    # Main results identical to the unexpanded search.
    assert [m["id"] for m in data["memories"]] == [m["id"] for m in baseline["memories"]]
    related = data["related_via_co_retrieval"]
    assert [r["id"] for r in related] == [associate["id"]]
    assert related[0]["co_retrieval_weight"] == 5


async def test_search_reinforces_associations_regardless_of_flag(
    db_conn: sqlite3.Connection, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every search reinforces co-retrieval, even with expand_co_retrieval unset."""
    import remind_me_mcp.tools as _tools_mod

    mem_a = memory_factory(content="zephyr expedition alpha")
    mem_b = memory_factory(content="zephyr expedition beta")

    recorded_ids: list[str] = []
    monkeypatch.setattr(
        _tools_mod, "record_co_retrieval",
        lambda ids: recorded_ids.extend(ids) or len(ids),
    )

    created_tasks: list[asyncio.Task] = []
    real_create_task = asyncio.create_task

    def capture_task(coro, **kwargs):
        task = real_create_task(coro, **kwargs)
        created_tasks.append(task)
        return task

    monkeypatch.setattr(asyncio, "create_task", capture_task)

    await memory_search(MemorySearchInput(
        query="zephyr expedition", response_format=ResponseFormat.JSON,
    ))
    await asyncio.gather(*created_tasks)

    assert set(recorded_ids) == {mem_a["id"], mem_b["id"]}


async def test_search_co_retrieval_markdown_format(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    seed = memory_factory(content="zephyr expedition notes")
    associate = memory_factory(content="basecamp logistics for the trip")
    _associate(db_conn, seed["id"], associate["id"], weight=2)

    result = await memory_search(MemorySearchInput(
        query="zephyr expedition", expand_co_retrieval=True,
    ))

    assert "Related via co-retrieval" in result
    assert associate["id"] in result
