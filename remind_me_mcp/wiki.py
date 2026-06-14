"""
remind_me_mcp.wiki — LLM Wiki synthesis layer (FT-08).

An implementation of Andrej Karpathy's "LLM Wiki" pattern layered on top of
remind_me's capture/retrieval store. Where the rest of the system *retrieves*
raw memories on demand (RAG-style), the wiki is a *synthesis* layer: Claude
distils raw memories into a small set of interlinked markdown pages
(entity/concept pages, summaries) that can be loaded directly into context
instead of searched.

Three layers, mirroring the original pattern:

* **Raw sources** — the existing ``memories`` table (captures, imports,
  decomposed facts). Immutable from the wiki's point of view.
* **The wiki** — plain markdown files under :data:`config.WIKI_DIR`, linked
  with ``[[Wikilinks]]``, plus an auto-generated ``index.md`` catalogue and an
  append-only ``log.md``.
* **The schema** — ``SCHEMA.md``, the maintainer instructions surfaced to
  Claude (seeded with a sensible default on first use).

**Files are the source of truth.** Every read path reconciles the database
index from the files first (cheap mtime comparison), so edits made outside the
MCP tools — by hand, by ``git pull``, by another editor — are picked up. The
``wiki_pages`` / ``wiki_links`` / ``wiki_fts`` tables are a rebuildable search
cache, never authoritative, which is why they carry no sync outbox triggers.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from remind_me_mcp import config
from remind_me_mcp import db as _db

log = logging.getLogger("remind_me_mcp.wiki")

# Reserved filenames that are never treated as ordinary content pages: the
# generated catalogue, the append-only log, and the maintainer schema.
INDEX_FILE = "index.md"
LOG_FILE = "log.md"
SCHEMA_FILE = "SCHEMA.md"
RESERVED_SLUGS = {"index", "log", "schema"}

_WIKILINK_RE = re.compile(r"\[\[([^\[\]|]+?)(?:\|([^\[\]]+?))?\]\]")
_H1_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)
_EPOCH = "1970-01-01T00:00:00+00:00"

# wiki_meta key holding the compile watermark: the created_at of the last raw
# memory folded into the wiki. Shared with tools.wiki._compile_sync so the
# "pending compile" count and the compile loop agree on the cutoff.
COMPILE_WATERMARK_KEY = "last_compile_at"


# ---------------------------------------------------------------------------
# Paths, slugs, titles
# ---------------------------------------------------------------------------


def wiki_dir() -> Path:
    """Return the wiki root, creating it on first use.

    Reads :data:`config.WIKI_DIR` at call time so tests can monkeypatch it.
    """
    d = Path(config.WIKI_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def slugify(title: str) -> str:
    """Map a human page title to a filesystem-safe, link-stable slug.

    Lowercases, replaces runs of non-alphanumeric characters with a single
    hyphen, and trims leading/trailing hyphens. ``"VLAN Setup!"`` and
    ``"vlan  setup"`` both yield ``"vlan-setup"`` so links resolve regardless
    of casing or punctuation drift.
    """
    s = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    return s or "untitled"


def page_path(title_or_slug: str) -> Path:
    """Return the on-disk path for a page given its title or slug."""
    return wiki_dir() / f"{slugify(title_or_slug)}.md"


def _extract_title(content: str, fallback: str) -> str:
    """Title is the first H1 heading; otherwise the humanised *fallback* slug."""
    m = _H1_RE.search(content)
    if m:
        return m.group(1).strip()
    return fallback.replace("-", " ").strip().title()


def _extract_summary(content: str) -> str:
    """First non-heading, non-blank line of body text — the page's one-liner."""
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Drop simple markdown emphasis/list markers for a cleaner summary.
        line = re.sub(r"^[-*>\s]+", "", line)
        if line:
            return line[:280]
    return ""


def parse_wikilinks(content: str) -> list[tuple[str, str]]:
    """Extract ``[[Target]]`` / ``[[Target|alias]]`` links from *content*.

    Returns ``(dst_slug, dst_title)`` pairs, de-duplicated by slug and in first
    appearance order. The display alias is ignored for resolution — links are
    keyed on the slug of the target title.
    """
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for target, _alias in _WIKILINK_RE.findall(content):
        title = target.strip()
        if not title:
            continue
        slug = slugify(title)
        if slug in seen:
            continue
        seen.add(slug)
        out.append((slug, title))
    return out


def estimate_tokens(text: str) -> int:
    """Rough token estimate (len // 4), matching the search token-budget heuristic."""
    return len(text) // 4


# ---------------------------------------------------------------------------
# Schema (maintainer instructions)
# ---------------------------------------------------------------------------


def default_schema() -> str:
    """The default ``SCHEMA.md`` seeded on first use — the maintainer contract."""
    return """\
# Wiki Schema & Maintenance Guide

You are the disciplined maintainer of this wiki. It is a *synthesis* of the
user's raw memories, not a copy of them. Keep it small, current, and
cross-linked so it can be loaded directly into context.

## Layers
- **Raw sources** — the `memories` store (captures, imports, facts). Treat as
  immutable ground truth. Pull pending ones with `remind_me_wiki_compile`.
- **Wiki pages** — the markdown files here. You create and revise them.
- **This schema** — the rules you follow.

## Page conventions
- One concept, entity, or project per page. The first line is an H1 title
  (`# Title`); the title's slug is its identity, so keep titles stable.
- Open with a one-sentence summary (it becomes the index entry).
- Link related pages with `[[Page Title]]` (or `[[Page Title|display text]]`).
  Dense cross-linking is the point — prefer linking over repeating.
- Keep pages concise. Distil; do not paste raw conversation.

## Maintenance workflow (when new sources arrive)
1. Read each source. Decide which page(s) it belongs to.
2. Create or update those pages — typically several per source. Revise
   existing summaries rather than appending duplicates.
3. **Flag contradictions** inline with a `> ⚠️ Contradiction:` blockquote that
   names both claims and their source memory ids.
4. Strengthen cross-links: add `[[links]]` to newly related pages.
5. When the batch is integrated, call `remind_me_wiki_compile` again with
   `mark_integrated=true` to advance the watermark.

## System files (do not edit by hand)
- `index.md` — auto-generated catalogue, rewritten on every page change.
- `log.md` — append-only change log.
"""


def ensure_schema_file() -> Path:
    """Create ``SCHEMA.md`` with the default contents if it does not exist."""
    p = wiki_dir() / SCHEMA_FILE
    if not p.exists():
        p.write_text(default_schema(), encoding="utf-8")
        log.info("Seeded wiki schema at %s", p)
    return p


def read_schema() -> str:
    """Return the maintainer schema text (seeding the default if absent)."""
    return ensure_schema_file().read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Meta (compile watermark, etc.)
# ---------------------------------------------------------------------------


def get_meta(key: str, default: str = "") -> str:
    """Read a ``wiki_meta`` value, or *default* when unset."""
    row = _db._get_db().execute(
        "SELECT value FROM wiki_meta WHERE key = ?", (key,)
    ).fetchone()
    return row[0] if row is not None else default


def set_meta(key: str, value: str) -> None:
    """Upsert a ``wiki_meta`` value."""
    db = _db._get_db()
    db.execute(
        "INSERT INTO wiki_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    db.commit()


def pending_compile_count() -> int:
    """Count raw memories awaiting wiki synthesis.

    These are non-superseded ``memories`` rows created after the compile
    watermark — exactly the set ``tools.wiki._compile_sync`` would surface
    (it caps each brief at its ``limit``; this is the uncapped total). Zero
    means the wiki is current with the memory store.
    """
    watermark = get_meta(COMPILE_WATERMARK_KEY, "") or _EPOCH
    row = _db._get_db().execute(
        "SELECT COUNT(*) FROM memories WHERE superseded_by IS NULL AND created_at > ?",
        (watermark,),
    ).fetchone()
    return int(row[0]) if row is not None else 0


# ---------------------------------------------------------------------------
# Index DB <- files reconciliation (files are the source of truth)
# ---------------------------------------------------------------------------


def _page_files() -> dict[str, Path]:
    """Map slug -> path for every content page on disk (reserved files excluded)."""
    out: dict[str, Path] = {}
    for p in sorted(wiki_dir().glob("*.md")):
        if p.name in (INDEX_FILE, LOG_FILE, SCHEMA_FILE):
            continue
        out[p.stem.lower()] = p
    return out


def _index_page(db, slug: str, path: Path) -> None:
    """(Re)index a single page file into wiki_pages + wiki_links."""
    content = path.read_text(encoding="utf-8")
    title = _extract_title(content, slug)
    summary = _extract_summary(content)
    mtime = path.stat().st_mtime
    db.execute(
        """INSERT INTO wiki_pages (slug, title, content, summary, mtime, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (slug) DO UPDATE SET
               title = excluded.title, content = excluded.content,
               summary = excluded.summary, mtime = excluded.mtime,
               updated_at = excluded.updated_at""",
        (slug, title, content, summary, mtime, _db._now_iso()),
    )
    db.execute("DELETE FROM wiki_links WHERE src_slug = ?", (slug,))
    for dst_slug, dst_title in parse_wikilinks(content):
        db.execute(
            "INSERT OR IGNORE INTO wiki_links (src_slug, dst_slug, dst_title) "
            "VALUES (?, ?, ?)",
            (slug, dst_slug, dst_title),
        )


def reconcile() -> dict[str, int]:
    """Sync the DB index to match the files on disk (files are canonical).

    Re-indexes pages whose on-disk mtime differs from the cached value (new or
    edited), and drops index rows whose file has been deleted. Cheap enough to
    run at the head of every read path. Returns counts of work done.
    """
    db = _db._get_db()
    files = _page_files()
    cached = {
        r["slug"]: r["mtime"]
        for r in db.execute("SELECT slug, mtime FROM wiki_pages").fetchall()
    }

    indexed = removed = 0
    for slug, path in files.items():
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if slug not in cached or cached[slug] != mtime:
            _index_page(db, slug, path)
            indexed += 1

    for gone in set(cached) - set(files):
        db.execute("DELETE FROM wiki_pages WHERE slug = ?", (gone,))
        db.execute("DELETE FROM wiki_links WHERE src_slug = ?", (gone,))
        removed += 1

    if indexed or removed:
        db.commit()
    return {"indexed": indexed, "removed": removed, "pages": len(files)}


# ---------------------------------------------------------------------------
# Index.md + log.md generation
# ---------------------------------------------------------------------------


def rebuild_index() -> str:
    """Regenerate ``index.md`` from the current pages and return its content."""
    db = _db._get_db()
    rows = db.execute(
        "SELECT slug, title, summary FROM wiki_pages ORDER BY title COLLATE NOCASE"
    ).fetchall()
    lines = [
        "# Wiki Index",
        "",
        f"_Auto-generated by remind_me — {len(rows)} page(s). Do not edit by hand._",
        "",
    ]
    if rows:
        for r in rows:
            summary = f" — {r['summary']}" if r["summary"] else ""
            lines.append(f"- [[{r['title']}]]{summary}")
    else:
        lines.append("_(empty)_")
    content = "\n".join(lines) + "\n"
    (wiki_dir() / INDEX_FILE).write_text(content, encoding="utf-8")
    return content


def append_log(note: str) -> None:
    """Append a timestamped line to the append-only ``log.md``."""
    p = wiki_dir() / LOG_FILE
    if not p.exists():
        p.write_text("# Wiki Change Log\n\n", encoding="utf-8")
    with p.open("a", encoding="utf-8") as fh:
        fh.write(f"- {_db._now_iso()} {note}\n")


# ---------------------------------------------------------------------------
# Page CRUD (files first, then index)
# ---------------------------------------------------------------------------


def write_page(title: str, content: str, *, log_note: str | None = None) -> dict[str, Any]:
    """Create or replace a page: write the file, re-index, refresh index/log.

    The H1 title is normalised onto the content (prepended when missing) so the
    file is self-describing and the on-disk title matches *title*. Returns a
    summary dict with the slug, whether it was created vs. updated, and the
    resolved links.
    """
    slug = slugify(title)
    if slug in RESERVED_SLUGS:
        raise ValueError(f"'{slug}' is a reserved system page and cannot be written directly")

    body = content.strip("\n")
    existing_title = _extract_title(body, slug)
    if not _H1_RE.search(body) or existing_title != title:
        # Ensure the file opens with the canonical title.
        body = re.sub(_H1_RE, "", body, count=1).strip("\n") if _H1_RE.search(body) else body
        body = f"# {title}\n\n{body}".rstrip() + "\n"
    else:
        body = body + "\n"

    path = page_path(slug)
    created = not path.exists()
    path.write_text(body, encoding="utf-8")

    db = _db._get_db()
    _index_page(db, slug, path)
    db.commit()

    rebuild_index()
    append_log(f"{'created' if created else 'updated'} [[{title}]]")
    if log_note:
        append_log(f"  note: {log_note}")

    links = parse_wikilinks(body)
    return {
        "status": "ok",
        "slug": slug,
        "title": title,
        "created": created,
        "path": str(path),
        "links": [t for _s, t in links],
    }


def read_page(title_or_slug: str) -> dict[str, Any] | None:
    """Return a page's content plus its outgoing links and backlinks, or None."""
    reconcile()
    slug = slugify(title_or_slug)
    db = _db._get_db()
    row = db.execute(
        "SELECT slug, title, content, summary, updated_at FROM wiki_pages WHERE slug = ?",
        (slug,),
    ).fetchone()
    if row is None:
        return None
    links = [
        {"slug": r["dst_slug"], "title": r["dst_title"]}
        for r in db.execute(
            "SELECT dst_slug, dst_title FROM wiki_links WHERE src_slug = ?", (slug,)
        ).fetchall()
    ]
    backlinks = [
        {"slug": r["src_slug"], "title": r["title"]}
        for r in db.execute(
            """SELECT wl.src_slug, wp.title
                 FROM wiki_links wl JOIN wiki_pages wp ON wp.slug = wl.src_slug
                WHERE wl.dst_slug = ? ORDER BY wp.title COLLATE NOCASE""",
            (slug,),
        ).fetchall()
    ]
    return {
        "slug": row["slug"],
        "title": row["title"],
        "content": row["content"],
        "summary": row["summary"],
        "updated_at": row["updated_at"],
        "links": links,
        "backlinks": backlinks,
    }


def delete_page(title_or_slug: str) -> bool:
    """Delete a page's file and index rows; refresh index/log. Returns False if absent."""
    slug = slugify(title_or_slug)
    if slug in RESERVED_SLUGS:
        raise ValueError(f"'{slug}' is a reserved system page and cannot be deleted")
    path = page_path(slug)
    db = _db._get_db()
    row = db.execute("SELECT title FROM wiki_pages WHERE slug = ?", (slug,)).fetchone()
    existed = path.exists() or row is not None
    if not existed:
        return False
    path.unlink(missing_ok=True)
    db.execute("DELETE FROM wiki_pages WHERE slug = ?", (slug,))
    db.execute("DELETE FROM wiki_links WHERE src_slug = ?", (slug,))
    db.commit()
    rebuild_index()
    append_log(f"deleted [[{row['title'] if row else slug}]]")
    return True


def list_pages() -> list[dict[str, Any]]:
    """Return every content page (slug/title/summary), index reconciled first."""
    reconcile()
    db = _db._get_db()
    rows = db.execute(
        "SELECT slug, title, summary, updated_at FROM wiki_pages "
        "ORDER BY title COLLATE NOCASE"
    ).fetchall()
    return [dict(r) for r in rows]


def search_pages(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Full-text search the wiki (BM25 over title+content). Index reconciled first."""
    reconcile()
    from remind_me_mcp.tools.search import _sanitize_fts_query

    match = _sanitize_fts_query(query)
    if not match:
        return []
    db = _db._get_db()
    try:
        rows = db.execute(
            """SELECT wp.slug, wp.title, wp.summary,
                      snippet(wiki_fts, 1, '[', ']', '…', 12) AS snippet
                 FROM wiki_fts
                 JOIN wiki_pages wp ON wp.rowid = wiki_fts.rowid
                WHERE wiki_fts MATCH ?
                ORDER BY bm25(wiki_fts)
                LIMIT ?""",
            (match, limit),
        ).fetchall()
    except Exception as e:  # noqa: BLE001 — malformed FTS expression, degrade gracefully
        log.debug("Wiki FTS query failed for %r: %s", query, e)
        return []
    return [dict(r) for r in rows]


def load_wiki(token_budget: int | None = None, include_index: bool = True) -> dict[str, Any]:
    """Concatenate the whole wiki into one markdown blob for direct context loading.

    This is the core Karpathy move: skip retrieval, load the synthesised wiki
    straight into context. Pages are concatenated newest-revised first until the
    token budget (estimated len//4) is hit; the rest are listed as omitted.

    Args:
        token_budget: Ceiling in estimated tokens. ``None`` uses
            :data:`config.WIKI_LOAD_TOKEN_BUDGET`; ``0`` means unlimited.
        include_index: Prepend the page catalogue.

    Returns:
        Dict with ``content`` (the blob), ``pages_included``, ``pages_omitted``,
        and ``estimated_tokens``.
    """
    reconcile()
    if token_budget is None:
        token_budget = config.WIKI_LOAD_TOKEN_BUDGET
    db = _db._get_db()
    rows = db.execute(
        "SELECT slug, title, content, summary FROM wiki_pages "
        "ORDER BY updated_at DESC, title COLLATE NOCASE"
    ).fetchall()

    parts: list[str] = []
    if include_index:
        cat = ["# Wiki Index", ""]
        cat += [
            f"- [[{r['title']}]]" + (f" — {r['summary']}" if r["summary"] else "")
            for r in sorted(rows, key=lambda r: r["title"].lower())
        ]
        parts.append("\n".join(cat))

    included = omitted = 0
    omitted_titles: list[str] = []
    used = estimate_tokens(parts[0]) if parts else 0
    for r in rows:
        block = r["content"].rstrip()
        cost = estimate_tokens(block)
        if token_budget and used + cost > token_budget and included > 0:
            omitted += 1
            omitted_titles.append(r["title"])
            continue
        parts.append(block)
        used += cost
        included += 1

    if omitted:
        parts.append(
            "---\n_Omitted (token budget) — load individually with "
            f"`remind_me_wiki_read`: {', '.join(omitted_titles)}_"
        )

    return {
        "content": "\n\n---\n\n".join(parts),
        "pages_included": included,
        "pages_omitted": omitted,
        "estimated_tokens": used,
    }


__all__ = [
    "INDEX_FILE",
    "LOG_FILE",
    "SCHEMA_FILE",
    "RESERVED_SLUGS",
    "COMPILE_WATERMARK_KEY",
    "wiki_dir",
    "slugify",
    "page_path",
    "parse_wikilinks",
    "estimate_tokens",
    "default_schema",
    "ensure_schema_file",
    "read_schema",
    "get_meta",
    "set_meta",
    "pending_compile_count",
    "reconcile",
    "rebuild_index",
    "append_log",
    "write_page",
    "read_page",
    "delete_page",
    "list_pages",
    "search_pages",
    "load_wiki",
]
