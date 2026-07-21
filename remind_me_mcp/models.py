"""
remind_me_mcp.models — Pydantic input models and ResponseFormat enum.

All MCP tool input types are defined here. FastMCP passes validated model
instances directly to tool handler functions.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from remind_me_mcp.config import is_in_export_roots, is_in_import_roots

log = logging.getLogger("remind_me_mcp.models")

# ---------------------------------------------------------------------------
# Response format enum
# ---------------------------------------------------------------------------


class ResponseFormat(StrEnum):
    """Output format for list/search operations."""

    MARKDOWN = "markdown"
    JSON = "json"


class RetrievalStrategy(StrEnum):
    """RRF weight profile for remind_me_search (Phase 6).

    AUTO applies a deterministic heuristic router based on query shape:
    quoted phrases, prefix* wildcards, or very short queries favor keyword
    relevance and the IDF signal; long natural-language/question-shaped
    queries favor semantic similarity. The other three values pin an
    explicit preset — an escape hatch, and easy to A/B in benchmarks/.
    BALANCED reproduces the tuned RRF defaults exactly (no signal
    overridden).
    """

    AUTO = "auto"
    BALANCED = "balanced"
    KEYWORD_FAVORED = "keyword_favored"
    SEMANTIC_FAVORED = "semantic_favored"


# ---------------------------------------------------------------------------
# Pydantic input models
# ---------------------------------------------------------------------------


class EntityInput(BaseModel):
    """An entity mentioned by a memory (FT-04 knowledge-graph layer).

    A mention with a NEW name creates a new entity — different names are
    never auto-merged into one entity. Alias merging is explicit: provide
    ``aliases`` to union-merge alternate names onto this entity's record.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(
        ...,
        description=(
            "Canonical entity name as mentioned (e.g. 'Bailey Robertson', "
            "'remind_me', 'Tailscale'). Identity is case/whitespace-insensitive."
        ),
        min_length=1,
        max_length=200,
    )
    kind: str | None = Field(
        default=None,
        description="Entity kind (e.g. 'person', 'project', 'tool', 'place', 'org')",
        max_length=50,
    )
    aliases: list[str] = Field(
        default_factory=list,
        description=(
            "Explicit alternate names to merge onto this entity "
            "(e.g. ['Bailey'] for canonical 'Bailey Robertson')"
        ),
        max_length=20,
    )


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
    subject: str | None = Field(
        default=None,
        description="Structured triple: subject (e.g. 'Bailey') — FT-04",
        max_length=200,
    )
    predicate: str | None = Field(
        default=None,
        description="Structured triple: predicate (e.g. 'prefers') — FT-04",
        max_length=200,
    )
    object: str | None = Field(
        default=None,
        description="Structured triple: object (e.g. 'dark mode') — FT-04",
        max_length=500,
    )
    entities: list[EntityInput] = Field(
        default_factory=list,
        description="Entities this memory mentions (FT-04 knowledge graph)",
        max_length=20,
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
    verbose: bool = Field(
        default=False,
        description="Include debug ranking signals (semantic_rank, keyword_rank, recency_rank, vitality_rank, idf_rank, days_old) per result",
    )
    expand_entities: bool = Field(
        default=False,
        description=(
            "Opt-in 1-hop knowledge-graph expansion (FT-04): after ranking, append "
            "up to 5 additional non-superseded memories that share a mentioned "
            "entity with the returned results, in a separate related_via_entities "
            "section. Does not affect the main ranking."
        ),
    )
    include_neighbors: bool = Field(
        default=False,
        description=(
            "Opt-in neighbor-aware chunk expansion: after ranking, append up to 5 "
            "additional non-superseded sibling chunks (same source document, "
            "adjacent chunk position) for any result that came from an import, "
            "in a separate related_via_neighbors section. Does not affect the "
            "main ranking."
        ),
    )
    strategy: RetrievalStrategy = Field(
        default=RetrievalStrategy.AUTO,
        description=(
            "RRF weight profile: 'auto' (default) routes by query shape — "
            "quoted phrases/prefix*/short queries favor keyword+IDF, long "
            "natural-language/question-shaped queries favor semantic. "
            "'balanced' pins the tuned defaults; 'keyword_favored' and "
            "'semantic_favored' pin an explicit preset regardless of query "
            "shape. Only affects the hybrid ranking path (not structured "
            "subject:/predicate:/entity: lookups)."
        ),
    )


class FeedbackInput(BaseModel):
    """Input for the remind_me_feedback tool: mark a search result helpful/unhelpful."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    memory_id: str = Field(..., description="ID of the memory to give feedback on", min_length=1)
    signal: Literal["helpful", "unhelpful"] = Field(
        ..., description="Whether the memory was helpful or unhelpful for the query it was retrieved for"
    )
    query: str | None = Field(
        default=None,
        description="Optional: the search query this feedback relates to (for future audit/reporting)",
        max_length=500,
    )


class EntityLookupInput(BaseModel):
    """Input for the remind_me_entity tool: look up an entity by name or alias."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(
        ...,
        description=(
            "Entity name or alias to look up (case/whitespace-insensitive, "
            "e.g. 'Bailey Robertson' or 'Bailey')"
        ),
        min_length=1,
        max_length=200,
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Max facts and max linked memories to return",
    )


class EntityTraverseInput(BaseModel):
    """Input for the remind_me_entity_traverse tool: multi-hop entity-relation traversal."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(
        ...,
        description=(
            "Entity name or alias to start the traversal from "
            "(case/whitespace-insensitive, e.g. 'Bailey Robertson' or 'Bailey')"
        ),
        min_length=1,
        max_length=200,
    )
    hops: int = Field(
        default=1,
        ge=1,
        le=3,
        description=(
            "Maximum traversal depth. 1 = direct relations only; 2-3 follow "
            "relations of relations (e.g. 'who introduced me to the person "
            "who recommended this tool')."
        ),
    )
    relation: str | None = Field(
        default=None,
        description="Optional: only follow edges whose relation label matches exactly",
        max_length=200,
    )
    cap: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Max number of relation edges to return",
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


class ImportKind(StrEnum):
    """How to parse an imported file (FT-02).

    AUTO routes by extension and content sniffing: .json/.jsonl always import
    as chat; .md/.markdown/.txt import as chat when they contain chat role
    markers (e.g. '**User:**', '## Assistant'), otherwise as a document.
    """

    AUTO = "auto"
    CHAT = "chat"
    DOCUMENT = "document"


class ChatImportInput(BaseModel):
    """Input for importing chat exports or documents into memory."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    file_path: str = Field(
        ...,
        description=(
            "Path to the file to import: a chat export (JSON, JSONL, or "
            "Markdown) or a notes/document file (Markdown or plain text)"
        ),
    )
    category: str = Field(
        default="chat_import",
        description=(
            "Category to assign to imported memories. The default "
            "'chat_import' becomes 'document' for document imports."
        ),
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
    kind: ImportKind = Field(
        default=ImportKind.AUTO,
        description=(
            "How to parse the file (FT-02): "
            "'auto' — detect by extension/content (chat-style markdown imports "
            "as chat, notes markdown/text as a document), "
            "'chat' — force the chat-export parser, "
            "'document' — force per-section/paragraph document chunking "
            "(.md/.markdown/.txt only)"
        ),
    )

    @field_validator("file_path")
    @classmethod
    def validate_path(cls, v: str) -> str:
        """Validate import-root containment, existence, and a supported extension.

        SE-02: the containment check (shared with the HTTP /api/import route)
        runs first so paths outside IMPORT_ROOTS are rejected without leaking
        whether they exist.
        """
        p = Path(v).expanduser().resolve()
        if not is_in_import_roots(p):
            raise ValueError(f"Path not in allowed import roots: {p}")
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


class ExportFormat(StrEnum):
    """Serialization format for memory exports (FT-01)."""

    JSON = "json"
    JSONL = "jsonl"


class ExportInput(BaseModel):
    """Input for exporting memories to JSON/JSONL (FT-01)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    format: ExportFormat = Field(
        default=ExportFormat.JSON,
        description=(
            "'json' — single indented JSON array, "
            "'jsonl' — one JSON record per line"
        ),
    )
    category: str | None = Field(
        default=None,
        description="Filter: only export memories with this category",
    )
    tags: list[str] | None = Field(
        default=None, description="Filter: memory must have ALL of these tags"
    )
    file_path: str | None = Field(
        default=None,
        description=(
            "Destination file path for the export. When omitted, small exports "
            "are returned inline. Must be inside the allowed export roots."
        ),
    )
    include_graph: bool = Field(
        default=True,
        description=(
            "Include the entity graph (entities and memory-entity links) as "
            "record_type-tagged records after the memories, so the backup "
            "captures the full knowledge graph (FT-06). Set false for a "
            "memories-only export."
        ),
    )

    @field_validator("file_path")
    @classmethod
    def validate_export_path(cls, v: str | None) -> str | None:
        """Validate export-root containment and a writable destination (FT-01).

        Mirrors the SE-02 import-root check (shared with the HTTP /api/export
        route): containment runs first so paths outside EXPORT_ROOTS are
        rejected without leaking whether they exist.
        """
        if v is None or not v.strip():
            return None
        p = Path(v).expanduser().resolve()
        if not is_in_export_roots(p):
            raise ValueError(f"Path not in allowed export roots: {p}")
        if p.is_dir():
            raise ValueError(f"Destination is a directory, not a file: {p}")
        if not p.parent.is_dir():
            raise ValueError(f"Parent directory not found: {p.parent}")
        return str(p)


class BulkImportDirInput(BaseModel):
    """Input for bulk-importing a directory of chat exports and/or documents."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    directory: str = Field(
        ...,
        description="Path to directory containing chat export and/or document files",
    )
    category: str = Field(default="chat_import")
    tags: list[str] = Field(default_factory=list)
    extract_mode: str = Field(default="assistant_messages")
    max_length: int = Field(default=10000, ge=100, le=50000)
    recursive: bool = Field(default=True, description="Search subdirectories")
    kind: ImportKind = Field(
        default=ImportKind.AUTO,
        description=(
            "Per-file parsing mode (FT-02): 'auto' (detect chat vs document "
            "per file), 'chat', or 'document'"
        ),
    )

    @field_validator("directory")
    @classmethod
    def validate_dir(cls, v: str) -> str:
        """Validate import-root containment (SE-02) and that the directory exists."""
        p = Path(v).expanduser().resolve()
        if not is_in_import_roots(p):
            raise ValueError(f"Path not in allowed import roots: {p}")
        if not p.is_dir():
            raise ValueError(f"Directory not found: {p}")
        return str(p)


class MempalaceImportInput(BaseModel):
    """Input for pulling memories from a MemPalace ChromaDB store."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    wing: str = Field(default="", description="Restrict to a MemPalace wing (project), or '' for all")
    room: str = Field(default="", description="Restrict to a room within the wing, or '' for all")
    limit: int = Field(
        default=500,
        ge=1,
        le=2000,
        description="Max drawers to fetch and embed in this call; page through with offset",
    )
    offset: int = Field(default=0, ge=0)
    category: str = Field(
        default="",
        description="Category for drawers not already in remind_me's own format (default: 'mempalace_import')",
    )
    tags: list[str] = Field(default_factory=list, description="Extra tags added to every imported memory")
    dry_run: bool = Field(default=False, description="Report what would be imported without writing")


class DbsImportInput(BaseModel):
    """Input for pulling memories from a dbs (daily-backup-system) SQLite store."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    db_path: str = Field(description="Path to the dbs SQLite database (e.g. dbs.sqlite3)")
    source: str = Field(default="", description="Restrict to one dbs source name (e.g. 'raindrop'), or '' for all")
    item_type: str = Field(default="", description="Restrict to one dbs item_kind (e.g. 'link'), or '' for all")
    limit: int = Field(
        default=500,
        ge=1,
        le=2000,
        description="Max items to fetch and embed in this call; page through with offset",
    )
    offset: int = Field(default=0, ge=0)
    tags: list[str] = Field(default_factory=list, description="Extra tags added to every imported memory")
    dry_run: bool = Field(default=False, description="Report what would be imported without writing")

    @field_validator("db_path")
    @classmethod
    def validate_db_path(cls, v: str) -> str:
        """Validate import-root containment and existence (SE-02).

        Mirrors ChatImportInput.validate_path/BulkImportDirInput.validate_dir
        -- db_path is a caller-supplied filesystem path like any other import
        source, so it gets the same containment check (the SDK's own
        _open_dbs_db only checked existence, letting a caller point this at
        any readable file anywhere on disk). The containment check runs
        first so a path outside IMPORT_ROOTS is rejected without leaking
        whether it exists.
        """
        p = Path(v).expanduser().resolve()
        if not is_in_import_roots(p):
            raise ValueError(f"Path not in allowed import roots: {p}")
        if not p.exists():
            raise ValueError(f"File not found: {p}")
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
    subject: str | None = Field(
        default=None,
        description="Structured triple: subject (e.g. 'Bailey') — FT-04",
        max_length=200,
    )
    predicate: str | None = Field(
        default=None,
        description="Structured triple: predicate (e.g. 'prefers') — FT-04",
        max_length=200,
    )
    object: str | None = Field(
        default=None,
        description="Structured triple: object (e.g. 'dark mode') — FT-04",
        max_length=500,
    )
    entities: list[EntityInput] = Field(
        default_factory=list,
        description="Entities this fact mentions (FT-04 knowledge graph)",
        max_length=20,
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
# Entity extraction / annotation models (FT-04)
# ---------------------------------------------------------------------------


class MemoryAnnotation(BaseModel):
    """A structured annotation for one existing memory (FT-04).

    Applies a subject/predicate/object triple and/or entity mentions to a
    memory after the fact (backfill path). Omitted SPO fields are left
    unchanged — annotations only add structure, never clear it.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    memory_id: str = Field(
        ..., description="The ID of the memory to annotate", min_length=1
    )
    subject: str | None = Field(default=None, max_length=200)
    predicate: str | None = Field(default=None, max_length=200)
    object: str | None = Field(default=None, max_length=500)
    entities: list[EntityInput] = Field(
        default_factory=list,
        description="Entities this memory mentions",
        max_length=20,
    )


class AnnotateInput(BaseModel):
    """Input for the remind_me_annotate tool: apply annotations in batch."""

    model_config = ConfigDict(extra="forbid")

    annotations: list[MemoryAnnotation] = Field(
        ...,
        description="List of {memory_id, subject?, predicate?, object?, entities?} annotations",
        min_length=1,
        max_length=100,
    )


class ExtractBatchInput(BaseModel):
    """Input for the remind_me_extract_batch tool: fetch unannotated memories."""

    model_config = ConfigDict(extra="forbid")

    batch_size: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Number of unannotated memories to return",
    )


# ---------------------------------------------------------------------------
# Ingest-time normalization models (FT-09, Phase 5b)
# ---------------------------------------------------------------------------


class NormalizeBatchInput(BaseModel):
    """Input for the remind_me_normalize_batch tool: fetch un-normalized imports."""

    model_config = ConfigDict(extra="forbid")

    batch_size: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Number of un-normalized document/chat import memories to return",
    )


class NormalizationEntry(BaseModel):
    """A single distilled normalization for one raw imported memory (Phase 5b)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    memory_id: str = Field(
        ...,
        description="The ID of the raw imported memory (document_import/chat_import) being normalized",
        min_length=1,
    )
    question: str = Field(
        ...,
        description="The question/topic this content answers (e.g. 'How is the VPN configured?')",
        min_length=1,
        max_length=500,
    )
    summary: str = Field(
        ...,
        description="A concise, distilled answer/summary of the raw content",
        min_length=1,
        max_length=10000,
    )
    resolution: str | None = Field(
        default=None,
        description="Optional resolution/outcome, when the content describes a problem that was resolved",
        max_length=5000,
    )
    refs: list[str] = Field(
        default_factory=list,
        description="Optional reference ids/urls/paths supporting this normalization",
        max_length=20,
    )
    entities: list[EntityInput] = Field(
        default_factory=list,
        description=(
            "Entities this distillation mentions (FT-04 knowledge graph). The raw "
            "imported memory is never entity-linked automatically, so pass entities "
            "here to make the normalized memory reachable via remind_me_entity/"
            "remind_me_entity_traverse."
        ),
        max_length=20,
    )


class NormalizeApplyInput(BaseModel):
    """Input for the remind_me_normalize_apply tool: apply normalizations in batch."""

    model_config = ConfigDict(extra="forbid")

    normalizations: list[NormalizationEntry] = Field(
        ...,
        description="List of distilled {memory_id, question, summary, resolution?, refs?} normalizations",
        min_length=1,
        max_length=50,
    )


# ---------------------------------------------------------------------------
# Consolidation models (Phase 14 Plan 01)
# ---------------------------------------------------------------------------


class ConsolidateInput(BaseModel):
    """Input for the remind_me_consolidate tool: find and merge duplicate memories."""

    model_config = ConfigDict(extra="forbid")

    similarity_threshold: float = Field(
        default=0.85,
        ge=0.5,
        le=1.0,
        description="Minimum cosine similarity to cluster memories together. Higher = stricter.",
    )
    dry_run: bool = Field(
        default=True,
        description="If True, report clusters without modifying data. Set False to auto-merge.",
    )
    category: str | None = Field(
        default=None,
        description="Limit consolidation to this category",
    )
    limit: int = Field(
        default=500,
        ge=10,
        le=5000,
        description="Maximum memories to consider (prevents runaway on large vaults)",
    )


# ---------------------------------------------------------------------------
# LLM Wiki models (FT-08)
# ---------------------------------------------------------------------------


class WikiWriteInput(BaseModel):
    """Input for remind_me_wiki_write: create or replace a wiki page (FT-08)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    title: str = Field(
        ...,
        description=(
            "Page title (e.g. 'Tailscale Setup', 'Bailey Robertson'). The title's "
            "slug is its identity — keep titles stable so [[wikilinks]] resolve."
        ),
        min_length=1,
        max_length=200,
    )
    content: str = Field(
        ...,
        description=(
            "Full markdown body of the page (REPLACES any existing content). "
            "Open with a one-sentence summary; link related pages with "
            "[[Page Title]]. A leading '# Title' H1 is added if absent."
        ),
        min_length=1,
        max_length=100000,
    )
    log_note: str | None = Field(
        default=None,
        description="Optional note recorded in log.md alongside the change (e.g. why).",
        max_length=500,
    )


class WikiReadInput(BaseModel):
    """Input for remind_me_wiki_read: fetch one page with its link graph (FT-08)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    title: str = Field(
        ...,
        description="Page title or slug to read (case/punctuation-insensitive).",
        min_length=1,
        max_length=200,
    )


class WikiDeleteInput(BaseModel):
    """Input for remind_me_wiki_delete: remove a wiki page (FT-08)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    title: str = Field(
        ...,
        description="Page title or slug to delete.",
        min_length=1,
        max_length=200,
    )


class WikiSearchInput(BaseModel):
    """Input for remind_me_wiki_search: full-text search wiki pages (FT-08)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(
        ...,
        description="Search query (FTS5 over page title + content).",
        min_length=1,
        max_length=500,
    )
    limit: int = Field(default=10, ge=1, le=50, description="Max pages to return.")


class WikiListInput(BaseModel):
    """Input for remind_me_wiki_list: list the wiki's pages (FT-08)."""

    model_config = ConfigDict(extra="forbid")

    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class WikiLoadInput(BaseModel):
    """Input for remind_me_wiki_load: load the whole wiki into context (FT-08)."""

    model_config = ConfigDict(extra="forbid")

    token_budget: int = Field(
        default=0,
        ge=0,
        le=200000,
        description=(
            "Estimated-token ceiling (len//4). 0 uses the configured default "
            "(REMIND_ME_WIKI_LOAD_TOKEN_BUDGET). Pages beyond the budget are listed, "
            "not included."
        ),
    )
    include_index: bool = Field(
        default=True, description="Prepend the page catalogue/index."
    )


class WikiCompileInput(BaseModel):
    """Input for remind_me_wiki_compile: drive the synthesis workflow (FT-08)."""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Max pending raw memories to surface for synthesis.",
    )
    mark_integrated: bool = Field(
        default=False,
        description=(
            "When False (default), return a synthesis brief of pending raw "
            "memories + the current wiki state + the schema. When True, advance "
            "the compile watermark past the surfaced batch (call this AFTER you "
            "have written the pages)."
        ),
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
    "RetrievalStrategy",
    "MemoryAddInput",
    "MemorySearchInput",
    "FeedbackInput",
    "MemoryListInput",
    "MemoryUpdateInput",
    "MemoryDeleteInput",
    "ImportKind",
    "ChatImportInput",
    "MemoryStatsInput",
    "ExportFormat",
    "ExportInput",
    "BulkImportDirInput",
    "AutoCaptureInput",
    "MemoryClassification",
    "ReclassifyInput",
    "ReclassifyBatchInput",
    "VALID_MEMORY_TYPES",
    "AtomicFact",
    "DecomposeInput",
    "DecomposeBatchInput",
    "EntityInput",
    "EntityLookupInput",
    "EntityTraverseInput",
    "MemoryAnnotation",
    "AnnotateInput",
    "ExtractBatchInput",
    "NormalizeBatchInput",
    "NormalizationEntry",
    "NormalizeApplyInput",
    "ConsolidateInput",
    "VitalityReportInput",
    "WikiWriteInput",
    "WikiReadInput",
    "WikiDeleteInput",
    "WikiSearchInput",
    "WikiListInput",
    "WikiLoadInput",
    "WikiCompileInput",
]
