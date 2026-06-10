"""
remind_me_mcp.api — Starlette HTTP API app builder and dashboard HTML builder.

Provides the REST API layer for the optional dashboard UI. All Starlette
imports are kept lazy (inside _build_api_app) to avoid importing the heavy
web framework when running in MCP stdio-only mode.

The dashboard HTML template loads JSX from the dashboard/App.jsx file at
build time, keeping the Python source clean and the JSX separately editable.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from remind_me_mcp.config import DB_PATH, is_in_export_roots, is_in_import_roots, resolve_api_key
from remind_me_mcp.db import (
    _embed_and_store,
    _entity_profile,
    _get_db,
    _make_id,
    _normalize_entity_name,
    _now_iso,
    _resolve_entity,
    _row_to_dict,
)
from remind_me_mcp.exporter import EXPORT_FORMATS, collect_export_records, export_memories, render_export
from remind_me_mcp.importer import IMPORT_KINDS, import_chat_file, import_directory

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, MutableMapping

    from starlette.applications import Starlette
    from starlette.requests import Request

    _Scope = MutableMapping[str, Any]
    _Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
    _Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
    _ASGIApp = Callable[[_Scope, _Receive, _Send], Awaitable[None]]

log = logging.getLogger("remind_me_mcp.api")

# FT-04: entity:NAME / entity:"Full Name" filter token in the search query —
# parity with the MCP search surface's structured-query syntax.
_ENTITY_QUERY_PATTERN = re.compile(r'entity:"([^"]+)"|entity:(\S+)')


# ---------------------------------------------------------------------------
# Shared ASGI middleware (no Starlette dependency — usable in stdio mode too)
# ---------------------------------------------------------------------------


async def _send_json(send: _Send, status: int, payload: dict[str, Any]) -> None:
    """Send a minimal JSON HTTP response over a raw ASGI ``send`` callable."""
    body = json.dumps(payload).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
    })
    await send({"type": "http.response.body", "body": body})


def _header(scope: _Scope, name: bytes) -> str:
    """Return the first value of an ASGI request header, or '' when absent."""
    for key, value in scope.get("headers", []):
        if key == name:
            return str(value.decode("latin-1"))
    return ""


class BearerAuthMiddleware:
    """Pure-ASGI bearer-token middleware (SE-05).

    Shared by the dashboard API app (gating ``/api/*``) and the combined-mode
    MCP HTTP wrapper in ``__main__`` (gating everything). The token comparison
    uses ``hmac.compare_digest`` to avoid timing side channels.

    Args:
        app: The downstream ASGI application.
        secret: The expected bearer token; ``None`` disables auth entirely.
        protect_prefix: Only paths starting with this prefix are gated.
        allow_paths: Exact paths that always pass (e.g. ``/health``).
    """

    def __init__(
        self,
        app: _ASGIApp,
        secret: str | None,
        protect_prefix: str = "/",
        allow_paths: tuple[str, ...] = (),
    ) -> None:
        self.app = app
        self.secret = secret
        self.protect_prefix = protect_prefix
        self.allow_paths = allow_paths

    async def __call__(self, scope: _Scope, receive: _Receive, send: _Send) -> None:
        """Enforce bearer auth on protected HTTP paths; pass everything else through."""
        if scope["type"] != "http" or self.secret is None:
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path in self.allow_paths or not path.startswith(self.protect_prefix):
            await self.app(scope, receive, send)
            return
        auth = _header(scope, b"authorization")
        expected = f"Bearer {self.secret}"
        if hmac.compare_digest(auth.encode("utf-8"), expected.encode("utf-8")):
            await self.app(scope, receive, send)
            return
        await _send_json(send, 401, {"error": "Unauthorized"})


class JSONContentTypeMiddleware:
    """Reject mutating API requests whose Content-Type is not JSON (SE-01).

    Browsers send cross-origin "simple" requests (no CORS preflight) only with
    text/plain, multipart/form-data, or application/x-www-form-urlencoded
    bodies. Requiring ``application/json`` on POST/PUT/PATCH forces a
    preflight, which the localhost-only CORS policy denies for foreign
    origins — closing the CSRF hole even when auth is explicitly disabled.
    """

    _MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH"})

    def __init__(self, app: _ASGIApp, protect_prefix: str = "/api/") -> None:
        self.app = app
        self.protect_prefix = protect_prefix

    async def __call__(self, scope: _Scope, receive: _Receive, send: _Send) -> None:
        """Return 415 for mutating /api/* requests without a JSON Content-Type."""
        if (
            scope["type"] == "http"
            and scope.get("method", "").upper() in self._MUTATING_METHODS
            and scope.get("path", "").startswith(self.protect_prefix)
        ):
            content_type = _header(scope, b"content-type").split(";")[0].strip().lower()
            if content_type != "application/json":
                await _send_json(
                    send, 415, {"error": "Content-Type must be application/json"}
                )
                return
        await self.app(scope, receive, send)

# ---------------------------------------------------------------------------
# Dashboard HTML builder
# ---------------------------------------------------------------------------


def _build_dashboard_html() -> str:
    """Return a self-contained HTML page with the React dashboard embedded.

    Reads the JSX component from dashboard/App.jsx at call time, embedding
    it inside a Babel standalone script block. The HTML wrapper stays in
    Python; only the JSX is in the separate file.
    """
    jsx_path = Path(__file__).parent / "dashboard" / "App.jsx"
    jsx_content = jsx_path.read_text(encoding="utf-8")

    # HY-04: CDN assets are pinned to exact versions with Subresource Integrity
    # (sha384 computed from the corresponding npm tarballs, which are the exact
    # bytes unpkg serves), so a compromised or substituted CDN response cannot
    # execute. NOTE: the dashboard still requires network access to unpkg.com
    # on first load (assets are not vendored), so it does not work offline.
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
<script src="https://unpkg.com/react@18.3.1/umd/react.production.min.js"
        integrity="sha384-DGyLxAyjq0f9SPpVevD6IgztCFlnMF6oW/XQGmfe+IsZ8TqEiDrcHkMLKI6fiB/Z"
        crossorigin="anonymous"></script>
<script src="https://unpkg.com/react-dom@18.3.1/umd/react-dom.production.min.js"
        integrity="sha384-gTGxhz21lVGYNMcdJOyq01Edg0jhn/c22nsx0kyqP0TxaV5WVdsSH1fSDUf5YJj1"
        crossorigin="anonymous"></script>
<script src="https://unpkg.com/@babel/standalone@7.29.7/babel.min.js"
        integrity="sha384-ezQ6HS3FLspd9te19o2McUV6FAK091+GG7KO54f/R8DKgCDi7fULhapNrd5LY+vG"
        crossorigin="anonymous"></script>
<script type="text/babel">
""" + jsx_content + """
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Starlette API app builder
# ---------------------------------------------------------------------------


def _build_api_app() -> Starlette:
    """Build a Starlette ASGI app that exposes the memory DB as a REST API.

    All Starlette imports are kept local to this function so the module can
    be imported without loading the web framework (used in MCP stdio mode).
    Includes CORS middleware restricted to localhost origins.

    Returns:
        A configured Starlette application with REST routes for memory CRUD,
        search, import, stats, and the dashboard UI.
    """
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.cors import CORSMiddleware
    from starlette.responses import HTMLResponse, JSONResponse, Response
    from starlette.routing import Route

    # -- helpers --
    def _json_ok(data: Any, status: int = 200) -> JSONResponse:
        return JSONResponse(data, status_code=status)

    def _json_err(msg: str, status: int = 400) -> JSONResponse:
        return JSONResponse({"error": msg}, status_code=status)

    def _int_param(params: Any, name: str, default: int) -> int:
        """Parse an integer query parameter (HY-06).

        Raises ValueError with a client-friendly message for garbage input so
        handlers can answer 400 instead of crashing with a 500.
        """
        raw = params.get(name)
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except ValueError:
            raise ValueError(
                f"Invalid integer for query parameter {name!r}: {raw!r}"
            ) from None

    # -- routes --
    #
    # PF-06: every handler runs its (blocking) SQLite work in a worker thread
    # via asyncio.to_thread instead of on the event loop. Each closure calls
    # _get_db() *inside* the thread — connections are threading.local-based,
    # so the to_thread pool thread gets (or creates) its own connection
    # rather than borrowing the event loop thread's.

    async def health(request: Request) -> JSONResponse:
        """Unauthenticated liveness probe (SE-04) — reveals no data.

        Used by the pid.py health check so `--status` and the already-running
        guard keep working when API auth is enabled.
        """
        return JSONResponse({"status": "ok"})

    async def api_stats(request: Request) -> JSONResponse:
        """Return aggregate statistics about the memory store."""

        def _work() -> JSONResponse:
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
                except (json.JSONDecodeError, TypeError) as e:
                    log.debug("Malformed tags field skipped during stats aggregation: %s", e)
            return _json_ok({
                "total": total,
                "imports": imports,
                "categories": {r["category"]: r["cnt"] for r in categories},
                "sources": {r["source"]: r["cnt"] for r in sources},
                "tags": dict(sorted(all_tags.items(), key=lambda x: -x[1])),
                "db_path": str(DB_PATH),
                "db_size_mb": round(DB_PATH.stat().st_size / 1_048_576, 2) if DB_PATH.exists() else 0,
            })

        return await asyncio.to_thread(_work)

    async def api_list(request: Request) -> JSONResponse:
        """List memories with optional category, source, and tag filters."""
        params = request.query_params
        conditions: list[str] = []
        bindings: list[Any] = []

        if cat := params.get("category"):
            conditions.append("m.category = ?")
            bindings.append(cat)
        if src := params.get("source"):
            conditions.append("m.source = ?")
            bindings.append(src)
        # Tag filtering via SQL JOIN on memory_tags (DATA-02 fix: correct pagination)
        if tag_param := params.get("tags"):
            tags_list = tag_param.split(",")
            for i, tag in enumerate(tags_list):
                alias = f"mt{i}"
                conditions.append(
                    f"EXISTS (SELECT 1 FROM memory_tags {alias}"
                    f" WHERE {alias}.memory_id = m.id AND {alias}.tag = ?)"
                )
                bindings.append(tag)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        try:
            limit = min(_int_param(params, "limit", 50), 200)
            offset = max(_int_param(params, "offset", 0), 0)
        except ValueError as e:
            return _json_err(str(e))

        def _work() -> JSONResponse:
            db = _get_db()
            total = db.execute(f"SELECT COUNT(*) as cnt FROM memories m {where}", bindings).fetchone()["cnt"]
            rows = db.execute(
                f"SELECT m.* FROM memories m {where} ORDER BY m.created_at DESC LIMIT ? OFFSET ?",
                bindings + [limit, offset],
            ).fetchall()
            memories = [_row_to_dict(r) for r in rows]

            return _json_ok({
                "total": total,
                "count": len(memories),
                "offset": offset,
                "has_more": total > offset + limit,
                "memories": memories,
            })

        return await asyncio.to_thread(_work)

    async def api_search(request: Request) -> JSONResponse:
        """Full-text search memories using FTS5 with optional category/tag filters.

        FT-04 parity with the MCP search surface: an ``entity:NAME`` (or
        ``entity:"Full Name"``) token in ``q`` filters results to memories
        linked to the resolved entity via memory_entities OR whose structured
        subject/object equals the entity's canonical name. The entity path
        excludes superseded memories (DI-02). With no free text remaining,
        matching memories are listed newest-first instead of FTS-ranked; an
        unknown entity yields an empty result with a message.
        """
        import sqlite3 as _sqlite3

        params = request.query_params
        raw_query = params.get("q", "").strip()
        if not raw_query:
            return _json_err("Missing 'q' parameter")

        try:
            limit = min(_int_param(params, "limit", 50), 200)
        except ValueError as e:
            return _json_err(str(e))

        # Extract an optional entity: token (FT-04); the remainder stays the
        # FTS query.
        entity_query: str | None = None
        query = raw_query
        if entity_match := _ENTITY_QUERY_PATTERN.search(raw_query):
            entity_query = entity_match.group(1) or entity_match.group(2)
            query = " ".join(_ENTITY_QUERY_PATTERN.sub("", raw_query).split())

        # Category/tag predicates go into the SQL so they apply before LIMIT
        # (DI-03; same pattern as api_list's DATA-02 fix).
        conditions = ""
        bindings: list[Any] = []
        if cat := params.get("category"):
            conditions += " AND m.category = ?"
            bindings.append(cat)
        if tag_param := params.get("tags"):
            for i, tag in enumerate(tag_param.split(",")):
                alias = f"mt{i}"
                conditions += (
                    f" AND EXISTS (SELECT 1 FROM memory_tags {alias}"
                    f" WHERE {alias}.memory_id = m.id AND {alias}.tag = ?)"
                )
                bindings.append(tag)

        def _work() -> JSONResponse:
            db = _get_db()

            entity_conditions = ""
            entity_bindings: list[Any] = []
            if entity_query is not None:
                ent = _resolve_entity(db, entity_query)
                if ent is None:
                    return _json_ok({
                        "count": 0,
                        "memories": [],
                        "message": f"No entity found matching {entity_query!r}.",
                    })
                canon = _normalize_entity_name(str(ent["name"]))
                entity_conditions = (
                    " AND m.superseded_by IS NULL"
                    " AND (EXISTS (SELECT 1 FROM memory_entities me"
                    " WHERE me.memory_id = m.id AND me.entity_id = ?)"
                    " OR lower(m.subject) = ? OR lower(m.object) = ?)"
                )
                entity_bindings = [ent["id"], canon, canon]

            try:
                if query:
                    rows = db.execute(
                        f"""SELECT m.* FROM memories m
                           JOIN memories_fts fts ON m.rowid = fts.rowid
                           WHERE memories_fts MATCH ?{conditions}{entity_conditions}
                           ORDER BY rank LIMIT ?""",
                        [query, *bindings, *entity_bindings, limit],
                    ).fetchall()
                else:
                    # entity:-only query — no FTS text left; list the
                    # entity's memories newest-first.
                    rows = db.execute(
                        f"""SELECT m.* FROM memories m
                           WHERE 1=1{conditions}{entity_conditions}
                           ORDER BY m.created_at DESC LIMIT ?""",
                        [*bindings, *entity_bindings, limit],
                    ).fetchall()
            except _sqlite3.OperationalError as e:
                return _json_err(f"Search error: {e}")

            memories = [_row_to_dict(r) for r in rows]
            return _json_ok({"count": len(memories), "memories": memories})

        return await asyncio.to_thread(_work)

    async def api_entity(request: Request) -> JSONResponse:
        """Look up a knowledge-graph entity by name or alias (FT-04).

        Query parameters: ``name`` (required), ``limit`` (max facts and max
        linked memories, default 20, capped at 100). Mirrors the
        remind_me_entity MCP tool: deterministic-id resolution on the
        canonical name first, then a case-insensitive name/alias scan;
        superseded memories are excluded and dangling sync links are
        invisible.
        """
        params = request.query_params
        name = (params.get("name") or "").strip()
        if not name:
            return _json_err("Missing 'name' parameter")
        try:
            limit = min(_int_param(params, "limit", 20), 100)
        except ValueError as e:
            return _json_err(str(e))

        def _work() -> JSONResponse:
            db = _get_db()
            profile = _entity_profile(db, name, limit=limit)
            if profile is None:
                return _json_err(f"No entity found matching {name!r}", 404)
            return _json_ok(profile)

        return await asyncio.to_thread(_work)

    async def api_get(request: Request) -> JSONResponse:
        """Retrieve a single memory by its ID."""
        memory_id = request.path_params["memory_id"]

        def _work() -> JSONResponse:
            db = _get_db()
            row = db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if not row:
                return _json_err("Not found", 404)
            return _json_ok(_row_to_dict(row))

        return await asyncio.to_thread(_work)

    async def api_add(request: Request) -> JSONResponse:
        """Create a new memory from a JSON request body."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            return _json_err(f"Invalid JSON body: {e}")

        content = body.get("content", "").strip()
        if not content:
            return _json_err("'content' is required")

        category = body.get("category", "general")
        tags = body.get("tags", [])
        source = body.get("source", "manual")
        metadata = body.get("metadata", {})

        def _work() -> JSONResponse:
            db = _get_db()
            mem_id = _make_id(content)
            now = _now_iso()
            db.execute(
                """INSERT INTO memories (id, content, category, tags, source, metadata, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (mem_id, content, category, json.dumps(tags), source, json.dumps(metadata), now, now),
            )
            db.commit()
            _embed_and_store(mem_id, content)
            row = db.execute("SELECT * FROM memories WHERE id = ?", (mem_id,)).fetchone()
            return _json_ok(_row_to_dict(row), status=201)

        return await asyncio.to_thread(_work)

    async def api_update(request: Request) -> JSONResponse:
        """Update fields on an existing memory by its ID."""
        memory_id = request.path_params["memory_id"]
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            return _json_err(f"Invalid JSON body: {e}")

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

        def _work() -> JSONResponse:
            db = _get_db()
            row = db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if not row:
                return _json_err("Not found", 404)

            db.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id = ?", bindings)
            db.commit()
            if "content" in body and body["content"] is not None:
                _embed_and_store(memory_id, body["content"])
            updated = db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            return _json_ok(_row_to_dict(updated))

        return await asyncio.to_thread(_work)

    async def api_delete(request: Request) -> JSONResponse:
        """Permanently delete a memory by its ID."""
        memory_id = request.path_params["memory_id"]

        def _work() -> JSONResponse:
            db = _get_db()
            result = db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            db.commit()
            if result.rowcount == 0:
                return _json_err("Not found", 404)
            return _json_ok({"deleted": memory_id})

        return await asyncio.to_thread(_work)

    async def api_import(request: Request) -> JSONResponse:
        """Import a chat export or document file (or directory) into the memory store.

        The optional ``kind`` body field ('auto' default, 'chat', or
        'document') controls routing (FT-02) — parity with the MCP import
        tools: chat-style files chunk per-message, documents per-section.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            return _json_err(f"Invalid JSON body: {e}")

        file_path = body.get("file_path", "").strip()
        if not file_path:
            return _json_err("'file_path' is required")

        p = Path(file_path).expanduser().resolve()

        # SEC-02: Reject paths outside configured import roots (shared helper, SE-02)
        if not is_in_import_roots(p):
            return _json_err(f"Path not in allowed import roots: {p}")

        if not p.exists():
            return _json_err(f"Path not found: {p}")

        category = body.get("category", "chat_import")
        tags = body.get("tags", [])
        extract_mode = body.get("extract_mode", "assistant_messages")
        max_length = body.get("max_length", 10000)
        kind = str(body.get("kind", "auto")).strip().lower()
        if kind not in IMPORT_KINDS:
            return _json_err(f"Invalid kind {kind!r}: use 'auto', 'chat', or 'document'")

        try:
            if p.is_dir():
                # Directory import — delegates to shared import_directory() (DRY)
                summary = await import_directory(
                    directory=str(p),
                    category=category,
                    tags=tags,
                    extract_mode=extract_mode,
                    max_length=max_length,
                    recursive=True,
                    kind=kind,
                )
                return _json_ok(summary)
            else:
                # Single file import — blocking parse/DB/embed work off-loop (PF-06)
                result = await asyncio.to_thread(
                    import_chat_file, str(p), category, tags, extract_mode, max_length, kind
                )
                return _json_ok(result)
        except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
            log.error("Import failed for %s: %s", file_path, e)
            return _json_err(f"Import error: {e}")

    async def api_export(request: Request) -> Response:
        """Export memories as JSON or JSONL (FT-01).

        Query parameters: ``format`` (json|jsonl, default json), ``category``,
        ``tags`` (comma-separated, memory must have ALL), ``file_path``, and
        ``include_graph`` (default true — entities and memory-entity links
        follow the memories as record_type-tagged records, FT-06; pass
        false/0/no for a memories-only export). Without ``file_path`` the
        export payload is returned as the response body
        (``curl .../api/export > backup.json``); with it, the export is
        written server-side to a path inside EXPORT_ROOTS and a JSON summary
        is returned. Embedding vectors are excluded (rebuildable via reindex);
        the records round-trip through POST /api/import (which also restores
        the graph records).
        """
        params = request.query_params
        fmt = (params.get("format") or "json").strip().lower()
        if fmt not in EXPORT_FORMATS:
            return _json_err(f"Invalid format {fmt!r}: use 'json' or 'jsonl'")
        category = params.get("category") or None
        tag_param = params.get("tags")
        tags = tag_param.split(",") if tag_param else None
        file_path = (params.get("file_path") or "").strip()
        include_graph = (
            (params.get("include_graph") or "true").strip().lower()
            not in ("0", "false", "no")
        )

        if file_path:
            p = Path(file_path).expanduser().resolve()
            # Mirror the SEC-02 import-root containment check for export
            # destinations (shared helper; checked before existence probes).
            if not is_in_export_roots(p):
                return _json_err(f"Path not in allowed export roots: {p}")
            if p.is_dir():
                return _json_err(f"file_path is a directory, not a file: {p}")
            if not p.parent.is_dir():
                return _json_err(f"Parent directory not found: {p.parent}")

            def _work_file() -> JSONResponse:
                try:
                    return _json_ok(
                        export_memories(
                            format=fmt,
                            category=category,
                            tags=tags,
                            file_path=str(p),
                            include_graph=include_graph,
                        )
                    )
                except OSError as e:
                    log.error("Export failed for %s: %s", p, e)
                    return _json_err(f"Export error: {e}", 500)

            return await asyncio.to_thread(_work_file)

        def _work() -> Response:
            records = collect_export_records(
                category=category, tags=tags, include_graph=include_graph
            )
            payload = render_export(records, fmt)
            media = "application/json" if fmt == "json" else "application/x-ndjson"
            return Response(payload, media_type=media)

        return await asyncio.to_thread(_work)

    async def index(request: Request) -> HTMLResponse:
        """Serve the dashboard UI as a single-page app."""
        return HTMLResponse(_build_dashboard_html())

    routes = [
        Route("/", index),
        Route("/health", health),
        Route("/api/stats", api_stats),
        Route("/api/memories", api_list, methods=["GET"]),
        Route("/api/memories", api_add, methods=["POST"]),
        Route("/api/memories/search", api_search),
        Route("/api/memories/{memory_id}", api_get, methods=["GET"]),
        Route("/api/memories/{memory_id}", api_update, methods=["PUT", "PATCH"]),
        Route("/api/memories/{memory_id}", api_delete, methods=["DELETE"]),
        Route("/api/import", api_import, methods=["POST"]),
        Route("/api/export", api_export, methods=["GET"]),
        Route("/api/entity", api_entity, methods=["GET"]),
    ]

    # SE-01: auth is on by default — resolve_api_key() auto-generates and
    # persists a key on first run; REMIND_ME_API_KEY=disabled opts out.
    api_key = resolve_api_key()

    middleware = [
        Middleware(
            CORSMiddleware,
            allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
            allow_methods=["*"],
            allow_headers=["*"],
        ),
        Middleware(BearerAuthMiddleware, secret=api_key, protect_prefix="/api/"),
        Middleware(JSONContentTypeMiddleware),
    ]

    return Starlette(routes=routes, middleware=middleware)


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "BearerAuthMiddleware",
    "JSONContentTypeMiddleware",
    "_build_api_app",
    "_build_dashboard_html",
]
