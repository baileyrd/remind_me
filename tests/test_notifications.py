"""
Tests for remind_me_mcp.notifications — outbound notification channels (issue #180).

WebhookNotifier is tested with an injectable httpx.Client over
httpx.MockTransport (mirroring test_sync.py's MockTransport style, adapted to
a sync client since notifications.notify() is called synchronously from both
the reminder scheduler thread and the sync thread). EmailNotifier is tested
with a fake smtplib.SMTP/SMTP_SSL class — no real network or SMTP connection
is ever made.

Also covers the two wiring points: the reminder scheduler's default delivery
hook (notify() called in addition to the existing log line) and sync.py's
fault-verdict notification (throttled so a persisting fault doesn't re-alert
on every remind_me_sync_reconcile call).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

import remind_me_mcp.notifications as notif
from remind_me_mcp.notifications import EmailNotifier, WebhookNotifier

FUTURE = lambda days=1: (datetime.now(UTC) + timedelta(days=days)).isoformat()  # noqa: E731
PAST = lambda days=1: (datetime.now(UTC) - timedelta(days=days)).isoformat()  # noqa: E731


# ---------------------------------------------------------------------------
# WebhookNotifier
# ---------------------------------------------------------------------------


def test_webhook_notifier_posts_expected_payload_to_the_configured_url():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        seen["url"] = str(request.url)
        seen["body"] = _json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    notifier = WebhookNotifier(url="http://example.test/hook", client=client)

    ok = notifier.send("Reminder due", "call mom back")

    assert ok is True
    assert seen["url"] == "http://example.test/hook"
    assert seen["body"] == {
        "subject": "Reminder due",
        "body": "call mom back",
        "source": "remind-me",
    }


def test_webhook_notifier_returns_false_and_logs_on_connect_error(
    caplog: pytest.LogCaptureFixture,
):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    notifier = WebhookNotifier(url="http://example.test/hook", client=client)

    with caplog.at_level("WARNING", logger="remind_me_mcp.notifications"):
        ok = notifier.send("subject", "body")

    assert ok is False
    assert any("Webhook notification" in r.message for r in caplog.records)


def test_webhook_notifier_returns_false_on_non_2xx_response(
    caplog: pytest.LogCaptureFixture,
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    notifier = WebhookNotifier(url="http://example.test/hook", client=client)

    with caplog.at_level("WARNING", logger="remind_me_mcp.notifications"):
        ok = notifier.send("subject", "body")

    assert ok is False
    assert any("Webhook notification" in r.message for r in caplog.records)


def test_webhook_notifier_defaults_to_module_config(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(notif, "NOTIFY_WEBHOOK_URL", "http://from-config.test/hook")
    monkeypatch.setattr(notif, "NOTIFY_WEBHOOK_TIMEOUT", 3)

    notifier = WebhookNotifier()

    assert notifier.url == "http://from-config.test/hook"
    assert notifier.timeout == 3


# ---------------------------------------------------------------------------
# EmailNotifier
# ---------------------------------------------------------------------------


class FakeSMTP:
    """Stand-in for smtplib.SMTP/SMTP_SSL — records what would have been sent."""

    instances: list[FakeSMTP] = []
    raise_on_init: Exception | None = None

    def __init__(self, host, port, timeout=None):
        if FakeSMTP.raise_on_init is not None:
            raise FakeSMTP.raise_on_init
        self.host = host
        self.port = port
        self.starttls_called = False
        self.login_args: tuple | None = None
        self.sent = None
        FakeSMTP.instances.append(self)

    def starttls(self) -> None:
        self.starttls_called = True

    def login(self, user, password) -> None:
        self.login_args = (user, password)

    def send_message(self, msg) -> None:
        self.sent = msg

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


@pytest.fixture(autouse=True)
def _reset_fake_smtp():
    FakeSMTP.instances.clear()
    FakeSMTP.raise_on_init = None
    yield
    FakeSMTP.instances.clear()
    FakeSMTP.raise_on_init = None


def test_email_notifier_builds_and_sends_expected_message(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(notif.smtplib, "SMTP", FakeSMTP)

    notifier = EmailNotifier(
        host="smtp.example.test",
        port=587,
        user="me@example.test",
        password="hunter2",
        from_addr="me@example.test",
        to_addrs="you@example.test, them@example.test",
        use_tls=True,
    )

    ok = notifier.send("Reminder due", "pay rent")

    assert ok is True
    assert len(FakeSMTP.instances) == 1
    smtp = FakeSMTP.instances[0]
    assert smtp.host == "smtp.example.test"
    assert smtp.port == 587
    assert smtp.starttls_called is True
    assert smtp.login_args == ("me@example.test", "hunter2")
    assert smtp.sent["Subject"] == "Reminder due"
    assert smtp.sent["From"] == "me@example.test"
    assert smtp.sent["To"] == "you@example.test, them@example.test"
    assert smtp.sent.get_content().strip() == "pay rent"


def test_email_notifier_uses_smtp_ssl_on_port_465(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(notif.smtplib, "SMTP_SSL", FakeSMTP)

    notifier = EmailNotifier(
        host="smtp.example.test", port=465, to_addrs="you@example.test",
    )

    ok = notifier.send("subject", "body")

    assert ok is True
    assert len(FakeSMTP.instances) == 1
    # Implicit TLS on 465 — starttls() must not be called.
    assert FakeSMTP.instances[0].starttls_called is False


def test_email_notifier_skips_auth_when_no_user_configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(notif.smtplib, "SMTP", FakeSMTP)

    notifier = EmailNotifier(host="smtp.example.test", port=587, user="", to_addrs="you@example.test")
    notifier.send("subject", "body")

    assert FakeSMTP.instances[0].login_args is None


def test_email_notifier_returns_false_with_no_recipients(caplog: pytest.LogCaptureFixture):
    notifier = EmailNotifier(host="smtp.example.test", port=587, to_addrs="")

    with caplog.at_level("WARNING", logger="remind_me_mcp.notifications"):
        ok = notifier.send("subject", "body")

    assert ok is False
    assert any("no recipients" in r.message for r in caplog.records)


def test_email_notifier_returns_false_and_logs_on_smtp_exception(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    monkeypatch.setattr(notif.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.raise_on_init = OSError("connection refused")

    notifier = EmailNotifier(host="smtp.example.test", port=587, to_addrs="you@example.test")

    with caplog.at_level("WARNING", logger="remind_me_mcp.notifications"):
        ok = notifier.send("subject", "body")

    assert ok is False
    assert any("Email notification" in r.message for r in caplog.records)


def test_email_notifier_from_falls_back_to_user(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(notif.smtplib, "SMTP", FakeSMTP)

    notifier = EmailNotifier(
        host="smtp.example.test", port=587, user="me@example.test",
        from_addr=None, to_addrs="you@example.test",
    )
    notifier.send("subject", "body")

    assert FakeSMTP.instances[0].sent["From"] == "me@example.test"


# ---------------------------------------------------------------------------
# get_notifiers()
# ---------------------------------------------------------------------------


def _clear_notify_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notif, "NOTIFY_WEBHOOK_URL", "")
    monkeypatch.setattr(notif, "NOTIFY_SMTP_HOST", "")
    monkeypatch.setattr(notif, "NOTIFY_SMTP_TO", "")


def test_get_notifiers_returns_empty_when_unconfigured(monkeypatch: pytest.MonkeyPatch):
    _clear_notify_config(monkeypatch)
    assert notif.get_notifiers() == []


def test_get_notifiers_returns_only_webhook_when_only_it_is_configured(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_notify_config(monkeypatch)
    monkeypatch.setattr(notif, "NOTIFY_WEBHOOK_URL", "http://example.test/hook")

    notifiers = notif.get_notifiers()

    assert len(notifiers) == 1
    assert isinstance(notifiers[0], WebhookNotifier)


def test_get_notifiers_returns_only_email_when_only_it_is_configured(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_notify_config(monkeypatch)
    monkeypatch.setattr(notif, "NOTIFY_SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(notif, "NOTIFY_SMTP_TO", "you@example.test")

    notifiers = notif.get_notifiers()

    assert len(notifiers) == 1
    assert isinstance(notifiers[0], EmailNotifier)


def test_get_notifiers_returns_both_when_both_are_configured(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(notif, "NOTIFY_WEBHOOK_URL", "http://example.test/hook")
    monkeypatch.setattr(notif, "NOTIFY_SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(notif, "NOTIFY_SMTP_TO", "you@example.test")

    notifiers = notif.get_notifiers()

    kinds = {type(n) for n in notifiers}
    assert kinds == {WebhookNotifier, EmailNotifier}


def test_get_notifiers_email_requires_both_host_and_recipients(
    monkeypatch: pytest.MonkeyPatch,
):
    """SMTP host alone (no recipients) must not count as configured."""
    _clear_notify_config(monkeypatch)
    monkeypatch.setattr(notif, "NOTIFY_SMTP_HOST", "smtp.example.test")

    assert notif.get_notifiers() == []


# ---------------------------------------------------------------------------
# notify()
# ---------------------------------------------------------------------------


def test_notify_fans_out_to_all_configured_notifiers(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    class Ok:
        def send(self, subject, body):
            calls.append("ok")
            return True

    class AlsoOk:
        def send(self, subject, body):
            calls.append("also_ok")
            return True

    monkeypatch.setattr(notif, "get_notifiers", lambda: [Ok(), AlsoOk()])

    notif.notify("subject", "body")

    assert calls == ["ok", "also_ok"]


def test_notify_never_raises_even_if_a_notifier_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    calls: list[str] = []

    class Broken:
        def send(self, subject, body):
            raise RuntimeError("boom")

    class Fine:
        def send(self, subject, body):
            calls.append("fine")
            return True

    # Order matters: the broken one runs first to prove it doesn't block Fine.
    monkeypatch.setattr(notif, "get_notifiers", lambda: [Broken(), Fine()])

    with caplog.at_level("WARNING", logger="remind_me_mcp.notifications"):
        notif.notify("subject", "body")  # must not raise

    assert calls == ["fine"]
    assert any("raised unexpectedly" in r.message for r in caplog.records)


def test_notify_is_a_noop_when_nothing_configured(monkeypatch: pytest.MonkeyPatch):
    _clear_notify_config(monkeypatch)
    # Must not raise even though there is nothing to fan out to.
    notif.notify("subject", "body")


# ---------------------------------------------------------------------------
# Scheduler wiring — a fired reminder triggers notify()
# ---------------------------------------------------------------------------


def test_scheduler_default_delivery_hook_calls_notify_and_still_logs(
    db_conn, memory_factory, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    import remind_me_mcp.scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "_get_db", lambda: db_conn)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        scheduler_mod.notifications, "notify", lambda subject, body: calls.append((subject, body))
    )

    mem = memory_factory(content="pay rent", remind_at=PAST(days=0.001))

    with caplog.at_level("INFO", logger="remind_me_mcp.scheduler"):
        count = scheduler_mod.poll_once()

    assert count == 1
    # Existing log-only behavior is preserved...
    assert any("Reminder due" in r.message for r in caplog.records)
    # ...and notify() is called in addition, with the memory's content as the body.
    assert len(calls) == 1
    subject, body = calls[0]
    assert mem["id"] in subject
    assert body == "pay rent"


def test_scheduler_default_delivery_hook_with_real_notify_never_blocks_delivery(
    db_conn, memory_factory, monkeypatch: pytest.MonkeyPatch
):
    """With no notification channel configured (the test default), the real
    (unmocked) notify() must still be a safe no-op — the reminder is recorded
    as delivered regardless."""
    import remind_me_mcp.scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "_get_db", lambda: db_conn)
    _clear_notify_config(monkeypatch)

    mem = memory_factory(content="renew passport", remind_at=PAST(days=0.001))
    count = scheduler_mod.poll_once()

    assert count == 1
    row = db_conn.execute(
        "SELECT * FROM reminder_deliveries WHERE memory_id = ?", (mem["id"],)
    ).fetchone()
    assert row is not None


# ---------------------------------------------------------------------------
# Sync fault wiring — throttled notification on a `fault` verdict
# ---------------------------------------------------------------------------


@pytest.fixture()
def sync_fault_db(monkeypatch: pytest.MonkeyPatch):
    """Minimal sync_db-equivalent: an in-memory DB wired into sync.py, with
    sync presented as configured (mirrors test_sync.py's sync_db + status_enabled)."""
    import sqlite3

    import remind_me_mcp.db as _db_mod
    import remind_me_mcp.sync as sync_mod
    from remind_me_mcp.db import _ensure_schema

    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    _ensure_schema(db)
    db.execute("INSERT OR REPLACE INTO sync_flags (key, value) VALUES ('sync_enabled', '1')")
    db.commit()

    monkeypatch.setattr(_db_mod, "_get_db", lambda: db)
    monkeypatch.setattr(sync_mod, "_get_db", lambda: db)
    monkeypatch.setattr(sync_mod, "SYNC_ENABLED", True)
    monkeypatch.setattr(sync_mod, "NODE_ID", "test-node")
    monkeypatch.setattr(sync_mod, "HUB_URL", "http://hub.example")
    monkeypatch.setattr(sync_mod, "SYNC_SECRET", "s3cret")
    monkeypatch.setattr(sync_mod, "SYNC_INTERVAL", 60)
    sync_mod._last_errors.clear()

    yield db

    sync_mod._last_errors.clear()
    db.close()


def _hub_stats(by_category: dict[str, int]) -> dict:
    from remind_me_mcp.db import _now_iso

    return {
        "role": "hub",
        "memories": {
            "total": sum(by_category.values()),
            "tombstones": 0,
            "oldest_updated_at": None,
            "newest_updated_at": None,
            "by_origin_node": {},
            "by_category": by_category,
        },
        "entities": 0,
        "memory_entities": 0,
        "entity_relations": 0,
        "time": _now_iso(),
    }


async def test_sync_fault_verdict_notifies_once_then_throttles(
    sync_fault_db, monkeypatch: pytest.MonkeyPatch
):
    """Two fault verdicts inside the throttle window produce one
    notification, not two — the alert-fatigue-avoiding behavior BACKLOG
    Wave 4 requires."""
    import remind_me_mcp.sync as sync_mod

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        sync_mod.notifications, "notify", lambda subject, body: calls.append((subject, body))
    )
    monkeypatch.setattr(sync_mod.config, "NOTIFY_SYNC_FAULT_INTERVAL", 3600)

    # No memory locally, hub has one -> hub ahead. No pull ever recorded ->
    # "never pulled" fault (see sync._verdict).
    sync_fault_db.execute(
        "INSERT OR REPLACE INTO sync_log (remote_id, last_attempt_at, last_pull_at) "
        "VALUES ('hub', ?, ?)",
        (sync_mod._now_iso(), sync_mod._EPOCH),
    )
    sync_fault_db.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/stats":
            return httpx.Response(200, json=_hub_stats({"general": 3}))
        return httpx.Response(404, json={"error": "not found"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = await sync_mod.reconcile_with_hub(client)
        second = await sync_mod.reconcile_with_hub(client)

    assert first["verdict"] == "fault"
    assert second["verdict"] == "fault"
    assert len(calls) == 1
    subject, body = calls[0]
    assert "fault" in subject.lower()
    assert "http://hub.example" in body


async def test_sync_pull_lag_verdict_does_not_notify(
    sync_fault_db, monkeypatch: pytest.MonkeyPatch
):
    """The benign pull-lag verdict must never trigger a notification."""
    import remind_me_mcp.sync as sync_mod

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        sync_mod.notifications, "notify", lambda subject, body: calls.append((subject, body))
    )

    recent = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    sync_fault_db.execute(
        "INSERT OR REPLACE INTO sync_log (remote_id, last_attempt_at, last_pull_at) "
        "VALUES ('hub', ?, ?)",
        (sync_mod._now_iso(), recent),
    )
    sync_fault_db.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/stats":
            return httpx.Response(200, json=_hub_stats({"general": 3}))
        return httpx.Response(404, json={"error": "not found"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await sync_mod.reconcile_with_hub(client)

    assert result["verdict"] == "pull-lag"
    assert calls == []
