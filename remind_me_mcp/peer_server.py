"""
remind_me_mcp.peer_server — Lightweight HTTP server for peer-to-peer sync.

Each remind_me instance runs this server locally so other peers on the
Tailscale network can push to and pull from it directly, without going
through the hub. Uses the same push/pull protocol as the hub.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlparse

from remind_me_mcp.config import NODE_ID, PEER_PORT, SYNC_SECRET
from remind_me_mcp.db import _get_db
from remind_me_mcp.sync import _upsert_records

log = logging.getLogger("remind_me_mcp.peer_server")


class PeerHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        log.debug(format, *args)

    def _auth(self) -> bool:
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {SYNC_SECRET}"

    def _send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
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
            limit = int(params.get("limit", ["500"])[0])

            db = _get_db()
            if exclude_node:
                rows = db.execute("""
                    SELECT m.* FROM memories m
                    WHERE m.updated_at > ?
                      AND (m.node_id IS NULL OR m.node_id != ?)
                    ORDER BY m.updated_at ASC
                    LIMIT ?
                """, (since, exclude_node, limit)).fetchall()
            else:
                rows = db.execute("""
                    SELECT * FROM memories
                    WHERE updated_at > ?
                    ORDER BY updated_at ASC
                    LIMIT ?
                """, (since, limit)).fetchall()

            records = []
            for row in rows:
                d = dict(row)
                # Ensure tags/metadata are parsed for JSON serialization
                for field in ("tags", "metadata"):
                    if isinstance(d.get(field), str):
                        try:
                            d[field] = json.loads(d[field])
                        except json.JSONDecodeError:
                            pass
                records.append(d)

            self._send_json(200, {"records": records, "count": len(records)})
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self._auth():
            self._send_json(401, {"error": "unauthorized"})
            return

        if self.path == "/sync/push":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            records = body.get("records", [])

            db = _get_db()
            upserted = _upsert_records(db, records)
            log.info(
                "Peer push from %s: %d records, %d upserted",
                body.get("node_id", "unknown"),
                len(records),
                upserted,
            )
            self._send_json(200, {"accepted": upserted})
            return

        self._send_json(404, {"error": "not found"})


def start_peer_server() -> Thread | None:
    """Start the peer HTTP server in a daemon thread.

    Returns the thread so the caller can join it on shutdown if needed.
    Returns None if the port is already in use (another instance is serving).
    """
    try:
        server = HTTPServer(("0.0.0.0", PEER_PORT), PeerHandler)
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
    log.info("Peer server listening on port %d", PEER_PORT)
    return thread