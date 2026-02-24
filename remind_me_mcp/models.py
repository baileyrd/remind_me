"""
remind_me_mcp.models — Pydantic input models and ResponseFormat enum.

All MCP tool input types are defined here. FastMCP passes validated model
instances directly to tool handler functions.
"""

from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

log = logging.getLogger("remind_me_mcp.models")

# ---------------------------------------------------------------------------
# Response format enum
# ---------------------------------------------------------------------------


class ResponseFormat(str, Enum):
    """Output format for list/search operations."""

    MARKDOWN = "markdown"
    JSON = "json"


# ---------------------------------------------------------------------------
# Pydantic input models
# ---------------------------------------------------------------------------


class MemoryAddInput(BaseModel):
    """Input for adding a new memory."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    content: str = Field(
        ...,
        description=(
            "The memory content to store. Can be a fact, note, preference, or any text."
        ),
        min_length=1,
        max_length=50000,
    )
    category: str = Field(
        default="general",
        description=(
            "Category for organization (e.g., 'preference', 'fact', 'project', 'person', 'decision')"
        ),
        max_length=100,
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Tags for filtering (e.g., ['python', 'work', 'important'])",
        max_length=20,
    )
    source: str = Field(
        default="manual",
        description=(
            "Origin of this memory (e.g., 'manual', 'chat_import', 'observation')"
        ),
        max_length=100,
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary metadata (e.g., {'conversation_id': '...', 'date': '...'})",
    )


class MemorySearchInput(BaseModel):
    """Input for searching memories."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(
        ...,
        description=(
            "Full-text search query. Supports FTS5 syntax: AND, OR, NOT, phrases in quotes, prefix*"
        ),
        min_length=1,
        max_length=500,
    )
    category: str | None = Field(default=None, description="Filter by category")
    tags: list[str] | None = Field(
        default=None, description="Filter: memory must have ALL of these tags"
    )
    limit: int = Field(default=20, description="Max results to return", ge=1, le=100)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class MemoryListInput(BaseModel):
    """Input for listing memories."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    category: str | None = Field(default=None, description="Filter by category")
    tags: list[str] | None = Field(
        default=None, description="Filter: memory must have ALL of these tags"
    )
    source: str | None = Field(
        default=None, description="Filter by source (e.g., 'chat_import', 'manual')"
    )
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class MemoryUpdateInput(BaseModel):
    """Input for updating a memory."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    memory_id: str = Field(
        ..., description="The ID of the memory to update", min_length=1
    )
    content: str | None = Field(
        default=None, description="New content (if changing)", max_length=50000
    )
    category: str | None = Field(default=None, max_length=100)
    tags: list[str] | None = Field(default=None, max_length=20)
    metadata: dict[str, Any] | None = Field(default=None)


class MemoryDeleteInput(BaseModel):
    """Input for deleting a memory."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    memory_id: str = Field(
        ..., description="The ID of the memory to delete", min_length=1
    )


class ChatImportInput(BaseModel):
    """Input for importing chat exports into memory."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    file_path: str = Field(
        ...,
        description="Path to the chat export file (JSON, JSONL, or Markdown)",
    )
    category: str = Field(
        default="chat_import",
        description="Category to assign to imported memories",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Additional tags to apply to all imported memories",
    )
    extract_mode: str = Field(
        default="assistant_messages",
        description=(
            "What to extract: "
            "'assistant_messages' — only assistant turns, "
            "'user_messages' — only user turns, "
            "'all_messages' — both turns, "
            "'conversations' — full conversations as single memories, "
            "'summaries' — only content under 'summary' keys"
        ),
    )
    max_length: int = Field(
        default=10000,
        description="Max characters per memory entry; longer content is chunked",
        ge=100,
        le=50000,
    )

    @field_validator("file_path")
    @classmethod
    def validate_path(cls, v: str) -> str:
        """Validate the file path exists and has a supported extension."""
        p = Path(v).expanduser().resolve()
        if not p.exists():
            raise ValueError(f"File not found: {p}")
        if p.suffix.lower() not in (".json", ".jsonl", ".md", ".markdown", ".txt"):
            raise ValueError(
                f"Unsupported file type: {p.suffix}. Use .json, .jsonl, or .md"
            )
        return str(p)


class MemoryStatsInput(BaseModel):
    """Input for getting memory statistics."""

    model_config = ConfigDict(extra="forbid")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class BulkImportDirInput(BaseModel):
    """Input for bulk-importing a directory of chat exports."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    directory: str = Field(
        ..., description="Path to directory containing chat export files"
    )
    category: str = Field(default="chat_import")
    tags: list[str] = Field(default_factory=list)
    extract_mode: str = Field(default="assistant_messages")
    max_length: int = Field(default=10000, ge=100, le=50000)
    recursive: bool = Field(default=True, description="Search subdirectories")

    @field_validator("directory")
    @classmethod
    def validate_dir(cls, v: str) -> str:
        """Validate the directory path exists."""
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
        description=(
            "Short title for the conversation "
            "(e.g., 'VLAN setup discussion', 'Python async patterns')"
        ),
        max_length=200,
    )
    tags: list[str] = Field(
        default_factory=list,
        description=(
            "Tags for both the dialog and summary "
            "(e.g., ['python', 'work', 'architecture'])"
        ),
        max_length=20,
    )
    category: str = Field(
        default="conversation",
        description=(
            "Category for the summary. The full dialog always uses 'dialog' category."
        ),
        max_length=100,
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Optional metadata "
            "(e.g., {'project': 'remind-me-mcp', 'context': 'Claude Desktop'})"
        ),
    )


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "ResponseFormat",
    "MemoryAddInput",
    "MemorySearchInput",
    "MemoryListInput",
    "MemoryUpdateInput",
    "MemoryDeleteInput",
    "ChatImportInput",
    "MemoryStatsInput",
    "BulkImportDirInput",
    "AutoCaptureInput",
]
