"""
remind_me_mcp.peer_server — Lightweight HTTP server for peer-to-peer sync.

Each remind_me instance runs this server locally so other peers on the
Tailscale network can push to and pull from it directly, without going
through the hub. Uses the same push/pull protocol as the hub, with two
additive extensions: pull accepts a ``since_id`` keyset-cursor parameter,
and push responses include ``processed_ids`` so the sender can mark
exactly the records this node handled.

FT-04 adds two endpoints for the entity graph: ``/sync/pull_entities`` and
``/sync/pull_links``. Old peers never call them, and a new peer pulling
from an old server treats the resulting 404 as "no entity support" —
backward compatible in both directions.
"""
from __future__ import annotations

import contextlib
import hmac
import json
import logging
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlparse

from remind_me_mcp.config import NODE_ID, PEER_BIND, PEER_PORT, SYNC_SECRET
from remind_me_mcp.db import _get_db
from remind_me_mcp.sync import _upsert_records

log = logging.getLogger("remind_me_mcp.peer_server")

# Request hardening limits
MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MiB per push
MAX_PULL_LIMIT = 500


class PeerHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        log.debug(format, *args)

    def _auth(self) -> bool:
        """Constant-time bearer check; always rejects when no secret is set."""
        if not SYNC_SECRET:
            return False
        auth = self.headers.get("Authorization", "")
        return hmac.compare_digest(auth, f"Bearer {SYNC_SECRET}")

    def _send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def do_GET(self):
        if not self._auth():
            self._send_json(401, {"error": "unauthorized"})
            return

        parsed = urlparse(self.path)

        if parsed.path == "/health":
            self._send_json(200, {
                "status": "ok",
                "node_id": NODE_ID,
                "time": datetime.now(UTC).isoformat(),
            })
            return

        if parsed.path == "/sync/pull":
            params = parse_qs(parsed.query)
            since = params.get("since", ["1970-01-01T00:00:00+00:00"])[0]
            exclude_node = params.get("exclude_node", [None])[0]
            try:
                limit = int(params.get("limit", [str(MAX_PULL_LIMIT)])[0])
            except ValueError:
                self._send_json(400, {"error": "invalid limit"})
                return
            limit = max(1, min(limit, MAX_PULL_LIMIT))

            # Keyset cursor (SY-04): when the client sends since_id, page on
            # (updated_at, id) so boundary-timestamp ties are never skipped.
            # Without since_id, keep the legacy strict updated_at comparison.
            if "since_id" in params:
                since_id = params.get("since_id", [""])[0]
                where = "(m.updated_at > ? OR (m.updated_at = ? AND m.id > ?))"
                bindings: list = [since, since, since_id]
            else:
                where = "m.updated_at > ?"
                bindings = [since]
            if exclude_node:
                where += " AND (m.node_id IS NULL OR m.node_id != ?)"
                bindings.append(exclude_node)
            bindings.append(limit)

            db = _get_db()
            rows = db.execute(f"""
                SELECT m.* FROM memories m
                WHERE {where}
                ORDER BY m.updated_at ASC, m.id ASC
                LIMIT ?
            """, bindings).fetchall()

            records = []
            for row in rows:
                d = dict(row)
                # Ensure tags/metadata are parsed for JSON serialization
                for field in ("tags", "metadata"):
                    if isinstance(d.get(field), str):
                        with contextlib.suppress(json.JSONDecodeError):
                            d[field] = json.loads(d[field])
                records.append(d)

            self._send_json(200, {"records": records, "count": len(records)})
            return

        if parsed.path == "/sync/pull_entities":
            self._pull_entities(parse_qs(parsed.query))
            return

        if parsed.path == "/sync/pull_links":
            self._pull_links(parse_qs(parsed.query))
            return

        self._send_json(404, {"error": "not found"})

    @staticmethod
    def _pull_params(params: dict) -> tuple[str, str, str | None, int] | None:
        """Parse the shared since/since_id/exclude_node/limit pull params."""
        since = params.get("since", ["1970-01-01T00:00:00+00:00"])[0]
        since_id = params.get("since_id", [""])[0]
        exclude_node = params.get("exclude_node", [None])[0]
        try:
            limit = int(params.get("limit", [str(MAX_PULL_LIMIT)])[0])
        except ValueError:
            return None
        return since, since_id, exclude_node, max(1, min(limit, MAX_PULL_LIMIT))

    def _pull_entities(self, params: dict) -> None:
        """Serve entity records newer than the keyset cursor (FT-04).

        Only FT-04-aware clients call this endpoint, so the keyset cursor
        ``(updated_at, id)`` is always used. Records carry
        ``record_type='entity'`` so the puller dispatches them correctly.
        """
        parsed = self._pull_params(params)
        if parsed is None:
            self._send_json(400, {"error": "invalid limit"})
            return
        since, since_id, exclude_node, limit = parsed

        where = "(e.updated_at > ? OR (e.updated_at = ? AND e.id > ?))"
        bindings: list = [since, since, since_id]
        if exclude_node:
            where += " AND (e.node_id IS NULL OR e.node_id != ?)"
            bindings.append(exclude_node)
        bindings.append(limit)

        db = _get_db()
        rows = db.execute(f"""
            SELECT e.* FROM entities e
            WHERE {where}
            ORDER BY e.updated_at ASC, e.id ASC
            LIMIT ?
        """, bindings).fetchall()

        records = []
        for row in rows:
            d = dict(row)
            if isinstance(d.get("aliases"), str):
                with contextlib.suppress(json.JSONDecodeError):
                    d["aliases"] = json.loads(d["aliases"])
            d["record_type"] = "entity"
            records.append(d)
        self._send_json(200, {"records": records, "count": len(records)})

    def _pull_links(self, params: dict) -> None:
        """Serve memory_entities link records newer than the cursor (FT-04).

        Links are immutable, so the keyset pages on ``created_at`` with the
        synthetic key ``memory_id || '|' || entity_id`` (the ORDER BY uses
        the same concatenated expression so server ordering and the client's
        cursor comparison agree exactly).
        """
        parsed = self._pull_params(params)
        if parsed is None:
            self._send_json(400, {"error": "invalid limit"})
            return
        since, since_id, _exclude_node, limit = parsed

        key = "(l.memory_id || '|' || l.entity_id)"
        db = _get_db()
        rows = db.execute(f"""
            SELECT l.memory_id, l.entity_id, l.created_at FROM memory_entities l
            WHERE (l.created_at > ? OR (l.created_at = ? AND {key} > ?))
            ORDER BY l.created_at ASC, {key} ASC
            LIMIT ?
        """, (since, since, since_id, limit)).fetchall()

        records = [
            {
                "record_type": "memory_entity",
                "id": f"{row['memory_id']}|{row['entity_id']}",
                "memory_id": row["memory_id"],
                "entity_id": row["entity_id"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
        self._send_json(200, {"records": records, "count": len(records)})

    def do_POST(self):
        if not self._auth():
            self._send_json(401, {"error": "unauthorized"})
            return

        if self.path == "/sync/push":
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                self._send_json(400, {"error": "invalid content-length"})
                return
            if length <= 0:
                self._send_json(400, {"error": "missing request body"})
                return
            if length > MAX_BODY_BYTES:
                self._send_json(413, {"error": "request body too large"})
                return

            try:
                body = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(400, {"error": "malformed JSON"})
                return
            if not isinstance(body, dict) or not isinstance(body.get("records", []), list):
                self._send_json(400, {"error": "invalid push payload"})
                return
            records = body.get("records", [])

            db = _get_db()
            result = _upsert_records(db, records)
            log.info(
                "Peer push from %s: %d records, %d upserted, %d failed",
                body.get("node_id", "unknown"),
                len(records),
                result.applied,
                result.failed,
            )
            self._send_json(200, {
                "accepted": result.applied,
                "processed_ids": result.processed_ids,
                "failed": result.failed,
            })
            return

        self._send_json(404, {"error": "not found"})


def start_peer_server() -> Thread | None:
    """Start the peer HTTP server in a daemon thread.

    Binds to config.PEER_BIND (default 0.0.0.0 so Tailscale peers can
    connect; narrow it via REMIND_ME_PEER_BIND). Refuses to start without a
    SYNC_SECRET — every request requires the bearer token, so an unsecured
    server would be useless and an empty secret must never authenticate.

    Returns the thread so the caller can join it on shutdown if needed.
    Returns None if the secret is missing or the port is already in use
    (another instance is serving).
    """
    if not SYNC_SECRET:
        log.warning(
            "Peer server not started: REMIND_ME_SYNC_SECRET is not configured"
        )
        return None
    try:
        server = ThreadingHTTPServer((PEER_BIND, PEER_PORT), PeerHandler)
        server.daemon_threads = True
    except OSError as exc:
        log.info(
            "Peer server port %d already in use (another instance is "
            "likely running) — skipping: %s",
            PEER_PORT,
            exc,
        )
        return None
    thread = Thread(target=server.serve_forever, daemon=True, name="peer-server")
    thread.start()
    log.info("Peer server listening on %s:%d", PEER_BIND, PEER_PORT)
    return thread
