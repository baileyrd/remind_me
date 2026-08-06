"""
Tests for the sensitive-memory convenience flag (issue #195).

Covers the v25->v26 schema migration (memories.sensitive + memory_revisions.
sensitive), remind_me_add/remind_me_update setting the flag, default
exclusion from remind_me_search/remind_me_list with include_sensitive
opt-in, remind_me_get's unaffected direct lookup, remind_me_digest's
unconditional exclusion (no override), remind_me_wiki_compile's unconditional
exclusion, and the sensitive-toggle revision-snapshot/revert round-trip
(issue #187 integration). Follows test_tombstones.py/test_history.py's shape:
db_conn fixture for an isolated in-memory database, memory_factory for
fixture rows.

NOT tested here (deliberately, per this feature's own scope): anything
resembling access control, encryption, or an audit trail beyond the existing
memory_revisions mechanism — see README.md's "Sensitive Memories" section and
ARCHITECTURE.md's Non-goals for why this is a "don't surface by default"
convenience filter, not a security boundary.
"""

from __future__ import annotations

import json
import sqlite3

import pytest  # noqa: TC002 — used at runtime via pytest.MonkeyPatch annotations

from remind_me_mcp import digest
from remind_me_mcp.db import _ensure_schema
from remind_me_mcp.models import (
    MemoryAddInput,
    MemoryListInput,
    MemorySearchInput,
    MemoryUpdateInput,
    RevertInput,
    RevisionHistoryInput,
)
from remind_me_mcp.tools import (
    memory_add,
    memory_get,
    memory_list,
    memory_search,
    memory_update,
    remind_me_history,
    remind_me_revert,
)

# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


def test_v25_to_v26_adds_sensitive_column_to_memories(db_conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in db_conn.execute("PRAGMA table_info(memories)").fetchall()}
    assert "sensitive" in cols


def test_v25_to_v26_adds_sensitive_column_to_memory_revisions(
    db_conn: sqlite3.Connection,
) -> None:
    cols = {r["name"] for r in db_conn.execute("PRAGMA table_info(memory_revisions)").fetchall()}
    assert "sensitive" in cols


def test_v25_to_v26_defaults_existing_rows_to_not_sensitive(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """A row inserted with no explicit sensitive value defaults to 0/false."""
    mem = memory_factory(content="plain memory")
    row = db_conn.execute(
        "SELECT sensitive FROM memories WHERE id = ?", (mem["id"],)
    ).fetchone()
    assert row["sensitive"] == 0


def test_v25_to_v26_is_idempotent() -> None:
    """Running the migration on an already-migrated DB (or twice) doesn't error."""
    from remind_me_mcp.db import _migrate_schema

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    _ensure_schema(db)
    _migrate_schema(db)  # second run — must be a safe no-op
    cols = {r["name"] for r in db.execute("PRAGMA table_info(memories)").fetchall()}
    assert "sensitive" in cols
    db.close()


def test_schema_version_is_current(db_conn: sqlite3.Connection) -> None:
    """A DB migrated through the sensitive-flag migration (v26) lands on the
    current schema version (v29, issue #220's reference memory_type) --
    updated alongside each later schema bump, mirroring test_db.py's own
    test_schema_version_is_current."""
    from remind_me_mcp.db import _SCHEMA_VERSION

    assert _SCHEMA_VERSION == 29
    version = db_conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 29


def test_sensitive_included_in_outbox_payload_columns() -> None:
    """Rides the normal sync outbox trigger like any other genuine content
    field (e.g. remind_at) -- see db._migrate_v25_to_v26's docstring."""
    from remind_me_mcp.db import _OUTBOX_PAYLOAD_COLUMNS

    assert "sensitive" in _OUTBOX_PAYLOAD_COLUMNS


# ---------------------------------------------------------------------------
# remind_me_add / remind_me_update set the flag
# ---------------------------------------------------------------------------


async def test_add_without_sensitive_defaults_to_false(db_conn: sqlite3.Connection) -> None:
    """Backward compatible: a plain add with no sensitive param is not sensitive."""
    msg = await memory_add(MemoryAddInput(content="an ordinary fact"))
    mem_id = msg.split("`")[1]
    row = db_conn.execute("SELECT sensitive FROM memories WHERE id = ?", (mem_id,)).fetchone()
    assert row["sensitive"] == 0
    assert "sensitive" not in msg.lower() or "excluded from search" not in msg.lower()


async def test_add_with_sensitive_true_sets_flag(db_conn: sqlite3.Connection) -> None:
    msg = await memory_add(MemoryAddInput(content="a private note", sensitive=True))
    mem_id = msg.split("`")[1]
    row = db_conn.execute("SELECT sensitive FROM memories WHERE id = ?", (mem_id,)).fetchone()
    assert row["sensitive"] == 1
    assert "Marked sensitive" in msg


async def test_update_without_sensitive_param_leaves_flag_unchanged(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    mem = memory_factory(content="already sensitive", sensitive=1)
    await memory_update(MemoryUpdateInput(memory_id=mem["id"], category="work"))
    row = db_conn.execute(
        "SELECT sensitive FROM memories WHERE id = ?", (mem["id"],)
    ).fetchone()
    assert row["sensitive"] == 1  # unchanged, since sensitive was omitted


async def test_update_sensitive_true_sets_flag(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    mem = memory_factory(content="not sensitive yet", sensitive=0)
    msg = await memory_update(MemoryUpdateInput(memory_id=mem["id"], sensitive=True))
    row = db_conn.execute(
        "SELECT sensitive FROM memories WHERE id = ?", (mem["id"],)
    ).fetchone()
    assert row["sensitive"] == 1
    assert "Marked sensitive" in msg


async def test_update_sensitive_false_clears_flag(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    mem = memory_factory(content="currently sensitive", sensitive=1)
    msg = await memory_update(MemoryUpdateInput(memory_id=mem["id"], sensitive=False))
    row = db_conn.execute(
        "SELECT sensitive FROM memories WHERE id = ?", (mem["id"],)
    ).fetchone()
    assert row["sensitive"] == 0
    assert "Cleared sensitive" in msg


# ---------------------------------------------------------------------------
# remind_me_search default-excludes sensitive memories; include_sensitive opts in
# ---------------------------------------------------------------------------


async def test_search_excludes_sensitive_by_default(db_conn: sqlite3.Connection) -> None:
    await memory_add(MemoryAddInput(content="zzqx classified launch codes", sensitive=True))
    await memory_add(MemoryAddInput(content="zzqx public release notes", sensitive=False))

    result = await memory_search(MemorySearchInput(query="zzqx"))

    assert "public release notes" in result
    assert "classified launch codes" not in result


async def test_search_include_sensitive_true_surfaces_it(db_conn: sqlite3.Connection) -> None:
    await memory_add(MemoryAddInput(content="zzqy classified launch codes", sensitive=True))

    result = await memory_search(
        MemorySearchInput(query="zzqy", include_sensitive=True)
    )

    assert "classified launch codes" in result


async def test_search_json_format_respects_sensitive_default_exclusion(
    db_conn: sqlite3.Connection,
) -> None:
    await memory_add(MemoryAddInput(content="zzqz secret project", sensitive=True))

    from remind_me_mcp.models import ResponseFormat

    result = await memory_search(
        MemorySearchInput(query="zzqz", response_format=ResponseFormat.JSON)
    )
    payload = json.loads(result)
    assert payload["returned"] == 0

    included = await memory_search(
        MemorySearchInput(
            query="zzqz", response_format=ResponseFormat.JSON, include_sensitive=True
        )
    )
    payload2 = json.loads(included)
    assert payload2["returned"] == 1


# ---------------------------------------------------------------------------
# remind_me_list default-excludes sensitive memories; include_sensitive opts in
# ---------------------------------------------------------------------------


async def test_list_excludes_sensitive_by_default(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    memory_factory(content="visible memory", category="notes", sensitive=0)
    memory_factory(content="hidden memory", category="notes", sensitive=1)

    result = await memory_list(MemoryListInput(category="notes"))

    assert "visible memory" in result
    assert "hidden memory" not in result


async def test_list_include_sensitive_true_surfaces_it(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    memory_factory(content="hidden memory", category="notes", sensitive=1)

    result = await memory_list(
        MemoryListInput(category="notes", include_sensitive=True)
    )

    assert "hidden memory" in result


# ---------------------------------------------------------------------------
# remind_me_get is unaffected by the sensitive flag (direct id lookup)
# ---------------------------------------------------------------------------


async def test_get_returns_sensitive_memory_regardless(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    mem = memory_factory(content="a sensitive fact", sensitive=1)

    result = await memory_get(mem["id"])

    assert "a sensitive fact" in result


# ---------------------------------------------------------------------------
# remind_me_digest always excludes sensitive memories, no override
# ---------------------------------------------------------------------------


def test_digest_recent_additions_excludes_sensitive(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    memory_factory(content="visible addition", sensitive=0)
    memory_factory(content="sensitive addition", sensitive=1)

    data = digest.build_digest_data(db_conn)

    contents = [m["content"] for m in data["recent_memories"]]
    assert "visible addition" in contents
    assert "sensitive addition" not in contents
    assert data["recent_total"] == 1


def test_digest_recent_total_excludes_sensitive_beyond_the_capped_list(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """recent_total (the uncapped count) also excludes sensitive rows, not
    just the capped recent_memories list."""
    for i in range(3):
        memory_factory(content=f"visible {i}", sensitive=0)
    for i in range(2):
        memory_factory(content=f"sensitive {i}", sensitive=1)

    data = digest.build_digest_data(db_conn)

    assert data["recent_total"] == 3


# ---------------------------------------------------------------------------
# remind_me_wiki_compile always excludes sensitive memories, no override
# ---------------------------------------------------------------------------


async def test_wiki_compile_brief_excludes_sensitive_memories(
    db_conn: sqlite3.Connection, memory_factory, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import remind_me_mcp.config as _cfg
    from remind_me_mcp.models import WikiCompileInput
    from remind_me_mcp.tools.wiki import wiki_compile

    monkeypatch.setattr(_cfg, "WIKI_DIR", tmp_path / "wiki")

    memory_factory(content="Bailey prefers dark mode.", sensitive=0)
    memory_factory(content="Bailey's private diary entry.", sensitive=1)

    brief = await wiki_compile(WikiCompileInput())

    assert "Bailey prefers dark mode." in brief
    assert "Bailey's private diary entry." not in brief


def test_pending_compile_count_excludes_sensitive(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    from remind_me_mcp import wiki

    memory_factory(content="visible", sensitive=0)
    memory_factory(content="hidden", sensitive=1)

    assert wiki.pending_compile_count() == 1


# ---------------------------------------------------------------------------
# Revision tracking / revert integration (issue #187)
# ---------------------------------------------------------------------------


async def test_toggling_sensitive_creates_a_revision_snapshot(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    mem = memory_factory(content="a fact", sensitive=0)
    await memory_update(MemoryUpdateInput(memory_id=mem["id"], sensitive=True))

    rows = db_conn.execute(
        "SELECT * FROM memory_revisions WHERE memory_id = ?", (mem["id"],)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["sensitive"] == 0  # pre-edit value captured


async def test_toggling_sensitive_to_same_value_creates_no_revision(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    mem = memory_factory(content="a fact", sensitive=1)
    await memory_update(MemoryUpdateInput(memory_id=mem["id"], sensitive=True))

    count = db_conn.execute(
        "SELECT COUNT(*) FROM memory_revisions WHERE memory_id = ?", (mem["id"],)
    ).fetchone()[0]
    assert count == 0


async def test_remind_me_history_shows_sensitive_toggle_revision(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    mem = memory_factory(content="a fact", sensitive=0)
    await memory_update(MemoryUpdateInput(memory_id=mem["id"], sensitive=True))

    result = await remind_me_history(RevisionHistoryInput(memory_id=mem["id"]))

    assert "Revision" in result


async def test_remind_me_revert_restores_prior_sensitivity(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    mem = memory_factory(content="a fact", sensitive=0)
    await memory_update(MemoryUpdateInput(memory_id=mem["id"], sensitive=True))

    row = db_conn.execute(
        "SELECT id FROM memory_revisions WHERE memory_id = ? ORDER BY id DESC LIMIT 1",
        (mem["id"],),
    ).fetchone()
    revision_id = row["id"]

    await remind_me_revert(RevertInput(memory_id=mem["id"], revision_id=revision_id))

    restored = db_conn.execute(
        "SELECT sensitive FROM memories WHERE id = ?", (mem["id"],)
    ).fetchone()
    assert restored["sensitive"] == 0  # reverted back to pre-toggle value


async def test_remind_me_revert_handles_pre_195_revision_with_null_sensitive(
    db_conn: sqlite3.Connection, memory_factory
) -> None:
    """A revision captured before this column existed has sensitive=NULL --
    reverting to it must not attempt to write NULL into the NOT NULL
    memories.sensitive column; it falls back to 0 (not sensitive)."""
    mem = memory_factory(content="original", sensitive=1)
    # Simulate a pre-#195 revision row: sensitive intentionally left NULL.
    db_conn.execute(
        """INSERT INTO memory_revisions
               (memory_id, content, category, tags, metadata, sensitive, edited_at)
           VALUES (?, 'original', 'general', '[]', '{}', NULL, '2026-01-01T00:00:00+00:00')""",
        (mem["id"],),
    )
    db_conn.commit()
    row = db_conn.execute(
        "SELECT id FROM memory_revisions WHERE memory_id = ? ORDER BY id DESC LIMIT 1",
        (mem["id"],),
    ).fetchone()
    revision_id = row["id"]

    result = await remind_me_revert(RevertInput(memory_id=mem["id"], revision_id=revision_id))

    assert "reverted" in result.lower()
    restored = db_conn.execute(
        "SELECT sensitive FROM memories WHERE id = ?", (mem["id"],)
    ).fetchone()
    assert restored["sensitive"] == 0
