"""
Tests for remind_me_mcp.tools.contradictions — free-text contradiction
candidate surfacing (issue #201).

Covers the deterministic entity-bounded candidate pairing heuristic
(maintenance.py), the remind_me_contradiction_candidates tool's shape/
bounding/read-only-ness, and the maintenance nudge's new
"contradiction_candidates" queue -- following the exact conventions
test_recalibrate.py/test_maintenance.py already establish.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from remind_me_mcp import maintenance
from remind_me_mcp.db import _entity_id, _now_iso
from remind_me_mcp.models import ContradictionCandidatesInput
from remind_me_mcp.tools.contradictions import remind_me_contradiction_candidates

if TYPE_CHECKING:
    import sqlite3


def _link_entity(db: sqlite3.Connection, memory_id: str, entity_name: str) -> None:
    """Create (if needed) an entity and link a memory to it, mirroring the
    real write path (db._upsert_entity + db._link_memory_entity) closely
    enough for these tests without pulling in their full signatures."""
    entity_id = _entity_id(entity_name)
    now = _now_iso()
    db.execute(
        "INSERT OR IGNORE INTO entities (id, name, kind, aliases, created_at, updated_at) "
        "VALUES (?, ?, NULL, '[]', ?, ?)",
        (entity_id, entity_name, now, now),
    )
    db.execute(
        "INSERT OR IGNORE INTO memory_entities (memory_id, entity_id, created_at) VALUES (?, ?, ?)",
        (memory_id, entity_id, now),
    )
    db.commit()


# ---------------------------------------------------------------------------
# Candidate pairing heuristic (maintenance.CONTRADICTION_CANDIDATE_PAIRS_SQL)
# ---------------------------------------------------------------------------


def test_two_memories_sharing_an_entity_are_a_candidate_pair(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    m1 = memory_factory(content="I moved to Boston last month.")
    m2 = memory_factory(content="I live in Seattle now.")
    _link_entity(db_conn, m1["id"], "Alex")
    _link_entity(db_conn, m2["id"], "Alex")

    assert maintenance.pending_counts(db_conn)["contradiction_candidates"] == 1


def test_two_memories_sharing_no_entity_are_not_a_candidate_pair(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    m1 = memory_factory(content="I moved to Boston last month.")
    m2 = memory_factory(content="The VPN uses WireGuard.")
    _link_entity(db_conn, m1["id"], "Alex")
    _link_entity(db_conn, m2["id"], "WireGuard")

    assert maintenance.pending_counts(db_conn)["contradiction_candidates"] == 0


def test_pair_with_no_entity_links_at_all_is_not_a_candidate(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    memory_factory(content="I moved to Boston last month.")
    memory_factory(content="I live in Seattle now.")

    assert maintenance.pending_counts(db_conn)["contradiction_candidates"] == 0


def test_exact_triple_match_pair_cannot_coexist_and_is_not_re_surfaced(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """A pair sharing normalized (subject, predicate) with a different object
    is exactly what db._supersede_contradicting_facts already auto-resolves
    at write time -- by the time both would otherwise be live, one already
    has superseded_by set, so it drops out of every superseded_by IS NULL
    queue, this one included. Model that directly: the "loser" side carries
    superseded_by, so it's already excluded before the subject/predicate
    clause is even reached."""
    m1 = memory_factory(
        content="I live in Seattle.", subject="I", predicate="lives_in", object="Seattle"
    )
    m2 = memory_factory(
        content="I live in Boston.",
        subject="I",
        predicate="lives_in",
        object="Boston",
        superseded_by=m1["id"],
    )
    _link_entity(db_conn, m1["id"], "Home")
    _link_entity(db_conn, m2["id"], "Home")

    # m2 is superseded, so no live pair exists to surface.
    assert maintenance.pending_counts(db_conn)["contradiction_candidates"] == 0


def test_matching_subject_predicate_pair_is_excluded_even_if_both_are_live(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """Belt-and-suspenders: even if two live memories somehow carried a
    matching normalized (subject, predicate) -- which the write path should
    never actually allow to coexist -- the exclusion clause itself still
    keeps them out, rather than relying solely on the "can't coexist"
    argument above."""
    m1 = memory_factory(
        content="I live in Seattle.", subject="I", predicate="lives_in", object="Seattle"
    )
    m2 = memory_factory(
        content="I live in Boston.", subject="I", predicate="lives_in", object="Boston"
    )
    _link_entity(db_conn, m1["id"], "Home")
    _link_entity(db_conn, m2["id"], "Home")

    assert maintenance.pending_counts(db_conn)["contradiction_candidates"] == 0


def test_matching_subject_but_different_predicate_is_not_excluded(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """Different predicates ("lives_in" vs. "visited") are exactly the false-
    positive risk the exact-triple mechanism deliberately does NOT resolve
    (README: "a differently-worded predicate never contradicts") -- so this
    queue should still surface it for review."""
    m1 = memory_factory(
        content="I live in Seattle.", subject="I", predicate="lives_in", object="Seattle"
    )
    m2 = memory_factory(
        content="I visited Boston.", subject="I", predicate="visited", object="Boston"
    )
    _link_entity(db_conn, m1["id"], "Home")
    _link_entity(db_conn, m2["id"], "Home")

    assert maintenance.pending_counts(db_conn)["contradiction_candidates"] == 1


def test_superseded_memory_is_excluded(db_conn: sqlite3.Connection, memory_factory) -> None:
    m1 = memory_factory(content="I moved to Boston.", superseded_by="some-other-id")
    m2 = memory_factory(content="I live in Seattle.")
    _link_entity(db_conn, m1["id"], "Alex")
    _link_entity(db_conn, m2["id"], "Alex")

    assert maintenance.pending_counts(db_conn)["contradiction_candidates"] == 0


def test_deleted_memory_is_excluded(db_conn: sqlite3.Connection, memory_factory) -> None:
    m1 = memory_factory(content="I moved to Boston.", deleted_at=_now_iso())
    m2 = memory_factory(content="I live in Seattle.")
    _link_entity(db_conn, m1["id"], "Alex")
    _link_entity(db_conn, m2["id"], "Alex")

    assert maintenance.pending_counts(db_conn)["contradiction_candidates"] == 0


def test_dialog_memories_are_excluded(db_conn: sqlite3.Connection, memory_factory) -> None:
    m1 = memory_factory(content="raw transcript ...", category="dialog")
    m2 = memory_factory(content="I live in Seattle.")
    _link_entity(db_conn, m1["id"], "Alex")
    _link_entity(db_conn, m2["id"], "Alex")

    assert maintenance.pending_counts(db_conn)["contradiction_candidates"] == 0


def test_a_memory_is_never_paired_with_itself(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    m1 = memory_factory(content="I moved to Boston.")
    _link_entity(db_conn, m1["id"], "Alex")

    assert maintenance.pending_counts(db_conn)["contradiction_candidates"] == 0


def test_shared_entity_yields_one_pair_not_a_duplicate_per_extra_shared_entity(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """DISTINCT on (id_a, id_b): sharing two entities must not double-count
    the same pair."""
    m1 = memory_factory(content="I moved to Boston with Alex.")
    m2 = memory_factory(content="I live in Seattle with Alex.")
    _link_entity(db_conn, m1["id"], "Alex")
    _link_entity(db_conn, m2["id"], "Alex")
    _link_entity(db_conn, m1["id"], "Boston")
    _link_entity(db_conn, m2["id"], "Boston")

    assert maintenance.pending_counts(db_conn)["contradiction_candidates"] == 1


def test_batch_tool_and_nudge_share_one_pairing_query() -> None:
    """Same discipline test_recalibrate.py already applies: the tool must
    import the pairing SQL, not copy it, or the nudge count and the batch the
    tool returns could silently drift apart."""
    from remind_me_mcp.tools import contradictions as contradictions_mod

    assert (
        contradictions_mod._CONTRADICTION_CANDIDATE_PAIRS_SQL
        is maintenance.CONTRADICTION_CANDIDATE_PAIRS_SQL
    )


# ---------------------------------------------------------------------------
# remind_me_contradiction_candidates
# ---------------------------------------------------------------------------


async def test_contradiction_candidates_returns_matching_pairs(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    m1 = memory_factory(content="I moved to Boston last month.")
    m2 = memory_factory(content="I live in Seattle now.")
    _link_entity(db_conn, m1["id"], "Alex")
    _link_entity(db_conn, m2["id"], "Alex")

    result = json.loads(
        await remind_me_contradiction_candidates(ContradictionCandidatesInput())
    )

    assert result["total_candidates"] == 1
    assert len(result["candidates"]) == 1
    pair = result["candidates"][0]
    ids = {pair["memory_a"]["id"], pair["memory_b"]["id"]}
    assert ids == {m1["id"], m2["id"]}
    assert "Boston" in pair["memory_a"]["content_snippet"] or "Boston" in pair["memory_b"]["content_snippet"]
    assert pair["shared_entities"] == ["Alex"]


async def test_contradiction_candidates_respects_limit(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    for i in range(5):
        m1 = memory_factory(content=f"Fact A{i} about a shared thing.")
        m2 = memory_factory(content=f"Fact B{i} about the same shared thing.")
        _link_entity(db_conn, m1["id"], f"Topic{i}")
        _link_entity(db_conn, m2["id"], f"Topic{i}")

    result = json.loads(
        await remind_me_contradiction_candidates(ContradictionCandidatesInput(limit=2))
    )
    assert result["total_candidates"] == 5
    assert len(result["candidates"]) == 2


async def test_contradiction_candidates_empty_when_nothing_qualifies(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    memory_factory(content="Just a note.")
    result = json.loads(
        await remind_me_contradiction_candidates(ContradictionCandidatesInput())
    )
    assert result["total_candidates"] == 0
    assert result["candidates"] == []


async def test_contradiction_candidates_default_limit_is_20() -> None:
    assert ContradictionCandidatesInput().limit == 20


async def test_contradiction_candidates_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError):
        ContradictionCandidatesInput(limit=5, bogus_field="nope")


async def test_contradiction_candidates_is_read_only(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """Calling the tool must not modify, delete, or supersede anything --
    only the calling session's subsequent use of existing tools should."""
    m1 = memory_factory(content="I moved to Boston last month.")
    m2 = memory_factory(content="I live in Seattle now.")
    _link_entity(db_conn, m1["id"], "Alex")
    _link_entity(db_conn, m2["id"], "Alex")

    before = {
        row["id"]: dict(row)
        for row in db_conn.execute("SELECT * FROM memories ORDER BY id").fetchall()
    }

    await remind_me_contradiction_candidates(ContradictionCandidatesInput())

    after = {
        row["id"]: dict(row)
        for row in db_conn.execute("SELECT * FROM memories ORDER BY id").fetchall()
    }
    assert before == after
    assert all(row["superseded_by"] is None for row in after.values())
    assert all(row["deleted_at"] is None for row in after.values())


# ---------------------------------------------------------------------------
# Maintenance nudge integration
# ---------------------------------------------------------------------------


def _add_contradiction_candidates(db: sqlite3.Connection, memory_factory, n: int) -> None:
    """Create *n* contradiction-candidate pairs (2n memories).

    ``memory_type`` is set to a non-default value and the wiki compile
    watermark is advanced past every created row afterward, so this helper
    exercises only the contradiction_candidates queue -- 2n memories would
    otherwise also cross the unclassified_memories (default memory_type is
    'unclassified') and pending_wiki_compile thresholds for anything but a
    tiny n, which would make these nudge-shape assertions flaky/wrong for
    reasons unrelated to what they're testing.
    """
    for i in range(n):
        m1 = memory_factory(
            content=f"Fact A{i} about a shared thing.", memory_type="fact"
        )
        m2 = memory_factory(
            content=f"Fact B{i} about the same shared thing.", memory_type="fact"
        )
        _link_entity(db, m1["id"], f"Topic{i}")
        _link_entity(db, m2["id"], f"Topic{i}")

    from remind_me_mcp import wiki

    wiki.set_meta(wiki.COMPILE_WATERMARK_KEY, _now_iso())


def test_pending_counts_includes_the_contradiction_queue(db_conn: sqlite3.Connection) -> None:
    counts = maintenance.pending_counts(db_conn)
    assert "contradiction_candidates" in counts


def test_no_nudge_below_the_threshold(db_conn: sqlite3.Connection, memory_factory) -> None:
    _add_contradiction_candidates(db_conn, memory_factory, maintenance.NUDGE_THRESHOLD - 1)
    assert maintenance.maybe_maintenance_notice(db_conn) is None


def test_nudge_at_the_threshold_names_the_queue_and_its_prompt(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    _add_contradiction_candidates(db_conn, memory_factory, maintenance.NUDGE_THRESHOLD)
    notice = maintenance.maybe_maintenance_notice(db_conn)
    assert notice is not None
    assert "possibly-contradictory" in notice
    assert "review_contradictions" in notice


@pytest.mark.asyncio
async def test_status_reports_the_contradiction_backlog(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    from remind_me_mcp.tools import remind_me_server_status

    _add_contradiction_candidates(db_conn, memory_factory, 4)
    result = await remind_me_server_status()
    assert "contradiction candidates" in result


# ---------------------------------------------------------------------------
# No new scheduler wiring (issue #200's architectural correction, applied
# the same way here per issue #201)
# ---------------------------------------------------------------------------


def test_contradictions_module_does_not_import_scheduler() -> None:
    """This is a read-only, on-demand tool plus a deterministic pairing
    query -- not a scheduler-hosted background job, per the same
    architectural correction issue #200 applied first (see
    tools/contradictions.py's module docstring)."""
    import ast

    import remind_me_mcp.tools.contradictions as contradictions_mod

    assert "scheduler" not in contradictions_mod.__dict__

    with open(contradictions_mod.__file__, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=contradictions_mod.__file__)

    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any("scheduler" in m for m in imported_modules)
