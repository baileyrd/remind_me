"""
Tests for remind_me_mcp.obsidian_import — the "obsidian" import connector
(FT-31, issue #193).

Covers: frontmatter parsing (including graceful degradation on unsupported
YAML shapes), [[wikilink]]/[[wikilink|alias]] resolution into FT-04 entities
(order-independent — linking to a note before that note is ever imported
still works), inline #tag extraction deduped against frontmatter tags,
`.obsidian/` directory exclusion from the folder watcher, and a full
round-trip through import -> search -> entity lookup.

Follows test_readwise_import.py's precedent: db_conn (function-scoped
in-memory DB, auto-patched into importer.py/tools/etc.) needs no extra
embedding mock — the default onnx embedding backend is not reachable in this
offline test environment and _embed_and_store_rows degrades to a no-op
that never blocks FTS5 keyword search, exactly like every other import
connector's existing test suite already relies on.
"""

from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING

import pytest

import remind_me_mcp.config as config_mod
import remind_me_mcp.watcher as watcher_mod
from remind_me_mcp.db import _entity_id
from remind_me_mcp.importer import _CONNECTORS, IMPORT_KINDS, import_chat_file
from remind_me_mcp.obsidian_import import (
    OBSIDIAN_CATEGORY,
    OBSIDIAN_SOURCE,
    _dedupe_ci,
    _obsidian_connector,
    extract_inline_tags,
    parse_frontmatter,
    parse_obsidian_wikilinks,
)
from remind_me_mcp.watcher import FolderWatcher

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


def test_parse_frontmatter_extracts_tags_and_fields() -> None:
    note = (
        "---\n"
        "tags: [project, work]\n"
        "status: active\n"
        "aliases:\n"
        "  - Alt Name\n"
        "  - Second Alias\n"
        "---\n"
        "# Project Titan\n\nBody content here.\n"
    )
    fields, body = parse_frontmatter(note)

    assert fields["tags"] == ["project", "work"]
    assert fields["status"] == "active"
    assert fields["aliases"] == ["Alt Name", "Second Alias"]
    assert body == "# Project Titan\n\nBody content here.\n"


def test_parse_frontmatter_single_string_tags_field() -> None:
    """A bare (non-list) tags value is still recognized -- Obsidian allows both."""
    note = "---\ntags: solo-tag\n---\nBody.\n"
    fields, _body = parse_frontmatter(note)
    assert fields["tags"] == "solo-tag"


def test_parse_frontmatter_no_leading_delimiter_returns_body_unchanged() -> None:
    text = "# Just a note\n\nNo frontmatter here.\n"
    fields, body = parse_frontmatter(text)
    assert fields == {}
    assert body == text


def test_parse_frontmatter_unterminated_block_not_treated_as_frontmatter() -> None:
    """A leading '---' with no closing '---' isn't frontmatter at all (e.g. a
    note that just happens to open with a horizontal rule) -- content is
    returned completely unchanged, not partially stripped."""
    text = "---\nThis never closes.\n\nMore text.\n"
    fields, body = parse_frontmatter(text)
    assert fields == {}
    assert body == text


def test_parse_frontmatter_nested_mapping_degrades_to_body_only() -> None:
    """Complex/nested YAML this hand-rolled parser can't represent degrades
    to 'skip frontmatter, ingest the body' rather than crashing or leaving
    raw delimiters in the stored content."""
    note = (
        "---\n"
        "nested:\n"
        "  sub:\n"
        "    deep: 1\n"
        "tags: [a]\n"
        "---\n"
        "Body text survives.\n"
    )
    fields, body = parse_frontmatter(note)

    assert fields == {}  # unsupported shape -> no fields extracted
    assert body == "Body text survives.\n"  # but the block is still stripped
    assert "---" not in body


def test_parse_frontmatter_flow_mapping_degrades_to_body_only() -> None:
    note = "---\nopts: {a: 1, b: 2}\n---\nBody.\n"
    fields, body = parse_frontmatter(note)
    assert fields == {}
    assert body == "Body.\n"


def test_obsidian_connector_never_crashes_on_malformed_frontmatter() -> None:
    """End-to-end: a connector call against unparseable frontmatter succeeds
    (no exception) and still chunks the body normally."""
    note = "---\nnested:\n  sub: 1\n---\n# Title\n\nSome content.\n"
    parsed, raw_entries = _obsidian_connector(note, {"suffix": ".md", "max_length": 10000})
    assert raw_entries == len(parsed) == 1
    assert "Some content." in parsed[0][0]
    assert "obsidian_frontmatter" not in parsed[0][1]


# ---------------------------------------------------------------------------
# Wikilink resolution
# ---------------------------------------------------------------------------


def test_parse_obsidian_wikilinks_plain_and_aliased() -> None:
    body = "See [[Project Alpha]] and [[Project Beta|Beta]]."
    links = parse_obsidian_wikilinks(body)
    assert links == [
        ("Project Alpha", "", "[[Project Alpha]]"),
        ("Project Beta", "Beta", "[[Project Beta|Beta]]"),
    ]


def test_parse_obsidian_wikilinks_strips_heading_anchor() -> None:
    """[[Note#Heading]] resolves to the 'Note' title -- the heading anchor is
    a documented v1 limitation (stripped, not tracked as a separate target)."""
    body = "Refer to [[Project Alpha#Overview]] for details."
    links = parse_obsidian_wikilinks(body)
    assert links == [("Project Alpha", "", "[[Project Alpha#Overview]]")]


def test_parse_obsidian_wikilinks_strips_block_anchor() -> None:
    body = "See [[Project Alpha^abc123]]."
    links = parse_obsidian_wikilinks(body)
    assert links[0][0] == "Project Alpha"


def test_plain_and_aliased_wikilink_resolve_to_same_entity(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """[[Note]] and [[Note|Alias]] both link to the SAME entity when they
    name the same note title -- the display alias never affects resolution."""
    note = tmp_path / "hub.md"
    note.write_text(
        "# Hub\n\nSee [[Project Alpha]] and also [[Project Alpha|the Alpha project]].\n",
        encoding="utf-8",
    )

    result = import_chat_file(str(note), "", [], "assistant_messages", 10000, kind="obsidian")
    assert result["status"] == "ok"

    rows = db_conn.execute(
        "SELECT entity_id FROM memory_entities me "
        "JOIN memories m ON m.id = me.memory_id WHERE m.doc_id = ?",
        (result["import_id"],),
    ).fetchall()
    entity_ids = {r["entity_id"] for r in rows}
    assert entity_ids == {_entity_id("Project Alpha")}


def test_wikilink_to_not_yet_seen_note_creates_entity(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A link to a note title that has never itself been imported still
    resolves -- order-independent: linking before the target exists must not
    fail or defer, it creates/matches the entity immediately."""
    note = tmp_path / "linker.md"
    note.write_text("# Linker\n\nSee [[Future Note]] for what's coming.\n", encoding="utf-8")

    result = import_chat_file(str(note), "", [], "assistant_messages", 10000, kind="obsidian")
    assert result["status"] == "ok"

    row = db_conn.execute(
        "SELECT name FROM entities WHERE id = ?", (_entity_id("Future Note"),)
    ).fetchone()
    assert row is not None
    assert row["name"] == "Future Note"

    link = db_conn.execute(
        "SELECT 1 FROM memory_entities WHERE entity_id = ?", (_entity_id("Future Note"),)
    ).fetchone()
    assert link is not None


def test_wikilink_mentions_attributed_to_the_chunk_that_contains_them(
    tmp_path: Path,
) -> None:
    """A note with two distinct sections, each linking a different note,
    attaches each mention only to the chunk it actually appears in."""
    note = (
        "# Overview\n\n"
        "## Section A\n\nLinks to [[Note A]].\n\n"
        "## Section B\n\nLinks to [[Note B]].\n"
    )
    parsed, _n = _obsidian_connector(note, {"suffix": ".md", "max_length": 10000})
    by_section = {meta.get("section"): meta.get("mention_entities", []) for _content, meta in parsed}
    assert by_section["Overview > Section A"] == ["Note A"]
    assert by_section["Overview > Section B"] == ["Note B"]


# ---------------------------------------------------------------------------
# Inline #tag extraction
# ---------------------------------------------------------------------------


def test_extract_inline_tags_basic() -> None:
    text = "Some #urgent work and a #nested/tag reference."
    assert extract_inline_tags(text) == ["urgent", "nested/tag"]


def test_extract_inline_tags_ignores_markdown_headings() -> None:
    """'# Heading' (space after #) is never mistaken for a tag."""
    text = "# Heading One\n\n## Heading Two\n\nBody with a real #tag here."
    assert extract_inline_tags(text) == ["tag"]


def test_extract_inline_tags_ignores_purely_numeric() -> None:
    text = "Not a tag: #2024. This #2024review is fine though."
    tags = extract_inline_tags(text)
    assert "2024" not in tags
    assert "2024review" in tags


def test_extract_inline_tags_ignores_code_fences_and_inline_code() -> None:
    text = (
        "Real #tagged content.\n\n"
        "```\n# not a heading, and #not_a_tag either\n```\n\n"
        "Inline `#not_a_tag` code span.\n"
    )
    assert extract_inline_tags(text) == ["tagged"]


def test_extract_inline_tags_excludes_wikilink_heading_anchors() -> None:
    """A wikilink's #Heading anchor is not an inline tag -- the caller passes
    the wikilink match spans as exclude_spans."""
    text = "See [[Note#Heading]] and also a real #tag."
    import re

    from remind_me_mcp.wiki import _WIKILINK_RE

    spans = [(m.start(), m.end()) for m in re.finditer(_WIKILINK_RE, text)]
    tags = extract_inline_tags(text, exclude_spans=spans)
    assert tags == ["tag"]


def test_dedupe_ci_keeps_first_seen_casing() -> None:
    assert _dedupe_ci(["Project", "project", "PROJECT", "work"]) == ["Project", "work"]


def test_connector_dedupes_inline_tags_against_frontmatter_tags(tmp_path: Path) -> None:
    note = "---\ntags: [Project, work]\n---\n# Note\n\nBody with #project and #new.\n"
    parsed, _n = _obsidian_connector(note, {"suffix": ".md", "max_length": 10000})
    tags = parsed[0][1]["extra_tags"]
    # "Project" (frontmatter) and "project" (inline) collapse to one entry.
    assert tags == ["Project", "work", "new"]


# ---------------------------------------------------------------------------
# .obsidian/ directory exclusion
# ---------------------------------------------------------------------------


@pytest.fixture()
def watch_dir(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    import remind_me_mcp.importer as _importer_mod

    monkeypatch.setattr(watcher_mod, "_get_db", lambda: db_conn)
    monkeypatch.setattr(_importer_mod, "_embed_and_store_rows", lambda rows: len(rows))
    d = tmp_path / "vault"
    d.mkdir()
    return d


def _write(path: Path, text: str, age: float = 3600.0) -> None:
    """Write *text* to *path* with an mtime *age* seconds in the past.

    Mirrors test_watcher.py's own helper: an old mtime puts the file outside
    the debounce grace window (default 5s) so a single scan_once() ingests it
    immediately instead of deferring it as "too fresh".
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    old = time.time() - age
    os.utime(path, (old, old))


def test_obsidian_config_dir_never_ingested(
    db_conn: sqlite3.Connection, watch_dir: Path
) -> None:
    """A file inside .obsidian/ is never ingested, even though the parent
    vault directory is watched -- confirms the watcher's existing dot-
    directory skip already covers Obsidian's own config folder (no code
    change was needed for this part, per the issue's own guidance to check
    first)."""
    obsidian_dir = watch_dir / ".obsidian"
    obsidian_dir.mkdir()
    _write(obsidian_dir / "workspace.json", '{"role": "user", "content": "x"}')
    _write(obsidian_dir / "plugins" / "some-plugin" / "data.json", "{}")

    real_note = watch_dir / "Real Note.md"
    _write(real_note, "# Real Note\n\nActual vault content.\n")

    watcher = FolderWatcher([watch_dir])
    counts = watcher.scan_once()

    assert counts["ingested"] == 1
    mems = db_conn.execute("SELECT metadata FROM memories").fetchall()
    assert len(mems) == 1
    assert json.loads(mems[0]["metadata"])["filename"] == "Real Note.md"


def test_is_obsidian_vault_detects_dot_obsidian_at_root(tmp_path: Path) -> None:
    vault = tmp_path / "myvault"
    vault.mkdir()
    (vault / ".obsidian").mkdir()
    assert config_mod.is_obsidian_vault(vault) is True


def test_is_obsidian_vault_detects_ancestor(tmp_path: Path) -> None:
    """A watched SUBFOLDER of a larger vault still detects the vault via its
    ancestor's .obsidian/ directory."""
    vault = tmp_path / "myvault"
    (vault / ".obsidian").mkdir(parents=True)
    sub = vault / "Projects"
    sub.mkdir()
    assert config_mod.is_obsidian_vault(sub) is True


def test_is_obsidian_vault_false_for_plain_directory(tmp_path: Path) -> None:
    plain = tmp_path / "notes"
    plain.mkdir()
    assert config_mod.is_obsidian_vault(plain) is False


def test_is_obsidian_vault_bounded_by_import_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ancestor walk stops at IMPORT_ROOTS -- an unrelated .obsidian/
    further up the filesystem tree (outside the allowed roots) is never
    found, so it can never false-positive a directory the user never
    intended as vault-adjacent."""
    outer = tmp_path / "outer"
    outer.mkdir()
    (outer / ".obsidian").mkdir()
    inner_root = outer / "allowed_root"
    inner_root.mkdir()
    target = inner_root / "notes"
    target.mkdir()

    monkeypatch.setattr(config_mod, "IMPORT_ROOTS", [inner_root])
    assert config_mod.is_obsidian_vault(target) is False


def test_resolve_import_kind_upgrades_auto_inside_vault(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    note = vault / "note.md"
    note.write_text("content", encoding="utf-8")
    assert config_mod.resolve_import_kind(note, "auto") == "obsidian"


def test_resolve_import_kind_leaves_explicit_kind_alone(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    note = vault / "note.md"
    note.write_text("content", encoding="utf-8")
    assert config_mod.resolve_import_kind(note, "document") == "document"


def test_resolve_import_kind_leaves_auto_outside_vault_alone(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    note = plain / "note.md"
    note.write_text("content", encoding="utf-8")
    assert config_mod.resolve_import_kind(note, "auto") == "auto"


def test_watcher_routes_markdown_in_vault_through_obsidian_kind(
    db_conn: sqlite3.Connection, watch_dir: Path
) -> None:
    """End-to-end zero-config check: a plain .md file dropped into a watched
    directory that has a .obsidian/ sibling is imported with kind=obsidian
    (frontmatter/wikilink/tag aware), with no explicit kind ever passed."""
    (watch_dir / ".obsidian").mkdir()
    note = watch_dir / "Tagged Note.md"
    _write(note, "---\ntags: [demo]\n---\n# Tagged Note\n\nLinks to [[Other Note]].\n")

    watcher = FolderWatcher([watch_dir])
    counts = watcher.scan_once()
    assert counts["ingested"] == 1

    row = db_conn.execute("SELECT source, category, tags FROM memories").fetchone()
    assert row["source"] == OBSIDIAN_SOURCE
    assert row["category"] == OBSIDIAN_CATEGORY
    assert "demo" in json.loads(row["tags"])

    entity_row = db_conn.execute(
        "SELECT 1 FROM entities WHERE id = ?", (_entity_id("Other Note"),)
    ).fetchone()
    assert entity_row is not None


# ---------------------------------------------------------------------------
# Connector / kind registration
# ---------------------------------------------------------------------------


def test_obsidian_registered_as_connector() -> None:
    assert "obsidian" in _CONNECTORS


def test_obsidian_is_a_valid_import_chat_file_kind() -> None:
    assert "obsidian" in IMPORT_KINDS


def test_obsidian_kind_rejected_for_non_markdown_suffix(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    bad = tmp_path / "note.txt"
    bad.write_text("plain text", encoding="utf-8")
    result = import_chat_file(str(bad), "", [], "assistant_messages", 10000, kind="obsidian")
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# Full pipeline: import -> searchable -> entity-linked
# ---------------------------------------------------------------------------


async def test_obsidian_note_round_trips_searchable_and_entity_linked(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    from remind_me_mcp.models import EntityLookupInput, MemorySearchInput, ResponseFormat
    from remind_me_mcp.tools import memory_search, remind_me_entity

    note = tmp_path / "Meeting Notes.md"
    note.write_text(
        "---\n"
        "tags: [work, meetings]\n"
        "---\n"
        "# Meeting Notes\n\n"
        "Discussed the roadmap with [[Alex Chen]] today. #followup needed.\n",
        encoding="utf-8",
    )

    result = import_chat_file(str(note), "", [], "assistant_messages", 10000, kind="obsidian")
    assert result["status"] == "ok"
    assert result["kind"] == "obsidian"

    # Searchable via remind_me_search (plain FTS5 keyword match).
    search_payload = json.loads(
        await memory_search(
            MemorySearchInput(query="roadmap Alex Chen", response_format=ResponseFormat.JSON)
        )
    )
    assert search_payload["returned"] >= 1
    assert any("roadmap" in m["content"] for m in search_payload["memories"])

    # Tags: frontmatter + inline #followup both landed on the memory.
    row = db_conn.execute("SELECT tags FROM memories WHERE doc_id = ?", (result["import_id"],)).fetchone()
    stored_tags = json.loads(row["tags"])
    assert {"work", "meetings", "followup"} <= set(stored_tags)

    # Entity-linked: the [[Alex Chen]] wikilink resolved to an entity via the
    # existing FT-04 entity-graph machinery, discoverable through
    # remind_me_entity (a mention link, not a typed entity_relations edge --
    # remind_me_entity_traverse walks entity_relations specifically and is
    # not what a plain wikilink mention populates; see tools/entity.py).
    entity_payload = json.loads(await remind_me_entity(EntityLookupInput(name="Alex Chen")))
    assert entity_payload["found"] is True
    assert entity_payload["entity"]["name"] == "Alex Chen"
    assert any("roadmap" in m["content_snippet"] for m in entity_payload["memories"])

    # Also discoverable via remind_me_search's structured entity: syntax
    # (the 1-hop-adjacent co-mention surface FT-04 already provides).
    entity_search = json.loads(
        await memory_search(
            MemorySearchInput(query='entity:"Alex Chen"', response_format=ResponseFormat.JSON)
        )
    )
    assert entity_search["returned"] >= 1
