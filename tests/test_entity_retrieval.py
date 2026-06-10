"""
Tests for FT-04 part 2 — the entity-graph retrieval surfaces:

  - _resolve_entity / _entity_profile (db.py shared helpers)
  - the remind_me_entity MCP lookup tool
  - the entity:"..." structured search syntax in remind_me_search
  - opt-in 1-hop neighbor expansion (expand_entities) in remind_me_search

HTTP API parity (GET /api/entity, entity: in GET /api/memories/search) is
covered in test_api.py alongside the other route tests.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

    import pytest

from remind_me_mcp.db import (
    _entity_id,
    _entity_profile,
    _link_memory_entity,
    _resolve_entity,
    _upsert_entity,
)
from remind_me_mcp.models import MemorySearchInput, ResponseFormat
from remind_me_mcp.tools import memory_search, remind_me_entity


def _mention(
    db: sqlite3.Connection,
    memory_id: str,
    name: str,
    kind: str | None = None,
    aliases: list[str] | None = None,
) -> str:
    """Upsert an entity, link it to *memory_id*, commit, return the entity id."""
    eid = _upsert_entity(db, name, kind, aliases)
    _link_memory_entity(db, memory_id, eid)
    db.commit()
    return eid


# ---------------------------------------------------------------------------
# _resolve_entity — lookup resolution order
# ---------------------------------------------------------------------------


def test_resolve_entity_by_canonical_name(db_conn: sqlite3.Connection) -> None:
    _upsert_entity(db_conn, "Bailey Robertson", "person")
    db_conn.commit()
    ent = _resolve_entity(db_conn, "Bailey Robertson")
    assert ent is not None
    assert ent["id"] == _entity_id("Bailey Robertson")
    assert ent["name"] == "Bailey Robertson"
    assert ent["kind"] == "person"


def test_resolve_entity_normalizes_case_and_whitespace(
    db_conn: sqlite3.Connection,
) -> None:
    _upsert_entity(db_conn, "Bailey Robertson")
    db_conn.commit()
    ent = _resolve_entity(db_conn, "  bailey   ROBERTSON ")
    assert ent is not None
    assert ent["name"] == "Bailey Robertson"


def test_resolve_entity_by_alias(db_conn: sqlite3.Connection) -> None:
    _upsert_entity(db_conn, "Bailey Robertson", "person", aliases=["BR", "Bailey R"])
    db_conn.commit()
    ent = _resolve_entity(db_conn, "br")  # alias match is case-insensitive too
    assert ent is not None
    assert ent["name"] == "Bailey Robertson"


def test_resolve_entity_prefers_canonical_name_over_alias(
    db_conn: sqlite3.Connection,
) -> None:
    """An exact canonical-name hit wins over another entity's alias."""
    _upsert_entity(db_conn, "Bailey")  # canonical name 'Bailey'
    _upsert_entity(db_conn, "Bailey Robertson", aliases=["Bailey"])
    db_conn.commit()
    ent = _resolve_entity(db_conn, "Bailey")
    assert ent is not None
    assert ent["name"] == "Bailey"


def test_resolve_entity_not_found(db_conn: sqlite3.Connection) -> None:
    _upsert_entity(db_conn, "Bailey Robertson")
    db_conn.commit()
    assert _resolve_entity(db_conn, "Nobody Known") is None


# ---------------------------------------------------------------------------
# remind_me_entity tool
# ---------------------------------------------------------------------------


async def test_entity_tool_returns_entity_facts_and_memories(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    from remind_me_mcp.models import EntityLookupInput

    linked = memory_factory(content="Tailscale config notes", category="note")
    _mention(db_conn, linked["id"], "Tailscale", kind="tool", aliases=["ts"])
    # SPO facts: subject match and object match, case-insensitive.
    memory_factory(
        content="Tailscale connects the laptops",
        subject="tailscale", predicate="connects", object="the laptops",
    )
    memory_factory(
        content="remind_me syncs over Tailscale",
        subject="remind_me", predicate="syncs over", object="Tailscale",
    )
    memory_factory(content="unrelated", subject="other", object="thing")

    result = json.loads(await remind_me_entity(EntityLookupInput(name="Tailscale")))

    assert result["found"] is True
    assert result["entity"]["id"] == _entity_id("Tailscale")
    assert result["entity"]["name"] == "Tailscale"
    assert result["entity"]["kind"] == "tool"
    assert result["entity"]["aliases"] == ["ts"]

    fact_contents = {f["content"] for f in result["facts"]}
    assert fact_contents == {
        "Tailscale connects the laptops",
        "remind_me syncs over Tailscale",
    }

    assert [m["id"] for m in result["memories"]] == [linked["id"]]
    assert result["memories"][0]["content_snippet"] == "Tailscale config notes"
    assert result["total_linked_memories"] == 1


async def test_entity_tool_lookup_by_alias_and_normalization(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    from remind_me_mcp.models import EntityLookupInput

    mem = memory_factory(content="Bailey's preferences")
    _mention(db_conn, mem["id"], "Bailey Robertson", kind="person", aliases=["Bailey"])

    by_alias = json.loads(await remind_me_entity(EntityLookupInput(name="bailey")))
    assert by_alias["found"] is True
    assert by_alias["entity"]["name"] == "Bailey Robertson"

    by_messy_name = json.loads(
        await remind_me_entity(EntityLookupInput(name="  BAILEY   robertson "))
    )
    assert by_messy_name["entity"]["id"] == by_alias["entity"]["id"]


async def test_entity_tool_not_found(db_conn: sqlite3.Connection) -> None:
    from remind_me_mcp.models import EntityLookupInput

    result = json.loads(await remind_me_entity(EntityLookupInput(name="Ghost")))
    assert result["found"] is False
    assert result["query"] == "Ghost"
    assert "No entity found" in result["message"]


async def test_entity_tool_excludes_superseded(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    from remind_me_mcp.models import EntityLookupInput

    current = memory_factory(content="current mention")
    stale = memory_factory(content="stale mention", superseded_by="newer-id")
    old_fact = memory_factory(
        content="old fact", subject="Tailscale", superseded_by="newer-id",
    )
    eid = _mention(db_conn, current["id"], "Tailscale")
    _link_memory_entity(db_conn, stale["id"], eid)
    db_conn.commit()

    result = json.loads(await remind_me_entity(EntityLookupInput(name="Tailscale")))
    assert [m["id"] for m in result["memories"]] == [current["id"]]
    assert result["total_linked_memories"] == 1
    assert old_fact["id"] not in {f["id"] for f in result["facts"]}


async def test_entity_tool_dangling_links_invisible(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """Sync may deliver a link before its memory — INNER joins hide it."""
    from remind_me_mcp.models import EntityLookupInput

    mem = memory_factory(content="real mention")
    eid = _mention(db_conn, mem["id"], "remind_me", kind="project")
    _link_memory_entity(db_conn, "not-arrived-yet", eid)
    db_conn.commit()

    result = json.loads(await remind_me_entity(EntityLookupInput(name="remind_me")))
    assert [m["id"] for m in result["memories"]] == [mem["id"]]
    assert result["total_linked_memories"] == 1


async def test_entity_tool_respects_limit(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    from remind_me_mcp.models import EntityLookupInput

    for i in range(4):
        mem = memory_factory(content=f"mention {i}")
        _mention(db_conn, mem["id"], "remind_me")

    result = json.loads(
        await remind_me_entity(EntityLookupInput(name="remind_me", limit=2))
    )
    assert len(result["memories"]) == 2
    assert result["total_linked_memories"] == 4


def test_entity_profile_returns_none_for_unknown(db_conn: sqlite3.Connection) -> None:
    assert _entity_profile(db_conn, "nobody") is None


# ---------------------------------------------------------------------------
# entity:"..." structured search syntax
# ---------------------------------------------------------------------------


async def test_search_entity_filter_via_links(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    linked = memory_factory(content="Tailscale handles the mesh")
    memory_factory(content="something else entirely")
    _mention(db_conn, linked["id"], "Tailscale", kind="tool")

    result = await memory_search(
        MemorySearchInput(query="entity:Tailscale", response_format=ResponseFormat.JSON)
    )
    data = json.loads(result)
    assert [m["id"] for m in data["memories"]] == [linked["id"]]


async def test_search_entity_filter_matches_spo_without_link(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """A memory whose subject/object equals the canonical name matches even
    without a memory_entities link (case-insensitive)."""
    _upsert_entity(db_conn, "Tailscale", "tool")
    db_conn.commit()
    spo_subject = memory_factory(
        content="subject side", subject="tailscale", predicate="connects",
    )
    spo_object = memory_factory(
        content="object side", subject="remind_me", object="Tailscale",
    )
    memory_factory(content="no relation", subject="other")

    data = json.loads(await memory_search(
        MemorySearchInput(query="entity:Tailscale", response_format=ResponseFormat.JSON)
    ))
    assert {m["id"] for m in data["memories"]} == {spo_subject["id"], spo_object["id"]}


async def test_search_entity_filter_quoted_multiword_and_alias(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    mem = memory_factory(content="notes about Bailey")
    _mention(db_conn, mem["id"], "Bailey Robertson", aliases=["BR"])

    quoted = json.loads(await memory_search(MemorySearchInput(
        query='entity:"Bailey Robertson"', response_format=ResponseFormat.JSON,
    )))
    assert [m["id"] for m in quoted["memories"]] == [mem["id"]]

    via_alias = json.loads(await memory_search(MemorySearchInput(
        query="entity:BR", response_format=ResponseFormat.JSON,
    )))
    assert [m["id"] for m in via_alias["memories"]] == [mem["id"]]


async def test_search_entity_combines_with_subject_and_predicate(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    prefers = memory_factory(
        content="Bailey prefers dark mode",
        subject="Bailey", predicate="prefers", object="dark mode",
    )
    uses = memory_factory(
        content="Bailey uses Python",
        subject="Bailey", predicate="uses", object="Python",
    )
    eid = _mention(db_conn, prefers["id"], "Bailey", kind="person")
    _link_memory_entity(db_conn, uses["id"], eid)
    db_conn.commit()

    data = json.loads(await memory_search(MemorySearchInput(
        query="entity:Bailey predicate:prefers", response_format=ResponseFormat.JSON,
    )))
    assert [m["id"] for m in data["memories"]] == [prefers["id"]]


async def test_search_entity_excludes_superseded(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    current = memory_factory(content="current")
    stale = memory_factory(content="stale", superseded_by="newer")
    eid = _mention(db_conn, current["id"], "remind_me")
    _link_memory_entity(db_conn, stale["id"], eid)
    db_conn.commit()

    data = json.loads(await memory_search(MemorySearchInput(
        query="entity:remind_me", response_format=ResponseFormat.JSON,
    )))
    assert [m["id"] for m in data["memories"]] == [current["id"]]


async def test_search_unresolvable_entity_returns_empty_with_message(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    memory_factory(content="Ghost stories are fun")  # would match FTS

    md = await memory_search(MemorySearchInput(query="entity:Ghost stories"))
    assert "No memories found" in md
    assert "Ghost" in md

    data = json.loads(await memory_search(MemorySearchInput(
        query="entity:Ghost stories", response_format=ResponseFormat.JSON,
    )))
    assert data["returned"] == 0
    assert data["memories"] == []
    assert "No entity found" in data["message"]


async def test_search_entity_no_hits_falls_back_to_fts_remainder(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """A resolvable entity with no matching memories falls through to FTS on
    the stripped remainder, consistent with subject:/predicate: behavior."""
    _upsert_entity(db_conn, "Lonely Entity")
    db_conn.commit()
    mem = memory_factory(content="fallback freetext target")

    data = json.loads(await memory_search(MemorySearchInput(
        query='entity:"Lonely Entity" fallback freetext',
        response_format=ResponseFormat.JSON,
    )))
    assert mem["id"] in {m["id"] for m in data["memories"]}


# ---------------------------------------------------------------------------
# 1-hop neighbor expansion (expand_entities)
# ---------------------------------------------------------------------------


async def test_expansion_off_by_default(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    seed = memory_factory(content="zephyr seed memory")
    neighbor = memory_factory(content="graph neighbor")
    eid = _mention(db_conn, seed["id"], "Zephyr Project")
    _link_memory_entity(db_conn, neighbor["id"], eid)
    db_conn.commit()

    data = json.loads(await memory_search(MemorySearchInput(
        query="zephyr seed", response_format=ResponseFormat.JSON,
    )))
    assert "related_via_entities" not in data


async def test_expansion_appends_neighbors_without_disturbing_ranking(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    seed = memory_factory(content="zephyr seed memory")
    neighbor = memory_factory(content="graph neighbor sharing the entity")
    eid = _mention(db_conn, seed["id"], "Zephyr Project", kind="project")
    _link_memory_entity(db_conn, neighbor["id"], eid)
    db_conn.commit()

    baseline = json.loads(await memory_search(MemorySearchInput(
        query="zephyr seed", response_format=ResponseFormat.JSON,
    )))
    data = json.loads(await memory_search(MemorySearchInput(
        query="zephyr seed", response_format=ResponseFormat.JSON,
        expand_entities=True,
    )))

    # Main results identical to the unexpanded search.
    assert [m["id"] for m in data["memories"]] == [
        m["id"] for m in baseline["memories"]
    ]
    related = data["related_via_entities"]
    assert [r["id"] for r in related] == [neighbor["id"]]
    assert related[0]["via_entities"] == ["Zephyr Project"]
    assert "content_snippet" in related[0]


async def test_expansion_no_duplicates_with_main_results(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """Neighbors already present in the main results are not repeated."""
    seed_a = memory_factory(content="zephyr seed alpha")
    seed_b = memory_factory(content="zephyr seed beta")
    eid = _mention(db_conn, seed_a["id"], "Zephyr Project")
    _link_memory_entity(db_conn, seed_b["id"], eid)
    db_conn.commit()

    data = json.loads(await memory_search(MemorySearchInput(
        query="zephyr seed", response_format=ResponseFormat.JSON,
        expand_entities=True,
    )))
    main_ids = {m["id"] for m in data["memories"]}
    assert {seed_a["id"], seed_b["id"]} <= main_ids
    assert data["related_via_entities"] == []


async def test_expansion_excludes_superseded_and_respects_cap(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    seed = memory_factory(content="zephyr seed memory")
    eid = _mention(db_conn, seed["id"], "Zephyr Project")
    superseded = memory_factory(content="old neighbor", superseded_by="newer")
    _link_memory_entity(db_conn, superseded["id"], eid)
    for i in range(7):
        nbr = memory_factory(content=f"neighbor number {i}")
        _link_memory_entity(db_conn, nbr["id"], eid)
    db_conn.commit()

    data = json.loads(await memory_search(MemorySearchInput(
        query="zephyr seed", response_format=ResponseFormat.JSON,
        expand_entities=True,
    )))
    related = data["related_via_entities"]
    assert len(related) == 5  # documented cap
    assert superseded["id"] not in {r["id"] for r in related}


async def test_expansion_multiple_linking_entities_reported(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    seed = memory_factory(content="zephyr seed memory")
    neighbor = memory_factory(content="shares two entities")
    e1 = _mention(db_conn, seed["id"], "Zephyr Project")
    e2 = _mention(db_conn, seed["id"], "Bailey", kind="person")
    _link_memory_entity(db_conn, neighbor["id"], e1)
    _link_memory_entity(db_conn, neighbor["id"], e2)
    db_conn.commit()

    data = json.loads(await memory_search(MemorySearchInput(
        query="zephyr seed", response_format=ResponseFormat.JSON,
        expand_entities=True,
    )))
    related = data["related_via_entities"]
    assert len(related) == 1
    assert set(related[0]["via_entities"]) == {"Zephyr Project", "Bailey"}


async def test_expansion_works_on_structured_entity_path(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """entity: search + expand_entities expands through co-mentioned entities."""
    seed = memory_factory(content="Bailey moved to Portland")
    bailey = _mention(db_conn, seed["id"], "Bailey", kind="person")
    portland = _mention(db_conn, seed["id"], "Portland", kind="place")
    neighbor = memory_factory(content="Portland has great coffee")
    _link_memory_entity(db_conn, neighbor["id"], portland)
    db_conn.commit()
    assert bailey != portland

    data = json.loads(await memory_search(MemorySearchInput(
        query="entity:Bailey", response_format=ResponseFormat.JSON,
        expand_entities=True,
    )))
    assert [m["id"] for m in data["memories"]] == [seed["id"]]
    related = data["related_via_entities"]
    assert [r["id"] for r in related] == [neighbor["id"]]
    assert related[0]["via_entities"] == ["Portland"]


async def test_expansion_markdown_section(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    seed = memory_factory(content="zephyr seed memory")
    neighbor = memory_factory(content="graph neighbor body text")
    eid = _mention(db_conn, seed["id"], "Zephyr Project")
    _link_memory_entity(db_conn, neighbor["id"], eid)
    db_conn.commit()

    md = await memory_search(MemorySearchInput(
        query="zephyr seed", expand_entities=True,
    ))
    assert "Related via entities" in md
    assert neighbor["id"] in md
    assert "Zephyr Project" in md


async def test_expansion_not_access_recorded(
    db_conn: sqlite3.Connection, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expanded hits are deliberately excluded from PF-02 access recording —
    graph adjacency must not inflate neighbor vitality."""
    import remind_me_mcp.tools as _tools_mod

    seed = memory_factory(content="zephyr seed memory")
    neighbor = memory_factory(content="graph neighbor")
    eid = _mention(db_conn, seed["id"], "Zephyr Project")
    _link_memory_entity(db_conn, neighbor["id"], eid)
    db_conn.commit()

    recorded_ids: list[str] = []
    monkeypatch.setattr(
        _tools_mod, "record_accesses",
        lambda ids: recorded_ids.extend(ids) or len(ids),
    )

    created_tasks: list[asyncio.Task] = []
    real_create_task = asyncio.create_task

    def capture_task(coro, **kwargs):
        task = real_create_task(coro, **kwargs)
        created_tasks.append(task)
        return task

    monkeypatch.setattr(asyncio, "create_task", capture_task)

    data = json.loads(await memory_search(MemorySearchInput(
        query="zephyr seed", response_format=ResponseFormat.JSON,
        expand_entities=True,
    )))
    assert [r["id"] for r in data["related_via_entities"]] == [neighbor["id"]]
    await asyncio.gather(*created_tasks)

    assert seed["id"] in recorded_ids
    assert neighbor["id"] not in recorded_ids
