"""
remind_me_mcp.tools — All MCP tool handlers and resource handlers.

All handlers are registered on the `mcp` instance imported from server.py.
This package imports mcp from server (not the other way around) to avoid
circular imports.

Formerly a single ~2000-line module; split into submodules (HY-02):

  - ``_shared``   — background-task set, sanitize flag, small shared helpers
  - ``search``    — remind_me_search + structured-query helpers and filters
  - ``crud``      — add / get / list / update / delete
  - ``capture``   — auto_capture / get_capture / decompose(+batch)
  - ``entity``    — remind_me_entity knowledge-graph lookup (FT-04)
  - ``lifecycle`` — vitality_report / reclassify(+batch) / consolidate
  - ``wiki``      — LLM Wiki: page read/write/list/search/load/delete + compile (FT-08)
  - ``admin``     — stats / reindex / server_status / updates / imports / resources

Import compatibility: every name the old ``remind_me_mcp.tools`` module
exposed (public and underscore-prefixed) is re-exported here, so both
``from remind_me_mcp.tools import X`` and monkeypatching
``remind_me_mcp.tools.X`` keep working. Submodules resolve patchable shared
state through this package namespace at call time, so a patch applied here
is seen by the handler that uses it.
"""

# ruff: noqa: F401 — this module exists to re-export names for registration
# side effects and import/monkeypatch compatibility.

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import sqlite3
from typing import TYPE_CHECKING, Any

from remind_me_mcp.config import CLIENT, EMBED_BATCH_SIZE, NODE_ID
from remind_me_mcp.consolidation import find_clusters, merge_cluster, pick_canonical
from remind_me_mcp.db import (
    _delete_chunks,
    _embed_and_store,
    _embed_and_store_rows,
    _entity_id,
    _entity_profile,
    _get_db,
    _link_memory_entity,
    _make_id,
    _normalize_entity_name,
    _now_iso,
    _prune_orphan_chunks,
    _resolve_entity,
    _row_to_dict,
    _semantic_search,
    _upsert_entity,
)
from remind_me_mcp.formatting import _fmt_memories, _fmt_memory_md
from remind_me_mcp.importer import import_chat_file, import_directory
from remind_me_mcp.models import (
    AnnotateInput,
    AutoCaptureInput,
    BulkImportDirInput,
    ChatImportInput,
    ConsolidateInput,
    DecomposeBatchInput,
    DecomposeInput,
    EntityInput,
    EntityLookupInput,
    ExportInput,
    ExtractBatchInput,
    FeedbackInput,
    MemoryAddInput,
    MemoryDeleteInput,
    MemoryListInput,
    MemorySearchInput,
    MemoryStatsInput,
    MemoryUpdateInput,
    ReclassifyBatchInput,
    ReclassifyInput,
    ResponseFormat,
    VitalityReportInput,
)
from remind_me_mcp.pid import get_server_status
from remind_me_mcp.query_expansion import expand_query
from remind_me_mcp.reranker import RERANK_TOP_K, maybe_rerank
from remind_me_mcp.retrieval import (
    apply_token_budget,
    build_debug_signals,
    compute_tier_breakdown,
    rank_rrf,
)
from remind_me_mcp.server import mcp

# Importing the submodules runs their @mcp.tool/@mcp.resource decorators,
# registering every handler on the shared FastMCP instance. These imports
# must come after the shared-state imports above so the package namespace is
# fully populated before any handler can run.
from remind_me_mcp.tools import admin, capture, crud, entity, lifecycle, search, wiki
from remind_me_mcp.tools._shared import (
    FTS_SANITIZE_FALLBACK,
    _background_tasks,
    _maybe_update_notice,
    _public_memory,
    _spawn_task,
    log,
)
from remind_me_mcp.tools.admin import (
    memory_export,
    memory_import_chat,
    memory_import_directory,
    memory_stats,
    remind_me_check_update,
    remind_me_reindex,
    remind_me_self_update,
    remind_me_server_status,
    remind_me_watch_status,
    resource_categories,
    resource_stats,
)
from remind_me_mcp.tools.capture import (
    remind_me_annotate,
    remind_me_auto_capture,
    remind_me_decompose,
    remind_me_decompose_batch,
    remind_me_extract_batch,
    remind_me_get_capture,
)
from remind_me_mcp.tools.crud import (
    memory_add,
    memory_delete,
    memory_get,
    memory_list,
    memory_update,
)
from remind_me_mcp.tools.entity import remind_me_entity
from remind_me_mcp.tools.lifecycle import (
    remind_me_consolidate,
    remind_me_reclassify,
    remind_me_reclassify_batch,
    remind_me_vitality_report,
)
from remind_me_mcp.tools.search import (
    _STRUCTURED_PATTERN,
    _apply_filters,
    _detect_structured_query,
    _envelope_json,
    _expand_via_entities,
    _expand_via_neighbors,
    _record_envelope_access,
    _sanitize_fts_query,
    _strip_structured_prefixes,
    _structured_lookup,
    memory_search,
    remind_me_feedback,
)
from remind_me_mcp.tools.wiki import (
    resource_wiki_index,
    resource_wiki_schema,
    wiki_compile,
    wiki_delete,
    wiki_list,
    wiki_load,
    wiki_read,
    wiki_search,
    wiki_write,
)
from remind_me_mcp.updater import pop_update_notice
from remind_me_mcp.vitality import (
    DECAY_RATES,
    compute_vitality,
    effective_vitality,
    get_effective_decay_rate,
    is_dormant,
    record_accesses,
    record_feedback,
)

# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "memory_add",
    "memory_search",
    "remind_me_feedback",
    "memory_list",
    "memory_get",
    "memory_update",
    "memory_delete",
    "memory_export",
    "memory_import_chat",
    "memory_import_directory",
    "memory_stats",
    "remind_me_auto_capture",
    "remind_me_get_capture",
    "remind_me_reindex",
    "remind_me_server_status",
    "remind_me_watch_status",
    "remind_me_check_update",
    "remind_me_self_update",
    "remind_me_reclassify",
    "remind_me_reclassify_batch",
    "remind_me_vitality_report",
    "remind_me_decompose",
    "remind_me_decompose_batch",
    "remind_me_extract_batch",
    "remind_me_annotate",
    "remind_me_entity",
    "remind_me_consolidate",
    "wiki_write",
    "wiki_read",
    "wiki_list",
    "wiki_search",
    "wiki_load",
    "wiki_delete",
    "wiki_compile",
    "resource_stats",
    "resource_categories",
    "resource_wiki_schema",
    "resource_wiki_index",
]
