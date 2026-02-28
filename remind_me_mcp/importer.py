"""
remind_me_mcp.importer — Chat export import engine.

Handles parsing JSON, JSONL, and Markdown chat export formats, chunking text
into memory-sized pieces, and storing results into the database.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

from remind_me_mcp.db import _embed_and_store, _get_db, _make_id, _now_iso

log = logging.getLogger("remind_me_mcp.importer")

IMPORT_CONCURRENCY = 8

# Serializes SQLite write operations when import_chat_file runs concurrently
# in multiple asyncio.to_thread workers sharing the same DB connection.
_import_lock = threading.Lock()

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
        return [text]
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
        chunks.append(text[:idx].strip())
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

    # Standard {role, content} list
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                # Check if it's a conversation wrapper
                if "messages" in item or "chat_messages" in item:
                    messages.extend(_extract_messages_from_json(item, extract_mode))
                elif "role" in item or "sender" in item:
                    role = item.get("role", item.get("sender", "unknown"))
                    content = item.get("content", item.get("text", ""))
                    if isinstance(content, list):
                        content = "\n".join(
                            b.get("text", "") if isinstance(b, dict) else str(b)
                            for b in content
                        )
                    if isinstance(content, str) and content.strip():
                        messages.append({"role": role, "content": content.strip()})
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
    # Common patterns: "## Human", "## Assistant", "**User:**", etc.
    pattern = re.compile(
        r"(?:^|\n)(?:#{1,3}\s*|(?:\*\*))?(Human|User|Assistant|Claude|Bot|System)(?:\*\*)?[:\s]*\n?",
        re.IGNORECASE,
    )
    parts = pattern.split(text)
    messages: list[dict[str, str]] = []
    i = 1
    while i < len(parts) - 1:
        role = parts[i].strip().lower()
        content = parts[i + 1].strip()
        if content:
            messages.append({"role": role, "content": content})
        i += 2

    if not messages:
        # No structure detected — treat entire file as one memory
        return [text.strip()] if text.strip() else []

    return _filter_messages(messages, extract_mode)


# ---------------------------------------------------------------------------
# Public import function
# ---------------------------------------------------------------------------


def import_chat_file(
    file_path: str,
    category: str,
    tags: list[str],
    extract_mode: str,
    max_length: int,
) -> dict[str, Any]:
    """Import a single chat export file into the memory store.

    Parses the file based on its extension (.json, .jsonl, .md/.markdown/.txt),
    extracts messages according to extract_mode, chunks them, and stores each
    chunk as a separate memory. Deduplicates by file hash — if the same file
    content has already been imported, returns a 'skipped' result immediately.

    Args:
        file_path: Path to the chat export file.
        category: Category to assign to all imported memories.
        tags: Tags to apply to all imported memories.
        extract_mode: Message extraction strategy (e.g., 'assistant_messages').
        max_length: Maximum characters per memory chunk.

    Returns:
        A status dict. On success: {'status': 'ok', 'import_id': str,
        'memories_created': int, 'raw_entries': int, 'file': str}.
        On skip: {'status': 'skipped', 'reason': str, 'file': str,
        'import_id': str}. On unsupported format: {'status': 'error',
        'reason': str, 'file': str}.
    """
    path = Path(file_path)

    # --- Phase 1: File I/O and parsing (no lock needed; pure CPU/disk work) ---
    fhash = _file_hash(file_path)
    suffix = path.suffix.lower()

    if suffix not in (".json", ".jsonl", ".md", ".markdown", ".txt"):
        return {"status": "error", "reason": f"unsupported format: {suffix}", "file": path.name}

    raw = path.read_text(encoding="utf-8", errors="replace")
    contents: list[str] = []

    if suffix in (".json",):
        data = json.loads(raw)
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
                msgs = _extract_messages_from_json(obj, extract_mode)
                contents.extend(_filter_messages(msgs, extract_mode))
            except json.JSONDecodeError:
                log.debug("Skipping malformed JSONL line")
                continue
    elif suffix in (".md", ".markdown", ".txt"):
        contents = _parse_markdown_chat(raw, extract_mode)

    # Pre-compute chunk/embed pairs before acquiring the lock
    now = _now_iso()
    import_id = _make_id(file_path)
    embed_pairs: list[tuple[str, str]] = []
    for content in contents:
        if not content.strip():
            continue
        for chunk in _chunk_text(content, max_length):
            embed_pairs.append((_make_id(chunk), chunk))

    # --- Phase 2: DB writes (serialized via lock for thread safety) ---
    with _import_lock:
        db = _get_db()

        # Check for duplicate import
        existing = db.execute("SELECT import_id FROM chat_imports WHERE hash = ?", (fhash,)).fetchone()
        if existing:
            return {"status": "skipped", "reason": "already_imported", "file": path.name, "import_id": existing["import_id"]}

        # Chunk and store — collect (mem_id, chunk) pairs so the same IDs are used
        # for both INSERT and embedding (BUGF-01 fix: prevents ID mismatch)
        stored = 0
        for mem_id, chunk in embed_pairs:
            db.execute(
                """INSERT OR IGNORE INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    mem_id,
                    chunk,
                    category,
                    json.dumps(tags),
                    "chat_import",
                    json.dumps({"import_id": import_id, "filename": path.name}),
                    now,
                    now,
                ),
            )
            stored += 1

        db.commit()

        # Embed using the SAME mem_id that was used for INSERT (no recomputation)
        for mem_id, chunk in embed_pairs:
            _embed_and_store(mem_id, chunk)

        stats = {"memories_created": stored, "raw_entries": len(contents), "file": path.name}
        db.execute(
            "INSERT INTO chat_imports (import_id, filename, hash, imported_at, stats) VALUES (?, ?, ?, ?, ?)",
            (import_id, path.name, fhash, now, json.dumps(stats)),
        )
        db.commit()
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
) -> dict[str, Any]:
    """Import all chat export files from a directory concurrently.

    Scans for .json, .jsonl, .md, .markdown, and .txt files. Skips
    already-imported files (hash-based deduplication). Files are processed
    concurrently using asyncio.gather with a semaphore bounded by
    IMPORT_CONCURRENCY (default 8) to prevent resource exhaustion.

    Args:
        directory: Path to the directory containing chat export files.
        category: Category to assign to all imported memories.
        tags: Optional tags to apply to all imported memories.
        extract_mode: Message extraction strategy.
        max_length: Max characters per memory chunk.
        recursive: Whether to search subdirectories.

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
    "import_chat_file",
    "import_directory",
    "_chunk_text",
    "_extract_messages_from_json",
    "_filter_messages",
    "_parse_markdown_chat",
    "_file_hash",
]
