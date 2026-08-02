"""
remind_me_mcp.readwise_import — Readwise highlight export connector (FT-20,
issue #182).

Registers a "readwise" import kind (:func:`~remind_me_mcp.importer.register_connector`)
that turns a Readwise "Export" JSON file into one memory per highlight.

**Source format, verified against Readwise's own docs**: the issue that
requested this connector described the export as "an array of book/article
objects, each with a highlights array." The actual, currently-documented
shape (Readwise's Export API, ``GET /api/v2/export/`` — see
https://readwise.io/api_deets) wraps that array one level deeper: a JSON
*object* with ``count``, ``nextPageCursor``, and ``results`` (the array of
book/article objects the issue described). This module accepts both: the
real ``{"results": [...]}`` shape a user gets by calling that endpoint and
saving the response, *and* a bare top-level array (e.g. if someone has
already unwrapped ``results`` before saving), so either form a real user
plausibly hands this tool works. Each ``results`` entry carries ``title``,
``author``, ``category`` (``books``/``articles``/``tweets``/``podcasts``/
...), ``source_url``, and a ``highlights`` array; each highlight carries
``text``, ``note``, ``location``, ``location_type``, ``highlighted_at``,
``url``, ``tags`` (a list of ``{"id", "name"}`` objects), and more. No live
call is made against that endpoint — Readwise's API requires an access
token (OAuth-adjacent), and this connector, like every other one in this
codebase, is file-import only: a user exports once (via the endpoint above,
or Readwise's own "Export" tooling) and hands this the resulting file.

**Granularity: one memory per highlight, not one memory per book/article.**
Considered against the alternative of grouping all of a book's highlights
into one chunked memory (mirroring how ``document.py`` chunks a Markdown
file by section, or how ``pdf_import.py`` chunks a PDF by page): a highlight
is Readwise's own atomic unit of meaning — a user does not re-read half a
highlight the way they might re-read half a document section — and each one
already carries everything needed to stand alone (the passage plus,
optionally, the user's own note on it). Grouping would force every search
hit for a single highlight to compete for ranking/embedding budget against
every *other* highlight from the same book, diluting exactly the retrieval
precision a memory store exists to provide. The cost of the finer grain —
losing the book as connective tissue between highlights — is paid back by
attaching the book's title/author/category/source_url as metadata on every
highlight it produced (mirroring how ``dbs_import.py`` attaches
``dbs_source``/``dbs_external_id`` per item), so the context is never lost,
just demoted from "shapes the embedding" to "travels alongside it."

**A highlight's user note is appended to its content, never discarded.**
Per the issue's explicit instruction: a note is frequently more valuable
than the highlight it's attached to (it's the *reason* the user cared enough
to highlight in the first place), so it rides in the same memory content as
``"{highlight text}\\n\\nNote: {note text}"`` rather than living only in
metadata, where full-text search (FTS5 indexes ``content``, not
``metadata``) would never surface it.

**Routing: ``kind="readwise"`` must be requested explicitly — never chosen
by ``kind="auto"``.** A Readwise export and an arbitrary chat export both
land on this codebase's doorstep as an unadorned ``.json`` file. The
existing chat/document auto-sniff (:func:`~remind_me_mcp.importer._looks_like_chat_markdown`)
gets away with content-sniffing because chat role markers (``**User:**``,
``## Assistant``) are a strong, low-false-positive-rate signal in Markdown
prose. JSON offers no equivalent: a naive sniff for "top-level array/object
with a highlights-shaped key" risks misrouting a legitimate chat export that
happens to nest a ``"highlights"`` key somewhere (e.g. a conversation about
Readwise itself, or a future chat-export field named the same thing) — and
getting that wrong silently corrupts someone's existing, working chat-import
behavior, which is a strictly worse failure than asking for one extra
keyword. This connector takes the safer of the two options the issue
explicitly allows: kept out of auto-detection entirely, documented as a
tradeoff rather than risked as a guess. See
:mod:`remind_me_mcp.importer`'s FT-20 docstring note for where this is
enforced (:func:`~remind_me_mcp.importer._validate_kind_and_suffix` and the
effective-kind resolution in :func:`~remind_me_mcp.importer._ingest_parsed`).

This module is wired in by ``tools/admin.py`` importing it for its
registration side effect (the same shape ``pdf_import.py``/``image_import.py``/
``mempalace_import.py``/``dbs_import.py`` use), never by ``importer.py``
itself.

Ingestion itself (hash dedup, chunk storage, batched embedding) is entirely
handled by :func:`remind_me_mcp.importer._ingest_parsed` — this module only
turns Readwise export JSON into ``(chunk_content, chunk_metadata)`` pairs,
same contract every other connector in this file family follows.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from remind_me_mcp.importer import _chunk_text, register_connector

log = logging.getLogger("remind_me_mcp.readwise_import")

READWISE_FORMAT_ERROR = (
    "Not a recognized Readwise export: expected a JSON object with a "
    "'results' array of book/article entries (the shape Readwise's Export "
    "API returns — see https://readwise.io/api_deets), or a bare array of "
    "the same entries. Each entry needs a 'highlights' array."
)
"""User-facing error for a JSON file that parses but isn't Readwise-shaped
(mirrors pdf_import.py/image_import.py's actionable-message convention)."""


def _extract_results(data: Any) -> list[Any]:
    """Pull the list of book/article entries out of a parsed Readwise export.

    Accepts the real, documented API response shape (a ``{"results": [...]}``
    object) and, for convenience, a bare top-level array of the same entries.

    Raises:
        RuntimeError: Neither shape matched — a clear, actionable message
            instead of a confusing downstream ``AttributeError``/``TypeError``
            from treating the wrong type as a list of entries.
    """
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        return data["results"]
    if isinstance(data, list):
        return data
    raise RuntimeError(READWISE_FORMAT_ERROR)


def _highlight_content(text: str, note: Any) -> str:
    """Compose one highlight's memory content: the passage, plus its note if any.

    The note is appended rather than discarded or metadata-only (per the
    issue's explicit instruction) — it's often the reason the highlight was
    made at all, and only content participates in full-text search.
    """
    content = text.strip()
    if isinstance(note, str) and note.strip():
        content = f"{content}\n\nNote: {note.strip()}"
    return content


def _highlight_metadata(entry: dict[str, Any], highlight: dict[str, Any]) -> dict[str, Any]:
    """Build a highlight's chunk metadata: book/article context plus the
    highlight's own provenance fields, all optional (only present keys are
    included — mirrors pdf_import's sparse ``{"page": N}`` convention rather
    than always emitting every key with null placeholders)."""
    meta: dict[str, Any] = {}

    title = entry.get("title")
    if isinstance(title, str) and title.strip():
        meta["readwise_title"] = title.strip()
    author = entry.get("author")
    if isinstance(author, str) and author.strip():
        meta["readwise_author"] = author.strip()
    category = entry.get("category")
    if isinstance(category, str) and category.strip():
        meta["readwise_category"] = category.strip()
    source_url = entry.get("source_url")
    if isinstance(source_url, str) and source_url.strip():
        meta["readwise_source_url"] = source_url.strip()
    book_id = entry.get("user_book_id")
    if isinstance(book_id, (int, str)):
        meta["readwise_book_id"] = book_id

    location = highlight.get("location")
    if isinstance(location, (int, str)):
        meta["readwise_location"] = location
    location_type = highlight.get("location_type")
    if isinstance(location_type, str) and location_type.strip():
        meta["readwise_location_type"] = location_type.strip()
    highlighted_at = highlight.get("highlighted_at")
    if isinstance(highlighted_at, str) and highlighted_at.strip():
        meta["readwise_highlighted_at"] = highlighted_at.strip()
    highlight_id = highlight.get("id")
    if isinstance(highlight_id, (int, str)):
        meta["readwise_highlight_id"] = highlight_id
    highlight_url = highlight.get("url")
    if isinstance(highlight_url, str) and highlight_url.strip():
        meta["readwise_url"] = highlight_url.strip()
    tags = highlight.get("tags")
    if isinstance(tags, list):
        tag_names = [
            t["name"] for t in tags if isinstance(t, dict) and isinstance(t.get("name"), str) and t["name"].strip()
        ]
        if tag_names:
            meta["readwise_tags"] = tag_names

    return meta


def _readwise_connector(
    raw: str, meta: dict[str, Any]
) -> tuple[list[tuple[str, dict[str, Any]]], int]:
    """Built-in ``readwise`` connector (FT-20): one memory per highlight.

    ``raw`` is the export file's decoded JSON text (Readwise exports are
    plain JSON, unlike pdf/image's binary formats, so — unlike those two
    connectors — this one uses ``raw`` directly and ignores
    ``meta["raw_bytes"]`` entirely).

    Malformed individual entries/highlights (not an object, missing/blank
    ``text``) are skipped with a warning rather than aborting the whole
    import — the same tolerance :func:`~remind_me_mcp.importer._chat_connector`
    already shows a malformed JSONL line. A malformed *top-level* shape (not
    a Readwise export at all) raises :data:`READWISE_FORMAT_ERROR` instead —
    a clear, actionable error rather than a crash partway through.
    """
    max_length = meta["max_length"]

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Could not parse Readwise export as JSON: {e}") from e

    entries = _extract_results(data)

    parsed: list[tuple[str, dict[str, Any]]] = []
    raw_entries = 0
    for entry in entries:
        if not isinstance(entry, dict):
            log.warning("Skipping non-object Readwise entry: %r", entry)
            continue
        highlights = entry.get("highlights")
        if not isinstance(highlights, list):
            log.warning(
                "Skipping Readwise entry with no 'highlights' array (title=%r)",
                entry.get("title"),
            )
            continue

        for highlight in highlights:
            if not isinstance(highlight, dict):
                log.warning("Skipping non-object Readwise highlight: %r", highlight)
                continue
            text = highlight.get("text")
            if not isinstance(text, str) or not text.strip():
                log.warning("Skipping Readwise highlight with no text: %r", highlight)
                continue

            raw_entries += 1
            content = _highlight_content(text, highlight.get("note"))
            chunk_meta = _highlight_metadata(entry, highlight)
            for chunk in _chunk_text(content, max_length):
                parsed.append((chunk, dict(chunk_meta)))

    return parsed, raw_entries


register_connector("readwise", _readwise_connector)


__all__ = [
    "READWISE_FORMAT_ERROR",
    "_extract_results",
    "_highlight_content",
    "_highlight_metadata",
    "_readwise_connector",
]
