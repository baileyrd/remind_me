"""
remind_me_mcp.webhook_server — Push/webhook ingestion endpoint (FT-09, Phase 5a).

Runs a small HTTP server so external senders (chat-export tools, CI jobs,
automations) can push content directly into memory over the network,
without staging a file for the folder watcher to find. Modeled directly on
``peer_server.py``'s ``PeerHandler``: constant-time bearer auth, a hard
request-body size cap, and a daemon-thread lifecycle wired into the server
lifespan — stopped before the database connections close (SE-07), mirroring
``watcher.py``'s ordering.

Disabled unless ``REMIND_ME_WEBHOOK_SECRET`` is configured: an unsecured
endpoint that writes arbitrary content into memory would be worse than
useless.

Wire format — ``POST /ingest``, JSON body:
    {"filename": "chat.json", "content": "<utf-8 text>",
     "category": "chat_import", "tags": [], "extract_mode": "assistant_messages",
     "max_length": 10000, "kind": "auto"}

Only ``filename`` and ``content`` are required; the rest default exactly
like ``ChatImportInput``. ``content`` is UTF-8 text (this endpoint ingests
the same text-native formats the file importer does — JSON/JSONL chat
exports and Markdown/plain-text documents — not arbitrary binary payloads).
"""
from __future__ import annotations

import contextlib
import hmac
import json
import logging
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any

from remind_me_mcp.config import WEBHOOK_BIND, WEBHOOK_PORT, WEBHOOK_SECRET
from remind_me_mcp.importer import IMPORT_KINDS, import_content

log = logging.getLogger("remind_me_mcp.webhook_server")

# Request hardening limit — mirrors peer_server.MAX_BODY_BYTES.
MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MiB per push

_MAX_LENGTH_RANGE = (100, 50000)  # mirrors ChatImportInput.max_length bounds

_ERROR_HISTORY = 10
"""How many recent error messages the status surface keeps."""


class WebhookHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        log.debug(format, *args)

    def _auth(self) -> bool:
        """Constant-time bearer check; always rejects when no secret is set."""
        if not WEBHOOK_SECRET:
            return False
        auth = self.headers.get("Authorization", "")
        return hmac.compare_digest(auth, f"Bearer {WEBHOOK_SECRET}")

    def _send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def _drain_body(self) -> None:
        """Read and discard a pending request body before an early rejection.

        Same rationale as peer_server._drain_body: avoids a hard RST when
        the connection closes with unread body bytes still in the socket's
        receive buffer. Bounded by MAX_BODY_BYTES regardless of the claimed
        Content-Length.
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return
        remaining = min(length, MAX_BODY_BYTES)
        with contextlib.suppress(OSError):
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)

    def do_POST(self):
        if not self._auth():
            self._drain_body()
            self._send_json(401, {"error": "unauthorized"})
            return

        if self.path != "/ingest":
            self._drain_body()
            self._send_json(404, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._send_json(400, {"error": "invalid content-length"})
            return
        if length <= 0:
            self._send_json(400, {"error": "missing request body"})
            return
        if length > MAX_BODY_BYTES:
            self._drain_body()
            self._send_json(413, {"error": "request body too large"})
            return

        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "malformed JSON"})
            return

        error = self._validate_payload(payload)
        if error is not None:
            self._send_json(400, {"error": error})
            return

        result = import_content(
            content=payload["content"].encode("utf-8"),
            filename=payload["filename"],
            category=payload.get("category", "chat_import"),
            tags=payload.get("tags", []),
            extract_mode=payload.get("extract_mode", "assistant_messages"),
            max_length=payload.get("max_length", 10000),
            kind=payload.get("kind", "auto"),
        )

        status = result.get("status")
        if status in ("ok", "skipped"):
            _record_result(status)
            self._send_json(200, result)
        else:
            _record_result("errored", result.get("reason"))
            self._send_json(422, result)

    @staticmethod
    def _validate_payload(payload: Any) -> str | None:
        """Return an error message, or None when the payload is well-formed.

        Deliberately conservative (reject unknown shapes up front) so
        malformed pushes fail fast with a clear 400 instead of a confusing
        error deeper in the import pipeline.
        """
        if not isinstance(payload, dict):
            return "invalid ingest payload"
        filename = payload.get("filename")
        if not isinstance(filename, str) or not filename.strip():
            return "missing or invalid 'filename'"
        content = payload.get("content")
        if not isinstance(content, str):
            return "missing or invalid 'content' (must be a UTF-8 text string)"
        kind = payload.get("kind", "auto")
        if kind not in IMPORT_KINDS:
            return f"invalid kind: {kind!r} (use 'auto', 'chat', or 'document')"
        category = payload.get("category", "chat_import")
        if not isinstance(category, str):
            return "'category' must be a string"
        tags = payload.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            return "'tags' must be a list of strings"
        extract_mode = payload.get("extract_mode", "assistant_messages")
        if not isinstance(extract_mode, str):
            return "'extract_mode' must be a string"
        max_length = payload.get("max_length", 10000)
        lo, hi = _MAX_LENGTH_RANGE
        if isinstance(max_length, bool) or not isinstance(max_length, int) or not (lo <= max_length <= hi):
            return f"'max_length' must be an integer between {lo} and {hi}"
        return None

    def do_GET(self):
        if not self._auth():
            self._send_json(401, {"error": "unauthorized"})
            return
        self._send_json(404, {"error": "not found"})


# ---------------------------------------------------------------------------
# Status counters
# ---------------------------------------------------------------------------

_stats_lock = threading.Lock()
_requests_ingested = 0
_requests_skipped = 0
_requests_errored = 0
_errors: deque[str] = deque(maxlen=_ERROR_HISTORY)


def _record_result(status: str, error: str | None = None) -> None:
    """Update the module-level request counters (thread-safe)."""
    global _requests_ingested, _requests_skipped, _requests_errored
    with _stats_lock:
        if status == "ok":
            _requests_ingested += 1
        elif status == "skipped":
            _requests_skipped += 1
        else:
            _requests_errored += 1
            if error:
                _errors.append(error)


# ---------------------------------------------------------------------------
# Module-level lifecycle (server lifespan + status tools)
# ---------------------------------------------------------------------------

_server: ThreadingHTTPServer | None = None
_thread: Thread | None = None
_server_lock = threading.Lock()


def start_webhook_server() -> Thread | None:
    """Start the webhook HTTP server in a daemon thread (Phase 5a).

    Binds to config.WEBHOOK_BIND (default 127.0.0.1; widen deliberately via
    REMIND_ME_WEBHOOK_BIND). Refuses to start without a WEBHOOK_SECRET —
    every request requires the bearer token, so an unsecured push endpoint
    would be worse than useless.

    Returns the thread so the caller can join it on shutdown if needed.
    Returns None if the secret is missing or the port is already in use
    (another instance is serving). Idempotent — a second call while already
    running returns the existing thread.
    """
    global _server, _thread
    if not WEBHOOK_SECRET:
        log.info("Webhook server not started: REMIND_ME_WEBHOOK_SECRET is not configured")
        return None
    with _server_lock:
        if _thread is not None and _thread.is_alive():
            return _thread
        try:
            server = ThreadingHTTPServer((WEBHOOK_BIND, WEBHOOK_PORT), WebhookHandler)
            server.daemon_threads = True
        except OSError as exc:
            log.info(
                "Webhook server port %d already in use (another instance is "
                "likely running) — skipping: %s",
                WEBHOOK_PORT,
                exc,
            )
            return None
        thread = Thread(target=server.serve_forever, daemon=True, name="webhook-server")
        thread.start()
        _server = server
        _thread = thread
        log.info("Webhook server listening on %s:%d", WEBHOOK_BIND, WEBHOOK_PORT)
        return thread


def stop_webhook_server(timeout: float = 10.0) -> None:
    """Stop and discard the webhook server (no-op when not running).

    Called from the server lifespan shutdown *before* ``_close_db()`` so the
    handler thread cannot write to closed connections (SE-07), mirroring
    ``watcher.stop_watcher()``.

    Args:
        timeout: Max seconds to wait for the thread to exit.
    """
    global _server, _thread
    with _server_lock:
        server, thread = _server, _thread
        _server, _thread = None, None
    if server is not None:
        server.shutdown()
        server.server_close()
    if thread is not None and thread.is_alive():
        thread.join(timeout)


def get_webhook_status() -> dict[str, Any]:
    """Return the webhook server's status for the MCP status tools.

    Returns:
        A dict with 'enabled'/'running' flags plus bind/port and request
        counters when enabled, or a configuration hint when disabled.
    """
    if not WEBHOOK_SECRET:
        return {
            "enabled": False,
            "running": False,
            "hint": "set REMIND_ME_WEBHOOK_SECRET to enable push/webhook ingestion",
        }
    with _server_lock:
        running = _thread is not None and _thread.is_alive()
    with _stats_lock:
        ingested, skipped, errored = _requests_ingested, _requests_skipped, _requests_errored
        recent_errors = list(_errors)
    return {
        "enabled": True,
        "running": running,
        "bind": WEBHOOK_BIND,
        "port": WEBHOOK_PORT,
        "requests_ingested": ingested,
        "requests_skipped": skipped,
        "requests_errored": errored,
        "recent_errors": recent_errors,
    }


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "WebhookHandler",
    "start_webhook_server",
    "stop_webhook_server",
    "get_webhook_status",
    "MAX_BODY_BYTES",
]
