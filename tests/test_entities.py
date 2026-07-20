"""
Tests for the FT-04 entity graph (part 1): the v9->v10 migration, the
deterministic entity id / normalization scheme, the entity upsert and mention
link helpers, the extraction write paths (decompose, memory_add), the
remind_me_extract_batch / remind_me_annotate tools, and delete cleanup.

Sync behavior for entities/links lives in test_sync.py and
test_peer_server.py, following the established MockTransport approach.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import sqlite3

from remind_me_mcp.db import (
    _SCHEMA_VERSION,
    _ensure_schema,
    _entity_id,
    _entity_relation_id,
    _link_memory_entity,
    _migrate_schema,
    _normalize_entity_name,
    _now_iso,
    _upsert_entity,
    _upsert_entity_relation,
)

# ---------------------------------------------------------------------------
# Normalization and deterministic ids
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Bailey Robertson", "bailey robertson"),
        ("  Bailey   Robertson  ", "bailey robertson"),
        ("BAILEY\tROBERTSON", "bailey robertson"),
        ("remind_me", "remind_me"),
        ("Multi\n line\r name", "multi line name"),
    ],
)
def test_normalize_entity_name(raw: str, expected: str) -> None:
    assert _normalize_entity_name(raw) == expected


def test_entity_id_is_deterministic() -> None:
    """Same normalized name -> same id, every time, on every machine."""
    a = _entity_id("Bailey Robertson")
    b = _entity_id("  bailey   ROBERTSON ")
    assert a == b
    assert len(a) == 12  # matches the _make_id length convention
    int(a, 16)  # hex string


def test_entity_id_differs_for_different_names() -> None:
    assert _entity_id("Bailey") != _entity_id("Bailey Robertson")


# ---------------------------------------------------------------------------
# v9 -> v10 migration
# ---------------------------------------------------------------------------


def test_v10_schema_objects_exist(db_conn: sqlite3.Connection) -> None:
    tables = {
        r[0]
        for r in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {"entities", "memory_entities"} <= tables

    indexes = {
        r[0]
        for r in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    assert "idx_entities_name" in indexes
    assert "idx_entities_updated_at" in indexes
    assert "idx_memory_entities_entity" in indexes
    assert "idx_memory_entities_created_at" in indexes

    triggers = {
        r[0]
        for r in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
    }
    assert {
        "entities_outbox_ai",
        "entities_outbox_au",
        "memory_entities_outbox_ai",
    } <= triggers


def test_migration_applies_on_existing_pre_v10_db(db_conn: sqlite3.Connection) -> None:
    """Re-running the migration chain on a pre-v10 database (simulated by
    dropping the entity objects and rewinding user_version to 7) recreates
    everything and preserves existing memory data."""
    now = _now_iso()
    db_conn.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata,
                                 created_at, updated_at)
           VALUES ('keepme', 'pre-migration data', 'general', '[]', 'manual', '{}', ?, ?)""",
        (now, now),
    )
    db_conn.executescript("""
        DROP TRIGGER IF EXISTS entities_outbox_ai;
        DROP TRIGGER IF EXISTS entities_outbox_au;
        DROP TRIGGER IF EXISTS memory_entities_outbox_ai;
        DROP TABLE IF EXISTS entities;
        DROP TABLE IF EXISTS memory_entities;
    """)
    db_conn.execute("PRAGMA user_version = 7")
    db_conn.commit()

    _migrate_schema(db_conn)

    assert db_conn.execute("PRAGMA user_version").fetchone()[0] == _SCHEMA_VERSION
    tables = {
        r[0]
        for r in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {"entities", "memory_entities"} <= tables
    row = db_conn.execute("SELECT content FROM memories WHERE id = 'keepme'").fetchone()
    assert row["content"] == "pre-migration data"


def test_migration_is_idempotent(db_conn: sqlite3.Connection) -> None:
    _ensure_schema(db_conn)
    _ensure_schema(db_conn)
    assert db_conn.execute("PRAGMA user_version").fetchone()[0] == _SCHEMA_VERSION


# ---------------------------------------------------------------------------
# _upsert_entity / _link_memory_entity helpers
# ---------------------------------------------------------------------------


def test_upsert_entity_creates_row(db_conn: sqlite3.Connection) -> None:
    eid = _upsert_entity(db_conn, "Bailey  Robertson", "person", ["Bailey"], node_id="n1")
    db_conn.commit()
    row = db_conn.execute("SELECT * FROM entities WHERE id = ?", (eid,)).fetchone()
    assert eid == _entity_id("bailey robertson")
    assert row["name"] == "Bailey Robertson"  # display name: whitespace-collapsed
    assert row["kind"] == "person"
    assert json.loads(row["aliases"]) == ["Bailey"]
    assert row["node_id"] == "n1"


def test_upsert_entity_unions_aliases(db_conn: sqlite3.Connection) -> None:
    """Aliases union-merge: dedup, order-preserving (existing first)."""
    eid = _upsert_entity(db_conn, "Bailey Robertson", "person", ["Bailey"])
    _upsert_entity(db_conn, "Bailey Robertson", aliases=["BR", "Bailey"])
    db_conn.commit()
    row = db_conn.execute("SELECT aliases FROM entities WHERE id = ?", (eid,)).fetchone()
    assert json.loads(row["aliases"]) == ["Bailey", "BR"]


def test_upsert_entity_fills_missing_kind_but_does_not_overwrite(
    db_conn: sqlite3.Connection,
) -> None:
    eid = _upsert_entity(db_conn, "Tailscale")
    _upsert_entity(db_conn, "Tailscale", kind="tool")
    row = db_conn.execute("SELECT kind FROM entities WHERE id = ?", (eid,)).fetchone()
    assert row["kind"] == "tool"
    # An existing kind is kept — local writes never clobber it.
    _upsert_entity(db_conn, "Tailscale", kind="company")
    row = db_conn.execute("SELECT kind FROM entities WHERE id = ?", (eid,)).fetchone()
    assert row["kind"] == "tool"


def test_upsert_entity_new_name_creates_new_entity(db_conn: sqlite3.Connection) -> None:
    """Different names are never auto-merged — 'Bailey' and 'Bailey Robertson'
    are distinct entities unless explicitly aliased."""
    a = _upsert_entity(db_conn, "Bailey")
    b = _upsert_entity(db_conn, "Bailey Robertson")
    assert a != b
    assert db_conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 2


def test_upsert_entity_noop_does_not_bump_updated_at(
    db_conn: sqlite3.Connection,
) -> None:
    eid = _upsert_entity(db_conn, "remind_me", "project", ["rm"])
    before = db_conn.execute(
        "SELECT updated_at FROM entities WHERE id = ?", (eid,)
    ).fetchone()["updated_at"]
    _upsert_entity(db_conn, "remind_me", "project", ["rm"], now="2099-01-01T00:00:00+00:00")
    after = db_conn.execute(
        "SELECT updated_at FROM entities WHERE id = ?", (eid,)
    ).fetchone()["updated_at"]
    assert after == before


def test_link_memory_entity_insert_or_ignore(db_conn: sqlite3.Connection) -> None:
    eid = _upsert_entity(db_conn, "remind_me")
    assert _link_memory_entity(db_conn, "mem-1", eid) is True
    assert _link_memory_entity(db_conn, "mem-1", eid) is False
    assert db_conn.execute("SELECT COUNT(*) FROM memory_entities").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# _entity_relation_id / _upsert_entity_relation helpers
# ---------------------------------------------------------------------------


def test_entity_relation_id_is_deterministic() -> None:
    """Same triple -> same id, regardless of relation-label casing/whitespace."""
    a = _entity_relation_id("subj-1", "works_with", "obj-1")
    b = _entity_relation_id("subj-1", "  Works_With ", "obj-1")
    assert a == b
    assert len(a) == 12
    int(a, 16)  # hex string


def test_entity_relation_id_differs_by_subject_relation_or_object() -> None:
    base = _entity_relation_id("subj-1", "works_with", "obj-1")
    assert base != _entity_relation_id("subj-2", "works_with", "obj-1")
    assert base != _entity_relation_id("subj-1", "reports_to", "obj-1")
    assert base != _entity_relation_id("subj-1", "works_with", "obj-2")


def test_upsert_entity_relation_creates_row(db_conn: sqlite3.Connection) -> None:
    subj = _upsert_entity(db_conn, "Bailey")
    obj = _upsert_entity(db_conn, "Alex")
    rid = _upsert_entity_relation(db_conn, subj, "works_with", obj, node_id="n1")
    db_conn.commit()

    row = db_conn.execute("SELECT * FROM entity_relations WHERE id = ?", (rid,)).fetchone()
    assert rid == _entity_relation_id(subj, "works_with", obj)
    assert row["subject_entity_id"] == subj
    assert row["relation"] == "works_with"
    assert row["object_entity_id"] == obj
    assert row["node_id"] == "n1"


def test_upsert_entity_relation_insert_or_ignore(db_conn: sqlite3.Connection) -> None:
    """Re-recording the same triple is a no-op — no duplicate row, same id."""
    subj = _upsert_entity(db_conn, "Bailey")
    obj = _upsert_entity(db_conn, "Alex")
    rid1 = _upsert_entity_relation(db_conn, subj, "works_with", obj)
    rid2 = _upsert_entity_relation(db_conn, subj, "works_with", obj)
    db_conn.commit()

    assert rid1 == rid2
    count = db_conn.execute(
        "SELECT COUNT(*) FROM entity_relations WHERE id = ?", (rid1,)
    ).fetchone()[0]
    assert count == 1


def test_upsert_entity_relation_distinguishes_direction(db_conn: sqlite3.Connection) -> None:
    """Subject and object are not interchangeable -- A->B is a different edge than B->A."""
    a = _upsert_entity(db_conn, "Bailey")
    b = _upsert_entity(db_conn, "Alex")
    rid_ab = _upsert_entity_relation(db_conn, a, "works_with", b)
    rid_ba = _upsert_entity_relation(db_conn, b, "works_with", a)
    db_conn.commit()

    assert rid_ab != rid_ba
    assert db_conn.execute("SELECT COUNT(*) FROM entity_relations").fetchone()[0] == 2


def test_upsert_entity_relation_strips_relation_whitespace(db_conn: sqlite3.Connection) -> None:
    subj = _upsert_entity(db_conn, "Bailey")
    obj = _upsert_entity(db_conn, "Alex")
    rid = _upsert_entity_relation(db_conn, subj, "  works_with  ", obj)
    db_conn.commit()
    row = db_conn.execute(
        "SELECT relation FROM entity_relations WHERE id = ?", (rid,)
    ).fetchone()
    assert row["relation"] == "works_with"


# ---------------------------------------------------------------------------
# Decompose write path
# ---------------------------------------------------------------------------


async def test_decompose_writes_spo_and_entities(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    from remind_me_mcp.models import AtomicFact, DecomposeInput, EntityInput
    from remind_me_mcp.tools import remind_me_decompose

    memory_factory(
        content="Talked about dark mode preferences",
        category="dialog",
        capture_id="cap_ft04",
    )
    params = DecomposeInput(
        capture_id="cap_ft04",
        facts=[
            AtomicFact(
                content="Bailey prefers dark mode",
                subject="Bailey",
                predicate="prefers",
                object="dark mode",
                entities=[EntityInput(name="Bailey", kind="person")],
            ),
            AtomicFact(content="No structure on this one"),
        ],
    )
    result = json.loads(await remind_me_decompose(params))
    assert result["created"] == 2
    assert result["entities_linked"] == 1

    fact_id = result["fact_ids"][0]
    row = db_conn.execute(
        "SELECT subject, predicate, object FROM memories WHERE id = ?", (fact_id,)
    ).fetchone()
    assert (row["subject"], row["predicate"], row["object"]) == (
        "Bailey", "prefers", "dark mode",
    )

    ent = db_conn.execute(
        "SELECT * FROM entities WHERE id = ?", (_entity_id("Bailey"),)
    ).fetchone()
    assert ent is not None
    assert ent["kind"] == "person"
    link = db_conn.execute(
        "SELECT * FROM memory_entities WHERE memory_id = ? AND entity_id = ?",
        (fact_id, ent["id"]),
    ).fetchone()
    assert link is not None

    # The unstructured fact has NULL SPO and no mentions.
    plain = db_conn.execute(
        "SELECT subject FROM memories WHERE id = ?", (result["fact_ids"][1],)
    ).fetchone()
    assert plain["subject"] is None

    # "dark mode" isn't in the fact's entities list, so it never resolves to
    # a known entity -- no relation edge is written (best-effort, Phase 3).
    assert result["relations_linked"] == 0
    assert db_conn.execute("SELECT COUNT(*) FROM entity_relations").fetchone()[0] == 0


async def test_decompose_links_entity_relation_when_both_sides_resolve(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """Phase 3: an SPO triple whose subject AND object both name known
    entities also writes a typed entity_relations edge."""
    from remind_me_mcp.models import AtomicFact, DecomposeInput, EntityInput
    from remind_me_mcp.tools import remind_me_decompose

    memory_factory(
        content="Talked about who works with whom",
        category="dialog",
        capture_id="cap_ft04_rel",
    )
    params = DecomposeInput(
        capture_id="cap_ft04_rel",
        facts=[
            AtomicFact(
                content="Bailey works with Alex",
                subject="Bailey",
                predicate="works_with",
                object="Alex",
                entities=[
                    EntityInput(name="Bailey", kind="person"),
                    EntityInput(name="Alex", kind="person"),
                ],
            ),
        ],
    )
    result = json.loads(await remind_me_decompose(params))
    assert result["relations_linked"] == 1

    row = db_conn.execute(
        "SELECT subject_entity_id, relation, object_entity_id FROM entity_relations"
    ).fetchone()
    assert row["subject_entity_id"] == _entity_id("Bailey")
    assert row["relation"] == "works_with"
    assert row["object_entity_id"] == _entity_id("Alex")


# ---------------------------------------------------------------------------
# memory_add write path
# ---------------------------------------------------------------------------


async def test_memory_add_with_spo_and_entities(db_conn: sqlite3.Connection) -> None:
    from remind_me_mcp.models import EntityInput, MemoryAddInput
    from remind_me_mcp.tools import memory_add

    params = MemoryAddInput(
        content="remind_me syncs over Tailscale",
        subject="remind_me",
        predicate="syncs over",
        object="Tailscale",
        entities=[
            EntityInput(name="remind_me", kind="project"),
            EntityInput(name="Tailscale", kind="tool", aliases=["ts"]),
        ],
    )
    result = await memory_add(params)
    assert "Memory stored" in result

    row = db_conn.execute(
        "SELECT id, subject, predicate, object FROM memories"
    ).fetchone()
    assert (row["subject"], row["predicate"], row["object"]) == (
        "remind_me", "syncs over", "Tailscale",
    )
    assert db_conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 2
    links = db_conn.execute(
        "SELECT entity_id FROM memory_entities WHERE memory_id = ?", (row["id"],)
    ).fetchall()
    assert {r["entity_id"] for r in links} == {
        _entity_id("remind_me"), _entity_id("Tailscale"),
    }
    ts = db_conn.execute(
        "SELECT aliases FROM entities WHERE id = ?", (_entity_id("Tailscale"),)
    ).fetchone()
    assert json.loads(ts["aliases"]) == ["ts"]


async def test_memory_add_without_entities_unchanged(
    db_conn: sqlite3.Connection,
) -> None:
    from remind_me_mcp.models import MemoryAddInput
    from remind_me_mcp.tools import memory_add

    await memory_add(MemoryAddInput(content="plain memory"))
    row = db_conn.execute("SELECT subject, predicate, object FROM memories").fetchone()
    assert row["subject"] is None
    assert db_conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# remind_me_annotate
# ---------------------------------------------------------------------------


async def test_annotate_applies_spo_and_entities(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    from remind_me_mcp.models import AnnotateInput, EntityInput, MemoryAnnotation
    from remind_me_mcp.tools import remind_me_annotate

    mem = memory_factory(content="Bailey lives in Portland", category="fact")
    before = db_conn.execute(
        "SELECT updated_at FROM memories WHERE id = ?", (mem["id"],)
    ).fetchone()["updated_at"]

    params = AnnotateInput(annotations=[
        MemoryAnnotation(
            memory_id=mem["id"],
            subject="Bailey",
            predicate="lives in",
            object="Portland",
            entities=[
                EntityInput(name="Bailey", kind="person"),
                EntityInput(name="Portland", kind="place"),
            ],
        ),
    ])
    result = json.loads(await remind_me_annotate(params))
    assert result["annotated"] == 1
    assert result["errors"] == []
    assert result["results"][0]["entities_linked"] == 2
    # Phase 3: subject "Bailey" and object "Portland" both name entities just
    # upserted above, so a typed entity_relations edge is also written.
    assert result["results"][0]["relation_linked"] is True

    row = db_conn.execute(
        "SELECT subject, predicate, object, updated_at FROM memories WHERE id = ?",
        (mem["id"],),
    ).fetchone()
    assert (row["subject"], row["predicate"], row["object"]) == (
        "Bailey", "lives in", "Portland",
    )
    # updated_at bumped so sync propagates the annotation
    assert row["updated_at"] > before
    assert db_conn.execute(
        "SELECT COUNT(*) FROM memory_entities WHERE memory_id = ?", (mem["id"],)
    ).fetchone()[0] == 2
    rel = db_conn.execute(
        "SELECT subject_entity_id, relation, object_entity_id FROM entity_relations"
    ).fetchone()
    assert rel["subject_entity_id"] == _entity_id("Bailey")
    assert rel["relation"] == "lives in"
    assert rel["object_entity_id"] == _entity_id("Portland")


async def test_annotate_missing_memory_reports_error(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    from remind_me_mcp.models import AnnotateInput, MemoryAnnotation
    from remind_me_mcp.tools import remind_me_annotate

    mem = memory_factory(content="real memory")
    params = AnnotateInput(annotations=[
        MemoryAnnotation(memory_id="nope-404", subject="x"),
        MemoryAnnotation(memory_id=mem["id"], subject="y"),
    ])
    result = json.loads(await remind_me_annotate(params))
    assert result["annotated"] == 1
    assert result["errors"] == [{"memory_id": "nope-404", "error": "memory not found"}]


async def test_annotate_partial_fields_leave_others_untouched(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    from remind_me_mcp.models import AnnotateInput, MemoryAnnotation
    from remind_me_mcp.tools import remind_me_annotate

    mem = memory_factory(
        content="x", subject="OldSubject", predicate="OldPred", object="OldObj"
    )
    await remind_me_annotate(AnnotateInput(annotations=[
        MemoryAnnotation(memory_id=mem["id"], object="NewObj"),
    ]))
    row = db_conn.execute(
        "SELECT subject, predicate, object FROM memories WHERE id = ?", (mem["id"],)
    ).fetchone()
    assert (row["subject"], row["predicate"], row["object"]) == (
        "OldSubject", "OldPred", "NewObj",
    )


async def test_annotate_partial_update_links_relation_from_current_triple(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """A second, partial annotate call (only predicate changes) still links a
    relation using the memory's CURRENT subject/object, not just this call's
    (possibly omitted) fields -- Phase 3."""
    from remind_me_mcp.models import AnnotateInput, EntityInput, MemoryAnnotation
    from remind_me_mcp.tools import remind_me_annotate

    mem = memory_factory(content="Bailey knows Alex")
    await remind_me_annotate(AnnotateInput(annotations=[
        MemoryAnnotation(
            memory_id=mem["id"], subject="Bailey", predicate="knows", object="Alex",
            entities=[EntityInput(name="Bailey"), EntityInput(name="Alex")],
        ),
    ]))
    assert db_conn.execute("SELECT COUNT(*) FROM entity_relations").fetchone()[0] == 1

    # Second call only changes predicate; subject/object are omitted.
    result = json.loads(await remind_me_annotate(AnnotateInput(annotations=[
        MemoryAnnotation(memory_id=mem["id"], predicate="works_with"),
    ])))
    assert result["results"][0]["relation_linked"] is True

    row = db_conn.execute(
        "SELECT subject_entity_id, relation, object_entity_id FROM entity_relations "
        "ORDER BY created_at"
    ).fetchall()
    assert len(row) == 2  # the old "knows" edge stays; a new "works_with" edge is added
    relations = {r["relation"] for r in row}
    assert relations == {"knows", "works_with"}


# ---------------------------------------------------------------------------
# remind_me_extract_batch filtering
# ---------------------------------------------------------------------------


async def test_extract_batch_filters_sensibly(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    from remind_me_mcp.models import ExtractBatchInput
    from remind_me_mcp.tools import remind_me_extract_batch

    plain_fact = memory_factory(content="a plain fact", category="fact")
    document = memory_factory(content="an imported document", category="document")
    memory_factory(  # already has SPO -> excluded
        content="structured", category="fact",
        subject="s", predicate="p", object="o",
    )
    memory_factory(  # superseded -> excluded
        content="old fact", category="fact", superseded_by="something",
    )
    memory_factory(content="raw dialog", category="dialog")  # dialog -> excluded
    mentioned = memory_factory(content="has a mention", category="general")
    eid = _upsert_entity(db_conn, "remind_me")
    _link_memory_entity(db_conn, mentioned["id"], eid)
    db_conn.commit()

    result = json.loads(await remind_me_extract_batch(ExtractBatchInput()))
    ids = {m["id"] for m in result["memories"]}
    assert ids == {plain_fact["id"], document["id"]}
    assert result["total_unannotated"] == 2


async def test_extract_batch_respects_batch_size(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    from remind_me_mcp.models import ExtractBatchInput
    from remind_me_mcp.tools import remind_me_extract_batch

    for i in range(5):
        memory_factory(content=f"fact {i}", category="fact")
    result = json.loads(
        await remind_me_extract_batch(ExtractBatchInput(batch_size=2))
    )
    assert len(result["memories"]) == 2
    assert result["total_unannotated"] == 5


# ---------------------------------------------------------------------------
# Delete cleanup
# ---------------------------------------------------------------------------


async def test_memory_delete_cleans_up_mention_links(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    from remind_me_mcp.models import MemoryDeleteInput
    from remind_me_mcp.tools import memory_delete

    mem = memory_factory(content="to be deleted")
    other = memory_factory(content="stays around")
    eid = _upsert_entity(db_conn, "Shared Entity")
    _link_memory_entity(db_conn, mem["id"], eid)
    _link_memory_entity(db_conn, other["id"], eid)
    db_conn.commit()

    result = await memory_delete(MemoryDeleteInput(memory_id=mem["id"]))
    assert "deleted" in result

    rows = db_conn.execute("SELECT memory_id FROM memory_entities").fetchall()
    assert [r["memory_id"] for r in rows] == [other["id"]]
    # The entity itself survives — other memories still mention it.
    assert db_conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 1
