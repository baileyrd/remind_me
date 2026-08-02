"""
Tests for remind_me_mcp.events — the raw automation event stream (issue #198).

Covers:

- ``emit_event``'s payload shape, disabled-by-default no-op, and graceful
  failure handling, using an injectable ``httpx.Client`` over
  ``httpx.MockTransport`` (mirroring test_notifications.py's WebhookNotifier
  style).
- The fire-and-forget task's PF-04 reference-holding, mirroring
  ``test_spawn_task_holds_strong_reference_until_done`` in test_tools.py.
- Integration: remind_me_add/update/delete (tools/crud.py) and their REST
  equivalents (api.py) each fire the right event with the right
  memory_id/category, and never leak memory content into the payload.
"""

from __future__ import annotations

import asyncio
import gc
import json
import re
import threading
from typing import TYPE_CHECKING

import httpx
import pytest
from starlette.testclient import TestClient

import remind_me_mcp.events as events
import remind_me_mcp.tools.crud as crud_module
from remind_me_mcp.api import _build_api_app
from remind_me_mcp.events import emit_event
from remind_me_mcp.models import (
    MemoryAddInput,
    MemoryDeleteInput,
    MemoryUpdateInput,
    SetReminderInput,
)
from remind_me_mcp.tools.crud import memory_add, memory_delete, memory_update
from remind_me_mcp.tools.reminders import remind_me_set_reminder

if TYPE_CHECKING:
    import sqlite3

# ---------------------------------------------------------------------------
# emit_event: payload shape, disabled no-op, graceful failure
# ---------------------------------------------------------------------------


async def test_emit_event_posts_expected_payload_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured event webhook receives the metadata-only event payload."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(events, "EVENT_WEBHOOK_URL", "http://example.test/events")
    monkeypatch.setattr(events, "EVENT_WEBHOOK_TIMEOUT", 5)

    task = emit_event("created", "mem-123", "notes", client=client)
    assert task is not None
    await task

    assert seen["url"] == "http://example.test/events"
    assert seen["body"]["event"] == "created"
    assert seen["body"]["memory_id"] == "mem-123"
    assert seen["body"]["category"] == "notes"
    assert "timestamp" in seen["body"]
    # Metadata only — exactly these four keys, nothing content-shaped.
    assert set(seen["body"].keys()) == {"event", "memory_id", "category", "timestamp"}


def test_emit_event_is_a_true_noop_when_url_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """No URL configured -> no task is created at all, and nothing raises."""
    monkeypatch.setattr(events, "EVENT_WEBHOOK_URL", "")

    task = emit_event("created", "mem-1", "notes")

    assert task is None
    assert len(events._background_tasks) == 0


def _connect_error_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused")


def _server_error_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, json={"error": "boom"})


@pytest.mark.parametrize(
    "handler",
    [
        pytest.param(_connect_error_handler, id="connect-error"),
        pytest.param(_server_error_handler, id="non-2xx"),
    ],
)
async def test_emit_event_swallows_failure_and_logs(
    handler, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A connection error or non-2xx response is caught and logged, never raised."""
    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(events, "EVENT_WEBHOOK_URL", "http://example.test/events")
    monkeypatch.setattr(events, "EVENT_WEBHOOK_TIMEOUT", 5)

    with caplog.at_level("WARNING", logger="remind_me_mcp.events"):
        task = emit_event("deleted", "mem-1", "notes", client=client)
        assert task is not None
        await task  # must not raise

    assert any("Event webhook POST" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# PF-04: the fire-and-forget task keeps a strong reference until done
# ---------------------------------------------------------------------------


async def test_spawn_task_holds_strong_reference_until_done() -> None:
    """events._spawn_task registers the task in the module-level set (so the
    event loop's own weak reference is not the only one) and discards it on
    completion -- mirrors test_tools.py's identical test for the tools
    package's copy of this pattern."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def waiter() -> str:
        started.set()
        await release.wait()
        return "done"

    task = events._spawn_task(waiter())
    await started.wait()
    assert task in events._background_tasks  # strong reference held while in flight

    release.set()
    assert await task == "done"
    await asyncio.sleep(0)
    assert task not in events._background_tasks  # no leak after completion


async def test_emit_event_task_survives_gc_while_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """The task returned by emit_event is reference-held even across an
    explicit gc.collect() pass while the POST is still in flight -- the
    concrete failure mode PF-04 fixes (a fire-and-forget asyncio.create_task
    result with no other reference can be garbage-collected mid-flight)."""
    proceed = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        assert proceed.wait(timeout=5), "test handler was never released"
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(events, "EVENT_WEBHOOK_URL", "http://example.test/events")
    monkeypatch.setattr(events, "EVENT_WEBHOOK_TIMEOUT", 5)

    task = emit_event("created", "mem-1", "notes", client=client)
    assert task is not None
    assert task in events._background_tasks

    gc.collect()  # would collect a bare create_task() result with no other ref
    assert task in events._background_tasks
    assert not task.done()

    proceed.set()
    await task
    await asyncio.sleep(0)
    assert task not in events._background_tasks


# ---------------------------------------------------------------------------
# Integration: remind_me_add / remind_me_update / remind_me_delete (crud.py)
# ---------------------------------------------------------------------------


async def test_memory_add_fires_created_event(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(
        crud_module, "emit_event", lambda event, memory_id, category: calls.append((event, memory_id, category))
    )

    result = await memory_add(MemoryAddInput(content="Event stream test content", category="notes"))
    m = re.search(r"`([a-f0-9]+)`", result)
    assert m is not None
    mem_id = m.group(1)

    assert calls == [("created", mem_id, "notes")]


async def test_memory_update_fires_updated_event(
    db_conn: sqlite3.Connection, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    mem = memory_factory(content="Original content", category="notes")
    calls: list[tuple] = []
    monkeypatch.setattr(
        crud_module, "emit_event", lambda event, memory_id, category: calls.append((event, memory_id, category))
    )

    await memory_update(MemoryUpdateInput(memory_id=mem["id"], content="Updated content"))

    assert calls == [("updated", mem["id"], "notes")]


async def test_memory_update_reports_new_category_when_changed(
    db_conn: sqlite3.Connection, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    mem = memory_factory(content="Recategorize me", category="old_cat")
    calls: list[tuple] = []
    monkeypatch.setattr(
        crud_module, "emit_event", lambda event, memory_id, category: calls.append((event, memory_id, category))
    )

    await memory_update(MemoryUpdateInput(memory_id=mem["id"], category="new_cat"))

    assert calls == [("updated", mem["id"], "new_cat")]


async def test_memory_delete_fires_deleted_event(
    db_conn: sqlite3.Connection, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    mem = memory_factory(content="Memory to delete", category="notes")
    calls: list[tuple] = []
    monkeypatch.setattr(
        crud_module, "emit_event", lambda event, memory_id, category: calls.append((event, memory_id, category))
    )

    await memory_delete(MemoryDeleteInput(memory_id=mem["id"]))

    assert calls == [("deleted", mem["id"], "notes")]


async def test_memory_add_not_found_paths_do_not_fire_events(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Update/delete against a nonexistent id must not fire an event."""
    calls: list[tuple] = []
    monkeypatch.setattr(crud_module, "emit_event", lambda *a, **k: calls.append((a, k)))

    update_result = await memory_update(MemoryUpdateInput(memory_id="doesnotexist99", content="x"))
    assert "not found" in update_result.lower()
    delete_result = await memory_delete(MemoryDeleteInput(memory_id="doesnotexist99"))
    assert "not found" in delete_result.lower()

    assert calls == []


async def test_set_reminder_does_not_fire_an_updated_event(
    db_conn: sqlite3.Connection, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """remind_me_set_reminder funnels through the same shared
    _apply_memory_field_update choke point remind_me_update uses, but a
    reminder set/clear is deliberately NOT treated as a "memory update"
    event (see crud.py's memory_update docstring comment) -- it touches no
    content/category/tags/metadata field."""
    mem = memory_factory(content="Reminder-only edit test")
    calls: list[tuple] = []
    monkeypatch.setattr(crud_module, "emit_event", lambda *a, **k: calls.append((a, k)))

    from datetime import UTC, datetime, timedelta

    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    result = await remind_me_set_reminder(SetReminderInput(memory_id=mem["id"], remind_at=future))
    assert "Reminder set" in result

    assert calls == []


async def test_memory_add_event_payload_excludes_content(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end through the real emit_event: even large, sensitive-looking
    content never appears in the event payload posted over the wire — only
    the metadata fields do."""
    real_emit_event = events.emit_event
    monkeypatch.setattr(events, "EVENT_WEBHOOK_URL", "http://example.test/events")
    monkeypatch.setattr(events, "EVENT_WEBHOOK_TIMEOUT", 5)

    captured: dict = {}

    def fake_post_json(url, payload, timeout, client=None):  # noqa: ANN001
        captured["payload"] = payload
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(events, "_post_json", fake_post_json)

    holder: dict = {}

    def spy_emit_event(event: str, memory_id: str, category: str):
        task = real_emit_event(event, memory_id, category)
        holder["task"] = task
        return task

    monkeypatch.setattr(crud_module, "emit_event", spy_emit_event)

    sensitive_content = "password: hunter2, SSN: 123-45-6789 " * 200
    result = await memory_add(MemoryAddInput(content=sensitive_content, category="secrets"))
    assert "Memory stored" in result

    assert holder.get("task") is not None
    await holder["task"]

    payload = captured["payload"]
    assert payload["event"] == "created"
    assert payload["category"] == "secrets"
    assert set(payload.keys()) == {"event", "memory_id", "category", "timestamp"}
    # No content-shaped field, and the actual sensitive text never appears
    # anywhere in the serialized payload.
    assert "content" not in payload
    assert sensitive_content not in json.dumps(payload)


# ---------------------------------------------------------------------------
# Integration: REST equivalents (api.py)
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(db_conn, monkeypatch):
    """Minimal auth-disabled TestClient, mirroring test_api.py's own fixture."""
    import remind_me_mcp.config as _cfg
    import remind_me_mcp.importer as _importer_mod

    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db_conn)
    monkeypatch.setattr(_cfg, "API_KEY", "disabled")

    app = _build_api_app()
    return TestClient(app)


def test_api_add_fires_created_event(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import remind_me_mcp.api as api_module

    calls: list[tuple] = []
    monkeypatch.setattr(
        api_module, "emit_event", lambda event, memory_id, category: calls.append((event, memory_id, category))
    )

    response = client.post("/api/memories", json={"content": "REST created event test", "category": "notes"})
    assert response.status_code == 201
    mem_id = response.json()["id"]

    assert calls == [("created", mem_id, "notes")]


def test_api_update_fires_updated_event(
    client: TestClient, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import remind_me_mcp.api as api_module

    mem = memory_factory(content="Original", category="notes")
    calls: list[tuple] = []
    monkeypatch.setattr(
        api_module, "emit_event", lambda event, memory_id, category: calls.append((event, memory_id, category))
    )

    response = client.put(f"/api/memories/{mem['id']}", json={"content": "Updated via REST"})
    assert response.status_code == 200

    assert calls == [("updated", mem["id"], "notes")]


def test_api_update_not_found_does_not_fire_event(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import remind_me_mcp.api as api_module

    calls: list[tuple] = []
    monkeypatch.setattr(api_module, "emit_event", lambda *a, **k: calls.append((a, k)))

    response = client.put("/api/memories/nonexistent_xyz", json={"content": "x"})
    assert response.status_code == 404

    assert calls == []


def test_api_delete_fires_deleted_event(
    client: TestClient, memory_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import remind_me_mcp.api as api_module

    mem = memory_factory(content="Memory to delete via REST", category="notes")
    calls: list[tuple] = []
    monkeypatch.setattr(
        api_module, "emit_event", lambda event, memory_id, category: calls.append((event, memory_id, category))
    )

    response = client.delete(f"/api/memories/{mem['id']}")
    assert response.status_code == 200

    assert calls == [("deleted", mem["id"], "notes")]


def test_api_delete_not_found_does_not_fire_event(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import remind_me_mcp.api as api_module

    calls: list[tuple] = []
    monkeypatch.setattr(api_module, "emit_event", lambda *a, **k: calls.append((a, k)))

    response = client.delete("/api/memories/nonexistent_xyz")
    assert response.status_code == 404

    assert calls == []
