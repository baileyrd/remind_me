"""
remind_me_mcp.models — Pydantic input models and ResponseFormat enum.

All MCP tool input types are defined here. FastMCP passes validated model
instances directly to tool handler functions.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
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
        description=(
            "Structured triple: subject (e.g. 'Bailey') — FT-04. WARNING: "
            "setting this together with predicate/object checks this triple "
            "against every OTHER memory sharing the same (subject, predicate) "
            "— one with a different object is treated as a contradiction and "
            "silently superseded (hidden from search/entity lookups, though "
            "still readable via remind_me_get). Correct for genuine "
            "contradictions ('I live in Seattle' -> 'I moved to Boston'), but "
            "a false positive if a (subject, predicate) pair is reused across "
            "unrelated facts. Use a specific predicate per distinct claim, "
            "never a shared generic one like 'must_always' or 'prefers'."
        ),
        max_length=200,
    )
    predicate: str | None = Field(
        default=None,
        description=(
            "Structured triple: predicate (e.g. 'prefers') — FT-04. See "
            "`subject` for the (subject, predicate) contradiction-supersession "
            "warning."
        ),
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
    sensitive: bool = Field(
        default=False,
        description=(
            "Mark this memory sensitive (issue #195) — a convenience flag, "
            "NOT access control (remind_me is single-user; anyone with DB "
            "access already sees everything). A sensitive memory is simply "
            "kept out of remind_me_search/remind_me_list/remind_me_digest/"
            "remind_me_wiki_compile by default, to reduce accidental "
            "exposure in ambient surfaces. It stays fully readable via "
            "remind_me_get and remind_me_search/remind_me_list with "
            "include_sensitive=true."
        ),
    )


class MemorySearchInput(BaseModel):
    """Input for searching memories."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(
        ...,
        description="Search query. FTS5 syntax supported: AND, OR, NOT, \"phrase\", prefix*",
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
        description="Token cap on results; 0 = unlimited.",
        ge=0,
        le=10000,
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)
    include_dormant: bool = Field(
        default=False,
        description="Include decayed-out memories (vitality < 0.05).",
    )
    include_sensitive: bool = Field(
        default=False,
        description=(
            "Include memories marked sensitive (issue #195). Off by default "
            "so a sensitive memory never surfaces in an ordinary search — "
            "opt in explicitly when you actually need it, e.g. because the "
            "question is specifically about that content."
        ),
    )
    min_vitality: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Minimum vitality; 0.0 = no filter.",
    )
    # The five flags below are the hot path's context cost: remind_me_search is
    # the single most expensive tool in the surface, and no tool profile can
    # narrow it away because it is the one tool every session needs. These
    # descriptions are therefore written for the *call decision* only — when to
    # set the flag — not to narrate what happens afterwards. Response-shape
    # details (which section an expansion lands in, that expansions never
    # affect ranking) belong in the README, which they now live in.
    verbose: bool = Field(
        default=False,
        description="Include per-result ranking signals. For debugging retrieval.",
    )
    expand_entities: bool = Field(
        default=False,
        description=(
            "Also surface up to 5 memories sharing an entity with the results. "
            "Use when the question is about how people/projects/tools connect."
        ),
    )
    include_neighbors: bool = Field(
        default=False,
        description=(
            "Also surface adjacent chunks from a result's source document. "
            "Use when a result reads as if cut off mid-context."
        ),
    )
    expand_co_retrieval: bool = Field(
        default=False,
        description=(
            "Also surface memories historically retrieved alongside these. "
            "Use for open-ended discovery, not for a specific question."
        ),
    )
    strategy: RetrievalStrategy = Field(
        default=RetrievalStrategy.AUTO,
        description=(
            "RRF weight profile. Leave at 'auto' (routes by query shape) unless "
            "deliberately A/B testing a pinned preset."
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
        description=(
            "The search query this memory was retrieved for. Strongly preferred: "
            "passing it makes the signal query-contextual, so it only affects future "
            "searches similar to this one. Omitting it applies a GLOBAL adjustment to "
            "the memory's ranking weight for every future query, which is rarely what "
            "you want — demoting a memory for one question then penalises it for "
            "unrelated ones."
        ),
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
    include_sensitive: bool = Field(
        default=False,
        description=(
            "Include memories marked sensitive (issue #195). Off by default, "
            "same reasoning and opt-in as remind_me_search's flag of the "
            "same name — browsing a category/tag/source slice should not "
            "surface sensitive content any more than searching it should."
        ),
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
    sensitive: bool | None = Field(
        default=None,
        description=(
            "Set or clear this memory's sensitive flag (issue #195) — "
            "true marks it sensitive (excluded from search/list/digest/wiki "
            "compile by default, see remind_me_add), false clears it. Omit "
            "to leave the current value unchanged."
        ),
    )
    clear_superseded: bool = Field(
        default=False,
        description=(
            "If true, clear this memory's superseded_by flag, un-hiding it "
            "from search, entity, and subject/predicate lookups. Recovery "
            "path for a false-positive contradiction-supersession (see "
            "remind_me_add's subject/predicate warning) — e.g. a reused "
            "generic (subject, predicate) pair that wrongly superseded an "
            "unrelated memory. Does not affect the memory that did the "
            "superseding."
        ),
    )


class MemoryDeleteInput(BaseModel):
    """Input for deleting a memory."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    memory_id: str = Field(
        ..., description="The ID of the memory to delete", min_length=1
    )


# ---------------------------------------------------------------------------
# Reminder models (issue #179)
# ---------------------------------------------------------------------------


class SetReminderInput(BaseModel):
    """Input for remind_me_set_reminder: set or clear a memory's reminder."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    memory_id: str = Field(
        ..., description="The ID of the memory to set or clear a reminder on", min_length=1
    )
    remind_at: str | None = Field(
        default=None,
        description=(
            "ISO-8601 timestamp for when this memory should be surfaced as a "
            "reminder (naive timestamps are assumed UTC). Must be in the "
            "future. Omit or pass null to clear an existing reminder instead "
            "of setting one."
        ),
    )

    @field_validator("remind_at")
    @classmethod
    def validate_remind_at(cls, v: str | None) -> str | None:
        """Reject an unparseable or non-future timestamp; canonicalize to UTC.

        A no-op reminder (one that would never fire because it is already in
        the past) is rejected outright rather than silently accepted, same
        reasoning as MemoryAddInput's contradiction-supersession warnings:
        surprising behavior should fail loudly, not quietly do nothing.
        """
        if v is None or not v.strip():
            return None
        try:
            dt = datetime.fromisoformat(v.strip())
        except ValueError as e:
            raise ValueError(f"remind_at is not a valid ISO-8601 timestamp: {v!r}") from e
        dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
        now = datetime.now(UTC)
        if dt <= now:
            raise ValueError(
                f"remind_at must be in the future, got {dt.isoformat()} "
                f"(current time: {now.isoformat()})"
            )
        return dt.isoformat()


class ReminderWindow(StrEnum):
    """Which reminders remind_me_list_reminders surfaces."""

    UPCOMING = "upcoming"
    OVERDUE = "overdue"
    ALL = "all"


class ListRemindersInput(BaseModel):
    """Input for remind_me_list_reminders: list memories with a set reminder."""

    model_config = ConfigDict(extra="forbid")

    when: ReminderWindow = Field(
        default=ReminderWindow.UPCOMING,
        description=(
            "'upcoming' — reminders still in the future. "
            "'overdue' — reminders whose time has passed but have not yet "
            "been delivered by the scheduler (e.g. the server was offline "
            "when they came due). "
            "'all' — the union of both."
        ),
    )
    limit: int = Field(default=20, ge=1, le=100)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


# ---------------------------------------------------------------------------
# Edit history models (issue #187)
# ---------------------------------------------------------------------------


class RevisionHistoryInput(BaseModel):
    """Input for remind_me_history: list a memory's prior content revisions."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    memory_id: str = Field(
        ..., description="The ID of the memory to list revision history for", min_length=1
    )
    limit: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Maximum number of revisions to return, newest first.",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class RevertInput(BaseModel):
    """Input for remind_me_revert: restore a memory to a prior revision."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    memory_id: str = Field(
        ..., description="The ID of the memory to revert", min_length=1
    )
    revision_id: int = Field(
        ...,
        description=(
            "The revision id to restore, from remind_me_history's output. "
            "Must belong to this memory_id."
        ),
    )
    reason: str | None = Field(
        default=None,
        description=(
            "Optional free-text note for why this revert happened, stored "
            "on the new revision this revert itself creates (a revert is "
            "just another edit — it snapshots the pre-revert state too, so "
            "it can itself be undone)."
        ),
        max_length=500,
    )


class DigestInput(BaseModel):
    """Input for remind_me_digest: a synthesized vault snapshot (issue #188).

    Assembles recent additions, vault vitality, reminders, and sync health
    into one read -- see remind_me_mcp.digest for the section-by-section
    detail. Works with no configuration; scheduled delivery of the same
    digest through a notification channel is a separate opt-in
    (REMIND_ME_DIGEST_INTERVAL).
    """

    model_config = ConfigDict(extra="forbid")

    since_days: int = Field(
        default=7,
        ge=1,
        le=365,
        description="How many days back counts as a 'recent addition' in the digest's first section.",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


# ---------------------------------------------------------------------------
# Saved searches (issue #194)
# ---------------------------------------------------------------------------


class SaveSearchInput(BaseModel):
    """Input for remind_me_save_search: create or update (by name) a saved search.

    A second call with the same `name` updates the existing saved search
    in place (query/filters/watch all overwritten with the new call's
    values) rather than creating a duplicate -- the same "same name is the
    same logical thing" convention `remind_me_wiki_write` already uses for
    pages. `include_sensitive` defaults to False for consistency with
    `remind_me_search`'s own default (issue #195) -- a saved search is just
    a stored, replayable `remind_me_search` call, so it should not
    surface sensitive memories by default either.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(
        ...,
        description="Unique name for this saved search. Saving again with the same name updates it.",
        min_length=1,
        max_length=200,
    )
    query: str = Field(
        ...,
        description="The search query to store and re-run, same syntax as remind_me_search's query.",
        min_length=1,
        max_length=500,
    )
    category: str | None = Field(default=None, description="Filter by category")
    tags: list[str] | None = Field(
        default=None, description="Filter: memory must have ALL of these tags"
    )
    include_sensitive: bool = Field(
        default=False,
        description=(
            "Include memories marked sensitive (issue #195) when this saved "
            "search runs or is polled. Off by default, same as remind_me_search."
        ),
    )
    watch: bool = Field(
        default=False,
        description=(
            "Actively poll this saved search in the background "
            "(REMIND_ME_SAVED_SEARCH_POLL_INTERVAL seconds, default 300) and "
            "notify (see Notifications) on genuinely new matches. The first "
            "poll after watch is turned on seeds its 'already seen' state "
            "from the current results WITHOUT notifying -- only a match that "
            "appears on a LATER poll triggers a notification."
        ),
    )


class ImportKind(StrEnum):
    """How to parse an imported file (FT-02, extended by FT-19, FT-20, FT-31, FT-32).

    AUTO routes by extension and content sniffing: .json/.jsonl always import
    as chat; .pdf always imports as pdf; .png/.jpg/.jpeg always import as
    image; .mp3/.m4a/.wav/.ogg always import as audio; .md/.markdown/.txt
    import as chat when they contain chat role markers (e.g. '**User:**',
    '## Assistant'), otherwise as a document.
    READWISE (a Readwise "Export" JSON file, one memory per highlight) is
    deliberately NOT reachable through AUTO — a Readwise export and a chat
    export are both plain .json with no reliable content-sniff to tell them
    apart, so it must be requested explicitly (see readwise_import.py).
    OBSIDIAN (frontmatter/wikilink/inline-tag-aware Markdown, one note's
    sections chunked like a document — see obsidian_import.py) is likewise
    never chosen by AUTO's *content* sniffing (frontmatter delimiters and
    double-bracket text both have legitimate non-Obsidian uses), but a
    caller that resolves the kind through
    remind_me_mcp.config.resolve_import_kind first — as the folder watcher
    and remind_me_import_directory both do — gets it automatically for a
    .md/.markdown file inside a detected Obsidian vault (a `.obsidian/`
    directory at or above the watched/imported root).
    AUDIO (a transcribed .mp3/.m4a/.wav/.ogg file, chunked per transcript
    segment with a start/end timestamp — see audio_import.py) IS reachable
    through AUTO, unconditionally, like PDF/IMAGE: there is nothing to
    content-sniff, a suffix on its list is always audio.
    """

    AUTO = "auto"
    CHAT = "chat"
    DOCUMENT = "document"
    PDF = "pdf"
    IMAGE = "image"
    READWISE = "readwise"
    OBSIDIAN = "obsidian"
    AUDIO = "audio"


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
            "How to parse the file (FT-02, extended by FT-19, FT-20, FT-31, FT-32): "
            "'auto' — detect by extension/content (chat-style markdown imports "
            "as chat, notes markdown/text as a document, .pdf as pdf, "
            "image extensions as image, audio extensions as audio; never "
            "resolves to 'readwise' by content — see below — but DOES resolve "
            "to 'obsidian' for a .md/.markdown file inside a detected Obsidian "
            "vault, i.e. a `.obsidian/` directory at or above an ancestor "
            "import root), "
            "'chat' — force the chat-export parser, "
            "'document' — force per-section/paragraph document chunking "
            "(.md/.markdown/.txt only), "
            "'pdf' — force per-page PDF chunking (.pdf only; requires the "
            "optional 'pdf' extra), "
            "'image' — force OCR of an image into a single memory "
            "(.png/.jpg/.jpeg only; requires the optional 'image' extra), "
            "'readwise' — force a Readwise 'Export' JSON file into one memory "
            "per highlight (.json only; must be requested explicitly — 'auto' "
            "never picks it, since a Readwise export and a chat export are both "
            "indistinguishable-by-extension .json files), "
            "'obsidian' — force frontmatter/wikilink/inline-#tag-aware Markdown "
            "import (.md/.markdown only): frontmatter 'tags' and inline '#tag' "
            "syntax become memory tags, and '[[wikilinks]]' resolve to entities "
            "linked to the memory via the existing knowledge-graph machinery, "
            "'audio' — force transcription of an audio file, chunked per "
            "transcript segment with a start/end timestamp "
            "(.mp3/.m4a/.wav/.ogg only; requires the optional 'audio' extra)"
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
        if p.suffix.lower() not in (
            ".json", ".jsonl", ".md", ".markdown", ".txt", ".pdf", ".png", ".jpg", ".jpeg",
            ".mp3", ".m4a", ".wav", ".ogg",
        ):
            raise ValueError(
                f"Unsupported file type: {p.suffix}. Use .json, .jsonl, .md, .pdf, an image, or audio"
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
    include_deleted: bool = Field(
        default=False,
        description=(
            "Include soft-deleted and superseded memories. Off by default: "
            "the importer treats every record as a fresh live message, so "
            "re-importing an export that included them would resurrect "
            "deleted/stale content as new live memories. Only set true for "
            "a genuine full-backup/audit export, not for moving memories "
            "between machines."
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
            "Per-file parsing mode (FT-02, extended by FT-19, FT-20, FT-31, FT-32): 'auto' "
            "(detect chat/document/pdf/image/audio per file — never 'readwise', which "
            "must be forced explicitly and then applies to every .json file in "
            "the directory; DOES resolve to 'obsidian' per .md/.markdown file "
            "inside a detected Obsidian vault), "
            "'chat', 'document', 'pdf', 'image', 'readwise', 'obsidian', or 'audio'"
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
    "reference",
}
"""Allowed memory_type values for classification (excludes 'unclassified').

``reference`` (issue #220) is for standing reference material -- source code,
diagrams, doc fragments imported wholesale -- rather than something distilled
from a conversation. It exists because the other seven all describe a *claim*
of some kind, and bulk-imported file contents assert nothing: they were being
filed as ``fact`` for want of anywhere better, which made ``fact``-filtered
views a mixture of real assertions and pasted-in source.

Named ``reference`` rather than ``source_snippet`` because the content is not
only code -- the batch that prompted this included SVG diagrams and
documentation -- and a name that says "code" would push the next importer of
non-code reference material back into ``fact``."""


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
            "decision, preference, fact, insight, learning, blocker, "
            "action_item, reference. Use 'reference' for standing reference "
            "material with no claim attached -- source code, diagrams, doc "
            "fragments imported wholesale -- and 'fact' only for a durable "
            "factual assertion."
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
            "decision, preference, fact, insight, learning, blocker, "
            "action_item, reference. Use 'reference' for raw reference "
            "material (source code, diagrams, doc fragments) rather than "
            "'fact', which is for durable factual assertions. "
            "Defaults to 'unclassified' if not provided."
        ),
    )
    extra_tags: list[str] = Field(
        default_factory=list,
        description="Additional tags to merge with the parent capture's tags",
    )
    subject: str | None = Field(
        default=None,
        description=(
            "Structured triple: subject (e.g. 'Bailey') — FT-04. WARNING: "
            "same (subject, predicate) as another non-superseded fact with a "
            "different object silently supersedes (hides) it — see "
            "MemoryAddInput.subject. Use a specific predicate per distinct "
            "claim, not a shared generic one."
        ),
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
    subject: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "WARNING: same (subject, predicate) as another non-superseded "
            "memory with a different object silently supersedes (hides) it "
            "— see MemoryAddInput.subject."
        ),
    )
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
    """Input for the remind_me_consolidate tool: find and merge duplicate memories.

    Two-step workflow (issue #55 — real summarization, not concatenation):
    1. Call with dry_run=True (default) to see the clusters found.
    2. Write a short summary for each cluster you want merged, then call
       again with dry_run=False and summaries={canonical_id: summary, ...}.
       A cluster whose canonical id has no entry in summaries is skipped
       (not merged with a raw concatenation) and reported separately.
    """

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
    summaries: dict[str, str] | None = Field(
        default=None,
        description=(
            "Required to actually merge a cluster when dry_run=False: "
            "{canonical_id: llm_authored_summary}, one entry per cluster "
            "(from a prior dry_run=True call) you want consolidated. This "
            "summary becomes the canonical memory's new content, replacing "
            "raw concatenation with genuine summarization. A found cluster "
            "with no matching entry here is skipped, not merged."
        ),
    )


# ---------------------------------------------------------------------------
# Importance recalibration models (issue #200)
# ---------------------------------------------------------------------------


class RecalibrateCandidatesInput(BaseModel):
    """Input for remind_me_recalibrate_candidates: surface memories whose
    importance classification may be stale (issue #200).

    Read-only surfacing half of a two-phase, Claude-driven workflow, the
    same shape as remind_me_normalize_batch/remind_me_reclassify_batch: this
    tool only narrows an unbounded set down to a reviewable batch via a
    deterministic heuristic (see maintenance.RECALIBRATION_CANDIDATE_WHERE).
    The actual judgment happens in the calling Claude session, which then
    applies anything that needs to change via the EXISTING
    remind_me_reclassify/remind_me_reclassify_batch tools (or
    remind_me_feedback for a pure importance nudge) -- there is no separate
    "apply" tool here.
    """

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Number of importance-review candidates to return",
    )


# ---------------------------------------------------------------------------
# Free-text contradiction candidates (issue #201)
# ---------------------------------------------------------------------------


class ContradictionCandidatesInput(BaseModel):
    """Input for remind_me_contradiction_candidates: surface pairs of memories
    that might conflict but were never caught by structured-triple
    supersession (issue #201).

    Read-only surfacing half of the same two-phase, Claude-driven shape as
    remind_me_recalibrate_candidates: this tool only narrows an unbounded
    all-pairs comparison down to a reviewable batch, bounded by the entity
    graph (pairs must share at least one linked entity) and excluding pairs
    already resolvable by the exact subject+predicate supersession mechanism
    (see maintenance.CONTRADICTION_CANDIDATE_PAIRS_SQL). The actual judgment
    -- whether a given pair genuinely conflicts -- happens in the calling
    Claude session, which then acts on a real finding with the EXISTING
    remind_me_update/remind_me_delete tools, or by writing a superseding
    remind_me_add with an explicit SPO triple -- there is no separate
    "apply"/"resolve" tool here.

    Paged with an ``(after_a, after_b)`` keyset cursor (issue #219). A keyset
    rather than an offset because the pair set is derived from live memories:
    an edit between calls shifts an offset's window, silently skipping or
    repeating rows, whereas "after this pair" stays correct.
    """

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Number of candidate pairs to return",
    )
    after_a: str | None = Field(
        default=None,
        description=(
            "Keyset cursor: return only pairs ordered after (after_a, after_b). "
            "Pass the next_after_a/next_after_b from the previous response to "
            "advance through the queue. Both must be given together."
        ),
    )
    after_b: str | None = Field(
        default=None,
        validate_default=True,
        description="Second half of the keyset cursor; see after_a.",
    )

    # validate_default=True is load-bearing: without it the validator never
    # runs when after_b is simply omitted, so `after_a` alone sails straight
    # through -- which is the exact half-a-cursor case it exists to reject.
    @field_validator("after_b", mode="after")
    @classmethod
    def _cursor_halves_travel_together(cls, v: str | None, info: Any) -> str | None:
        """Reject half a cursor (issue #219).

        A lone ``after_a`` would silently mean "from the first pair of that
        memory", and a lone ``after_b`` would be ignored entirely -- both
        read as a working call that quietly pages from the wrong place,
        which is the same class of silent-wrong-answer this issue is about.
        """
        after_a = info.data.get("after_a")
        if (after_a is None) != (v is None):
            raise ValueError(
                "after_a and after_b must be provided together (both or neither)"
            )
        return v


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


class UndoImportKind(StrEnum):
    """Which import-tracking table an undo targets.

    Each import path records what it created, but links back to ``memories``
    differently: mempalace and dbs store a ``memory_id`` per tracked row, while
    chat imports key on ``import_id`` and stamp it onto ``memories.doc_id``.
    """

    MEMPALACE = "mempalace"
    CHAT = "chat"
    DBS = "dbs"


class UndoImportInput(BaseModel):
    """Input for remind_me_undo_import: roll back a previous import."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    import_kind: UndoImportKind = Field(
        ..., description="Which import to undo: mempalace, chat, or dbs."
    )
    import_id: str | None = Field(
        default=None,
        description=(
            "Scope to one import run. For 'chat' this is the chat_imports "
            "import_id; for 'dbs' the dbs_source; for 'mempalace' a drawer_id "
            "prefix (e.g. a wing name). Omit to target every record from that "
            "import kind."
        ),
    )
    dry_run: bool = Field(
        default=True,
        description=(
            "When True (the default), report exactly what would be removed and "
            "change nothing. Pass False to actually delete — this is "
            "deliberately opt-in because the operation is bulk and, on a "
            "sync-enabled node, propagates to every other node."
        ),
    )
    limit: int = Field(
        default=500,
        ge=1,
        le=5000,
        description=(
            "Maximum memories to remove per call. The work is resumable: call "
            "repeatedly until 'remaining' reaches 0. Bounded so a large undo "
            "cannot exceed the MCP call timeout."
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
    "SetReminderInput",
    "ReminderWindow",
    "ListRemindersInput",
    "DigestInput",
    "SaveSearchInput",
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
    "UndoImportKind",
    "UndoImportInput",
]
