"""
remind_me_mcp.models — Pydantic input models and ResponseFormat enum.

All MCP tool input types are defined here. FastMCP passes validated model
instances directly to tool handler functions.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

log = logging.getLogger("remind_me_mcp.models")

# ---------------------------------------------------------------------------
# Response format enum
# ---------------------------------------------------------------------------


class ResponseFormat(StrEnum):
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
    token_budget: int = Field(
        default=800,
        description="Maximum token budget for results (estimate: len(content)//4). 0 means unlimited.",
        ge=0,
        le=10000,
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)
    include_dormant: bool = Field(
        default=False,
        description="Include dormant memories (vitality < 0.05) in results",
    )
    min_vitality: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Minimum vitality score filter. 0.0 means no filter (except dormant exclusion).",
    )


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
# Classification models (Phase 11 Plan 02)
# ---------------------------------------------------------------------------

VALID_MEMORY_TYPES: set[str] = {
    "decision",
    "preference",
    "fact",
    "insight",
    "learning",
    "blocker",
    "action_item",
}
"""Allowed memory_type values for classification (excludes 'unclassified')."""


class MemoryClassification(BaseModel):
    """A single memory classification: maps a memory ID to a memory type."""

    memory_id: str = Field(
        ...,
        description="The ID of the memory to classify",
        min_length=1,
    )
    memory_type: str = Field(
        ...,
        description=(
            "The classification type. Must be one of: "
            "decision, preference, fact, insight, learning, blocker, action_item"
        ),
    )

    @field_validator("memory_type")
    @classmethod
    def validate_memory_type(cls, v: str) -> str:
        """Validate that memory_type is one of the allowed classification values."""
        if v not in VALID_MEMORY_TYPES:
            raise ValueError(
                f"Invalid memory_type '{v}'. Must be one of: "
                f"{', '.join(sorted(VALID_MEMORY_TYPES))}"
            )
        return v


class ReclassifyInput(BaseModel):
    """Input for the remind_me_reclassify tool: apply classifications to memories."""

    model_config = ConfigDict(extra="forbid")

    classifications: list[MemoryClassification] = Field(
        ...,
        description="List of {memory_id, memory_type} pairs to classify",
        min_length=1,
        max_length=100,
    )


class ReclassifyBatchInput(BaseModel):
    """Input for the remind_me_reclassify_batch tool: fetch unclassified memories."""

    model_config = ConfigDict(extra="forbid")

    batch_size: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Number of unclassified memories to return",
    )


# ---------------------------------------------------------------------------
# Decomposition models (Phase 12 Plan 01)
# ---------------------------------------------------------------------------


class AtomicFact(BaseModel):
    """A single atomic fact extracted from a conversation capture."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    content: str = Field(
        ...,
        description="The atomic fact content to store as an individual memory",
        min_length=1,
        max_length=50000,
    )
    memory_type: str | None = Field(
        default=None,
        description=(
            "Optional classification type. Must be one of: "
            "decision, preference, fact, insight, learning, blocker, action_item. "
            "Defaults to 'unclassified' if not provided."
        ),
    )
    extra_tags: list[str] = Field(
        default_factory=list,
        description="Additional tags to merge with the parent capture's tags",
    )

    @field_validator("memory_type")
    @classmethod
    def validate_memory_type(cls, v: str | None) -> str | None:
        """Validate that memory_type is one of the allowed values when not None."""
        if v is not None and v not in VALID_MEMORY_TYPES:
            raise ValueError(
                f"Invalid memory_type '{v}'. Must be one of: "
                f"{', '.join(sorted(VALID_MEMORY_TYPES))}"
            )
        return v


class DecomposeInput(BaseModel):
    """Input for the remind_me_decompose tool: decompose a capture into atomic facts."""

    model_config = ConfigDict(extra="forbid")

    capture_id: str = Field(
        ...,
        description="The capture_id of the parent memory to decompose",
        min_length=1,
    )
    facts: list[AtomicFact] = Field(
        ...,
        description="List of atomic facts extracted from the capture",
        min_length=1,
        max_length=50,
    )


class DecomposeBatchInput(BaseModel):
    """Input for the remind_me_decompose_batch tool: fetch undecomposed captures."""

    model_config = ConfigDict(extra="forbid")

    batch_size: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Number of undecomposed captures to return",
    )


# ---------------------------------------------------------------------------
# Vitality report model (Phase 11 Plan 03)
# ---------------------------------------------------------------------------


class VitalityReportInput(BaseModel):
    """Input for the remind_me_vitality_report tool: vault health metrics."""

    model_config = ConfigDict(extra="forbid")

    response_format: ResponseFormat = Field(default=ResponseFormat.JSON)


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
    "MemoryClassification",
    "ReclassifyInput",
    "ReclassifyBatchInput",
    "VALID_MEMORY_TYPES",
    "AtomicFact",
    "DecomposeInput",
    "DecomposeBatchInput",
    "VitalityReportInput",
]
