"""
remind_me_mcp.importer — Chat export and document import engine.

Handles parsing JSON, JSONL, and Markdown chat export formats — plus generic
documents (plain Markdown notes and text files, FT-02) — chunking text into
memory-sized pieces, and storing results into the database.

Two import kinds share one pipeline (hash dedup, _import_lock, batched
embedding):

* ``chat`` — role-structured exports, chunked per-message (the original path).
* ``document`` — notes/docs files, chunked per-section (Markdown headings,
  with heading context kept on each chunk) or per-paragraph (plain text).

``kind="auto"`` (the default) routes .json/.jsonl to the chat parser and
sniffs .md/.markdown/.txt content: files with chat role markers
(``**User:**`` / ``## Assistant`` …) import as chat, everything else as a
document.

FT-06: exports may carry entity-graph records tagged with a ``record_type``
discriminator ('entity' / 'memory_entity'; absent = memory, mirroring the
FT-04 sync wire format). Message extraction skips them, and JSON/JSONL chat
imports restore them: entities upsert through the FT-04 helpers (alias
union-merge), links insert-or-ignore. Caveat: links reference original memory
ids while a chat re-import assigns NEW ids, so links only fully restore when
the referenced memories already exist in the target database — dangling links
are skipped and counted in the result.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import sqlite3

from remind_me_mcp.config import EMBED_BATCH_SIZE
from remind_me_mcp.db import (
    _embed_and_store_rows,
    _get_db,
    _link_memory_entity,
    _make_id,
    _now_iso,
    _upsert_entity,
)

log = logging.getLogger("remind_me_mcp.importer")

IMPORT_CONCURRENCY = 8

IMPORT_KINDS = ("auto", "chat", "document")
"""Valid values for the ``kind`` import parameter (FT-02)."""

DOCUMENT_SOURCE = "document_import"
"""``memories.source`` value for document imports (chat imports use 'chat_import')."""

DOCUMENT_CATEGORY = "document"
"""Default ``memories.category`` for document imports when the caller passed
the generic chat default ('chat_import') or an empty category."""

# Serializes the dedup-check + INSERT transaction when import_chat_file runs
# concurrently in multiple asyncio.to_thread workers. SQLite connections are
# per-thread (db._get_db), so this is not about sharing a connection — it
# prevents two workers importing the same file content from both passing the
# chat_imports hash check before either records its row (PF-03). Embedding
# happens outside the lock so workers actually run concurrently.
_import_lock = threading.Lock()

# Max ids per IN (...) clause when mapping memory ids to rowids (SQLite's
# default bound-parameter limit is 999).
_ROWID_LOOKUP_BATCH = 500

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _file_hash(path: str) -> str:
    """Compute a short SHA-256 hash of a file's raw bytes for deduplication.

    Reads the file in 8 KiB chunks to avoid loading large files into memory.
    The returned hash is used as the key in the chat_imports table to detect
    re-imports of the same file content, regardless of filename.

    Args:
        path: Absolute or relative path to the file to hash.

    Returns:
        First 16 hex characters of the SHA-256 digest (64-bit fingerprint).
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _chunk_text(text: str, max_len: int) -> list[str]:
    """Split text into chunks at natural boundaries, preferring paragraphs.

    Tries to break at paragraph boundaries (double newline), then single
    newlines, then sentence boundaries ('. '), and falls back to a hard
    character cut if no boundary is found within max_len.

    Args:
        text: The text to split into chunks.
        max_len: Maximum number of characters per chunk.

    Returns:
        List of non-empty stripped text chunks, each at most max_len chars.
    """
    if len(text) <= max_len:
        return [text] if text.strip() else []
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        # Try to break at paragraph
        idx = text.rfind("\n\n", 0, max_len)
        if idx == -1:
            idx = text.rfind("\n", 0, max_len)
        if idx == -1:
            idx = text.rfind(". ", 0, max_len)
        if idx == -1:
            idx = max_len
        else:
            idx += 1
        # Guard against empty chunks: a window of pure whitespace (e.g. a long
        # run of leading spaces) strips to "" and must not be stored (HY-06).
        chunk = text[:idx].strip()
        if chunk:
            chunks.append(chunk)
        text = text[idx:].strip()
    return chunks


def _extract_messages_from_json(data: Any, extract_mode: str) -> list[dict[str, str]]:
    """Extract a flat list of {role, content} messages from JSON data.

    Handles various JSON shapes:
      - List of {role, content} messages
      - Dict with 'messages' key
      - Claude export format with 'chat_messages' containing 'content' arrays
      - List of conversations (each containing 'messages' or 'chat_messages')

    Args:
        data: Parsed JSON value — may be a list, dict, or nested structure.
        extract_mode: Message extraction strategy (passed through to recursive
            calls; not used directly in this function).

    Returns:
        List of dicts with 'role' and 'content' string keys. Empty list if
        no recognisable message structure is found.
    """
    messages: list[dict[str, str]] = []

    # Entity-graph records (FT-06) carry a record_type discriminator and no
    # role/content — they are restored by _restore_graph_records, never
    # parsed as chat messages.
    if isinstance(data, dict) and "record_type" in data:
        return messages

    # If it's a single conversation object with chat_messages (Claude export format)
    if isinstance(data, dict) and "chat_messages" in data:
        for msg in data["chat_messages"]:
            role = msg.get("sender", msg.get("role", "unknown"))
            # Claude exports have content as a list of {type, text} blocks
            content_field = msg.get("content", msg.get("text", ""))
            if isinstance(content_field, list):
                text_parts = []
                for block in content_field:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        text_parts.append(block)
                content = "\n".join(text_parts)
            elif isinstance(content_field, str):
                content = content_field
            else:
                content = str(content_field)
            if content.strip():
                messages.append({"role": role, "content": content.strip()})
        return messages

    # Bare single {role, content} message object — the standard chat JSONL
    # shape (one message per line) and the record format written by the
    # exporter (FT-01). Reuses the list branch for role/content extraction.
    if isinstance(data, dict) and ("role" in data or "sender" in data) and "messages" not in data:
        return _extract_messages_from_json([data], extract_mode)

    # Standard {role, content} list
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                # Graph records mixed into an export array are not messages.
                if "record_type" in item:
                    continue
                # Check if it's a conversation wrapper
                if "messages" in item or "chat_messages" in item:
                    messages.extend(_extract_messages_from_json(item, extract_mode))
                elif "role" in item or "sender" in item:
                    role = item.get("role", item.get("sender", "unknown"))
                    content = item.get("content", item.get("text", ""))  # type: ignore[assignment]  # nested .get default may be None
                    if isinstance(content, list):
                        content = "\n".join(
                            b.get("text", "") if isinstance(b, dict) else str(b)
                            for b in content
                        )
                    if isinstance(content, str) and content.strip():
                        messages.append({"role": role, "content": content.strip()})  # type: ignore[dict-item]  # role from .get may be None
        return messages

    # Dict with 'messages' key
    if isinstance(data, dict) and "messages" in data:
        return _extract_messages_from_json(data["messages"], extract_mode)

    return messages


def _filter_messages(messages: list[dict[str, str]], mode: str) -> list[str]:
    """Filter and format messages according to the extraction mode.

    Args:
        messages: List of {role, content} dicts as returned by
            _extract_messages_from_json.
        mode: One of 'assistant_messages', 'user_messages', 'all_messages',
            'conversations', or 'summaries'. Any other value returns all
            content strings.

    Returns:
        List of content strings ready for chunking and storage.
    """
    if mode == "assistant_messages":
        return [m["content"] for m in messages if m["role"] in ("assistant", "bot")]
    elif mode == "user_messages":
        return [m["content"] for m in messages if m["role"] in ("user", "human")]
    elif mode == "all_messages":
        return [f"[{m['role']}] {m['content']}" for m in messages]
    elif mode == "conversations":
        if messages:
            return ["\n\n".join(f"**{m['role']}:** {m['content']}" for m in messages)]
        return []
    elif mode == "summaries":
        return [m["content"] for m in messages if "summary" in m.get("role", "").lower()]
    return [m["content"] for m in messages]


# Common chat role markers in markdown exports: "## Human", "## Assistant",
# "**User:**", etc. Shared by the chat parser and the auto-detection sniffer.
_CHAT_MD_PATTERN = re.compile(
    r"(?:^|\n)(?:#{1,3}\s*|(?:\*\*))?(Human|User|Assistant|Claude|Bot|System)(?:\*\*)?[:\s]*\n?",
    re.IGNORECASE,
)


def _split_chat_markdown(text: str) -> list[dict[str, str]]:
    """Split markdown text on chat role markers into {role, content} messages.

    Args:
        text: Raw markdown/plain text.

    Returns:
        List of {role, content} dicts; empty when no role-structured content
        is found (the basis for chat-vs-document auto-detection, FT-02).
    """
    parts = _CHAT_MD_PATTERN.split(text)
    messages: list[dict[str, str]] = []
    i = 1
    while i < len(parts) - 1:
        role = parts[i].strip().lower()
        content = parts[i + 1].strip()
        if content:
            messages.append({"role": role, "content": content})
        i += 2
    return messages


def _looks_like_chat_markdown(text: str) -> bool:
    """Return True when the text contains chat-export role structure (FT-02).

    Used by ``kind="auto"`` to route .md/.markdown/.txt files: exactly the
    files the chat parser would find messages in import as chat, so existing
    chat-export behavior is unchanged.
    """
    return bool(_split_chat_markdown(text))


def _parse_markdown_chat(text: str, extract_mode: str) -> list[str]:
    """Parse markdown-formatted chat exports into content strings.

    Detects common role heading patterns (## Human, **Assistant:**, etc.)
    and splits the text into labeled message segments. Falls back to
    treating the entire file as a single memory if no structure is found.

    Args:
        text: Raw markdown text from the chat export file.
        extract_mode: Passed to _filter_messages to select which roles to keep.

    Returns:
        List of content strings extracted according to extract_mode.
    """
    messages = _split_chat_markdown(text)

    if not messages:
        # No structure detected — treat entire file as one memory
        return [text.strip()] if text.strip() else []

    return _filter_messages(messages, extract_mode)


# ---------------------------------------------------------------------------
# Document parsing (FT-02)
# ---------------------------------------------------------------------------

_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_MD_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")


def _split_markdown_sections(text: str) -> list[tuple[str | None, str]]:
    """Split Markdown into (heading_path, body) sections on ATX headings.

    The heading path is a breadcrumb of the section's ancestor headings joined
    with ``" > "`` (e.g. ``"Projects > Remind Me"``), so nested context travels
    with each section. Content before the first heading becomes a section with
    heading ``None``. Lines inside fenced code blocks are never treated as
    headings. Sections whose body is empty (heading-only) are dropped.

    Args:
        text: Raw markdown text.

    Returns:
        List of (heading_path | None, stripped_body) tuples in document order.
    """
    sections: list[tuple[str | None, str]] = []
    heading_stack: list[tuple[int, str]] = []
    current_heading: str | None = None
    current_lines: list[str] = []
    in_fence = False

    def _flush() -> None:
        body = "\n".join(current_lines).strip()
        if body:
            sections.append((current_heading, body))

    for line in text.splitlines():
        if _MD_FENCE_RE.match(line.lstrip()):
            in_fence = not in_fence
            current_lines.append(line)
            continue
        match = None if in_fence else _MD_HEADING_RE.match(line)
        if match:
            _flush()
            current_lines = []
            level = len(match.group(1))
            title = match.group(2).strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            current_heading = " > ".join(t for _, t in heading_stack)
        else:
            current_lines.append(line)
    _flush()
    return sections


def _parse_document(text: str, suffix: str, max_length: int) -> list[tuple[str, str | None]]:
    """Chunk a notes/document file into (content, section_heading) pairs (FT-02).

    Markdown files are split per-section on headings; each chunk keeps its
    heading breadcrumb both prepended to the content (for search context) and
    as the second tuple element (stored in memory metadata). Long sections
    fall back to paragraph/size-based chunking via :func:`_chunk_text`. Plain
    text files are paragraph/size-chunked with no heading metadata.

    Args:
        text: Raw file text.
        suffix: Lowercased file extension ('.md', '.markdown', or '.txt').
        max_length: Maximum characters per chunk (including heading prefix).

    Returns:
        List of (chunk_content, heading_path | None) tuples.
    """
    pairs: list[tuple[str, str | None]] = []
    if suffix in (".md", ".markdown"):
        for heading, body in _split_markdown_sections(text):
            prefix = f"{heading}\n\n" if heading else ""
            # Keep the heading context inside the chunk budget; floor the
            # body budget so a pathological heading can't zero it out.
            budget = max(max_length - len(prefix), 100)
            for chunk in _chunk_text(body, budget):
                pairs.append((prefix + chunk, heading))
    else:
        for chunk in _chunk_text(text, max_length):
            pairs.append((chunk, None))
    return pairs


# ---------------------------------------------------------------------------
# Entity-graph restore (FT-06)
# ---------------------------------------------------------------------------


def _restore_graph_records(
    db: sqlite3.Connection, records: list[dict[str, Any]]
) -> dict[str, int]:
    """Restore exported entity-graph records into the database (FT-06).

    Entities are applied first, through :func:`_upsert_entity` — the same
    union-merge semantics as sync: aliases dedup-merge into any existing row,
    a missing kind is filled in, and the deterministic id is re-derived from
    the name. Links then insert-or-ignore, but only when BOTH endpoints exist:
    links reference original memory ids, and a fresh-DB chat re-import assigns
    NEW memory ids, so a link is restorable only when the referenced memory
    was kept with its original id (same DB, or a synced one). Dangling links
    are skipped and counted — restore is honest, not magic.

    Timestamps are assigned fresh, matching the lossy chat re-import semantics
    for memories (the originals remain in the export file). Malformed records
    and unknown record_type values are logged and skipped (defensive, like
    sync's dispatch). Does NOT commit.

    Args:
        db: An open SQLite connection.
        records: Export records carrying a ``record_type`` discriminator.

    Returns:
        Counts: {'entities_restored': int, 'links_restored': int,
        'links_skipped_dangling': int}. 'links_restored' counts newly
        inserted rows only (already-present links are no-ops).
    """
    counts = {"entities_restored": 0, "links_restored": 0, "links_skipped_dangling": 0}

    # Entities first so link endpoint checks see freshly restored rows.
    for rec in records:
        if rec.get("record_type") != "entity":
            continue
        name = rec.get("name")
        if not isinstance(name, str) or not name.strip():
            log.warning("Skipping entity record without a name: %r", rec)
            continue
        aliases = rec.get("aliases")
        if isinstance(aliases, str):
            try:
                aliases = json.loads(aliases)
            except json.JSONDecodeError:
                aliases = []
        if not isinstance(aliases, list):
            aliases = []
        kind = rec.get("kind")
        _upsert_entity(
            db,
            name,
            kind=kind if isinstance(kind, str) and kind else None,
            aliases=[a for a in aliases if isinstance(a, str)],
        )
        counts["entities_restored"] += 1

    for rec in records:
        if rec.get("record_type") != "memory_entity":
            continue
        memory_id, entity_id = rec.get("memory_id"), rec.get("entity_id")
        if not memory_id or not entity_id:
            log.warning("Skipping link record without memory_id/entity_id: %r", rec)
            continue
        memory_row = db.execute(
            "SELECT 1 FROM memories WHERE id = ?", (str(memory_id),)
        ).fetchone()
        entity_row = db.execute(
            "SELECT 1 FROM entities WHERE id = ?", (str(entity_id),)
        ).fetchone()
        if memory_row is None or entity_row is None:
            counts["links_skipped_dangling"] += 1
            continue
        if _link_memory_entity(db, str(memory_id), str(entity_id)):
            counts["links_restored"] += 1

    return counts


# ---------------------------------------------------------------------------
# Public import function
# ---------------------------------------------------------------------------


def import_chat_file(
    file_path: str,
    category: str,
    tags: list[str],
    extract_mode: str,
    max_length: int,
    kind: str = "auto",
) -> dict[str, Any]:
    """Import a single chat export or document file into the memory store.

    Parses the file based on its extension (.json, .jsonl, .md/.markdown/.txt)
    and the resolved import ``kind``. Chat exports extract messages according
    to extract_mode and chunk per-message; documents (FT-02) chunk per-section
    (Markdown headings) or per-paragraph (plain text), recording the section
    heading in each memory's metadata. Deduplicates by file hash — if the same
    file content has already been imported, returns a 'skipped' result
    immediately.

    Args:
        file_path: Path to the file to import.
        category: Category to assign to all imported memories. For document
            imports, the generic chat default ('chat_import') or an empty
            string is replaced with 'document'.
        tags: Tags to apply to all imported memories.
        extract_mode: Message extraction strategy (e.g., 'assistant_messages');
            chat imports only.
        max_length: Maximum characters per memory chunk.
        kind: 'chat', 'document', or 'auto' (default). Auto routes
            .json/.jsonl to the chat parser and sniffs .md/.markdown/.txt
            content: chat role markers import as chat, everything else as a
            document.

    Returns:
        A status dict. On success: {'status': 'ok', 'import_id': str,
        'kind': str, 'memories_created': int, 'raw_entries': int, 'file': str};
        when the file carried entity-graph records (FT-06 exports), also
        'entities_restored', 'links_restored', and 'links_skipped_dangling'.
        On skip: {'status': 'skipped', 'reason': str, 'file': str,
        'import_id': str}. On unsupported format/kind: {'status': 'error',
        'reason': str, 'file': str}.
    """
    path = Path(file_path)
    suffix = path.suffix.lower()

    if kind not in IMPORT_KINDS:
        return {
            "status": "error",
            "reason": f"invalid kind: {kind!r} (use 'auto', 'chat', or 'document')",
            "file": path.name,
        }

    if suffix not in (".json", ".jsonl", ".md", ".markdown", ".txt"):
        return {"status": "error", "reason": f"unsupported format: {suffix}", "file": path.name}

    if kind == "document" and suffix in (".json", ".jsonl"):
        return {
            "status": "error",
            "reason": f"document import does not support {suffix}: use .md, .markdown, or .txt",
            "file": path.name,
        }

    # --- Phase 1: hash dedup BEFORE any parsing/chunking (PF-03) so
    # re-importing an already-imported file short-circuits immediately. ---
    fhash = _file_hash(file_path)
    db = _get_db()
    with _import_lock:
        existing = db.execute(
            "SELECT import_id FROM chat_imports WHERE hash = ?", (fhash,)
        ).fetchone()
    if existing:
        return {
            "status": "skipped",
            "reason": "already_imported",
            "file": path.name,
            "import_id": existing["import_id"],
        }

    # --- Phase 2: file I/O and parsing (no lock needed; pure CPU/disk work) ---
    raw = path.read_text(encoding="utf-8", errors="replace")

    # Resolve the effective kind (FT-02). JSON/JSONL are always chat exports;
    # markdown/text files are content-sniffed in auto mode so chat-style
    # markdown keeps importing as chat (existing behavior preserved).
    if suffix in (".json", ".jsonl"):
        effective_kind = "chat"
    elif kind == "auto":
        effective_kind = "chat" if _looks_like_chat_markdown(raw) else "document"
    else:
        effective_kind = kind

    # (content, section_heading) pairs — section is always None for chat.
    parsed: list[tuple[str, str | None]] = []
    contents: list[str] = []
    # Entity-graph records found in JSON/JSONL exports (FT-06) — restored in
    # phase 3, never parsed as chat messages.
    graph_records: list[dict[str, Any]] = []

    if effective_kind == "document":
        parsed = _parse_document(raw, suffix, max_length)
    elif suffix in (".json",):
        data = json.loads(raw)
        if isinstance(data, list):
            graph_records = [
                item for item in data
                if isinstance(item, dict) and "record_type" in item
            ]
        # Could be a list of conversations or a single conversation
        if isinstance(data, list) and data and isinstance(data[0], dict) and ("chat_messages" in data[0] or "messages" in data[0]):
            # Multiple conversations
            for conv in data:
                msgs = _extract_messages_from_json(conv, extract_mode)
                contents.extend(_filter_messages(msgs, extract_mode))
        else:
            msgs = _extract_messages_from_json(data, extract_mode)
            contents.extend(_filter_messages(msgs, extract_mode))
    elif suffix in (".jsonl",):
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                log.debug("Skipping malformed JSONL line")
                continue
            if isinstance(obj, dict) and "record_type" in obj:
                graph_records.append(obj)
                continue
            msgs = _extract_messages_from_json(obj, extract_mode)
            contents.extend(_filter_messages(msgs, extract_mode))
    elif suffix in (".md", ".markdown", ".txt"):
        contents = _parse_markdown_chat(raw, extract_mode)

    if effective_kind == "document":
        # Documents are already chunked per-section/paragraph by _parse_document.
        raw_entries = len(parsed)
        source = DOCUMENT_SOURCE
        if category in ("", "chat_import"):
            category = DOCUMENT_CATEGORY
    else:
        # Chat: chunk each extracted message/content string.
        for content in contents:
            if not content.strip():
                continue
            for chunk in _chunk_text(content, max_length):
                parsed.append((chunk, None))
        raw_entries = len(contents)
        source = "chat_import"

    # Pre-compute chunk/embed entries before acquiring the lock
    now = _now_iso()
    import_id = _make_id(file_path)
    embed_entries: list[tuple[str, str, str | None]] = [
        (_make_id(chunk), chunk, section) for chunk, section in parsed
    ]

    # --- Phase 3: dedup re-check + INSERTs in one short locked transaction.
    # The lock covers only the DB writes; parsing (above) and embedding
    # (below) run unlocked so concurrent import workers make progress (PF-03).
    with _import_lock:
        # Re-check under the lock: another worker importing the same content
        # may have won the race since the early check in phase 1.
        existing = db.execute(
            "SELECT import_id FROM chat_imports WHERE hash = ?", (fhash,)
        ).fetchone()
        if existing:
            return {"status": "skipped", "reason": "already_imported", "file": path.name, "import_id": existing["import_id"]}

        # Chunk and store — collect (mem_id, chunk) pairs so the same IDs are used
        # for both INSERT and embedding (BUGF-01 fix: prevents ID mismatch).
        # doc_id/chunk_index group every chunk from this file together in
        # source order, so a search hit's siblings can be looked up directly
        # (neighbor-aware chunk retrieval) instead of re-parsing metadata.
        stored = 0
        for chunk_index, (mem_id, chunk, section) in enumerate(embed_entries):
            metadata: dict[str, Any] = {"import_id": import_id, "filename": path.name}
            if section is not None:
                metadata["section"] = section
            db.execute(
                """INSERT OR IGNORE INTO memories
                   (id, content, category, tags, source, metadata, created_at, updated_at, doc_id, chunk_index)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    mem_id,
                    chunk,
                    category,
                    json.dumps(tags),
                    source,
                    json.dumps(metadata),
                    now,
                    now,
                    import_id,
                    chunk_index,
                ),
            )
            stored += 1

        stats: dict[str, Any] = {
            "kind": effective_kind,
            "memories_created": stored,
            "raw_entries": raw_entries,
            "file": path.name,
        }
        if graph_records:
            # Restore the entity graph from an FT-06 export: entities upsert
            # (alias union-merge), links insert-or-ignore when both endpoints
            # exist — dangling links (the referenced memory id is gone, e.g.
            # a fresh-DB re-import assigned new ids) are skipped and counted.
            stats.update(_restore_graph_records(db, graph_records))
        db.execute(
            "INSERT INTO chat_imports (import_id, filename, hash, imported_at, stats) VALUES (?, ?, ?, ?, ?)",
            (import_id, path.name, fhash, now, json.dumps(stats)),
        )
        db.commit()

    # --- Phase 4: embed OUTSIDE the lock, in batches (PF-03). The rows use
    # the SAME mem_ids that were INSERTed (BUGF-01); any failure here is
    # healed later by remind_me_reindex. ---
    if embed_entries:
        chunk_by_id = {mem_id: chunk for mem_id, chunk, _section in embed_entries}
        ids = list(chunk_by_id)
        rows_to_embed: list[tuple[int, str]] = []
        # The quick rowid lookups reuse the lock only because tests may share
        # one connection across workers; the slow embed calls stay unlocked.
        with _import_lock:
            for i in range(0, len(ids), _ROWID_LOOKUP_BATCH):
                batch_ids = ids[i : i + _ROWID_LOOKUP_BATCH]
                placeholders = ",".join("?" for _ in batch_ids)
                for row in db.execute(
                    f"SELECT id, rowid FROM memories WHERE id IN ({placeholders})",
                    batch_ids,
                ).fetchall():
                    rows_to_embed.append((row["rowid"], chunk_by_id[row["id"]]))
        for i in range(0, len(rows_to_embed), EMBED_BATCH_SIZE):
            _embed_and_store_rows(rows_to_embed[i : i + EMBED_BATCH_SIZE])

    return {"status": "ok", "import_id": import_id, **stats}


# ---------------------------------------------------------------------------
# Public directory import function
# ---------------------------------------------------------------------------


async def import_directory(
    directory: str,
    category: str = "chat_import",
    tags: list[str] | None = None,
    extract_mode: str = "assistant_messages",
    max_length: int = 10000,
    recursive: bool = True,
    kind: str = "auto",
) -> dict[str, Any]:
    """Import all chat export and document files from a directory concurrently.

    Scans for .json, .jsonl, .md, .markdown, and .txt files. Skips
    already-imported files (hash-based deduplication). Files are processed
    concurrently using asyncio.gather with a semaphore bounded by
    IMPORT_CONCURRENCY (default 8) to prevent resource exhaustion.

    Args:
        directory: Path to the directory containing files to import.
        category: Category to assign to all imported memories (the chat
            default 'chat_import' becomes 'document' for document files).
        tags: Optional tags to apply to all imported memories.
        extract_mode: Message extraction strategy (chat files only).
        max_length: Max characters per memory chunk.
        recursive: Whether to search subdirectories.
        kind: 'chat', 'document', or 'auto' (default) — per-file routing,
            see :func:`import_chat_file` (FT-02).

    Returns:
        Summary dict with keys: files_processed, imported, skipped,
        errors, total_memories_created, details.
    """
    root = Path(directory)
    if tags is None:
        tags = []
    extensions = {".json", ".jsonl", ".md", ".markdown", ".txt"}
    if recursive:
        files = [f for f in root.rglob("*") if f.suffix.lower() in extensions and f.is_file()]
    else:
        files = [f for f in root.iterdir() if f.suffix.lower() in extensions and f.is_file()]

    sem = asyncio.Semaphore(IMPORT_CONCURRENCY)

    async def _import_one(f: Path) -> dict[str, Any]:
        async with sem:
            try:
                return await asyncio.to_thread(
                    import_chat_file,
                    file_path=str(f),
                    category=category,
                    tags=tags,
                    extract_mode=extract_mode,
                    max_length=max_length,
                    kind=kind,
                )
            except (json.JSONDecodeError, UnicodeDecodeError, FileNotFoundError, OSError) as e:
                log.warning("Failed to import %s: %s", f.name, e)
                return {"status": "error", "file": f.name, "error": str(e)}

    results = list(await asyncio.gather(*[_import_one(f) for f in sorted(files)]))

    ok = [r for r in results if r.get("status") == "ok"]
    skipped = [r for r in results if r.get("status") == "skipped"]
    errors = [r for r in results if r.get("status") == "error"]

    return {
        "files_processed": len(results),
        "imported": len(ok),
        "skipped": len(skipped),
        "errors": len(errors),
        "total_memories_created": sum(r.get("memories_created", 0) for r in ok),
        "details": results,
    }


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "IMPORT_KINDS",
    "DOCUMENT_SOURCE",
    "DOCUMENT_CATEGORY",
    "import_chat_file",
    "import_directory",
    "_chunk_text",
    "_extract_messages_from_json",
    "_filter_messages",
    "_parse_markdown_chat",
    "_split_chat_markdown",
    "_looks_like_chat_markdown",
    "_split_markdown_sections",
    "_parse_document",
    "_file_hash",
    "_restore_graph_records",
]
