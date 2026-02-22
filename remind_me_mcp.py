"""
Remind Me MCP Server — persistent, searchable memory across Claude interfaces.

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

MEMORY_DIR = Path(os.environ.get("REMIND_ME_MCP_DIR", "~/.remind-me")).expanduser()
DB_PATH = MEMORY_DIR / "memory.db"
IMPORT_LOG = MEMORY_DIR / "import_log.json"
PID_FILE = MEMORY_DIR / "server.pid"

# Ensure directory exists
MEMORY_DIR.mkdir(parents=True, exist_ok=True)

# Logging — stderr only (stdio transport reserves stdout)
logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s | %(message)s")
log = logging.getLogger("remind_me_mcp")


# ---------------------------------------------------------------------------
# Server instance detection
# ---------------------------------------------------------------------------

def _read_pid_file() -> dict[str, Any] | None:
    """Read the PID file to check if a UI server is running."""
    if not PID_FILE.exists():
        return None
    try:
        data = json.loads(PID_FILE.read_text())
        pid = data.get("pid")
        # Check if process is actually alive
        if pid:
            try:
                os.kill(pid, 0)  # signal 0 = just check existence
                return data
            except OSError:
                # Process is dead, clean up stale PID file
                PID_FILE.unlink(missing_ok=True)
                return None
        return None
    except (json.JSONDecodeError, KeyError, TypeError):
        PID_FILE.unlink(missing_ok=True)
        return None


def _write_pid_file(host: str, port: int) -> None:
    """Write PID file when UI server starts."""
    PID_FILE.write_text(json.dumps({
        "pid": os.getpid(),
        "host": host,
        "port": port,
        "url": f"http://{host}:{port}",
        "started_at": _now_iso(),
    }, indent=2))


def _remove_pid_file() -> None:
    """Clean up PID file on shutdown."""
    PID_FILE.unlink(missing_ok=True)


def _check_ui_server_health(url: str) -> bool:
    """Quick check if the UI server is actually responding."""
    import urllib.request
    try:
        req = urllib.request.Request(url + "/api/stats", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def get_server_status() -> dict[str, Any]:
    """Get the current status of all running instances."""
    info = _read_pid_file()
    if info and _check_ui_server_health(info.get("url", "")):
        return {
            "ui_server": "running",
            "ui_url": info["url"],
            "ui_pid": info["pid"],
            "ui_started": info.get("started_at", "unknown"),
            "db_path": str(DB_PATH),
            "db_exists": DB_PATH.exists(),
        }
    return {
        "ui_server": "stopped",
        "ui_url": None,
        "db_path": str(DB_PATH),
        "db_exists": DB_PATH.exists(),
    }

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


class AutoCaptureInput(BaseModel):
    """Input for automatically capturing a full conversation and its summary."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    conversation: str = Field(
        ...,
        description=(
            "The full conversation dialog to capture verbatim. "
            "Include all turns with role prefixes, e.g.:\n"
            "Human: ...\nAssistant: ...\nHuman: ...\nAssistant: ..."
        ),
        min_length=1,
        max_length=500000,
    )
    summary: str = Field(
        ...,
        description=(
            "A concise summary of the conversation covering: "
            "key topics discussed, decisions made, facts learned, "
            "preferences expressed, action items, and anything worth remembering. "
            "This is stored as a separate memory linked to the full dialog."
        ),
        min_length=1,
        max_length=50000,
    )
    title: str = Field(
        default="",
        description="Short title for the conversation (e.g., 'VLAN setup discussion', 'Python async patterns')",
        max_length=200,
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Tags for both the dialog and summary (e.g., ['python', 'work', 'architecture'])",
        max_length=20,
    )
    category: str = Field(
        default="conversation",
        description="Category for the summary. The full dialog always uses 'dialog' category.",
        max_length=100,
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional metadata (e.g., {'project': 'remind-me-mcp', 'context': 'Claude Desktop'})",
    )

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
async def app_lifespan(app):
    db = _get_db()
    log.info("Remind Me MCP started — db at %s", DB_PATH)
    yield {"db": db}
    db.close()

# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP("remind_me_mcp", lifespan=app_lifespan)

# ---- Tools ----------------------------------------------------------------

@mcp.tool(
    name="remind_me_add",
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
    name="remind_me_search",
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
    name="remind_me_list",
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
    name="remind_me_get",
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
    name="remind_me_update",
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
    name="remind_me_delete",
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
    name="remind_me_import_chat",
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
    name="remind_me_import_directory",
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
    name="remind_me_stats",
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


@mcp.tool(
    name="remind_me_auto_capture",
    annotations={
        "title": "Auto-Capture Conversation",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def remind_me_auto_capture(params: AutoCaptureInput) -> str:
    """Capture an entire conversation as two linked memories: the full verbatim dialog and a concise summary.

    Use this at the end of every conversation to persist both the raw exchange
    and a distilled summary of key information. The summary is linked to the
    dialog via metadata so they can be retrieved together.

    The full dialog is stored with category 'dialog' and the summary uses the
    category specified in params (default: 'conversation').

    Args:
        params (AutoCaptureInput): The conversation text, summary, tags, and metadata.

    Returns:
        str: Confirmation with both memory IDs.
    """
    db = _get_db()
    now = _now_iso()

    # Generate a shared capture_id to link dialog + summary
    capture_id = _make_id(params.conversation[:200] + params.summary[:200])

    title = params.title or params.summary[:80].split("\n")[0]

    # -- Store the full dialog --
    dialog_id = _make_id(params.conversation)
    dialog_meta = {
        **params.metadata,
        "capture_id": capture_id,
        "linked_summary": "",  # placeholder, filled after summary is created
        "title": title,
        "type": "dialog",
    }
    db.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            dialog_id,
            params.conversation,
            "dialog",
            json.dumps(params.tags),
            "auto_capture",
            json.dumps(dialog_meta),
            now,
            now,
        ),
    )

    # -- Store the summary --
    summary_id = _make_id(params.summary)
    summary_meta = {
        **params.metadata,
        "capture_id": capture_id,
        "linked_dialog": dialog_id,
        "title": title,
        "type": "summary",
    }
    db.execute(
        """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            summary_id,
            params.summary,
            params.category,
            json.dumps(params.tags),
            "auto_capture",
            json.dumps(summary_meta),
            now,
            now,
        ),
    )

    # -- Back-link the dialog to the summary --
    dialog_meta["linked_summary"] = summary_id
    db.execute(
        "UPDATE memories SET metadata = ? WHERE id = ?",
        (json.dumps(dialog_meta), dialog_id),
    )

    db.commit()

    tag_str = ", ".join(params.tags) if params.tags else "none"
    return (
        f"✓ Conversation captured!\n\n"
        f"**Title:** {title}\n"
        f"**Dialog:** `{dialog_id}` (category: dialog, {len(params.conversation)} chars)\n"
        f"**Summary:** `{summary_id}` (category: {params.category})\n"
        f"**Tags:** {tag_str}\n"
        f"**Capture ID:** `{capture_id}` (links both memories)\n\n"
        f"The full dialog and summary are linked — search for either and "
        f"use `remind_me_get_capture` with capture_id `{capture_id}` to retrieve both."
    )


@mcp.tool(
    name="remind_me_get_capture",
    annotations={
        "title": "Get Linked Dialog + Summary",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_get_capture(capture_id: str) -> str:
    """Retrieve a linked dialog and summary pair by their shared capture_id.

    When a conversation is auto-captured, both the full dialog and its summary
    share a capture_id in their metadata. This tool retrieves both.

    Args:
        capture_id (str): The capture_id that links a dialog and summary.

    Returns:
        str: Both memories formatted together, or an error if not found.
    """
    db = _get_db()
    # Search for memories with this capture_id in metadata
    rows = db.execute(
        "SELECT * FROM memories WHERE metadata LIKE ? ORDER BY category",
        (f'%"capture_id": "{capture_id}"%',),
    ).fetchall()

    if not rows:
        # Try alternate JSON formatting (no space after colon)
        rows = db.execute(
            "SELECT * FROM memories WHERE metadata LIKE ? ORDER BY category",
            (f'%"capture_id":"{capture_id}"%',),
        ).fetchall()

    if not rows:
        return f"No capture found with id `{capture_id}`."

    memories = [_row_to_dict(r) for r in rows]
    dialog = next((m for m in memories if m.get("metadata", {}).get("type") == "dialog"), None)
    summary = next((m for m in memories if m.get("metadata", {}).get("type") == "summary"), None)

    title = (summary or dialog or {}).get("metadata", {}).get("title", "Untitled")
    parts = [f"## Capture: {title}", f"**Capture ID:** `{capture_id}`\n"]

    if summary:
        tags = ", ".join(summary.get("tags", [])) or "none"
        parts.append(f"### Summary (`{summary['id']}`)")
        parts.append(f"**Category:** {summary['category']}  |  **Tags:** {tags}")
        parts.append(f"**Captured:** {summary['created_at']}\n")
        parts.append(summary["content"])
        parts.append("")

    if dialog:
        char_count = len(dialog["content"])
        parts.append(f"### Full Dialog (`{dialog['id']}` — {char_count:,} chars)")
        parts.append(f"**Category:** dialog\n")
        # Show first 3000 chars with truncation notice
        if char_count > 3000:
            parts.append(dialog["content"][:3000])
            parts.append(f"\n\n… _({char_count - 3000:,} more characters — use `remind_me_get` with id `{dialog['id']}` for full text)_")
        else:
            parts.append(dialog["content"])

    if not dialog and not summary:
        parts.append("_Found memories with this capture_id but couldn't identify dialog/summary types._\n")
        for m in memories:
            parts.append(_fmt_memory_md(m))

    return "\n".join(parts)


@mcp.tool(
    name="remind_me_server_status",
    annotations={
        "title": "Server Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def remind_me_server_status() -> str:
    """Check the status of Remind Me services: whether the UI dashboard server is running, the database path, and connection info.

    Use this to verify the system is operational or to get the dashboard URL.

    Returns:
        str: Status information about running instances.
    """
    status = get_server_status()
    lines = ["## Remind Me Server Status\n"]

    if status["ui_server"] == "running":
        lines.append(f"**Dashboard UI:** ✓ Running at {status['ui_url']}")
        lines.append(f"**UI PID:** {status['ui_pid']}")
        lines.append(f"**Started:** {status['ui_started']}")
    else:
        lines.append("**Dashboard UI:** ✗ Not running")
        lines.append("_Start with: `python remind_me_mcp.py --serve-ui`_")

    lines.append(f"\n**Database:** `{status['db_path']}`")
    lines.append(f"**DB exists:** {'yes' if status['db_exists'] else 'no'}")
    lines.append(f"\n**MCP (stdio):** ✓ Active (this connection)")

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
# HTTP API Server (optional — for dashboard UI)
# ---------------------------------------------------------------------------

SERVE_UI = os.environ.get("REMIND_ME_MCP_SERVE_UI", "").lower() in ("true", "1", "yes")
UI_PORT = int(os.environ.get("REMIND_ME_MCP_UI_PORT", "5199"))


def _build_api_app():
    """Build a Starlette ASGI app that exposes the memory DB as a REST API."""
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.cors import CORSMiddleware
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse
    from starlette.routing import Route

    # -- helpers --
    def _json_ok(data: Any, status: int = 200) -> JSONResponse:
        return JSONResponse(data, status_code=status)

    def _json_err(msg: str, status: int = 400) -> JSONResponse:
        return JSONResponse({"error": msg}, status_code=status)

    # -- routes --

    async def api_stats(request: Request) -> JSONResponse:
        db = _get_db()
        total = db.execute("SELECT COUNT(*) as cnt FROM memories").fetchone()["cnt"]
        categories = db.execute(
            "SELECT category, COUNT(*) as cnt FROM memories GROUP BY category ORDER BY cnt DESC"
        ).fetchall()
        sources = db.execute(
            "SELECT source, COUNT(*) as cnt FROM memories GROUP BY source ORDER BY cnt DESC"
        ).fetchall()
        imports = db.execute("SELECT COUNT(*) as cnt FROM chat_imports").fetchone()["cnt"]
        all_tags: dict[str, int] = {}
        for row in db.execute("SELECT tags FROM memories").fetchall():
            try:
                for t in json.loads(row["tags"]):
                    all_tags[t] = all_tags.get(t, 0) + 1
            except (json.JSONDecodeError, TypeError):
                pass
        return _json_ok({
            "total": total,
            "imports": imports,
            "categories": {r["category"]: r["cnt"] for r in categories},
            "sources": {r["source"]: r["cnt"] for r in sources},
            "tags": dict(sorted(all_tags.items(), key=lambda x: -x[1])),
            "db_path": str(DB_PATH),
            "db_size_mb": round(DB_PATH.stat().st_size / 1_048_576, 2) if DB_PATH.exists() else 0,
        })

    async def api_list(request: Request) -> JSONResponse:
        db = _get_db()
        params = request.query_params
        conditions: list[str] = []
        bindings: list[Any] = []

        if cat := params.get("category"):
            conditions.append("category = ?")
            bindings.append(cat)
        if src := params.get("source"):
            conditions.append("source = ?")
            bindings.append(src)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        limit = min(int(params.get("limit", 50)), 200)
        offset = max(int(params.get("offset", 0)), 0)

        total = db.execute(f"SELECT COUNT(*) as cnt FROM memories {where}", bindings).fetchone()["cnt"]
        rows = db.execute(
            f"SELECT * FROM memories {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            bindings + [limit, offset],
        ).fetchall()
        memories = [_row_to_dict(r) for r in rows]

        # Tag filtering
        if tag_param := params.get("tags"):
            tag_set = set(tag_param.split(","))
            memories = [m for m in memories if tag_set.issubset(set(m.get("tags", [])))]

        return _json_ok({
            "total": total,
            "count": len(memories),
            "offset": offset,
            "has_more": total > offset + limit,
            "memories": memories,
        })

    async def api_search(request: Request) -> JSONResponse:
        db = _get_db()
        params = request.query_params
        query = params.get("q", "").strip()
        if not query:
            return _json_err("Missing 'q' parameter")

        limit = min(int(params.get("limit", 50)), 200)
        try:
            rows = db.execute(
                """SELECT m.* FROM memories m
                   JOIN memories_fts fts ON m.rowid = fts.rowid
                   WHERE memories_fts MATCH ?
                   ORDER BY rank LIMIT ?""",
                (query, limit),
            ).fetchall()
        except sqlite3.OperationalError as e:
            return _json_err(f"Search error: {e}")

        memories = [_row_to_dict(r) for r in rows]

        if cat := params.get("category"):
            memories = [m for m in memories if m["category"] == cat]
        if tag_param := params.get("tags"):
            tag_set = set(tag_param.split(","))
            memories = [m for m in memories if tag_set.issubset(set(m.get("tags", [])))]

        return _json_ok({"count": len(memories), "memories": memories})

    async def api_get(request: Request) -> JSONResponse:
        db = _get_db()
        memory_id = request.path_params["memory_id"]
        row = db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if not row:
            return _json_err("Not found", 404)
        return _json_ok(_row_to_dict(row))

    async def api_add(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return _json_err("Invalid JSON body")

        content = body.get("content", "").strip()
        if not content:
            return _json_err("'content' is required")

        db = _get_db()
        mem_id = _make_id(content)
        now = _now_iso()
        category = body.get("category", "general")
        tags = body.get("tags", [])
        source = body.get("source", "manual")
        metadata = body.get("metadata", {})

        db.execute(
            """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (mem_id, content, category, json.dumps(tags), source, json.dumps(metadata), now, now),
        )
        db.commit()
        row = db.execute("SELECT * FROM memories WHERE id = ?", (mem_id,)).fetchone()
        return _json_ok(_row_to_dict(row), status=201)

    async def api_update(request: Request) -> JSONResponse:
        memory_id = request.path_params["memory_id"]
        try:
            body = await request.json()
        except Exception:
            return _json_err("Invalid JSON body")

        db = _get_db()
        row = db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if not row:
            return _json_err("Not found", 404)

        sets: list[str] = []
        bindings: list[Any] = []
        for field in ("content", "category", "source"):
            if field in body and body[field] is not None:
                sets.append(f"{field} = ?")
                bindings.append(body[field])
        if "tags" in body and body["tags"] is not None:
            sets.append("tags = ?")
            bindings.append(json.dumps(body["tags"]))
        if "metadata" in body and body["metadata"] is not None:
            sets.append("metadata = ?")
            bindings.append(json.dumps(body["metadata"]))

        if not sets:
            return _json_err("No fields to update")

        sets.append("updated_at = ?")
        bindings.append(_now_iso())
        bindings.append(memory_id)

        db.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id = ?", bindings)
        db.commit()
        updated = db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return _json_ok(_row_to_dict(updated))

    async def api_delete(request: Request) -> JSONResponse:
        memory_id = request.path_params["memory_id"]
        db = _get_db()
        result = db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        db.commit()
        if result.rowcount == 0:
            return _json_err("Not found", 404)
        return _json_ok({"deleted": memory_id})

    async def api_import(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return _json_err("Invalid JSON body")

        file_path = body.get("file_path", "").strip()
        if not file_path:
            return _json_err("'file_path' is required")

        p = Path(file_path).expanduser().resolve()
        if not p.exists():
            return _json_err(f"Path not found: {p}")

        category = body.get("category", "chat_import")
        tags = body.get("tags", [])
        extract_mode = body.get("extract_mode", "assistant_messages")
        max_length = body.get("max_length", 10000)

        try:
            if p.is_dir():
                # Directory import
                extensions = {".json", ".jsonl", ".md", ".markdown", ".txt"}
                files = [f for f in p.rglob("*") if f.suffix.lower() in extensions and f.is_file()]
                results = []
                for f in sorted(files):
                    try:
                        r = import_chat_file(str(f), category, tags, extract_mode, max_length)
                        results.append(r)
                    except Exception as e:
                        results.append({"status": "error", "file": f.name, "error": str(e)})
                ok = [r for r in results if r.get("status") == "ok"]
                skipped = [r for r in results if r.get("status") == "skipped"]
                errors = [r for r in results if r.get("status") == "error"]
                return _json_ok({
                    "files_processed": len(results),
                    "imported": len(ok),
                    "skipped": len(skipped),
                    "errors": len(errors),
                    "total_memories_created": sum(r.get("memories_created", 0) for r in ok),
                    "details": results,
                })
            else:
                # Single file import
                result = import_chat_file(str(p), category, tags, extract_mode, max_length)
                return _json_ok(result)
        except Exception as e:
            return _json_err(f"Import error: {e}")

    async def index(request: Request) -> HTMLResponse:
        """Serve the dashboard UI as a single-page app."""
        return HTMLResponse(_build_dashboard_html())

    routes = [
        Route("/", index),
        Route("/api/stats", api_stats),
        Route("/api/memories", api_list, methods=["GET"]),
        Route("/api/memories", api_add, methods=["POST"]),
        Route("/api/memories/search", api_search),
        Route("/api/memories/{memory_id}", api_get, methods=["GET"]),
        Route("/api/memories/{memory_id}", api_update, methods=["PUT", "PATCH"]),
        Route("/api/memories/{memory_id}", api_delete, methods=["DELETE"]),
        Route("/api/import", api_import, methods=["POST"]),
    ]

    middleware = [
        Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]),
    ]

    return Starlette(routes=routes, middleware=middleware)


def _build_dashboard_html() -> str:
    """Return a self-contained HTML page with the React dashboard embedded."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Remind Me — Memory Dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0a0a0f; color: #e4e4ed; font-family: 'IBM Plex Sans', -apple-system, BlinkMacSystemFont, sans-serif; }
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap');
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: #2a2a3a; border-radius: 3px; }
  ::selection { background: rgba(99,102,241,0.25); }
</style>
</head>
<body>
<div id="root"></div>
<script src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
<script src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
<script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
<script type="text/babel">
""" + _get_dashboard_script() + """
</script>
</body>
</html>"""


def _get_dashboard_script() -> str:
    """Return the React dashboard JS, wired to the live API."""
    return r'''
const { useState, useEffect, useCallback, useRef } = React;

const API = window.location.origin + "/api";

const theme = {
  bg: "#0a0a0f", surface: "#12121a", surfaceHover: "#1a1a26", surfaceActive: "#22222e",
  border: "#2a2a3a", borderFocus: "#6366f1", text: "#e4e4ed", textSecondary: "#8888a4",
  textMuted: "#55556a", accent: "#6366f1", accentHover: "#818cf8",
  accentSubtle: "rgba(99,102,241,0.12)", danger: "#ef4444", dangerSubtle: "rgba(239,68,68,0.12)",
  success: "#22c55e", successSubtle: "rgba(34,197,94,0.12)",
  warning: "#f59e0b", warningSubtle: "rgba(245,158,11,0.12)",
  categoryColors: {
    general: "#6366f1", preference: "#f59e0b", fact: "#22c55e", project: "#06b6d4",
    person: "#ec4899", decision: "#8b5cf6", chat_import: "#64748b", observation: "#14b8a6",
  },
};
const mono = "'IBM Plex Mono', 'JetBrains Mono', monospace";
const sans = "'IBM Plex Sans', -apple-system, sans-serif";

// --- API layer ---
async function api(path, opts = {}) {
  const url = path.startsWith("http") ? path : API + path;
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  return res.json();
}

function useMemoryStore() {
  const [memories, setMemories] = useState([]);
  const [stats, setStats] = useState({ total: 0, categories: {}, sources: {}, tags: {} });
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async (params = {}) => {
    setLoading(true);
    try {
      const qs = new URLSearchParams();
      if (params.category) qs.set("category", params.category);
      if (params.tags && params.tags.length) qs.set("tags", params.tags.join(","));
      if (params.source) qs.set("source", params.source);
      qs.set("limit", "200");
      const data = await api("/memories?" + qs.toString());
      setMemories(data.memories || []);
    } catch (e) { console.error("refresh:", e); }
    try {
      const s = await api("/stats");
      setStats(s);
    } catch (e) { console.error("stats:", e); }
    setLoading(false);
  }, []);

  const search = useCallback(async (query, category, tags) => {
    if (!query.trim()) { refresh({ category, tags }); return; }
    setLoading(true);
    try {
      const qs = new URLSearchParams({ q: query, limit: "200" });
      if (category) qs.set("category", category);
      if (tags && tags.length) qs.set("tags", tags.join(","));
      const data = await api("/memories/search?" + qs.toString());
      setMemories(data.memories || []);
    } catch (e) { console.error("search:", e); }
    setLoading(false);
  }, [refresh]);

  const add = useCallback(async (mem) => {
    await api("/memories", { method: "POST", body: mem });
    refresh();
  }, [refresh]);

  const update = useCallback(async (id, updates) => {
    await api("/memories/" + id, { method: "PUT", body: updates });
    refresh();
  }, [refresh]);

  const remove = useCallback(async (id) => {
    await api("/memories/" + id, { method: "DELETE" });
    refresh();
  }, [refresh]);

  useEffect(() => { refresh(); }, [refresh]);

  return { memories, stats, loading, refresh, search, add, update, remove };
}

// --- Icons ---
const Icons = {
  Search: () => React.createElement("svg", {width:16,height:16,viewBox:"0 0 24 24",fill:"none",stroke:"currentColor",strokeWidth:2,strokeLinecap:"round"}, React.createElement("circle",{cx:11,cy:11,r:8}), React.createElement("path",{d:"m21 21-4.35-4.35"})),
  Plus: () => React.createElement("svg", {width:16,height:16,viewBox:"0 0 24 24",fill:"none",stroke:"currentColor",strokeWidth:2,strokeLinecap:"round"}, React.createElement("path",{d:"M12 5v14M5 12h14"})),
  Trash: () => React.createElement("svg", {width:14,height:14,viewBox:"0 0 24 24",fill:"none",stroke:"currentColor",strokeWidth:2,strokeLinecap:"round"}, React.createElement("polyline",{points:"3 6 5 6 21 6"}), React.createElement("path",{d:"M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"})),
  Edit: () => React.createElement("svg", {width:14,height:14,viewBox:"0 0 24 24",fill:"none",stroke:"currentColor",strokeWidth:2,strokeLinecap:"round"}, React.createElement("path",{d:"M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"}), React.createElement("path",{d:"M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"})),
  Brain: () => React.createElement("svg", {width:20,height:20,viewBox:"0 0 24 24",fill:"none",stroke:"currentColor",strokeWidth:1.5,strokeLinecap:"round"}, React.createElement("path",{d:"M12 2a7 7 0 0 0-7 7c0 3 2 5.5 4 7l3 3 3-3c2-1.5 4-4 4-7a7 7 0 0 0-7-7z"}), React.createElement("path",{d:"M12 2v10"}), React.createElement("path",{d:"M8 6c1.5 1 3 1.5 4 1.5s2.5-.5 4-1.5"})),
  Chart: () => React.createElement("svg", {width:18,height:18,viewBox:"0 0 24 24",fill:"none",stroke:"currentColor",strokeWidth:2,strokeLinecap:"round"}, React.createElement("path",{d:"M18 20V10M12 20V4M6 20v-6"})),
  Upload: () => React.createElement("svg", {width:16,height:16,viewBox:"0 0 24 24",fill:"none",stroke:"currentColor",strokeWidth:2,strokeLinecap:"round"}, React.createElement("path",{d:"M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"}), React.createElement("polyline",{points:"17 8 12 3 7 8"}), React.createElement("line",{x1:12,y1:3,x2:12,y2:15})),
  X: () => React.createElement("svg", {width:14,height:14,viewBox:"0 0 24 24",fill:"none",stroke:"currentColor",strokeWidth:2,strokeLinecap:"round"}, React.createElement("path",{d:"M18 6 6 18M6 6l12 12"})),
  Copy: () => React.createElement("svg", {width:14,height:14,viewBox:"0 0 24 24",fill:"none",stroke:"currentColor",strokeWidth:2,strokeLinecap:"round"}, React.createElement("rect",{x:9,y:9,width:13,height:13,rx:2}), React.createElement("path",{d:"M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"})),
  Tag: () => React.createElement("svg", {width:12,height:12,viewBox:"0 0 24 24",fill:"none",stroke:"currentColor",strokeWidth:2,strokeLinecap:"round"}, React.createElement("path",{d:"M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"}), React.createElement("line",{x1:7,y1:7,x2:7.01,y2:7})),
  Check: () => React.createElement("svg", {width:14,height:14,viewBox:"0 0 24 24",fill:"none",stroke:"currentColor",strokeWidth:2.5,strokeLinecap:"round"}, React.createElement("polyline",{points:"20 6 9 17 4 12"})),
  Database: () => React.createElement("svg", {width:16,height:16,viewBox:"0 0 24 24",fill:"none",stroke:"currentColor",strokeWidth:2,strokeLinecap:"round"}, React.createElement("ellipse",{cx:12,cy:5,rx:9,ry:3}), React.createElement("path",{d:"M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"}), React.createElement("path",{d:"M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"})),
  Loader: () => React.createElement("svg", {width:16,height:16,viewBox:"0 0 24 24",fill:"none",stroke:"currentColor",strokeWidth:2,strokeLinecap:"round",style:{animation:"spin 1s linear infinite"}}, React.createElement("path",{d:"M21 12a9 9 0 1 1-6.219-8.56"})),
};

const iconBtn = { background:"none", border:"none", color:theme.textSecondary, cursor:"pointer", padding:4, borderRadius:4, display:"flex", alignItems:"center", transition:"color 0.15s" };
const inputSt = { width:"100%", padding:"10px 12px", borderRadius:6, border:"1px solid "+theme.border, background:theme.bg, color:theme.text, fontSize:14, fontFamily:sans, outline:"none", transition:"border-color 0.15s", boxSizing:"border-box" };
const labelSt = { display:"block", fontSize:12, fontWeight:600, fontFamily:mono, color:theme.textSecondary, marginBottom:6, textTransform:"uppercase", letterSpacing:"0.04em" };

function CategoryBadge({category}) {
  const c = theme.categoryColors[category] || theme.accent;
  return React.createElement("span", {style:{display:"inline-flex",alignItems:"center",gap:4,padding:"2px 8px",borderRadius:4,fontSize:11,fontWeight:600,fontFamily:mono,letterSpacing:"0.04em",textTransform:"uppercase",background:c+"18",color:c,border:"1px solid "+c+"30"}}, category);
}

function TagPill({tag, onClick, removable, onRemove}) {
  return React.createElement("span", {onClick, style:{display:"inline-flex",alignItems:"center",gap:3,padding:"1px 7px",borderRadius:3,fontSize:11,fontFamily:mono,background:theme.surfaceActive,color:theme.textSecondary,border:"1px solid "+theme.border,cursor:onClick?"pointer":"default",transition:"all 0.15s"}},
    React.createElement(Icons.Tag), tag,
    removable && onRemove && React.createElement("span", {onClick:e=>{e.stopPropagation();onRemove()}, style:{cursor:"pointer",marginLeft:2,opacity:0.6}}, "\u00d7")
  );
}

function MemoryCard({memory:m, onEdit, onDelete, onTagClick, expanded, onToggle}) {
  const [copied, setCopied] = useState(false);
  const handleCopy = () => { navigator.clipboard.writeText(m.content); setCopied(true); setTimeout(()=>setCopied(false),1500); };
  const isLong = m.content.length > 200;
  const display = expanded || !isLong ? m.content : m.content.slice(0,200) + "\u2026";
  const meta = Object.entries(m.metadata||{}).filter(([k])=>k!=="import_id");

  return React.createElement("div", {style:{background:theme.surface,border:"1px solid "+theme.border,borderRadius:8,padding:"16px 18px",transition:"all 0.2s"},
    onMouseEnter:e=>{e.currentTarget.style.borderColor=theme.borderFocus+"60";e.currentTarget.style.background=theme.surfaceHover},
    onMouseLeave:e=>{e.currentTarget.style.borderColor=theme.border;e.currentTarget.style.background=theme.surface}},
    // header
    React.createElement("div", {style:{display:"flex",justifyContent:"space-between",alignItems:"flex-start",marginBottom:8}},
      React.createElement("div", {style:{display:"flex",alignItems:"center",gap:8,flexWrap:"wrap"}},
        React.createElement(CategoryBadge, {category:m.category}),
        React.createElement("code", {style:{fontSize:11,color:theme.textMuted,fontFamily:mono}}, m.id),
        m.source==="chat_import" && m.metadata?.filename && React.createElement("span",{style:{fontSize:11,color:theme.textMuted,fontFamily:mono}},"\u2190 "+m.metadata.filename)
      ),
      React.createElement("div", {style:{display:"flex",gap:4}},
        React.createElement("button", {onClick:handleCopy,title:"Copy",style:iconBtn}, copied ? React.createElement(Icons.Check) : React.createElement(Icons.Copy)),
        React.createElement("button", {onClick:()=>onEdit(m),title:"Edit",style:iconBtn}, React.createElement(Icons.Edit)),
        React.createElement("button", {onClick:()=>onDelete(m.id),title:"Delete",style:{...iconBtn,color:theme.danger}}, React.createElement(Icons.Trash))
      )
    ),
    // content
    React.createElement("div", {onClick:isLong?onToggle:undefined,style:{fontFamily:sans,fontSize:14,lineHeight:1.65,color:theme.text,whiteSpace:"pre-wrap",wordBreak:"break-word",cursor:isLong?"pointer":"default"}}, display),
    isLong && React.createElement("button", {onClick:onToggle,style:{background:"none",border:"none",color:theme.accent,fontSize:12,fontFamily:mono,cursor:"pointer",padding:"4px 0",marginTop:4}}, expanded?"Show less":"Show more"),
    // tags
    React.createElement("div", {style:{display:"flex",flexWrap:"wrap",gap:4,marginTop:10}},
      (m.tags||[]).map(t => React.createElement(TagPill, {key:t, tag:t, onClick:()=>onTagClick(t)}))
    ),
    meta.length > 0 && React.createElement("div",{style:{marginTop:6,fontSize:11,color:theme.textMuted,fontFamily:mono}}, meta.map(([k,v])=>k+": "+v).join(" \u00b7 ")),
    React.createElement("div", {style:{marginTop:8,fontSize:11,color:theme.textMuted,fontFamily:mono}},
      new Date(m.created_at).toLocaleDateString("en-US",{month:"short",day:"numeric",year:"numeric"}),
      m.updated_at !== m.created_at ? " \u00b7 edited "+new Date(m.updated_at).toLocaleDateString("en-US",{month:"short",day:"numeric"}) : ""
    )
  );
}

function StatCard({label, value, color, icon}) {
  return React.createElement("div", {style:{background:theme.surface,border:"1px solid "+theme.border,borderRadius:8,padding:"14px 16px",flex:"1 1 140px",minWidth:140}},
    React.createElement("div", {style:{display:"flex",alignItems:"center",gap:6,marginBottom:6}},
      React.createElement("span",{style:{color:color||theme.accent}}, icon),
      React.createElement("span",{style:{fontSize:11,color:theme.textMuted,fontFamily:mono,textTransform:"uppercase",letterSpacing:"0.06em"}}, label)
    ),
    React.createElement("div", {style:{fontSize:28,fontWeight:700,fontFamily:mono,color:theme.text,lineHeight:1}}, value)
  );
}

function BarChart({data, colorMap}) {
  const max = Math.max(...Object.values(data), 1);
  const entries = Object.entries(data).sort((a,b)=>b[1]-a[1]);
  return React.createElement("div", {style:{display:"flex",flexDirection:"column",gap:6}},
    entries.map(([label, count]) =>
      React.createElement("div", {key:label, style:{display:"flex",alignItems:"center",gap:8}},
        React.createElement("span", {style:{width:90,fontSize:12,fontFamily:mono,color:theme.textSecondary,textAlign:"right",flexShrink:0}}, label),
        React.createElement("div", {style:{flex:1,height:20,background:theme.surfaceActive,borderRadius:3,overflow:"hidden"}},
          React.createElement("div", {style:{height:"100%",width:(count/max*100)+"%",background:(colorMap&&colorMap[label])||theme.accent,borderRadius:3,transition:"width 0.4s ease",display:"flex",alignItems:"center",justifyContent:"flex-end",paddingRight:6}},
            React.createElement("span",{style:{fontSize:10,fontWeight:700,fontFamily:mono,color:"#fff"}}, count)
          )
        )
      )
    )
  );
}

function Modal({open, onClose, title, children, width}) {
  if (!open) return null;
  return React.createElement("div", {style:{position:"fixed",inset:0,zIndex:1000,display:"flex",alignItems:"center",justifyContent:"center",background:"rgba(0,0,0,0.65)",backdropFilter:"blur(4px)"}, onClick:onClose},
    React.createElement("div", {onClick:e=>e.stopPropagation(), style:{background:theme.surface,border:"1px solid "+theme.border,borderRadius:12,padding:24,width:width||480,maxWidth:"92vw",maxHeight:"85vh",overflowY:"auto",boxShadow:"0 24px 80px rgba(0,0,0,0.5)"}},
      React.createElement("div", {style:{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:20}},
        React.createElement("h2",{style:{margin:0,fontSize:18,fontFamily:sans,fontWeight:600,color:theme.text}}, title),
        React.createElement("button",{onClick:onClose,style:{...iconBtn,color:theme.textMuted}}, React.createElement(Icons.X))
      ),
      children
    )
  );
}

function MemoryForm({initial, onSubmit, onCancel}) {
  const [content, setContent] = useState(initial?.content||"");
  const [category, setCategory] = useState(initial?.category||"general");
  const [tagInput, setTagInput] = useState("");
  const [tags, setTags] = useState(initial?.tags||[]);
  const categories = ["general","preference","fact","project","person","decision","observation"];

  const handleTagKey = e => {
    if ((e.key==="Enter"||e.key===",") && tagInput.trim()) {
      e.preventDefault();
      const t = tagInput.trim().toLowerCase().replace(/,/g,"");
      if (t && !tags.includes(t)) setTags([...tags, t]);
      setTagInput("");
    }
  };
  const handleSubmit = () => { if (content.trim()) onSubmit({content:content.trim(), category, tags}); };

  return React.createElement("div", {style:{display:"flex",flexDirection:"column",gap:16}},
    React.createElement("div", null,
      React.createElement("label",{style:labelSt},"Content"),
      React.createElement("textarea",{value:content,onChange:e=>setContent(e.target.value),rows:5,placeholder:"What should I remember?",style:{...inputSt,resize:"vertical",fontFamily:sans,lineHeight:1.6},onFocus:e=>{e.target.style.borderColor=theme.borderFocus},onBlur:e=>{e.target.style.borderColor=theme.border}})
    ),
    React.createElement("div", null,
      React.createElement("label",{style:labelSt},"Category"),
      React.createElement("div",{style:{display:"flex",flexWrap:"wrap",gap:6}},
        categories.map(c => React.createElement("button",{key:c,onClick:()=>setCategory(c),style:{padding:"6px 12px",borderRadius:6,fontSize:12,fontFamily:mono,border:"1px solid "+(category===c?(theme.categoryColors[c]||theme.accent):theme.border),background:category===c?(theme.categoryColors[c]||theme.accent)+"18":"transparent",color:category===c?(theme.categoryColors[c]||theme.accent):theme.textSecondary,cursor:"pointer",transition:"all 0.15s",textTransform:"uppercase",fontWeight:category===c?600:400}}, c))
      )
    ),
    React.createElement("div", null,
      React.createElement("label",{style:labelSt},"Tags"),
      tags.length>0 && React.createElement("div",{style:{display:"flex",flexWrap:"wrap",gap:4,marginBottom:8}},
        tags.map(t => React.createElement(TagPill,{key:t,tag:t,removable:true,onRemove:()=>setTags(tags.filter(x=>x!==t))}))
      ),
      React.createElement("input",{value:tagInput,onChange:e=>setTagInput(e.target.value),onKeyDown:handleTagKey,placeholder:"Type a tag and press Enter\u2026",style:inputSt,onFocus:e=>{e.target.style.borderColor=theme.borderFocus},onBlur:e=>{e.target.style.borderColor=theme.border}})
    ),
    React.createElement("div", {style:{display:"flex",gap:8,justifyContent:"flex-end",marginTop:8}},
      React.createElement("button",{onClick:onCancel,style:{padding:"8px 16px",borderRadius:6,border:"1px solid "+theme.border,background:"transparent",color:theme.textSecondary,fontSize:13,fontFamily:mono,cursor:"pointer"}},"Cancel"),
      React.createElement("button",{onClick:handleSubmit,disabled:!content.trim(),style:{padding:"8px 20px",borderRadius:6,border:"none",background:content.trim()?theme.accent:theme.surfaceActive,color:content.trim()?"#fff":theme.textMuted,fontSize:13,fontWeight:600,fontFamily:mono,cursor:content.trim()?"pointer":"not-allowed"}}, initial?"Save Changes":"Add Memory")
    )
  );
}

function ImportForm({onComplete, onCancel}) {
  const [filePath, setFilePath] = useState("");
  const [directory, setDirectory] = useState("");
  const [mode, setMode] = useState("file"); // file or directory
  const [extractMode, setExtractMode] = useState("assistant_messages");
  const [category, setCategory] = useState("chat_import");
  const [tagInput, setTagInput] = useState("");
  const [tags, setTags] = useState([]);
  const [importing, setImporting] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  const extractModes = [
    {value:"assistant_messages", label:"Assistant messages", desc:"Only Claude/AI responses (best for knowledge base)"},
    {value:"user_messages", label:"User messages", desc:"Only your messages"},
    {value:"all_messages", label:"All messages", desc:"Both sides, prefixed with role"},
    {value:"conversations", label:"Full conversations", desc:"Entire conversations as single memories"},
  ];

  const handleTagKey = e => {
    if ((e.key==="Enter"||e.key===",") && tagInput.trim()) {
      e.preventDefault();
      const t = tagInput.trim().toLowerCase().replace(/,/g,"");
      if (t && !tags.includes(t)) setTags([...tags, t]);
      setTagInput("");
    }
  };

  const handleImport = async () => {
    const path = mode === "file" ? filePath.trim() : directory.trim();
    if (!path) return;
    setImporting(true); setError(null); setResult(null);
    try {
      const body = { file_path: path, category, tags, extract_mode: extractMode };
      const res = await fetch(API + "/import", {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (data.error) { setError(data.error); }
      else { setResult(data); onComplete(); }
    } catch (e) { setError("Import failed: " + e.message); }
    setImporting(false);
  };

  return React.createElement("div", {style:{display:"flex",flexDirection:"column",gap:16}},
    // Mode toggle
    React.createElement("div", null,
      React.createElement("label",{style:labelSt},"Import Type"),
      React.createElement("div",{style:{display:"flex",gap:6}},
        [["file","Single File"],["directory","Directory"]].map(([v,l])=>
          React.createElement("button",{key:v,onClick:()=>setMode(v),style:{padding:"6px 14px",borderRadius:6,fontSize:12,fontFamily:mono,border:"1px solid "+(mode===v?theme.accent:theme.border),background:mode===v?theme.accentSubtle:"transparent",color:mode===v?theme.accent:theme.textSecondary,cursor:"pointer",fontWeight:mode===v?600:400}},l)
        )
      )
    ),
    // Path input
    React.createElement("div", null,
      React.createElement("label",{style:labelSt}, mode==="file" ? "File Path" : "Directory Path"),
      React.createElement("input",{
        value: mode==="file" ? filePath : directory,
        onChange: e => mode==="file" ? setFilePath(e.target.value) : setDirectory(e.target.value),
        placeholder: mode==="file"
          ? "~/Downloads/claude-export/conversations.json"
          : "~/Downloads/claude-export/",
        style: inputSt,
        onFocus:e=>{e.target.style.borderColor=theme.borderFocus},
        onBlur:e=>{e.target.style.borderColor=theme.border},
      }),
      React.createElement("div",{style:{fontSize:11,color:theme.textMuted,fontFamily:mono,marginTop:4}},
        mode==="file"
          ? "Supports .json, .jsonl, .md, .txt files"
          : "Will scan for all supported files" + " (recursively)"
      )
    ),
    // Extract mode
    React.createElement("div", null,
      React.createElement("label",{style:labelSt},"Extract Mode"),
      React.createElement("div",{style:{display:"flex",flexDirection:"column",gap:4}},
        extractModes.map(em =>
          React.createElement("button",{key:em.value,onClick:()=>setExtractMode(em.value),style:{display:"flex",flexDirection:"column",alignItems:"flex-start",padding:"8px 12px",borderRadius:6,border:"1px solid "+(extractMode===em.value?theme.accent:theme.border),background:extractMode===em.value?theme.accentSubtle:"transparent",cursor:"pointer",transition:"all 0.15s"}},
            React.createElement("span",{style:{fontSize:13,fontFamily:mono,fontWeight:extractMode===em.value?600:400,color:extractMode===em.value?theme.accent:theme.text}}, em.label),
            React.createElement("span",{style:{fontSize:11,color:theme.textMuted,fontFamily:sans,marginTop:2}}, em.desc)
          )
        )
      )
    ),
    // Category
    React.createElement("div", null,
      React.createElement("label",{style:labelSt},"Category"),
      React.createElement("input",{value:category,onChange:e=>setCategory(e.target.value),style:inputSt,
        onFocus:e=>{e.target.style.borderColor=theme.borderFocus},onBlur:e=>{e.target.style.borderColor=theme.border}})
    ),
    // Tags
    React.createElement("div", null,
      React.createElement("label",{style:labelSt},"Tags (applied to all imported memories)"),
      tags.length > 0 && React.createElement("div",{style:{display:"flex",flexWrap:"wrap",gap:4,marginBottom:8}},
        tags.map(t => React.createElement(TagPill,{key:t,tag:t,removable:true,onRemove:()=>setTags(tags.filter(x=>x!==t))}))
      ),
      React.createElement("input",{value:tagInput,onChange:e=>setTagInput(e.target.value),onKeyDown:handleTagKey,placeholder:"Type a tag and press Enter\u2026",style:inputSt,
        onFocus:e=>{e.target.style.borderColor=theme.borderFocus},onBlur:e=>{e.target.style.borderColor=theme.border}})
    ),
    // Error
    error && React.createElement("div",{style:{padding:"10px 14px",borderRadius:6,background:theme.dangerSubtle,border:"1px solid "+theme.danger+"40",color:theme.danger,fontSize:13,fontFamily:mono}}, error),
    // Result
    result && React.createElement("div",{style:{padding:"10px 14px",borderRadius:6,background:theme.successSubtle,border:"1px solid "+theme.success+"40",color:theme.success,fontSize:13,fontFamily:mono}},
      result.status === "ok"
        ? "\u2713 Imported "+result.memories_created+" memories from "+result.file
        : result.status === "skipped"
          ? "Skipped: "+result.reason + (result.file ? " ("+result.file+")" : "")
          : result.files_processed
            ? "\u2713 Processed "+result.files_processed+" files: "+result.total_memories_created+" memories created, "+result.skipped+" skipped"
            : JSON.stringify(result)
    ),
    // Actions
    React.createElement("div", {style:{display:"flex",gap:8,justifyContent:"flex-end",marginTop:8}},
      React.createElement("button",{onClick:onCancel,style:{padding:"8px 16px",borderRadius:6,border:"1px solid "+theme.border,background:"transparent",color:theme.textSecondary,fontSize:13,fontFamily:mono,cursor:"pointer"}},"Close"),
      React.createElement("button",{onClick:handleImport,disabled:importing||!(mode==="file"?filePath.trim():directory.trim()),style:{padding:"8px 20px",borderRadius:6,border:"none",background:importing||!(mode==="file"?filePath.trim():directory.trim())?theme.surfaceActive:theme.accent,color:importing||!(mode==="file"?filePath.trim():directory.trim())?theme.textMuted:"#fff",fontSize:13,fontWeight:600,fontFamily:mono,cursor:importing?"wait":"pointer",display:"flex",alignItems:"center",gap:6}},
        importing && React.createElement(Icons.Loader),
        importing ? "Importing\u2026" : "Import"
      )
    )
  );
}

function App() {
  const store = useMemoryStore();
  const [view, setView] = useState("browse");
  const [searchQuery, setSearchQuery] = useState("");
  const [filterCategory, setFilterCategory] = useState("");
  const [filterTags, setFilterTags] = useState([]);
  const [showAddModal, setShowAddModal] = useState(false);
  const [showImportModal, setShowImportModal] = useState(false);
  const [editMemory, setEditMemory] = useState(null);
  const [deleteConfirm, setDeleteConfirm] = useState(null);
  const [expandedIds, setExpandedIds] = useState(new Set());
  const searchRef = useRef(null);
  const debounceRef = useRef(null);

  useEffect(() => {
    const h = e => { if ((e.metaKey||e.ctrlKey)&&e.key==="k") { e.preventDefault(); searchRef.current?.focus(); } };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, []);

  // Debounced search
  useEffect(() => {
    clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      if (searchQuery.trim()) {
        store.search(searchQuery, filterCategory||null, filterTags.length?filterTags:null);
      } else {
        store.refresh({ category: filterCategory||undefined, tags: filterTags.length?filterTags:undefined });
      }
    }, 250);
  }, [searchQuery, filterCategory, filterTags]);

  const handleAdd = async data => { await store.add(data); setShowAddModal(false); };
  const handleEdit = async data => { if (editMemory) { await store.update(editMemory.id, data); setEditMemory(null); } };
  const handleDelete = async id => { await store.remove(id); setDeleteConfirm(null); };
  const toggleExpand = id => setExpandedIds(prev => { const n=new Set(prev); n.has(id)?n.delete(id):n.add(id); return n; });
  const handleTagClick = tag => { if (!filterTags.includes(tag)) setFilterTags([...filterTags, tag]); };

  const stats = store.stats;
  const allCategories = Object.keys(stats.categories||{});
  const allTags = Object.keys(stats.tags||{}).sort((a,b)=>(stats.tags[b]||0)-(stats.tags[a]||0));

  return React.createElement("div", {style:{minHeight:"100vh",background:theme.bg,color:theme.text,fontFamily:sans}},
    React.createElement("style",null,"@keyframes spin{to{transform:rotate(360deg)}}"),
    // Header
    React.createElement("header", {style:{borderBottom:"1px solid "+theme.border,padding:"16px 24px",display:"flex",alignItems:"center",justifyContent:"space-between",position:"sticky",top:0,zIndex:100,background:theme.bg+"e6",backdropFilter:"blur(12px)"}},
      React.createElement("div",{style:{display:"flex",alignItems:"center",gap:10}},
        React.createElement("div",{style:{width:36,height:36,borderRadius:8,background:"linear-gradient(135deg,"+theme.accent+",#a855f7)",display:"flex",alignItems:"center",justifyContent:"center"}}, React.createElement(Icons.Brain)),
        React.createElement("div",null,
          React.createElement("h1",{style:{margin:0,fontSize:18,fontWeight:700,fontFamily:sans,letterSpacing:"-0.02em"}},"Remind Me"),
          React.createElement("span",{style:{fontSize:11,color:theme.textMuted,fontFamily:mono}}, (stats.total||0)+" memories \u00b7 "+((stats.db_path||"").replace(/.*\//,"~/"))),
        )
      ),
      React.createElement("div",{style:{display:"flex",alignItems:"center",gap:8}},
        store.loading && React.createElement("span",{style:{color:theme.textMuted}}, React.createElement(Icons.Loader)),
        React.createElement("div",{style:{display:"flex",background:theme.surface,borderRadius:6,border:"1px solid "+theme.border,overflow:"hidden"}},
          [["browse","Browse"],["stats","Stats"]].map(([v,l])=>React.createElement("button",{key:v,onClick:()=>setView(v),style:{padding:"6px 14px",border:"none",fontSize:12,fontFamily:mono,fontWeight:500,cursor:"pointer",background:view===v?theme.accent:"transparent",color:view===v?"#fff":theme.textSecondary,transition:"all 0.15s"}},l))
        ),
        React.createElement("button",{onClick:()=>setShowImportModal(true),style:{display:"flex",alignItems:"center",gap:6,padding:"8px 14px",borderRadius:6,border:"1px solid "+theme.border,background:"transparent",color:theme.textSecondary,fontSize:13,fontWeight:500,fontFamily:mono,cursor:"pointer",transition:"all 0.15s"},onMouseEnter:e=>{e.currentTarget.style.borderColor=theme.accent;e.currentTarget.style.color=theme.text},onMouseLeave:e=>{e.currentTarget.style.borderColor=theme.border;e.currentTarget.style.color=theme.textSecondary}}, React.createElement(Icons.Upload), " Import"),
        React.createElement("button",{onClick:()=>setShowAddModal(true),style:{display:"flex",alignItems:"center",gap:6,padding:"8px 14px",borderRadius:6,border:"none",background:theme.accent,color:"#fff",fontSize:13,fontWeight:600,fontFamily:mono,cursor:"pointer"}}, React.createElement(Icons.Plus), " Add")
      )
    ),
    // Body
    React.createElement("div",{style:{display:"flex",maxWidth:1200,margin:"0 auto"}},
      // Sidebar
      view==="browse" && React.createElement("aside",{style:{width:220,borderRight:"1px solid "+theme.border,padding:"20px 16px",flexShrink:0,position:"sticky",top:69,height:"calc(100vh - 69px)",overflowY:"auto"}},
        React.createElement("div",{style:{marginBottom:20}},
          React.createElement("div",{style:{...labelSt,marginBottom:10}},"Categories"),
          React.createElement("button",{onClick:()=>setFilterCategory(""),style:{display:"block",width:"100%",textAlign:"left",padding:"6px 10px",borderRadius:5,border:"none",background:!filterCategory?theme.accentSubtle:"transparent",color:!filterCategory?theme.accent:theme.textSecondary,fontSize:13,fontFamily:sans,cursor:"pointer",fontWeight:!filterCategory?600:400,marginBottom:2}}, "All ("+(stats.total||0)+")"),
          allCategories.map(cat=>React.createElement("button",{key:cat,onClick:()=>setFilterCategory(filterCategory===cat?"":cat),style:{display:"flex",alignItems:"center",justifyContent:"space-between",width:"100%",textAlign:"left",padding:"6px 10px",borderRadius:5,border:"none",background:filterCategory===cat?(theme.categoryColors[cat]||theme.accent)+"18":"transparent",color:filterCategory===cat?(theme.categoryColors[cat]||theme.accent):theme.textSecondary,fontSize:13,fontFamily:sans,cursor:"pointer",fontWeight:filterCategory===cat?600:400,marginBottom:2}},
            React.createElement("span",null,cat),
            React.createElement("span",{style:{fontSize:11,fontFamily:mono,opacity:0.7}}, stats.categories[cat])
          ))
        ),
        React.createElement("div",null,
          React.createElement("div",{style:{...labelSt,marginBottom:10}},"Popular Tags"),
          React.createElement("div",{style:{display:"flex",flexWrap:"wrap",gap:4}},
            allTags.slice(0,15).map(t=>React.createElement(TagPill,{key:t,tag:t,onClick:()=>handleTagClick(t)}))
          )
        )
      ),
      // Main
      React.createElement("main",{style:{flex:1,padding:"20px 24px",minWidth:0}},
        view==="browse" ? React.createElement(React.Fragment,null,
          // Search
          React.createElement("div",{style:{position:"relative",marginBottom:16}},
            React.createElement("div",{style:{position:"absolute",left:12,top:"50%",transform:"translateY(-50%)",color:theme.textMuted}}, React.createElement(Icons.Search)),
            React.createElement("input",{ref:searchRef,value:searchQuery,onChange:e=>setSearchQuery(e.target.value),placeholder:"Search memories\u2026 (\u2318K)",style:{...inputSt,paddingLeft:36,background:theme.surface,fontSize:15},onFocus:e=>{e.target.style.borderColor=theme.borderFocus},onBlur:e=>{e.target.style.borderColor=theme.border}})
          ),
          // Active tag filters
          filterTags.length>0 && React.createElement("div",{style:{display:"flex",alignItems:"center",gap:6,marginBottom:12,flexWrap:"wrap"}},
            React.createElement("span",{style:{fontSize:12,color:theme.textMuted,fontFamily:mono}},"Filtered by:"),
            filterTags.map(t=>React.createElement(TagPill,{key:t,tag:t,removable:true,onRemove:()=>setFilterTags(filterTags.filter(x=>x!==t))})),
            React.createElement("button",{onClick:()=>setFilterTags([]),style:{background:"none",border:"none",color:theme.accent,fontSize:12,fontFamily:mono,cursor:"pointer"}},"Clear all")
          ),
          React.createElement("div",{style:{fontSize:12,color:theme.textMuted,fontFamily:mono,marginBottom:12}}, store.memories.length+" "+(store.memories.length===1?"memory":"memories")+(searchQuery||filterCategory||filterTags.length?" matching filters":"")),
          // Cards
          React.createElement("div",{style:{display:"flex",flexDirection:"column",gap:10}},
            store.memories.map(m=>React.createElement(MemoryCard,{key:m.id,memory:m,onEdit:setEditMemory,onDelete:setDeleteConfirm,onTagClick:handleTagClick,expanded:expandedIds.has(m.id),onToggle:()=>toggleExpand(m.id)})),
            store.memories.length===0 && !store.loading && React.createElement("div",{style:{textAlign:"center",padding:"60px 20px",color:theme.textMuted}},
              React.createElement("div",{style:{fontSize:40,marginBottom:12}},"\u2205"),
              React.createElement("div",{style:{fontSize:15,marginBottom:6}},"No memories found"),
              React.createElement("div",{style:{fontSize:13}},"Try adjusting your search or filters")
            )
          )
        ) :
        // Stats view
        React.createElement("div",null,
          React.createElement("h2",{style:{fontFamily:sans,fontWeight:700,fontSize:22,marginBottom:20,letterSpacing:"-0.02em"}},"Memory Statistics"),
          React.createElement("div",{style:{display:"flex",gap:12,marginBottom:24,flexWrap:"wrap"}},
            React.createElement(StatCard,{label:"Total Memories",value:stats.total||0,color:theme.accent,icon:React.createElement(Icons.Database)}),
            React.createElement(StatCard,{label:"Categories",value:Object.keys(stats.categories||{}).length,color:"#22c55e",icon:React.createElement(Icons.Chart)}),
            React.createElement(StatCard,{label:"Unique Tags",value:Object.keys(stats.tags||{}).length,color:"#f59e0b",icon:React.createElement(Icons.Tag)}),
            React.createElement(StatCard,{label:"Sources",value:Object.keys(stats.sources||{}).length,color:"#06b6d4",icon:React.createElement(Icons.Upload)})
          ),
          React.createElement("div",{style:{display:"grid",gridTemplateColumns:"1fr 1fr",gap:16}},
            React.createElement("div",{style:{background:theme.surface,border:"1px solid "+theme.border,borderRadius:8,padding:20}},
              React.createElement("h3",{style:{fontFamily:mono,fontSize:13,fontWeight:600,color:theme.textSecondary,marginBottom:16,textTransform:"uppercase",letterSpacing:"0.04em"}},"By Category"),
              React.createElement(BarChart,{data:stats.categories||{},colorMap:theme.categoryColors})
            ),
            React.createElement("div",{style:{background:theme.surface,border:"1px solid "+theme.border,borderRadius:8,padding:20}},
              React.createElement("h3",{style:{fontFamily:mono,fontSize:13,fontWeight:600,color:theme.textSecondary,marginBottom:16,textTransform:"uppercase",letterSpacing:"0.04em"}},"By Source"),
              React.createElement(BarChart,{data:stats.sources||{},colorMap:{manual:theme.accent,chat_import:"#64748b"}})
            )
          ),
          React.createElement("div",{style:{background:theme.surface,border:"1px solid "+theme.border,borderRadius:8,padding:20,marginTop:16}},
            React.createElement("h3",{style:{fontFamily:mono,fontSize:13,fontWeight:600,color:theme.textSecondary,marginBottom:16,textTransform:"uppercase",letterSpacing:"0.04em"}},"Top Tags"),
            React.createElement(BarChart,{data:Object.fromEntries(Object.entries(stats.tags||{}).sort((a,b)=>b[1]-a[1]).slice(0,10))})
          ),
          React.createElement("div",{style:{background:theme.surface,border:"1px solid "+theme.border,borderRadius:8,padding:20,marginTop:16}},
            React.createElement("h3",{style:{fontFamily:mono,fontSize:13,fontWeight:600,color:theme.textSecondary,marginBottom:12,textTransform:"uppercase",letterSpacing:"0.04em"}},"Server Info"),
            React.createElement("div",{style:{fontFamily:mono,fontSize:13,color:theme.textSecondary,lineHeight:2}},
              React.createElement("div",null, React.createElement("span",{style:{color:theme.textMuted}},"Database: "), React.createElement("code",{style:{color:theme.text}}, stats.db_path||"~/.remind-me/memory.db")),
              React.createElement("div",null, React.createElement("span",{style:{color:theme.textMuted}},"Size: "), React.createElement("code",{style:{color:theme.text}}, (stats.db_size_mb||0)+" MB")),
              React.createElement("div",null, React.createElement("span",{style:{color:theme.textMuted}},"Search engine: "), React.createElement("code",{style:{color:theme.text}}, "SQLite FTS5")),
              React.createElement("div",null, React.createElement("span",{style:{color:theme.textMuted}},"API: "), React.createElement("code",{style:{color:theme.text}}, window.location.origin))
            )
          )
        )
      )
    ),
    // Modals
    React.createElement(Modal,{open:showAddModal,onClose:()=>setShowAddModal(false),title:"Add Memory",width:520},
      React.createElement(MemoryForm,{onSubmit:handleAdd,onCancel:()=>setShowAddModal(false)})
    ),
    React.createElement(Modal,{open:showImportModal,onClose:()=>setShowImportModal(false),title:"Import Chat History",width:560},
      React.createElement(ImportForm,{onComplete:()=>store.refresh(),onCancel:()=>setShowImportModal(false)})
    ),
    React.createElement(Modal,{open:!!editMemory,onClose:()=>setEditMemory(null),title:"Edit Memory",width:520},
      editMemory && React.createElement(MemoryForm,{initial:editMemory,onSubmit:handleEdit,onCancel:()=>setEditMemory(null)})
    ),
    React.createElement(Modal,{open:!!deleteConfirm,onClose:()=>setDeleteConfirm(null),title:"Delete Memory",width:400},
      React.createElement("p",{style:{color:theme.textSecondary,fontFamily:sans,fontSize:14,lineHeight:1.6}}, "Are you sure you want to permanently delete memory ", React.createElement("code",{style:{fontFamily:mono,color:theme.text}},deleteConfirm), "? This cannot be undone."),
      React.createElement("div",{style:{display:"flex",gap:8,justifyContent:"flex-end",marginTop:20}},
        React.createElement("button",{onClick:()=>setDeleteConfirm(null),style:{padding:"8px 16px",borderRadius:6,border:"1px solid "+theme.border,background:"transparent",color:theme.textSecondary,fontSize:13,fontFamily:mono,cursor:"pointer"}},"Cancel"),
        React.createElement("button",{onClick:()=>handleDelete(deleteConfirm),style:{padding:"8px 20px",borderRadius:6,border:"none",background:theme.danger,color:"#fff",fontSize:13,fontWeight:600,fontFamily:mono,cursor:"pointer"}},"Delete")
      )
    )
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(React.createElement(App));
'''


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import atexit
    import signal

    parser = argparse.ArgumentParser(description="Remind Me MCP Server")
    parser.add_argument("--serve-ui", action="store_true", default=SERVE_UI, help="Start the HTTP dashboard UI server")
    parser.add_argument("--ui-port", type=int, default=UI_PORT, help="Port for the dashboard UI (default: 5199)")
    parser.add_argument("--ui-host", type=str, default="127.0.0.1", help="Host to bind the UI server (default: 127.0.0.1)")
    parser.add_argument("--status", action="store_true", help="Check if the UI server is running and exit")
    args = parser.parse_args()

    # -- Status check mode --
    if args.status:
        status = get_server_status()
        if status["ui_server"] == "running":
            print(f"✓ Dashboard running at {status['ui_url']} (PID {status['ui_pid']})")
        else:
            print("✗ Dashboard not running")
        print(f"  Database: {status['db_path']} ({'exists' if status['db_exists'] else 'missing'})")
        sys.exit(0)

    # -- UI server mode --
    if args.serve_ui:
        import uvicorn

        # Check if already running
        existing = _read_pid_file()
        if existing and _check_ui_server_health(existing.get("url", "")):
            log.warning(
                "Dashboard is already running at %s (PID %d). "
                "Stop it first or use a different port with --ui-port.",
                existing["url"], existing["pid"],
            )
            sys.exit(1)

        # Write PID file and register cleanup
        _write_pid_file(args.ui_host, args.ui_port)
        atexit.register(_remove_pid_file)

        def _signal_handler(signum, frame):
            _remove_pid_file()
            sys.exit(0)
        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)

        app = _build_api_app()
        log.info("Starting Remind Me dashboard at http://%s:%d", args.ui_host, args.ui_port)
        uvicorn.run(app, host=args.ui_host, port=args.ui_port, log_level="info")

    # -- MCP stdio mode --
    else:
        # Check if UI server is running and log it
        existing = _read_pid_file()
        if existing and _check_ui_server_health(existing.get("url", "")):
            log.info("Dashboard UI is running at %s", existing["url"])
        mcp.run()