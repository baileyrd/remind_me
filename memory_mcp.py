"""
Claude Memory MCP Server — persistent, searchable memory across Claude interfaces.

Supports:
  - Claude.ai, Claude Code, Claude Desktop (via stdio transport)
  - Multi-machine sync via any file-sync tool (Syncthing, git, Dropbox, etc.)
  - Bulk import of exported chat histories (JSON, JSONL, Markdown)
  - Full-text search with SQLite FTS5
  - Tagging, categorization, and structured retrieval
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MEMORY_DIR = Path(os.environ.get("MEMORY_MCP_DIR", "~/.remind-me")).expanduser()
DB_PATH = MEMORY_DIR / "memory.db"
IMPORT_LOG = MEMORY_DIR / "import_log.json"

# Ensure directory exists
MEMORY_DIR.mkdir(parents=True, exist_ok=True)

# Logging — stderr only (stdio transport reserves stdout)
logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s | %(message)s")
log = logging.getLogger("memory_mcp")

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _get_db() -> sqlite3.Connection:
    """Open (and lazily initialize) the SQLite database."""
    db = sqlite3.connect(str(DB_PATH), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")  # safe for concurrent readers
    db.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(db)
    return db


def _ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id          TEXT PRIMARY KEY,
            content     TEXT NOT NULL,
            category    TEXT NOT NULL DEFAULT 'general',
            tags        TEXT NOT NULL DEFAULT '[]',  -- JSON array
            source      TEXT NOT NULL DEFAULT 'manual',
            metadata    TEXT NOT NULL DEFAULT '{}',  -- JSON object
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chat_imports (
            import_id   TEXT PRIMARY KEY,
            filename    TEXT NOT NULL,
            hash        TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            stats       TEXT NOT NULL DEFAULT '{}'
        );

        -- FTS5 virtual table for full-text search
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            content, category, tags,
            content='memories',
            content_rowid='rowid'
        );

        -- Triggers to keep FTS in sync
        CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(rowid, content, category, tags)
            VALUES (new.rowid, new.content, new.category, new.tags);
        END;

        CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content, category, tags)
            VALUES ('delete', old.rowid, old.content, old.category, old.tags);
        END;

        CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content, category, tags)
            VALUES ('delete', old.rowid, old.content, old.category, old.tags);
            INSERT INTO memories_fts(rowid, content, category, tags)
            VALUES (new.rowid, new.content, new.category, new.tags);
        END;

        CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category);
        CREATE INDEX IF NOT EXISTS idx_memories_source ON memories(source);
        CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at);
    """)
    db.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_id(content: str) -> str:
    """Deterministic short id from content hash + timestamp."""
    ts = _now_iso()
    return hashlib.sha256(f"{content}{ts}".encode()).hexdigest()[:12]


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for key in ("tags", "metadata", "stats"):
        if key in d and isinstance(d[key], str):
            try:
                d[key] = json.loads(d[key])
            except json.JSONDecodeError:
                pass
    return d

# ---------------------------------------------------------------------------
# Pydantic input models
# ---------------------------------------------------------------------------

class ResponseFormat(str, Enum):
    MARKDOWN = "markdown"
    JSON = "json"


class MemoryAddInput(BaseModel):
    """Input for adding a new memory."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    content: str = Field(..., description="The memory content to store. Can be a fact, note, preference, or any text.", min_length=1, max_length=50000)
    category: str = Field(default="general", description="Category for organization (e.g., 'preference', 'fact', 'project', 'person', 'decision')", max_length=100)
    tags: list[str] = Field(default_factory=list, description="Tags for filtering (e.g., ['python', 'work', 'important'])", max_length=20)
    source: str = Field(default="manual", description="Origin of this memory (e.g., 'manual', 'chat_import', 'observation')", max_length=100)
    metadata: dict[str, Any] = Field(default_factory=dict, description="Arbitrary metadata (e.g., {'conversation_id': '...', 'date': '...'})")


class MemorySearchInput(BaseModel):
    """Input for searching memories."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(..., description="Full-text search query. Supports FTS5 syntax: AND, OR, NOT, phrases in quotes, prefix*", min_length=1, max_length=500)
    category: Optional[str] = Field(default=None, description="Filter by category")
    tags: Optional[list[str]] = Field(default=None, description="Filter: memory must have ALL of these tags")
    limit: int = Field(default=20, description="Max results to return", ge=1, le=100)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class MemoryListInput(BaseModel):
    """Input for listing memories."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    category: Optional[str] = Field(default=None, description="Filter by category")
    tags: Optional[list[str]] = Field(default=None, description="Filter: memory must have ALL of these tags")
    source: Optional[str] = Field(default=None, description="Filter by source (e.g., 'chat_import', 'manual')")
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class MemoryUpdateInput(BaseModel):
    """Input for updating a memory."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    memory_id: str = Field(..., description="The ID of the memory to update", min_length=1)
    content: Optional[str] = Field(default=None, description="New content (if changing)", max_length=50000)
    category: Optional[str] = Field(default=None, max_length=100)
    tags: Optional[list[str]] = Field(default=None, max_length=20)
    metadata: Optional[dict[str, Any]] = Field(default=None)


class MemoryDeleteInput(BaseModel):
    """Input for deleting a memory."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    memory_id: str = Field(..., description="The ID of the memory to delete", min_length=1)


class ChatImportInput(BaseModel):
    """Input for importing chat exports into memory."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    file_path: str = Field(..., description="Path to the chat export file (JSON, JSONL, or Markdown)")
    category: str = Field(default="chat_import", description="Category to assign to imported memories")
    tags: list[str] = Field(default_factory=list, description="Additional tags to apply to all imported memories")
    extract_mode: str = Field(
        default="assistant_messages",
        description=(
            "What to extract: "
            "'assistant_messages' — only assistant turns, "
            "'user_messages' — only user turns, "
            "'all_messages' — both turns, "
            "'conversations' — full conversations as single memories, "
            "'summaries' — only content under 'summary' keys"
        )
    )
    max_length: int = Field(default=10000, description="Max characters per memory entry; longer content is chunked", ge=100, le=50000)

    @field_validator("file_path")
    @classmethod
    def validate_path(cls, v: str) -> str:
        p = Path(v).expanduser().resolve()
        if not p.exists():
            raise ValueError(f"File not found: {p}")
        if p.suffix.lower() not in (".json", ".jsonl", ".md", ".markdown", ".txt"):
            raise ValueError(f"Unsupported file type: {p.suffix}. Use .json, .jsonl, or .md")
        return str(p)


class MemoryStatsInput(BaseModel):
    """Input for getting memory statistics."""
    model_config = ConfigDict(extra="forbid")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class BulkImportDirInput(BaseModel):
    """Input for bulk-importing a directory of chat exports."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    directory: str = Field(..., description="Path to directory containing chat export files")
    category: str = Field(default="chat_import")
    tags: list[str] = Field(default_factory=list)
    extract_mode: str = Field(default="assistant_messages")
    max_length: int = Field(default=10000, ge=100, le=50000)
    recursive: bool = Field(default=True, description="Search subdirectories")

    @field_validator("directory")
    @classmethod
    def validate_dir(cls, v: str) -> str:
        p = Path(v).expanduser().resolve()
        if not p.is_dir():
            raise ValueError(f"Directory not found: {p}")
        return str(p)

# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_memory_md(m: dict) -> str:
    tags = ", ".join(m.get("tags", [])) if m.get("tags") else "none"
    meta = m.get("metadata", {})
    meta_str = ", ".join(f"{k}={v}" for k, v in meta.items()) if meta else ""
    lines = [
        f"### Memory `{m['id']}`",
        f"**Category:** {m['category']}  |  **Tags:** {tags}  |  **Source:** {m.get('source', '?')}",
    ]
    if meta_str:
        lines.append(f"**Metadata:** {meta_str}")
    lines.append(f"**Created:** {m['created_at']}  |  **Updated:** {m['updated_at']}")
    lines.append("")
    lines.append(m["content"][:2000] + ("…" if len(m["content"]) > 2000 else ""))
    lines.append("")
    return "\n".join(lines)


def _fmt_memories(memories: list[dict], fmt: ResponseFormat, total: int | None = None) -> str:
    if fmt == ResponseFormat.JSON:
        payload: dict[str, Any] = {"count": len(memories), "memories": memories}
        if total is not None:
            payload["total"] = total
        return json.dumps(payload, indent=2, default=str)
    if not memories:
        return "_No memories found._"
    parts = []
    if total is not None:
        parts.append(f"**Showing {len(memories)} of {total} memories**\n")
    for m in memories:
        parts.append(_fmt_memory_md(m))
    return "\n---\n".join(parts)

# ---------------------------------------------------------------------------
# Chat import engine
# ---------------------------------------------------------------------------

def _file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _chunk_text(text: str, max_len: int) -> list[str]:
    """Split text into chunks, preferring paragraph boundaries."""
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
    """
    Handles various JSON shapes:
      - List of {role, content} messages
      - Dict with 'messages' key
      - Claude export format with 'chat_messages' containing 'content' arrays
      - List of conversations
    Returns list of dicts with 'role' and 'content' keys.
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
    """Filter messages based on extract_mode and return content strings."""
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
    """Parse markdown-formatted chat exports."""
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


def import_chat_file(
    file_path: str,
    category: str,
    tags: list[str],
    extract_mode: str,
    max_length: int,
) -> dict[str, Any]:
    """Import a single chat export file. Returns stats dict."""
    path = Path(file_path)
    fhash = _file_hash(file_path)
    db = _get_db()

    # Check for duplicate import
    existing = db.execute("SELECT import_id FROM chat_imports WHERE hash = ?", (fhash,)).fetchone()
    if existing:
        return {"status": "skipped", "reason": "already_imported", "file": path.name, "import_id": existing["import_id"]}

    suffix = path.suffix.lower()
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
                continue
    elif suffix in (".md", ".markdown", ".txt"):
        contents = _parse_markdown_chat(raw, extract_mode)
    else:
        return {"status": "error", "reason": f"unsupported format: {suffix}", "file": path.name}

    # Chunk and store
    stored = 0
    now = _now_iso()
    import_id = _make_id(file_path)

    for content in contents:
        if not content.strip():
            continue
        for chunk in _chunk_text(content, max_length):
            mem_id = _make_id(chunk)
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

    stats = {"memories_created": stored, "raw_entries": len(contents), "file": path.name}
    db.execute(
        "INSERT INTO chat_imports (import_id, filename, hash, imported_at, stats) VALUES (?, ?, ?, ?, ?)",
        (import_id, path.name, fhash, now, json.dumps(stats)),
    )
    db.commit()
    return {"status": "ok", "import_id": import_id, **stats}

# ---------------------------------------------------------------------------
# Lifespan — open DB at startup, close on shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def app_lifespan():
    db = _get_db()
    log.info("Memory MCP started — db at %s", DB_PATH)
    yield {"db": db}
    db.close()

# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP("memory_mcp", lifespan=app_lifespan)

# ---- Tools ----------------------------------------------------------------

@mcp.tool(
    name="memory_add",
    annotations={
        "title": "Add a Memory",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def memory_add(params: MemoryAddInput) -> str:
    """Store a new memory. Use this to save facts, preferences, decisions, observations, or any information that should persist across conversations.

    Args:
        params (MemoryAddInput): Memory content and metadata.

    Returns:
        str: Confirmation with the new memory's ID.
    """
    db = _get_db()
    mem_id = _make_id(params.content)
    now = _now_iso()
    db.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            mem_id,
            params.content,
            params.category,
            json.dumps(params.tags),
            params.source,
            json.dumps(params.metadata),
            now,
            now,
        ),
    )
    db.commit()
    return f"✓ Memory stored with id `{mem_id}` in category '{params.category}'."


@mcp.tool(
    name="memory_search",
    annotations={
        "title": "Search Memories",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def memory_search(params: MemorySearchInput) -> str:
    """Full-text search across all stored memories. Supports FTS5 query syntax (AND, OR, NOT, "exact phrase", prefix*).

    Args:
        params (MemorySearchInput): Search query and optional filters.

    Returns:
        str: Matching memories in the requested format.
    """
    db = _get_db()
    try:
        rows = db.execute(
            """SELECT m.* FROM memories m
               JOIN memories_fts fts ON m.rowid = fts.rowid
               WHERE memories_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (params.query, params.limit),
        ).fetchall()
    except sqlite3.OperationalError as e:
        return f"Search error: {e}. Try simpler query terms or quote exact phrases."

    memories = [_row_to_dict(r) for r in rows]

    # Apply optional filters
    if params.category:
        memories = [m for m in memories if m["category"] == params.category]
    if params.tags:
        tag_set = set(params.tags)
        memories = [m for m in memories if tag_set.issubset(set(m.get("tags", [])))]

    return _fmt_memories(memories, params.response_format)


@mcp.tool(
    name="memory_list",
    annotations={
        "title": "List Memories",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def memory_list(params: MemoryListInput) -> str:
    """List memories with optional filtering by category, tags, or source. Results are paginated.

    Args:
        params (MemoryListInput): Filters and pagination.

    Returns:
        str: Memories in the requested format with pagination info.
    """
    db = _get_db()
    conditions: list[str] = []
    bindings: list[Any] = []

    if params.category:
        conditions.append("category = ?")
        bindings.append(params.category)
    if params.source:
        conditions.append("source = ?")
        bindings.append(params.source)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    total = db.execute(f"SELECT COUNT(*) as cnt FROM memories {where}", bindings).fetchone()["cnt"]
    rows = db.execute(
        f"SELECT * FROM memories {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        bindings + [params.limit, params.offset],
    ).fetchall()
    memories = [_row_to_dict(r) for r in rows]

    # Tag filtering in Python (JSON array in column)
    if params.tags:
        tag_set = set(params.tags)
        memories = [m for m in memories if tag_set.issubset(set(m.get("tags", [])))]

    return _fmt_memories(memories, params.response_format, total=total)


@mcp.tool(
    name="memory_get",
    annotations={
        "title": "Get a Memory by ID",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def memory_get(memory_id: str) -> str:
    """Retrieve a single memory by its ID.

    Args:
        memory_id (str): The memory ID.

    Returns:
        str: The memory in markdown format, or an error message.
    """
    db = _get_db()
    row = db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if not row:
        return f"Memory `{memory_id}` not found."
    return _fmt_memory_md(_row_to_dict(row))


@mcp.tool(
    name="memory_update",
    annotations={
        "title": "Update a Memory",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def memory_update(params: MemoryUpdateInput) -> str:
    """Update an existing memory's content, category, tags, or metadata.

    Args:
        params (MemoryUpdateInput): The memory ID and fields to update.

    Returns:
        str: Confirmation or error message.
    """
    db = _get_db()
    row = db.execute("SELECT * FROM memories WHERE id = ?", (params.memory_id,)).fetchone()
    if not row:
        return f"Memory `{params.memory_id}` not found."

    sets: list[str] = []
    bindings: list[Any] = []
    if params.content is not None:
        sets.append("content = ?")
        bindings.append(params.content)
    if params.category is not None:
        sets.append("category = ?")
        bindings.append(params.category)
    if params.tags is not None:
        sets.append("tags = ?")
        bindings.append(json.dumps(params.tags))
    if params.metadata is not None:
        sets.append("metadata = ?")
        bindings.append(json.dumps(params.metadata))

    if not sets:
        return "Nothing to update — no fields provided."

    sets.append("updated_at = ?")
    bindings.append(_now_iso())
    bindings.append(params.memory_id)

    db.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id = ?", bindings)
    db.commit()
    return f"✓ Memory `{params.memory_id}` updated."


@mcp.tool(
    name="memory_delete",
    annotations={
        "title": "Delete a Memory",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def memory_delete(params: MemoryDeleteInput) -> str:
    """Permanently delete a memory by ID.

    Args:
        params (MemoryDeleteInput): The memory ID to delete.

    Returns:
        str: Confirmation or error message.
    """
    db = _get_db()
    result = db.execute("DELETE FROM memories WHERE id = ?", (params.memory_id,))
    db.commit()
    if result.rowcount == 0:
        return f"Memory `{params.memory_id}` not found."
    return f"✓ Memory `{params.memory_id}` deleted."


@mcp.tool(
    name="memory_import_chat",
    annotations={
        "title": "Import Chat Export",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def memory_import_chat(params: ChatImportInput) -> str:
    """Import a chat export file (JSON, JSONL, or Markdown) into memory.

    Supports Claude's export format, OpenAI's export format, and generic {role, content} message arrays.
    Deduplicates by file hash — re-importing the same file is a no-op.

    Args:
        params (ChatImportInput): File path, extraction mode, and tagging options.

    Returns:
        str: Import statistics.
    """
    result = import_chat_file(
        file_path=params.file_path,
        category=params.category,
        tags=params.tags,
        extract_mode=params.extract_mode,
        max_length=params.max_length,
    )
    return json.dumps(result, indent=2)


@mcp.tool(
    name="memory_import_directory",
    annotations={
        "title": "Bulk Import Chat Directory",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def memory_import_directory(params: BulkImportDirInput) -> str:
    """Bulk import all chat export files from a directory.

    Scans for .json, .jsonl, and .md files. Skips already-imported files.

    Args:
        params (BulkImportDirInput): Directory path and import options.

    Returns:
        str: Summary of import results.
    """
    root = Path(params.directory)
    extensions = {".json", ".jsonl", ".md", ".markdown", ".txt"}
    if params.recursive:
        files = [f for f in root.rglob("*") if f.suffix.lower() in extensions and f.is_file()]
    else:
        files = [f for f in root.iterdir() if f.suffix.lower() in extensions and f.is_file()]

    results = []
    for f in sorted(files):
        try:
            r = import_chat_file(
                file_path=str(f),
                category=params.category,
                tags=params.tags,
                extract_mode=params.extract_mode,
                max_length=params.max_length,
            )
            results.append(r)
        except Exception as e:
            results.append({"status": "error", "file": f.name, "error": str(e)})

    ok = [r for r in results if r.get("status") == "ok"]
    skipped = [r for r in results if r.get("status") == "skipped"]
    errors = [r for r in results if r.get("status") == "error"]
    total_memories = sum(r.get("memories_created", 0) for r in ok)

    summary = {
        "files_processed": len(results),
        "imported": len(ok),
        "skipped": len(skipped),
        "errors": len(errors),
        "total_memories_created": total_memories,
        "details": results,
    }
    return json.dumps(summary, indent=2)


@mcp.tool(
    name="memory_stats",
    annotations={
        "title": "Memory Statistics",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def memory_stats(params: MemoryStatsInput) -> str:
    """Get statistics about the memory store: total count, categories, sources, recent activity.

    Args:
        params (MemoryStatsInput): Response format preference.

    Returns:
        str: Statistics in the requested format.
    """
    db = _get_db()
    total = db.execute("SELECT COUNT(*) as cnt FROM memories").fetchone()["cnt"]
    categories = db.execute(
        "SELECT category, COUNT(*) as cnt FROM memories GROUP BY category ORDER BY cnt DESC"
    ).fetchall()
    sources = db.execute(
        "SELECT source, COUNT(*) as cnt FROM memories GROUP BY source ORDER BY cnt DESC"
    ).fetchall()
    imports = db.execute("SELECT COUNT(*) as cnt FROM chat_imports").fetchone()["cnt"]
    recent = db.execute(
        "SELECT id, category, substr(content, 1, 80) as preview, created_at FROM memories ORDER BY created_at DESC LIMIT 5"
    ).fetchall()

    data = {
        "total_memories": total,
        "total_imports": imports,
        "categories": {r["category"]: r["cnt"] for r in categories},
        "sources": {r["source"]: r["cnt"] for r in sources},
        "recent": [dict(r) for r in recent],
        "db_path": str(DB_PATH),
        "db_size_mb": round(DB_PATH.stat().st_size / 1_048_576, 2) if DB_PATH.exists() else 0,
    }

    if params.response_format == ResponseFormat.JSON:
        return json.dumps(data, indent=2, default=str)

    lines = [
        f"## Memory Store Statistics",
        f"",
        f"**Total memories:** {total}",
        f"**Total imports:** {imports}",
        f"**Database:** `{DB_PATH}` ({data['db_size_mb']} MB)",
        f"",
        f"### Categories",
    ]
    for cat, cnt in data["categories"].items():
        lines.append(f"- **{cat}**: {cnt}")
    lines.append("")
    lines.append("### Sources")
    for src, cnt in data["sources"].items():
        lines.append(f"- **{src}**: {cnt}")
    lines.append("")
    lines.append("### Recent Memories")
    for r in data["recent"]:
        lines.append(f"- `{r['id']}` [{r['category']}] {r['preview']}…")
    return "\n".join(lines)


# ---- Resources ------------------------------------------------------------

@mcp.resource("memory://stats")
async def resource_stats() -> str:
    """Quick stats for the memory store."""
    db = _get_db()
    total = db.execute("SELECT COUNT(*) as cnt FROM memories").fetchone()["cnt"]
    return json.dumps({"total_memories": total, "db_path": str(DB_PATH)})


@mcp.resource("memory://categories")
async def resource_categories() -> str:
    """List all memory categories with counts."""
    db = _get_db()
    rows = db.execute("SELECT category, COUNT(*) as cnt FROM memories GROUP BY category ORDER BY cnt DESC").fetchall()
    return json.dumps({r["category"]: r["cnt"] for r in rows}, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()