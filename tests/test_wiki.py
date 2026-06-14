"""Tests for the FT-08 LLM Wiki synthesis layer (remind_me_mcp.wiki + tools.wiki)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from remind_me_mcp import wiki

if TYPE_CHECKING:
    from pathlib import Path
from remind_me_mcp.models import (
    WikiCompileInput,
    WikiDeleteInput,
    WikiListInput,
    WikiLoadInput,
    WikiReadInput,
    WikiSearchInput,
    WikiWriteInput,
)


@pytest.fixture()
def wiki_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point config.WIKI_DIR at a fresh temp directory for each test."""
    import remind_me_mcp.config as _cfg

    d = tmp_path / "wiki"
    monkeypatch.setattr(_cfg, "WIKI_DIR", d)
    return d


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("VLAN Setup", "vlan-setup"),
        ("  Bailey  Robertson  ", "bailey-robertson"),
        ("Python 3.11!", "python-3-11"),
        ("---weird---", "weird"),
        ("", "untitled"),
    ],
)
def test_slugify(title: str, expected: str) -> None:
    assert wiki.slugify(title) == expected


def test_parse_wikilinks_dedup_and_alias() -> None:
    content = "See [[Tailscale]] and [[VLAN Setup|the VLAN page]] and [[Tailscale]] again."
    links = wiki.parse_wikilinks(content)
    assert links == [("tailscale", "Tailscale"), ("vlan-setup", "VLAN Setup")]


def test_extract_title_and_summary() -> None:
    content = "# My Page\n\nThis is the first real line.\n\nMore."
    assert wiki._extract_title(content, "fallback") == "My Page"
    assert wiki._extract_summary(content) == "This is the first real line."


# ---------------------------------------------------------------------------
# Write / read / links
# ---------------------------------------------------------------------------


def test_write_page_creates_file_index_and_log(db_conn, wiki_dir: Path) -> None:
    result = wiki.write_page("Tailscale", "A mesh VPN built on WireGuard.")
    assert result["created"] is True
    assert result["slug"] == "tailscale"

    page_file = wiki_dir / "tailscale.md"
    assert page_file.exists()
    text = page_file.read_text()
    assert text.startswith("# Tailscale")  # H1 normalised on
    assert "mesh VPN" in text

    # Index row + FTS searchable
    row = db_conn.execute("SELECT title, summary FROM wiki_pages WHERE slug = 'tailscale'").fetchone()
    assert row["title"] == "Tailscale"
    assert "mesh VPN" in row["summary"]

    # Generated system files
    assert (wiki_dir / "index.md").exists()
    assert "[[Tailscale]]" in (wiki_dir / "index.md").read_text()
    assert "created [[Tailscale]]" in (wiki_dir / "log.md").read_text()


def test_write_page_replaces_and_marks_updated(db_conn, wiki_dir: Path) -> None:
    wiki.write_page("Tailscale", "First version.")
    result = wiki.write_page("Tailscale", "Second version, totally different.")
    assert result["created"] is False
    text = (wiki_dir / "tailscale.md").read_text()
    assert "Second version" in text
    assert "First version" not in text


def test_read_page_links_and_backlinks(db_conn, wiki_dir: Path) -> None:
    wiki.write_page("Networking", "Overview. See [[Tailscale]] for the VPN.")
    wiki.write_page("Tailscale", "A mesh VPN. Part of [[Networking]].")

    page = wiki.read_page("Tailscale")
    assert page is not None
    assert {ln["slug"] for ln in page["links"]} == {"networking"}
    assert {bl["slug"] for bl in page["backlinks"]} == {"networking"}


def test_read_missing_page_returns_none(db_conn, wiki_dir: Path) -> None:
    assert wiki.read_page("nope") is None


def test_reserved_slugs_rejected(db_conn, wiki_dir: Path) -> None:
    for reserved in ("index", "log", "schema"):
        with pytest.raises(ValueError, match="reserved"):
            wiki.write_page(reserved, "x")
        with pytest.raises(ValueError, match="reserved"):
            wiki.delete_page(reserved)


# ---------------------------------------------------------------------------
# Reconcile (files are source of truth)
# ---------------------------------------------------------------------------


def test_reconcile_picks_up_external_edit(db_conn, wiki_dir: Path) -> None:
    wiki.write_page("Notes", "Original body.")
    # Simulate a hand edit / git pull bumping mtime and changing content.
    page_file = wiki_dir / "notes.md"
    import os
    import time

    page_file.write_text("# Notes\n\nEdited externally with [[Tailscale]].\n")
    os.utime(page_file, (time.time() + 10, time.time() + 10))

    stats = wiki.reconcile()
    assert stats["indexed"] == 1
    row = db_conn.execute("SELECT content FROM wiki_pages WHERE slug = 'notes'").fetchone()
    assert "Edited externally" in row["content"]
    # New wikilink indexed too.
    link = db_conn.execute(
        "SELECT dst_slug FROM wiki_links WHERE src_slug = 'notes'"
    ).fetchone()
    assert link["dst_slug"] == "tailscale"


def test_reconcile_drops_deleted_files(db_conn, wiki_dir: Path) -> None:
    wiki.write_page("Ephemeral", "Goes away.")
    (wiki_dir / "ephemeral.md").unlink()
    stats = wiki.reconcile()
    assert stats["removed"] == 1
    assert db_conn.execute("SELECT COUNT(*) FROM wiki_pages").fetchone()[0] == 0


def test_delete_page(db_conn, wiki_dir: Path) -> None:
    wiki.write_page("Temp", "delete me")
    assert wiki.delete_page("Temp") is True
    assert not (wiki_dir / "temp.md").exists()
    assert wiki.read_page("Temp") is None
    assert wiki.delete_page("Temp") is False  # already gone


# ---------------------------------------------------------------------------
# Search / list / load
# ---------------------------------------------------------------------------


def test_search_pages(db_conn, wiki_dir: Path) -> None:
    wiki.write_page("Tailscale", "A mesh VPN built on WireGuard.")
    wiki.write_page("Postgres", "A relational database.")
    results = wiki.search_pages("WireGuard VPN")
    slugs = {r["slug"] for r in results}
    assert "tailscale" in slugs
    assert "postgres" not in slugs


def test_list_pages_sorted(db_conn, wiki_dir: Path) -> None:
    wiki.write_page("Zebra", "z")
    wiki.write_page("Apple", "a")
    titles = [p["title"] for p in wiki.list_pages()]
    assert titles == ["Apple", "Zebra"]


def test_load_wiki_respects_budget(db_conn, wiki_dir: Path) -> None:
    wiki.write_page("Page One", "x" * 400)
    wiki.write_page("Page Two", "y" * 400)
    # Tiny budget — only one page body fits (≈100 tokens each).
    result = wiki.load_wiki(token_budget=120, include_index=False)
    assert result["pages_included"] == 1
    assert result["pages_omitted"] == 1


def test_load_wiki_unlimited(db_conn, wiki_dir: Path) -> None:
    wiki.write_page("A", "aaa")
    wiki.write_page("B", "bbb")
    result = wiki.load_wiki(token_budget=0)
    assert result["pages_included"] == 2
    assert result["pages_omitted"] == 0


# ---------------------------------------------------------------------------
# Schema + meta
# ---------------------------------------------------------------------------


def test_schema_seeded_on_first_read(db_conn, wiki_dir: Path) -> None:
    text = wiki.read_schema()
    assert "Wiki Schema" in text
    assert (wiki_dir / "SCHEMA.md").exists()


def test_meta_roundtrip(db_conn, wiki_dir: Path) -> None:
    assert wiki.get_meta("k", "default") == "default"
    wiki.set_meta("k", "v")
    assert wiki.get_meta("k") == "v"


# ---------------------------------------------------------------------------
# Tool handlers (async)
# ---------------------------------------------------------------------------


async def test_tool_write_then_read(db_conn, wiki_dir: Path) -> None:
    from remind_me_mcp.tools.wiki import wiki_read, wiki_write

    out = await wiki_write(WikiWriteInput(title="Tailscale", content="A mesh VPN. [[Networking]]."))
    payload = json.loads(out)
    assert payload["status"] == "ok"
    assert "Networking" in payload["links"]

    read = await wiki_read(WikiReadInput(title="tailscale"))
    assert "mesh VPN" in read
    assert "Links:" in read


async def test_tool_list_and_load(db_conn, wiki_dir: Path) -> None:
    from remind_me_mcp.tools.wiki import wiki_list, wiki_load, wiki_write

    await wiki_write(WikiWriteInput(title="Alpha", content="First page."))
    listing = await wiki_list(WikiListInput())
    assert "[[Alpha]]" in listing

    loaded = await wiki_load(WikiLoadInput())
    assert "First page." in loaded
    assert "Wiki:" in loaded  # coverage header


async def test_tool_search(db_conn, wiki_dir: Path) -> None:
    from remind_me_mcp.tools.wiki import wiki_search, wiki_write

    await wiki_write(WikiWriteInput(title="Postgres", content="A relational database engine."))
    out = await wiki_search(WikiSearchInput(query="relational database"))
    payload = json.loads(out)
    assert payload["count"] >= 1
    assert payload["results"][0]["slug"] == "postgres"


# ---------------------------------------------------------------------------
# Compile workflow
# ---------------------------------------------------------------------------


async def test_compile_brief_then_mark(db_conn, wiki_dir: Path, memory_factory) -> None:
    from remind_me_mcp.tools.wiki import wiki_compile

    memory_factory(content="Bailey prefers dark mode.", category="preference")
    memory_factory(content="Tailscale is a mesh VPN.", category="fact")

    # Phase 1: brief surfaces the pending sources + schema + (empty) index.
    brief = await wiki_compile(WikiCompileInput())
    assert "Wiki Compile Brief" in brief
    assert "Bailey prefers dark mode." in brief
    assert "Maintainer schema" in brief

    # Brief is idempotent — watermark not advanced yet.
    again = await wiki_compile(WikiCompileInput())
    assert "Tailscale is a mesh VPN." in again

    # Phase 2: mark integrated advances the watermark past the batch.
    marked = json.loads(await wiki_compile(WikiCompileInput(mark_integrated=True)))
    assert marked["status"] == "integrated"
    assert marked["sources_marked"] == 2

    # Now nothing pending.
    empty = await wiki_compile(WikiCompileInput())
    assert "nothing pending" in empty


async def test_compile_mark_with_nothing_pending(db_conn, wiki_dir: Path) -> None:
    from remind_me_mcp.tools.wiki import wiki_compile

    out = json.loads(await wiki_compile(WikiCompileInput(mark_integrated=True)))
    assert out["status"] == "noop"


# ---------------------------------------------------------------------------
# Tool error/empty paths + resources
# ---------------------------------------------------------------------------


async def test_tool_write_reserved_returns_error(db_conn, wiki_dir: Path) -> None:
    from remind_me_mcp.tools.wiki import wiki_write

    out = json.loads(await wiki_write(WikiWriteInput(title="index", content="x")))
    assert out["status"] == "error"
    assert "reserved" in out["error"]


async def test_tool_read_missing(db_conn, wiki_dir: Path) -> None:
    from remind_me_mcp.tools.wiki import wiki_read

    out = await wiki_read(WikiReadInput(title="ghost"))
    assert "not found" in out


async def test_tool_list_empty_and_json(db_conn, wiki_dir: Path) -> None:
    from remind_me_mcp.tools.wiki import wiki_list, wiki_write

    empty = await wiki_list(WikiListInput())
    assert "empty" in empty

    await wiki_write(WikiWriteInput(title="Solo", content="only page"))
    payload = json.loads(await wiki_list(WikiListInput(response_format="json")))
    assert payload["count"] == 1


async def test_tool_load_empty(db_conn, wiki_dir: Path) -> None:
    from remind_me_mcp.tools.wiki import wiki_load

    out = await wiki_load(WikiLoadInput())
    assert "empty" in out


async def test_tool_delete_paths(db_conn, wiki_dir: Path) -> None:
    from remind_me_mcp.tools.wiki import wiki_delete, wiki_write

    assert "not found" in await wiki_delete(WikiDeleteInput(title="missing"))

    err = json.loads(await wiki_delete(WikiDeleteInput(title="schema")))
    assert err["status"] == "error"

    await wiki_write(WikiWriteInput(title="Doomed", content="bye"))
    assert "deleted" in await wiki_delete(WikiDeleteInput(title="Doomed"))


async def test_resources(db_conn, wiki_dir: Path) -> None:
    from remind_me_mcp.tools.wiki import resource_wiki_index, resource_wiki_schema, wiki_write

    schema = await resource_wiki_schema()
    assert "Wiki Schema" in schema

    await wiki_write(WikiWriteInput(title="Indexed", content="hi"))
    idx = json.loads(await resource_wiki_index())
    assert idx["count"] == 1
