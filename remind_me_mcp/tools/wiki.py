"""
remind_me_mcp.tools.wiki — LLM Wiki tool + resource handlers (FT-08).

Surfaces the :mod:`remind_me_mcp.wiki` synthesis layer to MCP clients: page
read/write/list/delete, full-text search, whole-wiki context loading, and the
``remind_me_wiki_compile`` workflow that drives Claude to distil pending raw
memories into pages. Two resources expose the maintainer schema and the live
index.

Blocking file/DB work runs via ``asyncio.to_thread`` (PF-06 convention).
"""

from __future__ import annotations

import asyncio
import json

from remind_me_mcp import tools as _pkg
from remind_me_mcp import wiki
from remind_me_mcp.models import (  # noqa: TC001 — FastMCP resolves these at runtime
    WikiCompileInput,
    WikiDeleteInput,
    WikiListInput,
    WikiLoadInput,
    WikiReadInput,
    WikiSearchInput,
    WikiWriteInput,
)
from remind_me_mcp.server import mcp
from remind_me_mcp.tools._shared import _maybe_update_notice, log

# Per-source content shown in the compile brief — distil, don't dump.
_COMPILE_SOURCE_CHARS = 1200


@mcp.tool(
    name="remind_me_wiki_write",
    annotations={
        "title": "Write a Wiki Page",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def wiki_write(params: WikiWriteInput) -> str:
    """Create or replace a wiki page (FT-08 LLM Wiki — synthesis layer).

    The wiki is your synthesised, cross-linked knowledge base distilled from
    raw memories — not a copy of them. Use this to record or revise a concept,
    entity, or project page. The content REPLACES the page if it already
    exists, so include the full body. Link related pages with [[Page Title]].
    Writing refreshes index.md and appends to log.md automatically.

    Args:
        params (WikiWriteInput): Page title, full markdown content, optional log note.

    Returns:
        str: JSON summary — slug, created/updated, and the page's outgoing links.
    """
    try:
        result = await asyncio.to_thread(
            wiki.write_page, params.title, params.content, log_note=params.log_note
        )
    except ValueError as e:
        return json.dumps({"status": "error", "error": str(e)})
    except OSError as e:
        log.error("Wiki write failed for %r: %s", params.title, e)
        return json.dumps({"status": "error", "error": f"Failed to write page: {e}"})
    return _maybe_update_notice(json.dumps(result, indent=2))


@mcp.tool(
    name="remind_me_wiki_read",
    annotations={
        "title": "Read a Wiki Page",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def wiki_read(params: WikiReadInput) -> str:
    """Read a single wiki page with its outgoing links and backlinks (FT-08).

    Returns the page markdown plus the pages it links to and the pages that
    link to it, so you can navigate the knowledge graph.

    Args:
        params (WikiReadInput): Page title or slug.

    Returns:
        str: The page in markdown (with a links/backlinks footer), or a not-found note.
    """
    page = await asyncio.to_thread(wiki.read_page, params.title)
    if page is None:
        return f"Wiki page '{params.title}' not found. List pages with `remind_me_wiki_list`."
    lines = [page["content"].rstrip(), ""]
    if page["links"]:
        lines.append("**Links:** " + ", ".join(f"[[{ln['title']}]]" for ln in page["links"]))
    if page["backlinks"]:
        lines.append("**Backlinks:** " + ", ".join(f"[[{bl['title']}]]" for bl in page["backlinks"]))
    return "\n".join(lines)


@mcp.tool(
    name="remind_me_wiki_list",
    annotations={
        "title": "List Wiki Pages",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def wiki_list(params: WikiListInput) -> str:
    """List every wiki page with its one-line summary (the index) (FT-08).

    Args:
        params (WikiListInput): Response format.

    Returns:
        str: The page catalogue in markdown or JSON.
    """
    pages = await asyncio.to_thread(wiki.list_pages)
    if params.response_format.value == "json":
        return json.dumps({"count": len(pages), "pages": pages}, indent=2)
    if not pages:
        return "_The wiki is empty._ Synthesise pages from raw memories with `remind_me_wiki_compile`."
    lines = [f"## Wiki — {len(pages)} page(s)", ""]
    for p in pages:
        summary = f" — {p['summary']}" if p["summary"] else ""
        lines.append(f"- [[{p['title']}]]{summary}")
    return "\n".join(lines)


@mcp.tool(
    name="remind_me_wiki_search",
    annotations={
        "title": "Search the Wiki",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def wiki_search(params: WikiSearchInput) -> str:
    """Full-text search the wiki pages (FT-08).

    Searches synthesised pages (title + body) — distinct from `remind_me_search`,
    which searches the raw memory store. Use this to find the right page to read
    or update before writing.

    Args:
        params (WikiSearchInput): Query and result limit.

    Returns:
        str: JSON list of matching pages with snippets.
    """
    results = await asyncio.to_thread(wiki.search_pages, params.query, params.limit)
    return json.dumps({"count": len(results), "results": results}, indent=2)


@mcp.tool(
    name="remind_me_wiki_load",
    annotations={
        "title": "Load the Whole Wiki",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def wiki_load(params: WikiLoadInput) -> str:
    """Load the entire wiki into context as one markdown document (FT-08).

    This is the core LLM-Wiki move: instead of retrieving fragments, load the
    pre-synthesised, cross-linked knowledge base directly. Pages are included
    newest-revised first up to the token budget; any overflow is listed by title
    so you can `remind_me_wiki_read` them individually.

    Args:
        params (WikiLoadInput): Token budget (0 = configured default) and whether
            to prepend the index.

    Returns:
        str: The concatenated wiki markdown, prefixed with a coverage note.
    """
    budget = params.token_budget if params.token_budget > 0 else None
    result = await asyncio.to_thread(
        wiki.load_wiki, budget, params.include_index
    )
    if result["pages_included"] == 0 and result["pages_omitted"] == 0:
        return "_The wiki is empty._ Synthesise pages from raw memories with `remind_me_wiki_compile`."
    header = (
        f"<!-- Wiki: {result['pages_included']} page(s) loaded, "
        f"{result['pages_omitted']} omitted, ~{result['estimated_tokens']} tokens -->\n\n"
    )
    return header + result["content"]


@mcp.tool(
    name="remind_me_wiki_delete",
    annotations={
        "title": "Delete a Wiki Page",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def wiki_delete(params: WikiDeleteInput) -> str:
    """Delete a wiki page by title or slug (FT-08).

    Args:
        params (WikiDeleteInput): Page title or slug.

    Returns:
        str: Confirmation or a not-found note.
    """
    try:
        ok = await asyncio.to_thread(wiki.delete_page, params.title)
    except ValueError as e:
        return json.dumps({"status": "error", "error": str(e)})
    if not ok:
        return f"Wiki page '{params.title}' not found."
    return f"✓ Wiki page '{params.title}' deleted."


@mcp.tool(
    name="remind_me_wiki_compile",
    annotations={
        "title": "Compile Memories into the Wiki",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def wiki_compile(params: WikiCompileInput) -> str:
    """Drive the wiki synthesis workflow over pending raw memories (FT-08).

    This is the maintainer loop, in two phases:

    1. **Brief (default, `mark_integrated=false`)** — returns the maintainer
       schema, the current page index, and up to `limit` raw memories created
       since the last compile (the "pending sources"). Read it, then create or
       update pages with `remind_me_wiki_write`: distil facts onto the right
       pages, revise summaries, flag contradictions, and add [[cross-links]].
    2. **Mark integrated (`mark_integrated=true`)** — call this AFTER writing the
       pages to advance the watermark past the surfaced batch so they are not
       re-served next time.

    Calling phase 1 repeatedly is safe and idempotent — it never advances the
    watermark on its own.

    Args:
        params (WikiCompileInput): Batch size and the mark_integrated flag.

    Returns:
        str: A synthesis brief (markdown), or a JSON confirmation when marking.
    """
    return await asyncio.to_thread(_compile_sync, params)


def _compile_sync(params: WikiCompileInput) -> str:
    """Blocking body of remind_me_wiki_compile (runs in a worker thread)."""
    wiki.reconcile()
    db = _pkg._get_db()
    watermark = wiki.get_meta(wiki.COMPILE_WATERMARK_KEY, "")
    rows = db.execute(
        """SELECT id, category, content, created_at FROM memories
            WHERE superseded_by IS NULL AND deleted_at IS NULL AND created_at > ?
            ORDER BY created_at ASC LIMIT ?""",
        (watermark or wiki._EPOCH, params.limit),
    ).fetchall()

    if params.mark_integrated:
        if not rows:
            return json.dumps(
                {"status": "noop", "reason": "no pending memories to mark", "watermark": watermark}
            )
        new_watermark = rows[-1]["created_at"]
        wiki.set_meta(wiki.COMPILE_WATERMARK_KEY, new_watermark)
        wiki.append_log(f"compiled {len(rows)} source(s) — watermark -> {new_watermark}")
        return json.dumps(
            {
                "status": "integrated",
                "sources_marked": len(rows),
                "watermark": new_watermark,
            },
            indent=2,
        )

    if not rows:
        return (
            "## Wiki compile — nothing pending\n\n"
            "All raw memories created so far are already integrated into the wiki "
            f"(watermark: `{watermark or 'never'}`). Add or import memories, then "
            "run `remind_me_wiki_compile` again."
        )

    pages = wiki.list_pages()
    index_md = (
        "\n".join(f"- [[{p['title']}]]" + (f" — {p['summary']}" if p["summary"] else "") for p in pages)
        if pages
        else "_(the wiki is currently empty — you are bootstrapping it)_"
    )

    src_lines = []
    for r in rows:
        body = r["content"]
        if len(body) > _COMPILE_SOURCE_CHARS:
            body = body[:_COMPILE_SOURCE_CHARS] + " …[truncated]"
        src_lines.append(f"### `{r['id']}` [{r['category']}] ({r['created_at']})\n{body}")

    return "\n".join(
        [
            "# Wiki Compile Brief",
            "",
            f"{len(rows)} pending raw memory(ies) to synthesise into the wiki "
            f"(watermark: `{watermark or 'never'}`).",
            "",
            "## Maintainer schema",
            "",
            wiki.read_schema().strip(),
            "",
            "## Current wiki pages",
            "",
            index_md,
            "",
            "## Pending raw sources",
            "",
            *src_lines,
            "",
            "## Your task",
            "",
            "1. For each source above, decide which page(s) it belongs to.",
            "2. Create or update those pages with `remind_me_wiki_write` (distil — "
            "do not paste raw text; revise existing summaries; add [[cross-links]]; "
            "flag contradictions per the schema).",
            "3. When done, call `remind_me_wiki_compile` again with "
            "`mark_integrated=true` to advance the watermark.",
        ]
    )


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@mcp.resource("wiki://schema")
async def resource_wiki_schema() -> str:
    """The wiki maintainer schema (SCHEMA.md), seeded with a default if absent."""
    return await asyncio.to_thread(wiki.read_schema)


@mcp.resource("wiki://index")
async def resource_wiki_index() -> str:
    """The live wiki index — every page with its one-line summary, as JSON."""
    pages = await asyncio.to_thread(wiki.list_pages)
    return json.dumps({"count": len(pages), "pages": pages}, indent=2)


__all__ = [
    "wiki_write",
    "wiki_read",
    "wiki_list",
    "wiki_search",
    "wiki_load",
    "wiki_delete",
    "wiki_compile",
    "resource_wiki_schema",
    "resource_wiki_index",
]
