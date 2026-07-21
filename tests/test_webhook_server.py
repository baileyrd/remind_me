"""
Tests for remind_me_mcp.webhook_server — the push/webhook ingestion HTTP
endpoint (FT-09, Phase 5a).

A real server is started on an ephemeral 127.0.0.1 port for each test; the
database is a shared in-memory SQLite connection (check_same_thread=False so
the handler thread can use it), mirroring test_peer_server.py's approach.
Embedding is stubbed out — these tests only exercise the HTTP/import wiring.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

import remind_me_mcp.webhook_server as webhook_server
from remind_me_mcp.db import _ensure_schema

SECRET = "test-webhook-secret"
AUTH = {"Authorization": f"Bearer {SECRET}"}


@pytest.fixture(autouse=True)
def _reset_webhook_stats() -> Iterator[None]:
    """The request counters are module-level globals — reset around every test."""
    webhook_server._requests_ingested = 0
    webhook_server._requests_skipped = 0
    webhook_server._requests_errored = 0
    webhook_server._errors.clear()
    yield


@pytest.fixture()
def webhook_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[sqlite3.Connection]:
    """In-memory DB shared with the webhook handler thread."""
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    _ensure_schema(db)

    import remind_me_mcp.db as _db_mod
    import remind_me_mcp.importer as _importer_mod

    monkeypatch.setattr(_db_mod, "_get_db", lambda: db)
    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db)
    monkeypatch.setattr(_importer_mod, "_embed_and_store_rows", lambda rows: 0)

    yield db
    db.close()


@pytest.fixture()
def webhook_url(
    webhook_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> Iterator[str]:
    """A live webhook server bound to an ephemeral localhost port."""
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", SECRET)

    from http.server import HTTPServer
    from threading import Thread

    server = HTTPServer(("127.0.0.1", 0), webhook_server.WebhookHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def chat_payload(**overrides) -> dict:
    base = {
        "filename": "webhook_chat.json",
        "content": json.dumps({
            "chat_messages": [
                {"sender": "assistant", "content": "Pushed via webhook, hello world."}
            ]
        }),
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_ingest_requires_auth(webhook_url: str) -> None:
    resp = httpx.post(f"{webhook_url}/ingest", json=chat_payload())
    assert resp.status_code == 401


def test_ingest_wrong_secret_rejected(webhook_url: str) -> None:
    resp = httpx.post(
        f"{webhook_url}/ingest",
        json=chat_payload(),
        headers={"Authorization": "Bearer wrong"},
    )
    assert resp.status_code == 401


def test_empty_secret_never_authenticates(
    webhook_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    resp = httpx.post(
        f"{webhook_url}/ingest", json=chat_payload(), headers={"Authorization": "Bearer"}
    )
    assert resp.status_code == 401


def test_get_requires_auth_and_is_404(webhook_url: str) -> None:
    assert httpx.get(f"{webhook_url}/anything").status_code == 401
    assert httpx.get(f"{webhook_url}/anything", headers=AUTH).status_code == 404


# ---------------------------------------------------------------------------
# Successful ingestion
# ---------------------------------------------------------------------------


def test_ingest_creates_memories(
    webhook_url: str, webhook_db: sqlite3.Connection
) -> None:
    resp = httpx.post(f"{webhook_url}/ingest", json=chat_payload(), headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["memories_created"] == 1

    row = webhook_db.execute(
        "SELECT content, source FROM memories WHERE source = 'chat_import'"
    ).fetchone()
    assert row["content"] == "Pushed via webhook, hello world."


def test_ingest_wrapped_in_telemetry_span(
    webhook_url: str, webhook_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Webhook ingestion previously had no OTEL span at all, unlike every
    other ingestion path (file import, watcher scan, sync cycle) -- assert
    do_POST wraps import_content() in a 'webhook.ingest' span."""
    spans: list[str] = []
    real_maybe_span = webhook_server.maybe_span

    def spy_maybe_span(name, **attrs):
        spans.append(name)
        return real_maybe_span(name, **attrs)

    monkeypatch.setattr(webhook_server, "maybe_span", spy_maybe_span)

    resp = httpx.post(f"{webhook_url}/ingest", json=chat_payload(), headers=AUTH)
    assert resp.status_code == 200
    assert spans == ["webhook.ingest"]


def test_ingest_document_kind(webhook_url: str, webhook_db: sqlite3.Connection) -> None:
    resp = httpx.post(
        f"{webhook_url}/ingest",
        json=chat_payload(
            filename="notes.md",
            content="# A Section\n\nSome document content pushed via webhook.",
            kind="document",
            category="",
        ),
        headers=AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["kind"] == "document"

    row = webhook_db.execute(
        "SELECT category, source FROM memories WHERE source = 'document_import'"
    ).fetchone()
    assert row["category"] == "document"


def test_ingest_dedup_skips_second_identical_push(webhook_url: str) -> None:
    payload = chat_payload()
    first = httpx.post(f"{webhook_url}/ingest", json=payload, headers=AUTH).json()
    assert first["status"] == "ok"

    second = httpx.post(f"{webhook_url}/ingest", json=payload, headers=AUTH).json()
    assert second["status"] == "skipped"
    assert second["import_id"] == first["import_id"]


def test_ingest_updates_status_counters(webhook_url: str) -> None:
    httpx.post(f"{webhook_url}/ingest", json=chat_payload(), headers=AUTH)
    httpx.post(f"{webhook_url}/ingest", json=chat_payload(), headers=AUTH)  # dedup skip

    status = webhook_server.get_webhook_status()
    assert status["enabled"] is True
    assert status["requests_ingested"] == 1
    assert status["requests_skipped"] == 1
    assert status["requests_errored"] == 0


# ---------------------------------------------------------------------------
# Malformed / invalid payloads
# ---------------------------------------------------------------------------


def test_ingest_missing_filename_400(webhook_url: str) -> None:
    payload = chat_payload()
    del payload["filename"]
    resp = httpx.post(f"{webhook_url}/ingest", json=payload, headers=AUTH)
    assert resp.status_code == 400


def test_ingest_missing_content_400(webhook_url: str) -> None:
    payload = chat_payload()
    del payload["content"]
    resp = httpx.post(f"{webhook_url}/ingest", json=payload, headers=AUTH)
    assert resp.status_code == 400


def test_ingest_non_string_content_400(webhook_url: str) -> None:
    resp = httpx.post(
        f"{webhook_url}/ingest",
        json=chat_payload(content=123),
        headers=AUTH,
    )
    assert resp.status_code == 400


def test_ingest_invalid_kind_400(webhook_url: str) -> None:
    resp = httpx.post(
        f"{webhook_url}/ingest", json=chat_payload(kind="bogus"), headers=AUTH
    )
    assert resp.status_code == 400


def test_ingest_invalid_tags_400(webhook_url: str) -> None:
    resp = httpx.post(
        f"{webhook_url}/ingest", json=chat_payload(tags="not-a-list"), headers=AUTH
    )
    assert resp.status_code == 400

    resp = httpx.post(
        f"{webhook_url}/ingest", json=chat_payload(tags=[1, 2]), headers=AUTH
    )
    assert resp.status_code == 400


def test_ingest_invalid_category_400(webhook_url: str) -> None:
    resp = httpx.post(
        f"{webhook_url}/ingest", json=chat_payload(category=123), headers=AUTH
    )
    assert resp.status_code == 400


def test_ingest_invalid_extract_mode_400(webhook_url: str) -> None:
    resp = httpx.post(
        f"{webhook_url}/ingest", json=chat_payload(extract_mode=123), headers=AUTH
    )
    assert resp.status_code == 400


def test_ingest_invalid_max_length_400(webhook_url: str) -> None:
    resp = httpx.post(
        f"{webhook_url}/ingest", json=chat_payload(max_length=10), headers=AUTH
    )
    assert resp.status_code == 400

    resp = httpx.post(
        f"{webhook_url}/ingest", json=chat_payload(max_length=True), headers=AUTH
    )
    assert resp.status_code == 400


def test_ingest_unsupported_suffix_returns_422(webhook_url: str) -> None:
    """A validation error surfaced from import_content (not the handler's
    own up-front checks) comes back as 422, carrying the importer's reason."""
    resp = httpx.post(
        f"{webhook_url}/ingest",
        json=chat_payload(filename="notes.pdf"),
        headers=AUTH,
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["status"] == "error"
    assert "unsupported format" in body["reason"]


def test_ingest_malformed_json_400(webhook_url: str) -> None:
    resp = httpx.post(
        f"{webhook_url}/ingest",
        content=b"{this is not json",
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_ingest_non_object_payload_400(webhook_url: str) -> None:
    resp = httpx.post(f"{webhook_url}/ingest", json=[1, 2, 3], headers=AUTH)
    assert resp.status_code == 400


def test_ingest_empty_body_400(webhook_url: str) -> None:
    resp = httpx.post(f"{webhook_url}/ingest", headers=AUTH)
    assert resp.status_code == 400


def test_ingest_oversized_body_rejected(
    webhook_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(webhook_server, "MAX_BODY_BYTES", 64)
    resp = httpx.post(
        f"{webhook_url}/ingest",
        json=chat_payload(content="y" * 500),
        headers=AUTH,
    )
    assert resp.status_code == 413


def test_unknown_post_route_404(webhook_url: str) -> None:
    resp = httpx.post(f"{webhook_url}/nope", json={}, headers=AUTH)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


def test_start_webhook_server_requires_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    assert webhook_server.start_webhook_server() is None


def test_start_webhook_server_port_in_use(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket
    from http.server import ThreadingHTTPServer

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]

    def bind_loopback(addr, handler):
        return ThreadingHTTPServer(("127.0.0.1", port), handler)

    monkeypatch.setattr(webhook_server, "WEBHOOK_PORT", port)
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(webhook_server, "ThreadingHTTPServer", bind_loopback)
    try:
        assert webhook_server.start_webhook_server() is None
    finally:
        sock.close()


def test_start_and_stop_webhook_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(webhook_server, "WEBHOOK_PORT", 0)  # ephemeral
    monkeypatch.setattr(webhook_server, "WEBHOOK_BIND", "127.0.0.1")
    try:
        thread = webhook_server.start_webhook_server()
        assert thread is not None
        assert thread.is_alive()

        status = webhook_server.get_webhook_status()
        assert status["enabled"] is True
        assert status["running"] is True

        # Idempotent: a second call while running returns the same thread.
        assert webhook_server.start_webhook_server() is thread
    finally:
        webhook_server.stop_webhook_server()

    assert webhook_server.get_webhook_status()["running"] is False


def test_get_webhook_status_disabled_without_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(webhook_server, "WEBHOOK_SECRET", "")
    status = webhook_server.get_webhook_status()
    assert status == {
        "enabled": False,
        "running": False,
        "hint": "set REMIND_ME_WEBHOOK_SECRET to enable push/webhook ingestion",
    }
