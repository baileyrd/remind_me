```python
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
from remind_me_mcp.telemetry import maybe_span

log = logging.getLogger("remind_me_mcp.webhook_server")

# Request hardening limit — mirrors peer_server.MAX_BODY_BYTES.
MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MiB per push

_MAX_LENGTH_RANGE = (100, 50000)  # mirrors ChatImportInput.max_length bounds

_ERROR_HISTORY = 10
"""How many recent error messages the status surface keeps."""

# ---------------------------------------------------------------------------
# Module-level status counters (mirrors peer_server pattern)
# ---------------------------------------------------------------------------
_requests_received: int = 0
_requests_succeeded: int = 0
_requests_errored: int = 0
_recent_errors: deque[str] = deque(maxlen=_ERROR_HISTORY)
_status_lock = threading.Lock()


def _record_result(outcome: str, detail: str = "") -> None:
    """Thread-safe update of module-level webhook status counters."""
    global _requests_received, _requests_succeeded, _requests_errored
    with _status_lock:
        _requests_received += 1
        if outcome == "succeeded":
            _requests_succeeded += 1
        else:
            _requests_errored += 1
            if detail:
                _recent_errors.append(detail)


def get_webhook_status() -> dict[str, Any]:
    """Return a snapshot of webhook server health for the status surface."""
    with _status_lock:
        return {
            "requests_received": _requests_received,
            "requests_succeeded": _requests_succeeded,
            "requests_errored": _requests_errored,
            "recent_errors": list(_recent_errors),
        }


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

        Same rationale as peer_server._drain_body: avoids a half-written
        request leaving the connection in a bad state on some HTTP/1.1
        clients.
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            length = 0
        if length > 0:
            self.rfile.read(min(length, MAX_BODY_BYTES))

    def do_POST(self) -> None:  # noqa: N802
        """Handle POST /ingest with full exception guard."""
        if self.path != "/ingest":
            self._drain_body()
            self._send_json(404, {"error": "not found"})
            return

        if not self._auth():
            self._drain_body()
            self._send_json(401, {"error": "unauthorized"})
            return

        # --- read body ---------------------------------------------------
        try:
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                self._send_json(411, {"error": "Content-Length required"})
                return
            length = int(raw_length)
        except (ValueError, TypeError):
            self._drain_body()
            self._send_json(400, {"error": "invalid Content-Length"})
            return

        if length > MAX_BODY_BYTES:
            self._drain_body()
            self._send_json(413, {"error": "payload too large"})
            return

        try:
            raw = self.rfile.read(length)
        except Exception as exc:  # noqa: BLE001
            error_msg = f"Failed to read request body: {exc}"
            log.exception("webhook_server: error reading request body")
            _record_result("errored", error_msg)
            self._send_json(500, {"error": "failed to read request body"})
            return

        # --- parse JSON --------------------------------------------------
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            error_msg = f"Invalid JSON payload: {exc}"
            log.warning("webhook_server: %s", error_msg)
            _record_result("errored", error_msg)
            self._send_json(400, {"error": "invalid JSON"})
            return

        if not isinstance(payload, dict):
            error_msg = "Payload must be a JSON object"
            _record_result("errored", error_msg)
            self._send_json(400, {"error": error_msg})
            return

        # --- validate required fields ------------------------------------
        filename = payload.get("filename")
        content = payload.get("content")

        if not filename or not isinstance(filename, str):
            error_msg = "Missing or invalid 'filename' field"
            _record_result("errored", error_msg)
            self._send_json(400, {"error": error_msg})
            return

        if not content or not isinstance(content, str):
            error_msg = "Missing or invalid 'content' field"
            _record_result("errored", error_msg)
            self._send_json(400, {"error": error_msg})
            return

        # --- optional fields with defaults -------------------------------
        category = payload.get("category", "chat_import")
        tags = payload.get("tags", [])
        extract_mode = payload.get("extract_mode", "assistant_messages")
        max_length = payload.get("max_length", 10000)
        kind = payload.get("kind", "auto")

        if not isinstance(tags, list):
            error_msg = "'tags' must be a list"
            _record_result("errored", error_msg)
            self._send_json(400, {"error": error_msg})
            return

        if not isinstance(max_length, int) or not (
            _MAX_LENGTH_RANGE[0] <= max_length <= _MAX_LENGTH_RANGE[1]
        ):
            error_msg = (
                f"'max_length' must be an integer between "
                f"{_MAX_LENGTH_RANGE[0]} and {_MAX_LENGTH_RANGE[1]}"
            )
            _record_result("errored", error_msg)
            self._send_json(400, {"error": error_msg})
            return

        if kind not in IMPORT_KINDS:
            error_msg = f"'kind' must be one of {sorted(IMPORT_KINDS)}"
            _record_result("errored", error_msg)
            self._send_json(400, {"error": error_msg})
            return

        # --- ingest with full exception guard ----------------------------
        try:
            with maybe_span("webhook_ingest"):
                result = import_content(
                    filename=filename,
                    content=content,
                    category=category,
                    tags=tags,
                    extract_mode=extract_mode,
                    max_length=max_length,
                    kind=kind,
                )
        except Exception as exc:  # noqa: BLE001
            error_msg = f"Ingest failed for '{filename}': {type(exc).__name__}: {exc}"
            log.exception("webhook_server: unhandled exception during ingest")
            _record_result("errored", error_msg)
            self._send_json(500, {"error": "internal server error during ingest"})
            return

        _record_result("succeeded")