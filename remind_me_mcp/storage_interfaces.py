"""
remind_me_mcp.storage_interfaces — Storage-layer interface documentation
(cognee gap #1, Phase 8, prep-work only).

**This module ships no new backend.** remind_me stores everything in a
single SQLite file (plus the `sqlite-vec` extension for vector KNN) by
design — that is the local-first, zero-ops, single-user architecture the
project is built around, and swapping in Neo4j/Qdrant/Postgres-as-primary
would conflict with it directly (a second service to run, a second failure
mode, a second thing to back up). See the cognee/Cerebras capability review
docs under `docs/` for the full reasoning.

What this module *does* provide: a documented, type-checked description of
the storage operations `remind_me_mcp.db` already implements as free
functions, expressed as `typing.Protocol`s. Each Protocol's `__call__`
signature matches one of those functions exactly, verified at the bottom of
this file via mypy-checked assignments (`if TYPE_CHECKING`) — not a runtime
`isinstance` check, since `Protocol.__call__` shape-matching that way only
confirms attribute presence, not parameter/return types.

Why bother with Protocols nobody implements a second time (yet)? Two
reasons:
  1. **Documentation with teeth.** The Protocol docstrings are the closest
     thing this codebase has to a storage-layer API contract — a clearer
     read than grepping db.py for every place these operations get called.
  2. **A real seam, if the project's scope ever changes.** If remind_me
     ever needed a second backend (unlikely, per the design center above,
     but not impossible — e.g. a hosted multi-tenant variant, which is
     itself a deferred, scope-changing decision — see cognee gap #6 in the
     README), these Protocols mark exactly which operations a replacement
     would need to satisfy, and callers that type-hint against them instead
     of importing `remind_me_mcp.db` directly would already be
     backend-agnostic. Nothing in the codebase does that today — this is
     prep, not a migration.

Deliberately not covered: schema migration (`_ensure_schema`,
`_migrate_schema`), sync/outbox plumbing, and the FTS5 keyword-search path —
these are SQLite-specific enough (PRAGMA user_version, FTS5 virtual tables,
WAL-mode connection setup) that abstracting them behind a Protocol would be
premature generalization with no plausible second implementation, not a
useful seam.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import sqlite3

# ---------------------------------------------------------------------------
# Entity graph storage
# ---------------------------------------------------------------------------


class EntityUpserter(Protocol):
    """Create or merge a named entity (matches `db._upsert_entity`).

    A new name creates a new entity with a deterministic id; re-upserting an
    existing name union-merges aliases and fills a missing `kind`, never
    overwriting an existing one. Does not commit — the caller controls the
    transaction boundary (batch operations upsert many entities per commit).
    """

    def __call__(
        self,
        db: sqlite3.Connection,
        name: str,
        kind: str | None = None,
        aliases: list[str] | None = None,
        *,
        node_id: str | None = None,
        now: str | None = None,
    ) -> str:
        """Returns the entity's deterministic id (12 hex chars)."""
        ...


class MemoryEntityLinker(Protocol):
    """Record that a memory mentions an entity (matches `db._link_memory_entity`).

    Immutable insert-or-ignore — a link is never updated or removed once
    created. Does not commit.
    """

    def __call__(
        self,
        db: sqlite3.Connection,
        memory_id: str,
        entity_id: str,
        now: str | None = None,
    ) -> bool:
        """Returns True if a new link row was created (False if it already existed)."""
        ...


class EntityRelationUpserter(Protocol):
    """Create or confirm a typed subject-relation-object edge between two
    entities (matches `db._upsert_entity_relation`).

    Unlike entities, relation triples are immutable — insert-or-ignore only,
    no merge semantics. Does not commit.
    """

    def __call__(
        self,
        db: sqlite3.Connection,
        subject_entity_id: str,
        relation: str,
        object_entity_id: str,
        *,
        node_id: str | None = None,
        now: str | None = None,
    ) -> str:
        """Returns the relation's deterministic id (created-or-existing)."""
        ...


class EntityResolver(Protocol):
    """Look up an entity by canonical name or alias, case/whitespace-insensitive
    (matches `db._resolve_entity`)."""

    def __call__(self, db: sqlite3.Connection, query: str) -> dict[str, Any] | None:
        """Returns the entity row as a dict, or None if unresolved."""
        ...


class EntityProfileReader(Protocol):
    """Resolve an entity and assemble its full profile: canonical record,
    SPO facts naming it, and linked memories (matches `db._entity_profile`).

    The read path shared by both the `remind_me_entity` MCP tool and
    `GET /api/entity`.
    """

    def __call__(
        self, db: sqlite3.Connection, query: str, limit: int = 20
    ) -> dict[str, Any] | None:
        """Returns {'entity', 'facts', 'memories', 'total_linked_memories'}, or None if unresolved."""
        ...


# ---------------------------------------------------------------------------
# Vector storage
# ---------------------------------------------------------------------------


class VectorSearcher(Protocol):
    """Semantic KNN search over embedded memory chunks (matches `db._semantic_search`).

    Implementations are expected to over-fetch at chunk granularity and
    collapse to one best-distance hit per memory, excluding superseded rows.
    Must degrade to an empty list (not raise) when no embedder or vector
    table is available — semantic search is optional everywhere it's called.
    """

    def __call__(
        self,
        query: str,
        limit: int = 20,
        extra_texts: list[str] | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Returns ranked memory dicts (best first), each carrying a
        'semantic_distance' key; empty list if semantic search is unavailable."""
        ...


class ChunkEmbedder(Protocol):
    """Chunk and embed a single memory's content (matches `db._embed_and_store`)."""

    def __call__(self, memory_id: str, content: str) -> bool:
        """Returns True if at least one chunk was stored."""
        ...


class ChunkBatchEmbedder(Protocol):
    """Chunk and embed multiple memories in one batched pass (matches
    `db._embed_and_store_rows`) — the hot path for import/reindex, where
    batching amortizes model overhead."""

    def __call__(self, rows: list[tuple[int, str]]) -> int:
        """rows: (memory_rowid, content) pairs. Returns the count of memories
        that ended up with at least one chunk stored."""
        ...


class OrphanChunkPruner(Protocol):
    """Delete vector rows left behind by deleted memories whose rowid was
    since reused (matches `db._prune_orphan_chunks`)."""

    def __call__(self, db: sqlite3.Connection) -> int:
        """Returns the count of orphaned chunk rows removed. Commits."""
        ...


# ---------------------------------------------------------------------------
# Verification: remind_me_mcp.db's real functions satisfy these Protocols.
#
# Type-check-only (never executed) so this module stays free of db.py's
# import weight at runtime -- its only job is defining the Protocols above.
# `mypy remind_me_mcp` (the CI type-check step) fails this file if any
# function's signature drifts from its Protocol, which is the actual
# verification: a Protocol's __call__ match confirms parameter names, types,
# and return type all line up, not just that an attribute exists.
# ---------------------------------------------------------------------------

if TYPE_CHECKING:
    from remind_me_mcp import db as _db

    _upsert_entity: EntityUpserter = _db._upsert_entity
    _link_memory_entity: MemoryEntityLinker = _db._link_memory_entity
    _upsert_entity_relation: EntityRelationUpserter = _db._upsert_entity_relation
    _resolve_entity: EntityResolver = _db._resolve_entity
    _entity_profile: EntityProfileReader = _db._entity_profile
    _semantic_search: VectorSearcher = _db._semantic_search
    _embed_and_store: ChunkEmbedder = _db._embed_and_store
    _embed_and_store_rows: ChunkBatchEmbedder = _db._embed_and_store_rows
    _prune_orphan_chunks: OrphanChunkPruner = _db._prune_orphan_chunks

# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "EntityUpserter",
    "MemoryEntityLinker",
    "EntityRelationUpserter",
    "EntityResolver",
    "EntityProfileReader",
    "VectorSearcher",
    "ChunkEmbedder",
    "ChunkBatchEmbedder",
    "OrphanChunkPruner",
]
