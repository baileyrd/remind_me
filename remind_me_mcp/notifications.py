"""
remind_me_mcp.notifications — Optional outbound notification channels (issue #180).

Two channels, each independently gated on its own env vars being non-empty --
mirrors reranker.py/embeddings.py's "available if configured" gating rather
than a single feature-flag switch, so a deployment can run either, both, or
neither without an extra enable flag to keep in sync with the config that
actually matters.

Both channels degrade gracefully: a failed or unconfigured notifier must
never raise out of a calling path (the reminder scheduler's poll loop, sync
fault detection) -- see reranker.py's module docstring for the discipline
this follows. ``Notifier.send`` returns whether delivery succeeded instead of
raising; :func:`notify` additionally isolates each notifier from the others,
so one broken channel can't block another or the caller.

``WebhookNotifier`` POSTs a single generic JSON payload
(``{"subject": ..., "body": ..., "source": "remind-me"}``) to
``REMIND_ME_NOTIFY_WEBHOOK_URL``. One config covers ntfy/Slack/Discord/
Mattermost/Pushover-via-webhook uniformly -- no per-service payload
formatting (Slack "blocks", ntfy priority headers, Discord embeds, ...) is
built here. Users of those services who want native formatting need a small
relay/transform in front of this webhook; that's a documented limitation,
not something this module solves.

``EmailNotifier`` sends via the stdlib ``smtplib``/``email.message.EmailMessage``
-- no new dependency.

:func:`get_notifiers` builds whichever of the two are configured (both, one,
or neither). :func:`notify` fans out to every configured notifier.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Protocol

import httpx

from remind_me_mcp.config import (
    NOTIFY_SMTP_FROM,
    NOTIFY_SMTP_HOST,
    NOTIFY_SMTP_PASSWORD,
    NOTIFY_SMTP_PORT,
    NOTIFY_SMTP_TO,
    NOTIFY_SMTP_USE_TLS,
    NOTIFY_SMTP_USER,
    NOTIFY_WEBHOOK_TIMEOUT,
    NOTIFY_WEBHOOK_URL,
)

log = logging.getLogger("remind_me_mcp.notifications")


class Notifier(Protocol):
    """A configured outbound notification channel."""

    def send(self, subject: str, body: str) -> bool:
        """Attempt delivery. Returns whether it succeeded; never raises."""
        ...


# ---------------------------------------------------------------------------
# Webhook notifier
# ---------------------------------------------------------------------------


class WebhookNotifier:
    """POSTs a generic JSON payload to a configured webhook URL.

    One config (``REMIND_ME_NOTIFY_WEBHOOK_URL``) covers ntfy/Slack/Discord/
    Mattermost/Pushover-via-webhook uniformly: the payload is always
    ``{"subject": ..., "body": ..., "source": "remind-me"}``, with no
    per-service formatting applied. A user who wants native formatting on one
    of those services (Slack blocks, ntfy priority headers, ...) needs a
    small relay/transform in front of this webhook -- documented as a known
    limitation rather than solved here, since building and maintaining N
    service-specific formatters is out of scope for a single generic sink.
    """

    def __init__(
        self,
        url: str | None = None,
        timeout: float | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        """Configure the notifier.

        Args:
            url: Webhook URL. Defaults to module-level NOTIFY_WEBHOOK_URL.
            timeout: Seconds before giving up. Defaults to NOTIFY_WEBHOOK_TIMEOUT.
            client: Optional httpx.Client to POST through, for tests (mirrors
                ``sync.reconcile_with_hub``'s injectable client). A fresh
                one-shot request via ``httpx.post`` is used when omitted.
        """
        self.url = url if url is not None else NOTIFY_WEBHOOK_URL
        self.timeout = timeout if timeout is not None else NOTIFY_WEBHOOK_TIMEOUT
        self._client = client

    def send(self, subject: str, body: str) -> bool:
        """POST the notification; return False (and log) on any failure.

        A short timeout bounds how long a hung endpoint can block the caller
        -- the reminder scheduler's poll loop and the sync thread both call
        this synchronously and must not stall indefinitely on a dead webhook.
        """
        payload = {"subject": subject, "body": body, "source": "remind-me"}
        try:
            if self._client is not None:
                resp = self._client.post(self.url, json=payload, timeout=self.timeout)
            else:
                resp = httpx.post(self.url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            return True
        except Exception as e:  # noqa: BLE001 — a notifier must never raise
            log.warning("Webhook notification to %s failed: %s", self.url, e)
            return False


# ---------------------------------------------------------------------------
# Email notifier
# ---------------------------------------------------------------------------


class EmailNotifier:
    """Sends a notification via SMTP using stdlib smtplib/EmailMessage.

    No new dependency: Python's standard library already covers this.
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        from_addr: str | None = None,
        to_addrs: str | None = None,
        use_tls: bool | None = None,
    ) -> None:
        """Configure the notifier from explicit args or module-level config.

        Args:
            host: SMTP host. Defaults to NOTIFY_SMTP_HOST.
            port: SMTP port. Defaults to NOTIFY_SMTP_PORT (465 = implicit
                TLS via SMTP_SSL regardless of use_tls; anything else = plain
                SMTP with STARTTLS applied when use_tls is true).
            user: SMTP AUTH username. Defaults to NOTIFY_SMTP_USER; empty
                skips SMTP AUTH.
            password: SMTP AUTH password. Defaults to NOTIFY_SMTP_PASSWORD.
            from_addr: From header. Defaults to NOTIFY_SMTP_FROM, falling
                back to the resolved user when both are unset.
            to_addrs: Comma-separated recipients. Defaults to NOTIFY_SMTP_TO.
            use_tls: Whether to STARTTLS a plaintext connection. Defaults to
                NOTIFY_SMTP_USE_TLS. Ignored on port 465 (always implicit TLS).
        """
        self.host = host if host is not None else NOTIFY_SMTP_HOST
        self.port = port if port is not None else NOTIFY_SMTP_PORT
        self.user = user if user is not None else NOTIFY_SMTP_USER
        self.password = password if password is not None else NOTIFY_SMTP_PASSWORD
        resolved_from = from_addr if from_addr is not None else NOTIFY_SMTP_FROM
        self.from_addr = resolved_from or self.user
        self.to_addrs = to_addrs if to_addrs is not None else NOTIFY_SMTP_TO
        self.use_tls = use_tls if use_tls is not None else NOTIFY_SMTP_USE_TLS

    def send(self, subject: str, body: str) -> bool:
        """Send the message; return False (and log) on any SMTP failure."""
        recipients = [a.strip() for a in self.to_addrs.split(",") if a.strip()]
        if not recipients:
            log.warning("Email notification skipped: no recipients configured")
            return False

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.from_addr or "remind-me@localhost"
        msg["To"] = ", ".join(recipients)
        msg.set_content(body)

        try:
            smtp_cls = smtplib.SMTP_SSL if self.port == 465 else smtplib.SMTP
            with smtp_cls(self.host, self.port, timeout=10) as smtp:
                if self.port != 465 and self.use_tls:
                    smtp.starttls()
                if self.user:
                    smtp.login(self.user, self.password)
                smtp.send_message(msg)
            return True
        except Exception as e:  # noqa: BLE001 — a notifier must never raise
            log.warning("Email notification via %s:%s failed: %s", self.host, self.port, e)
            return False


# ---------------------------------------------------------------------------
# Factory + fan-out
# ---------------------------------------------------------------------------


def get_notifiers() -> list[Notifier]:
    """Build whichever notifiers are configured -- both, one, or neither.

    Availability is gated on env-var presence rather than a separate on/off
    switch, mirroring how reranker.py/embeddings.py decide availability from
    configuration alone: a webhook URL or SMTP host+recipient being set *is*
    the opt-in.

    Returns:
        A list of ready-to-use Notifier instances (possibly empty).
    """
    notifiers: list[Notifier] = []
    if NOTIFY_WEBHOOK_URL:
        notifiers.append(WebhookNotifier())
    if NOTIFY_SMTP_HOST and NOTIFY_SMTP_TO:
        notifiers.append(EmailNotifier())
    return notifiers


def notify(subject: str, body: str) -> None:
    """Fan out a notification to every configured channel. Never raises.

    A no-op when nothing is configured, so callers (the reminder scheduler's
    default delivery hook, sync fault detection) can call this
    unconditionally without checking availability themselves first -- the
    same discipline as ``reranker.maybe_rerank``. Each notifier is isolated:
    one raising or returning False never stops the others from being tried.

    Args:
        subject: Short notification subject/title.
        body: Notification body text.
    """
    for notifier in get_notifiers():
        try:
            notifier.send(subject, body)
        except Exception as e:  # noqa: BLE001 — one broken channel must not block another
            log.warning("Notifier %s raised unexpectedly: %s", type(notifier).__name__, e)


__all__ = [
    "Notifier",
    "WebhookNotifier",
    "EmailNotifier",
    "get_notifiers",
    "notify",
]
