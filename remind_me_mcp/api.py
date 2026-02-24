"""
remind_me_mcp.api — Starlette HTTP API app builder and dashboard HTML builder.

Provides the REST API layer for the optional dashboard UI. All Starlette
imports are kept lazy (inside _build_api_app) to avoid importing the heavy
web framework when running in MCP stdio-only mode.

The dashboard HTML template loads JSX from the dashboard/App.jsx file at
build time, keeping the Python source clean and the JSX separately editable.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from remind_me_mcp.config import DB_PATH
from remind_me_mcp.db import _get_db, _make_id, _now_iso, _row_to_dict
from remind_me_mcp.importer import import_chat_file, import_directory

log = logging.getLogger("remind_me_mcp.api")

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
    Includes CORS middleware allowing all origins for dashboard access.

    Returns:
        A configured Starlette application with REST routes for memory CRUD,
        search, import, stats, and the dashboard UI.
    """
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
        """Return aggregate statistics about the memory store."""
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

    async def api_list(request: Request) -> JSONResponse:
        """List memories with optional category, source, and tag filters."""
        db = _get_db()
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
        limit = min(int(params.get("limit", 50)), 200)
        offset = max(int(params.get("offset", 0)), 0)

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

    async def api_search(request: Request) -> JSONResponse:
        """Full-text search memories using FTS5 with optional category/tag filters."""
        import sqlite3 as _sqlite3

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
        except _sqlite3.OperationalError as e:
            return _json_err(f"Search error: {e}")

        memories = [_row_to_dict(r) for r in rows]

        if cat := params.get("category"):
            memories = [m for m in memories if m["category"] == cat]
        if tag_param := params.get("tags"):
            tag_set = set(tag_param.split(","))
            memories = [m for m in memories if tag_set.issubset(set(m.get("tags", [])))]

        return _json_ok({"count": len(memories), "memories": memories})

    async def api_get(request: Request) -> JSONResponse:
        """Retrieve a single memory by its ID."""
        db = _get_db()
        memory_id = request.path_params["memory_id"]
        row = db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if not row:
            return _json_err("Not found", 404)
        return _json_ok(_row_to_dict(row))

    async def api_add(request: Request) -> JSONResponse:
        """Create a new memory from a JSON request body."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            return _json_err(f"Invalid JSON body: {e}")

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
        """Update fields on an existing memory by its ID."""
        memory_id = request.path_params["memory_id"]
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            return _json_err(f"Invalid JSON body: {e}")

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
        """Permanently delete a memory by its ID."""
        memory_id = request.path_params["memory_id"]
        db = _get_db()
        result = db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        db.commit()
        if result.rowcount == 0:
            return _json_err("Not found", 404)
        return _json_ok({"deleted": memory_id})

    async def api_import(request: Request) -> JSONResponse:
        """Import a chat export file or directory into the memory store."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            return _json_err(f"Invalid JSON body: {e}")

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
                # Directory import — delegates to shared import_directory() (DRY)
                summary = import_directory(
                    directory=str(p),
                    category=category,
                    tags=tags,
                    extract_mode=extract_mode,
                    max_length=max_length,
                    recursive=True,
                )
                return _json_ok(summary)
            else:
                # Single file import
                result = import_chat_file(str(p), category, tags, extract_mode, max_length)
                return _json_ok(result)
        except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
            log.error("Import failed for %s: %s", file_path, e)
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


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "_build_api_app",
    "_build_dashboard_html",
]
