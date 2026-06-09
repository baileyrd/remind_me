"""
benchmarks.harness — Drive the real Remind Me retrieval stack in-process.

This module stands up an isolated SQLite database (with sqlite-vec loaded when
available), points every Remind Me module that calls ``_get_db`` at it, selects
an embedder, and exposes two operations the runner needs:

- :meth:`Harness.ingest` — store a memory exactly the way ``remind_me_add``
  does (same INSERT columns + the same ``_embed_and_store`` vector write), and
  return the memory id mapped to its source session.
- :meth:`Harness.search` — call the real ``remind_me_search`` tool and return
  the ranked memory dicts, so the benchmark measures the *actual* RRF + hybrid
  ranking, not a reimplementation.

Embedder modes:
- ``real`` — the ONNX all-MiniLM-L6-v2 engine (downloads the model on first
  use; requires network/HuggingFace access).
- ``fake`` — a deterministic stand-in that exercises the full sqlite-vec path
  offline. Vectors are content-seeded and meaningless for relevance, so use
  this only for pipeline/plumbing validation, not for quality numbers.
- ``none`` — no embedder; search degrades to FTS5 keyword-only.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import sqlite3
from typing import Any

from benchmarks.embedders import DeterministicEmbedder

_PATCH_TARGET_MODULES = (
    "remind_me_mcp.db",
    "remind_me_mcp.api",
    "remind_me_mcp.importer",
    "remind_me_mcp.tools",
    "remind_me_mcp.vitality",
)


class Harness:
    """An isolated, in-process Remind Me retrieval environment for benchmarking."""

    def __init__(self, embedder_mode: str = "real") -> None:
        """Create a harness. Call :meth:`setup` before use and :meth:`teardown` after."""
        self.embedder_mode = embedder_mode
        self._db: sqlite3.Connection | None = None
        self._saved: list[tuple[Any, str, Any]] = []
        self._counter = 0
        self.has_vec = False

    # -- lifecycle ---------------------------------------------------------

    def setup(self) -> None:
        """Build the isolated DB, patch ``_get_db``/embedder/access across modules."""
        import remind_me_mcp.db as db_mod

        db = sqlite3.connect(":memory:", check_same_thread=False)
        db.row_factory = sqlite3.Row

        # Load sqlite-vec into this connection if installed, so the vector table
        # (and therefore hybrid search) is available.
        try:
            import sqlite_vec

            db.enable_load_extension(True)
            sqlite_vec.load(db)
            db.enable_load_extension(False)
            self.has_vec = True
        except Exception:  # noqa: BLE001 - any failure means "no vector search", which is fine
            self.has_vec = False

        db_mod._ensure_schema(db)
        self._db = db

        # Route every module's _get_db at our single shared connection.
        for mod_name in _PATCH_TARGET_MODULES:
            self._patch(mod_name, "_get_db", lambda: db)

        self._install_embedder()

        # Neutralize the fire-and-forget access recorder: it mutates vitality of
        # returned rows and spawns background tasks we'd otherwise have to drain.
        # Retrieval results are unaffected because each query runs on a fresh
        # haystack, so this keeps runs clean and deterministic.
        self._patch("remind_me_mcp.tools", "record_access", lambda *_a, **_k: None)

    def teardown(self) -> None:
        """Undo all patches and close the database."""
        for target, attr, original in reversed(self._saved):
            setattr(target, attr, original)
        self._saved.clear()
        if self._db is not None:
            with contextlib.suppress(Exception):
                self._db.close()
            self._db = None

    def __enter__(self) -> Harness:
        self.setup()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.teardown()

    # -- patching helpers --------------------------------------------------

    def _patch(self, module_name: str, attr: str, value: Any) -> None:
        """Set ``module.attr = value``, remembering the original for teardown."""
        import importlib

        module = importlib.import_module(module_name)
        if hasattr(module, attr):
            self._saved.append((module, attr, getattr(module, attr)))
            setattr(module, attr, value)

    def _install_embedder(self) -> None:
        """Select and patch the embedder according to ``embedder_mode``."""
        if self.embedder_mode == "real":
            # Use the package default — nothing to patch. If the model can't be
            # loaded (e.g. offline), search transparently falls back to FTS5.
            return
        if self.embedder_mode == "none":
            self._patch("remind_me_mcp.db", "_get_embedder", lambda: None)
            self._patch("remind_me_mcp.embeddings", "_get_embedder", lambda: None)
            return
        if self.embedder_mode == "fake":
            fake = DeterministicEmbedder()
            self._patch("remind_me_mcp.db", "_get_embedder", lambda: fake)
            self._patch("remind_me_mcp.embeddings", "_get_embedder", lambda: fake)
            self._patch("remind_me_mcp.embeddings", "_embedder", fake)
            return
        if self.embedder_mode == "ollama":
            from remind_me_mcp.embeddings import OllamaEmbedder

            emb = OllamaEmbedder()
            resolved = emb if emb.available else None  # None => FTS-only fallback
            self._patch("remind_me_mcp.db", "_get_embedder", lambda: resolved)
            self._patch("remind_me_mcp.embeddings", "_get_embedder", lambda: resolved)
            return
        raise ValueError(f"Unknown embedder mode {self.embedder_mode!r} (real|fake|none|ollama)")

    # -- operations --------------------------------------------------------

    def reset(self) -> None:
        """Clear all memories (and vectors) so the next haystack starts empty."""
        assert self._db is not None
        self._db.execute("DELETE FROM memories")
        if self.has_vec:
            with contextlib.suppress(sqlite3.Error):
                self._db.execute("DELETE FROM memories_vec")
                self._db.execute("DELETE FROM vec_chunks")
        self._db.commit()

    def _new_id(self, content: str) -> str:
        """Generate a collision-free 12-char id (counter-salted, unlike _make_id)."""
        self._counter += 1
        return hashlib.sha256(f"{content}|{self._counter}".encode()).hexdigest()[:12]

    def ingest(self, content: str, session_id: str, *, embed: bool = True) -> str:
        """Store one memory the way remind_me_add does; return its id.

        The source session id is recorded in metadata so results can be mapped
        back to sessions, and also returned for the runner's own id→session map.
        """
        from remind_me_mcp.db import _embed_and_store, _now_iso

        assert self._db is not None
        mem_id = self._new_id(content)
        now = _now_iso()
        self._db.execute(
            """INSERT INTO memories
               (id, content, category, tags, source, metadata, created_at, updated_at, node_id, client)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                mem_id,
                content,
                "benchmark",
                json.dumps([]),
                "benchmark",
                json.dumps({"session_id": session_id}),
                now,
                now,
                "",
                "benchmark",
            ),
        )
        self._db.commit()
        if embed:
            _embed_and_store(mem_id, content)
        return mem_id

    def ingest_batch(
        self,
        units: list,
        *,
        embed: bool = True,
        batch_size: int = 64,
    ) -> dict[str, str]:
        """Store many memories, embedding them in **batched** calls; return id→session.

        Equivalent to calling :meth:`ingest` per unit, but all vectors for the
        batch are generated with the embedder's batch ``embed()`` (one Ollama
        ``/api/embed`` request / one ONNX forward pass per *batch_size* chunk)
        instead of one call per memory. Same rows, same vectors — just far fewer
        round-trips, which dominates wall-clock when embedding over HTTP.

        Each ``unit`` must expose ``.content`` and ``.session_id``.
        """
        from remind_me_mcp.db import _embed_and_store_rows, _now_iso

        assert self._db is not None
        now = _now_iso()
        id_to_session: dict[str, str] = {}
        rows: list[tuple[int, str]] = []  # (rowid, content) for embedding

        cur = self._db.cursor()
        for unit in units:
            mem_id = self._new_id(unit.content)
            cur.execute(
                """INSERT INTO memories
                   (id, content, category, tags, source, metadata, created_at, updated_at, node_id, client)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    mem_id,
                    unit.content,
                    "benchmark",
                    json.dumps([]),
                    "benchmark",
                    json.dumps({"session_id": unit.session_id}),
                    now,
                    now,
                    "",
                    "benchmark",
                ),
            )
            rows.append((cur.lastrowid, unit.content))
            id_to_session[mem_id] = unit.session_id
        self._db.commit()

        if embed and self.has_vec and rows:
            # Same chunked, multi-vector write as remind_me_add — batched in
            # slices to bound each embed() round-trip (one ONNX pass / Ollama
            # request per slice).
            for start in range(0, len(rows), batch_size):
                _embed_and_store_rows(rows[start : start + batch_size])

        return id_to_session

    async def search(self, query: str, limit: int = 100) -> list[dict[str, Any]]:
        """Run the real remind_me_search and return ranked memory dicts.

        Uses JSON output, an unlimited token budget (no trimming), and includes
        dormant memories so decay never hides a freshly-ingested result.
        """
        from remind_me_mcp.models import MemorySearchInput, ResponseFormat
        from remind_me_mcp.tools import memory_search

        params = MemorySearchInput(
            query=query[:500],  # model caps query length at 500 chars
            limit=limit,
            token_budget=0,
            response_format=ResponseFormat.JSON,
            include_dormant=True,
            min_vitality=0.0,
        )
        raw = await memory_search(params)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return payload.get("memories", [])

    def search_sync(self, query: str, limit: int = 100) -> list[dict[str, Any]]:
        """Synchronous convenience wrapper around :meth:`search`."""
        return asyncio.run(self.search(query, limit=limit))
