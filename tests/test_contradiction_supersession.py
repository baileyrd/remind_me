"""
Tests for contradiction-based supersession (gap #5).

Supersession previously only happened via similarity-merge
(remind_me_consolidate). This adds a deterministic SPO-triple conflict
check: a new/updated fact whose (subject, predicate) matches an existing
non-superseded, non-deleted memory but whose object differs supersedes that
memory -- e.g. "I moved to Boston" supersedes "I live in Seattle" even
though the two statements share no text. Wired into memory_add,
remind_me_decompose, and remind_me_annotate (every place an SPO triple gets
attached to a memory).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from remind_me_mcp.db import _now_iso, _supersede_contradicting_facts

if TYPE_CHECKING:
    import sqlite3

# ---------------------------------------------------------------------------
# _supersede_contradicting_facts (db.py) — unit tests
# ---------------------------------------------------------------------------


def _insert(db: sqlite3.Connection, mem_id: str, subject=None, predicate=None, object=None, **extra) -> None:
    now = _now_iso()
    db.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata,
                                 created_at, updated_at, subject, predicate, object)
           VALUES (?, ?, 'general', '[]', 'manual', '{}', ?, ?, ?, ?, ?)""",
        (mem_id, f"content for {mem_id}", now, now, subject, predicate, object),
    )
    if extra:
        sets = ", ".join(f"{k} = ?" for k in extra)
        db.execute(f"UPDATE memories SET {sets} WHERE id = ?", (*extra.values(), mem_id))
    db.commit()


def test_contradicting_object_supersedes(db_conn: sqlite3.Connection) -> None:
    """Same subject+predicate, different object -- the headline case."""
    _insert(db_conn, "seattle", subject="I", predicate="lives in", object="Seattle")

    now = _now_iso()
    superseded = _supersede_contradicting_facts(
        db_conn, "boston", "I", "lives in", "Boston", now
    )

    assert superseded == ["seattle"]
    row = db_conn.execute(
        "SELECT superseded_by FROM memories WHERE id = 'seattle'"
    ).fetchone()
    assert row["superseded_by"] == "boston"


def test_same_fact_restated_is_not_a_contradiction(db_conn: sqlite3.Connection) -> None:
    """Same subject+predicate+object -- the fact is restated, not contradicted."""
    _insert(db_conn, "seattle", subject="I", predicate="lives in", object="Seattle")

    superseded = _supersede_contradicting_facts(
        db_conn, "seattle-again", "I", "lives in", "Seattle", _now_iso()
    )

    assert superseded == []
    row = db_conn.execute(
        "SELECT superseded_by FROM memories WHERE id = 'seattle'"
    ).fetchone()
    assert row["superseded_by"] is None


def test_different_predicate_does_not_supersede(db_conn: sqlite3.Connection) -> None:
    """'I visited Boston' (predicate=visited) must not supersede 'I live in
    Seattle' (predicate=lives in) -- false-positive avoidance, the exact
    case named in the review."""
    _insert(db_conn, "seattle", subject="I", predicate="lives in", object="Seattle")

    superseded = _supersede_contradicting_facts(
        db_conn, "boston-visit", "I", "visited", "Boston", _now_iso()
    )

    assert superseded == []
    row = db_conn.execute(
        "SELECT superseded_by FROM memories WHERE id = 'seattle'"
    ).fetchone()
    assert row["superseded_by"] is None


def test_different_subject_does_not_supersede(db_conn: sqlite3.Connection) -> None:
    _insert(db_conn, "seattle", subject="I", predicate="lives in", object="Seattle")

    superseded = _supersede_contradicting_facts(
        db_conn, "other-person", "Alex", "lives in", "Boston", _now_iso()
    )

    assert superseded == []


def test_comparison_is_case_and_whitespace_insensitive(db_conn: sqlite3.Connection) -> None:
    _insert(db_conn, "seattle", subject=" I ", predicate="LIVES IN", object="Seattle")

    superseded = _supersede_contradicting_facts(
        db_conn, "boston", "i", "lives in", "Boston", _now_iso()
    )

    assert superseded == ["seattle"]


def test_already_superseded_candidate_is_ignored(db_conn: sqlite3.Connection) -> None:
    """A candidate that's already superseded (by an unrelated event, e.g.
    consolidation) isn't touched again."""
    _insert(
        db_conn, "seattle", subject="I", predicate="lives in", object="Seattle",
        superseded_by="something-else",
    )

    superseded = _supersede_contradicting_facts(
        db_conn, "boston", "I", "lives in", "Boston", _now_iso()
    )

    assert superseded == []
    row = db_conn.execute(
        "SELECT superseded_by FROM memories WHERE id = 'seattle'"
    ).fetchone()
    assert row["superseded_by"] == "something-else"  # unchanged


def test_deleted_candidate_is_ignored(db_conn: sqlite3.Connection) -> None:
    """A soft-deleted (tombstoned) memory isn't resurrected-by-supersession."""
    _insert(
        db_conn, "seattle", subject="I", predicate="lives in", object="Seattle",
        deleted_at=_now_iso(),
    )

    superseded = _supersede_contradicting_facts(
        db_conn, "boston", "I", "lives in", "Boston", _now_iso()
    )

    assert superseded == []


def test_missing_triple_fields_are_a_noop(db_conn: sqlite3.Connection) -> None:
    _insert(db_conn, "seattle", subject="I", predicate="lives in", object="Seattle")

    assert _supersede_contradicting_facts(db_conn, "x", None, "lives in", "Boston", _now_iso()) == []
    assert _supersede_contradicting_facts(db_conn, "x", "I", None, "Boston", _now_iso()) == []
    assert _supersede_contradicting_facts(db_conn, "x", "I", "lives in", None, _now_iso()) == []
    assert _supersede_contradicting_facts(db_conn, "x", "", "lives in", "Boston", _now_iso()) == []


def test_new_memory_excluded_from_its_own_candidate_search(db_conn: sqlite3.Connection) -> None:
    """A memory can't supersede itself (relevant for remind_me_annotate,
    which re-checks a memory's own just-updated triple)."""
    _insert(db_conn, "m1", subject="I", predicate="lives in", object="Seattle")

    superseded = _supersede_contradicting_facts(
        db_conn, "m1", "I", "lives in", "Seattle", _now_iso()
    )

    assert superseded == []


def test_multiple_contradicting_facts_all_superseded(db_conn: sqlite3.Connection) -> None:
    """More than one prior fact sharing the same subject+predicate (e.g. from
    duplicate/independent captures) are all superseded by the new one."""
    _insert(db_conn, "seattle-a", subject="I", predicate="lives in", object="Seattle")
    _insert(db_conn, "seattle-b", subject="I", predicate="lives in", object="Seattle")

    superseded = _supersede_contradicting_facts(
        db_conn, "boston", "I", "lives in", "Boston", _now_iso()
    )

    assert set(superseded) == {"seattle-a", "seattle-b"}


def test_updated_at_bumped_on_superseded_row(db_conn: sqlite3.Connection) -> None:
    """The superseded row's updated_at is bumped so the change propagates
    over sync, same as any other supersession."""
    _insert(db_conn, "seattle", subject="I", predicate="lives in", object="Seattle")
    before = db_conn.execute(
        "SELECT updated_at FROM memories WHERE id = 'seattle'"
    ).fetchone()["updated_at"]

    later = _now_iso()
    _supersede_contradicting_facts(db_conn, "boston", "I", "lives in", "Boston", later)

    after = db_conn.execute(
        "SELECT updated_at FROM memories WHERE id = 'seattle'"
    ).fetchone()["updated_at"]
    assert after == later
    assert after >= before


# ---------------------------------------------------------------------------
# memory_add (crud.py) integration
# ---------------------------------------------------------------------------


async def test_memory_add_supersedes_contradicting_fact(db_conn: sqlite3.Connection) -> None:
    from remind_me_mcp.models import MemoryAddInput
    from remind_me_mcp.tools import memory_add

    await memory_add(MemoryAddInput(
        content="I live in Seattle", subject="I", predicate="lives in", object="Seattle",
    ))
    seattle_id = db_conn.execute(
        "SELECT id FROM memories WHERE content = 'I live in Seattle'"
    ).fetchone()["id"]

    result = await memory_add(MemoryAddInput(
        content="I moved to Boston", subject="I", predicate="lives in", object="Boston",
    ))

    assert "superseded" in result.lower()
    row = db_conn.execute(
        "SELECT superseded_by FROM memories WHERE id = ?", (seattle_id,)
    ).fetchone()
    assert row["superseded_by"] is not None


async def test_memory_add_visiting_does_not_supersede_living(db_conn: sqlite3.Connection) -> None:
    """The exact false-positive case named in the review."""
    from remind_me_mcp.models import MemoryAddInput
    from remind_me_mcp.tools import memory_add

    await memory_add(MemoryAddInput(
        content="I live in Seattle", subject="I", predicate="lives in", object="Seattle",
    ))
    seattle_id = db_conn.execute(
        "SELECT id FROM memories WHERE content = 'I live in Seattle'"
    ).fetchone()["id"]

    result = await memory_add(MemoryAddInput(
        content="I visited Boston", subject="I", predicate="visited", object="Boston",
    ))

    assert "superseded" not in result.lower()
    row = db_conn.execute(
        "SELECT superseded_by FROM memories WHERE id = ?", (seattle_id,)
    ).fetchone()
    assert row["superseded_by"] is None


async def test_memory_add_without_spo_triggers_no_supersession(db_conn: sqlite3.Connection) -> None:
    """A plain memory_add with no subject/predicate/object never triggers a
    check (the common case: most memories aren't structured facts)."""
    from remind_me_mcp.models import MemoryAddInput
    from remind_me_mcp.tools import memory_add

    await memory_add(MemoryAddInput(
        content="I live in Seattle", subject="I", predicate="lives in", object="Seattle",
    ))
    result = await memory_add(MemoryAddInput(content="Just a plain note, no triple"))

    assert "superseded" not in result.lower()


async def test_superseded_fact_excluded_from_search(db_conn_with_vec, mock_embedder) -> None:
    """End-to-end: the superseded fact is invisible to search/structured
    lookup after contradiction-based supersession, same as any other
    supersession."""
    from remind_me_mcp.models import MemoryAddInput, MemorySearchInput
    from remind_me_mcp.tools import memory_add, memory_search

    await memory_add(MemoryAddInput(
        content="I live in Seattle", subject="I", predicate="lives in", object="Seattle",
    ))
    await memory_add(MemoryAddInput(
        content="I moved to Boston", subject="I", predicate="lives in", object="Boston",
    ))

    result = await memory_search(MemorySearchInput(query='subject:"I" predicate:"lives in"'))
    assert "Boston" in result
    assert "Seattle" not in result


# ---------------------------------------------------------------------------
# remind_me_decompose integration
# ---------------------------------------------------------------------------


async def test_decompose_supersedes_contradicting_fact(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    from remind_me_mcp.models import AtomicFact, DecomposeInput
    from remind_me_mcp.tools import remind_me_decompose

    memory_factory(content="Bailey's whereabouts", category="dialog", capture_id="cap_spo")
    await remind_me_decompose(DecomposeInput(
        capture_id="cap_spo",
        facts=[AtomicFact(content="Bailey lives in Seattle", subject="Bailey", predicate="lives in", object="Seattle")],
    ))
    seattle_id = db_conn.execute(
        "SELECT id FROM memories WHERE object = 'Seattle'"
    ).fetchone()["id"]

    memory_factory(content="Bailey's whereabouts, take 2", category="dialog", capture_id="cap_spo2")
    result = json.loads(await remind_me_decompose(DecomposeInput(
        capture_id="cap_spo2",
        facts=[AtomicFact(content="Bailey moved to Boston", subject="Bailey", predicate="lives in", object="Boston")],
    )))

    assert result["superseded_ids"] == [seattle_id]
    row = db_conn.execute(
        "SELECT superseded_by FROM memories WHERE id = ?", (seattle_id,)
    ).fetchone()
    assert row["superseded_by"] is not None


# ---------------------------------------------------------------------------
# remind_me_annotate integration
# ---------------------------------------------------------------------------


async def test_annotate_supersedes_contradicting_fact(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    from remind_me_mcp.models import AnnotateInput, MemoryAnnotation
    from remind_me_mcp.tools import remind_me_annotate

    old = memory_factory(content="I live in Seattle")
    await remind_me_annotate(AnnotateInput(annotations=[
        MemoryAnnotation(memory_id=old["id"], subject="I", predicate="lives in", object="Seattle"),
    ]))

    new = memory_factory(content="I moved to Boston")
    result = json.loads(await remind_me_annotate(AnnotateInput(annotations=[
        MemoryAnnotation(memory_id=new["id"], subject="I", predicate="lives in", object="Boston"),
    ])))

    assert result["results"][0]["superseded_ids"] == [old["id"]]
    row = db_conn.execute(
        "SELECT superseded_by FROM memories WHERE id = ?", (old["id"],)
    ).fetchone()
    assert row["superseded_by"] == new["id"]


async def test_annotate_partial_annotation_rechecks_full_current_triple(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """Annotating only `object` (subject/predicate already set from an
    earlier annotation) still triggers the check against the memory's full,
    now-current triple -- not just the partial fields in this call."""
    from remind_me_mcp.models import AnnotateInput, MemoryAnnotation
    from remind_me_mcp.tools import remind_me_annotate

    old = memory_factory(content="I live in Seattle")
    await remind_me_annotate(AnnotateInput(annotations=[
        MemoryAnnotation(memory_id=old["id"], subject="I", predicate="lives in", object="Seattle"),
    ]))

    new = memory_factory(content="I moved to Boston")
    # First set subject/predicate only...
    await remind_me_annotate(AnnotateInput(annotations=[
        MemoryAnnotation(memory_id=new["id"], subject="I", predicate="lives in"),
    ]))
    # ...then set object in a separate call. The contradiction only becomes
    # detectable once the full triple (I, lives in, Boston) is assembled.
    result = json.loads(await remind_me_annotate(AnnotateInput(annotations=[
        MemoryAnnotation(memory_id=new["id"], object="Boston"),
    ])))

    assert result["results"][0]["superseded_ids"] == [old["id"]]
