"""
remind_me_mcp.digest — Vault digest synthesis (issue #188).

Assembles a compressed snapshot -- recent additions, vault vitality,
reminders, and sync health -- entirely from data other tools already report
independently:

  - **Recent additions**: a plain query over ``memories`` (created since
    ``since_days`` days ago), rendered with ``formatting._fmt_memories`` --
    the same renderer ``remind_me_list_reminders``/``remind_me_get`` use, so
    memory blocks look identical everywhere. Sensitive memories (issue #195)
    are always excluded here, with no override -- see
    :func:`build_digest_data`.
  - **Vault vitality**: :func:`remind_me_mcp.vitality.build_vitality_report`,
    the exact function ``remind_me_vitality_report`` calls -- this can never
    disagree with that tool's bucket counts.
  - **Reminders**: :func:`remind_me_mcp.reminders.list_reminders`, the exact
    function ``remind_me_list_reminders`` calls (issue #179/#188 factoring).
  - **Sync health**: :func:`remind_me_mcp.sync.get_sync_status`, the exact
    function ``remind_me_sync_status`` calls -- purely local (no network),
    so it's safe to call unconditionally. The deeper hub-reconcile verdict
    (``in-sync``/``pull-lag``/``node-ahead``/``fault``) is a network call
    (:func:`remind_me_mcp.sync.reconcile_with_hub`) and is therefore left to
    the async ``remind_me_digest`` tool wrapper to fetch and pass in via
    *reconcile* -- this module stays synchronous and dependency-light so the
    scheduler's synchronous poll loop (below) can call it directly.

No new database queries are introduced beyond the recent-additions scan --
every other section is delegated to an existing function.

Optional scheduled delivery (``REMIND_ME_DIGEST_INTERVAL``, opt-in, unlike
the reminder scheduler's always-on poll) piggybacks on
:mod:`remind_me_mcp.scheduler`'s existing poll loop rather than spinning up a
second background thread: a digest is daily/weekly at the coarsest, so a
once-a-tick check inside that loop's 60s-default interval is cheap --
:func:`maybe_send_scheduled_digest` returns immediately without touching the
database at all when disabled (the default), and is otherwise gated by
:func:`is_digest_due`. The "already due" watermark is persisted in the
``sync_flags`` key/value table :mod:`remind_me_mcp.sync` already established
for exactly this kind of cross-restart timestamp (its drain-rate baseline,
its ``sync_enabled`` gate) under the key ``digest_last_sent_at``, so a server
restart mid-interval does not immediately re-fire -- the same discipline
``maintenance._due`` uses in-memory, just durable across restarts.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from remind_me_mcp import config, notifications
from remind_me_mcp.db import _get_db, _now_iso, _row_to_dict
from remind_me_mcp.formatting import _fmt_memories
from remind_me_mcp.models import ReminderWindow, ResponseFormat
from remind_me_mcp.reminders import list_reminders
from remind_me_mcp.sync import get_sync_status
from remind_me_mcp.vitality import build_vitality_report

if TYPE_CHECKING:
    import sqlite3

log = logging.getLogger("remind_me_mcp.digest")

DEFAULT_SINCE_DAYS = 7
"""Default lookback window for the 'recent additions' section."""

MAX_RECENT_MEMORIES = 20
"""Cap on how many recent memories are rendered in full -- recent_total
still reports the true count, this only bounds the markdown/JSON body size."""

MAX_DIGEST_REMINDERS = 10
"""Cap on how many upcoming/overdue reminders are listed per section."""

_DIGEST_FLAG_KEY = "digest_last_sent_at"


# ---------------------------------------------------------------------------
# Assembly (pure synthesis -- no new queries beyond recent-additions)
# ---------------------------------------------------------------------------


def build_digest_data(
    db: sqlite3.Connection, since_days: int = DEFAULT_SINCE_DAYS
) -> dict[str, Any]:
    """Assemble the digest's underlying data (JSON-serializable).

    ``recent_memories``/``recent_total`` always exclude memories marked
    sensitive (issue #195) -- unlike remind_me_search/remind_me_list, this
    has no ``include_sensitive`` override; a digest is precisely the
    ambient/passive surface the flag exists to keep sensitive content off
    of by default.

    Args:
        db: An open SQLite connection.
        since_days: How many days back counts as "recent" for the first
            section.

    Returns:
        A dict with ``generated_at``, ``since_days``, ``recent_memories``
        (capped at :data:`MAX_RECENT_MEMORIES`), ``recent_total`` (the true
        count, uncapped), ``vitality`` (from
        :func:`remind_me_mcp.vitality.build_vitality_report`),
        ``reminders_upcoming``/``reminders_overdue`` (from
        :func:`remind_me_mcp.reminders.list_reminders`), and ``sync`` (from
        :func:`remind_me_mcp.sync.get_sync_status`).
    """
    cutoff = (datetime.now(UTC) - timedelta(days=since_days)).isoformat()

    # Issue #195: sensitive memories never appear in the digest, with no
    # opt-in override (unlike remind_me_search/remind_me_list's
    # include_sensitive) -- a digest is exactly the ambient/passive surface
    # this "don't surface by default" convenience feature exists to protect,
    # and it has no per-call caller intent to opt back in against (it's
    # often delivered on a schedule to a notification channel, not read in
    # response to a specific question).
    recent_rows = db.execute(
        """SELECT * FROM memories
            WHERE deleted_at IS NULL AND sensitive = 0 AND created_at >= ?
            ORDER BY created_at DESC
            LIMIT ?""",
        (cutoff, MAX_RECENT_MEMORIES),
    ).fetchall()
    recent_memories = [_row_to_dict(r) for r in recent_rows]

    (recent_total,) = db.execute(
        "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL AND sensitive = 0 AND created_at >= ?",
        (cutoff,),
    ).fetchone()

    return {
        "generated_at": _now_iso(),
        "since_days": since_days,
        "recent_memories": recent_memories,
        "recent_total": int(recent_total),
        "vitality": build_vitality_report(db),
        "reminders_upcoming": list_reminders(db, ReminderWindow.UPCOMING, MAX_DIGEST_REMINDERS),
        "reminders_overdue": list_reminders(db, ReminderWindow.OVERDUE, MAX_DIGEST_REMINDERS),
        "sync": get_sync_status(),
    }


def _reminder_line(m: dict[str, Any]) -> str:
    content = (m.get("content") or "")[:120]
    if len(m.get("content") or "") > 120:
        content += "…"
    return f"- `{m['id']}` due {m.get('remind_at')}: {content}"


def render_digest_markdown(data: dict[str, Any], reconcile: dict[str, Any] | None = None) -> str:
    """Render digest data (from :func:`build_digest_data`) as Markdown.

    Every section degrades to a plain "nothing to report" line instead of
    disappearing or crashing when its underlying data is empty -- a
    zero-data vault still gets a coherent, readable digest.

    Args:
        data: The dict returned by :func:`build_digest_data`.
        reconcile: Optional result of
            :func:`remind_me_mcp.sync.reconcile_with_hub`, appended as an
            extra sync-health line when its ``status`` is ``"ok"``. Omitted
            entirely by the synchronous scheduled-delivery path (network
            call, async-only); the on-demand ``remind_me_digest`` tool
            fetches it and passes it in.

    Returns:
        A Markdown string ending in a single trailing newline.
    """
    since_days = data["since_days"]
    lines = [
        "# remind_me Digest",
        "",
        f"_Generated {data['generated_at']}_",
        "",
        f"## Recent Additions (last {since_days} day{'s' if since_days != 1 else ''})",
        "",
    ]

    recent_total = data["recent_total"]
    if recent_total == 0:
        lines.append("_No new memories in this window._")
    else:
        noun = "memory" if recent_total == 1 else "memories"
        lines.append(f"**{recent_total}** new {noun} added.")
        lines.append("")
        lines.append(_fmt_memories(data["recent_memories"], ResponseFormat.MARKDOWN))
    lines.append("")

    v = data["vitality"]
    lines.append("## Vault Vitality")
    lines.append("")
    if v["total_memories"] == 0:
        lines.append("_The vault is empty — nothing to report._")
    else:
        lines.append(
            f"**Total:** {v['total_memories']}  |  **Active:** {v['active_count']}  |  "
            f"**Dormant:** {v['dormant_count']}  |  **Health:** {v['vault_health_score']}"
        )
        lines.append("")
        for label, count in v["vitality_buckets"].items():
            lines.append(f"- {label}: {count}")
    lines.append("")

    upcoming = data["reminders_upcoming"]
    overdue = data["reminders_overdue"]
    lines.append("## Reminders")
    lines.append("")
    if not upcoming and not overdue:
        lines.append("_No reminders set._")
    else:
        lines.append(f"**Upcoming:** {len(upcoming)}  |  **Overdue:** {len(overdue)}")
        if overdue:
            lines.append("")
            lines.append("### Overdue")
            lines.extend(_reminder_line(m) for m in overdue)
        if upcoming:
            lines.append("")
            lines.append("### Upcoming")
            lines.extend(_reminder_line(m) for m in upcoming)
    lines.append("")

    sync = data["sync"]
    lines.append("## Sync Health")
    lines.append("")
    if not sync.get("enabled"):
        lines.append(f"_Sync disabled — {sync.get('hint', 'not configured')}_")
    else:
        drain = sync["outbox"]["drain"]["verdict"]
        lines.append(
            f"Node `{sync['node_id']}` → {sync['hub_url']}, outbox "
            f"{sync['outbox']['pending']} pending ({drain})."
        )
        for r in sync["remotes"]:
            if r["last_error"]:
                state = f"error: {r['last_error']['error']}"
            elif not r["ever_contacted"]:
                state = "never contacted"
            else:
                state = "ok"
            lines.append(f"- **{r['remote_id']}**: {r['pending']} pending — {state}")
        if reconcile is not None and reconcile.get("status") == "ok":
            lines.append(f"- hub reconcile verdict: **{reconcile['verdict']}**")
    lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"


def build_digest_markdown(
    db: sqlite3.Connection,
    since_days: int = DEFAULT_SINCE_DAYS,
    reconcile: dict[str, Any] | None = None,
) -> str:
    """Convenience wrapper: assemble and render a digest in one call.

    Args:
        db: An open SQLite connection.
        since_days: How many days back counts as "recent".
        reconcile: Optional hub-reconcile result, see
            :func:`render_digest_markdown`.

    Returns:
        The rendered Markdown digest.
    """
    return render_digest_markdown(build_digest_data(db, since_days), reconcile)


# ---------------------------------------------------------------------------
# Scheduled delivery (opt-in, REMIND_ME_DIGEST_INTERVAL -- issue #188)
# ---------------------------------------------------------------------------


def _digest_watermark(db: sqlite3.Connection) -> datetime | None:
    """Read the persisted 'last digest sent' timestamp, or None if never sent."""
    row = db.execute(
        "SELECT value FROM sync_flags WHERE key = ?", (_DIGEST_FLAG_KEY,)
    ).fetchone()
    if row is None:
        return None
    try:
        dt = datetime.fromisoformat(str(row["value"]))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _mark_digest_sent(db: sqlite3.Connection, when: datetime | None = None) -> None:
    """Persist *when* (default: now) as the 'last digest sent' watermark.

    Reuses the ``sync_flags`` key/value table :mod:`remind_me_mcp.sync`
    already established for exactly this kind of cross-restart timestamp,
    under its own key -- no new table needed.
    """
    ts = (when or datetime.now(UTC)).isoformat()
    db.execute(
        "INSERT INTO sync_flags (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (_DIGEST_FLAG_KEY, ts),
    )
    db.commit()


def is_digest_due(db: sqlite3.Connection, interval_seconds: int | None) -> bool:
    """Whether a scheduled digest is due, given the persisted watermark.

    Args:
        db: An open SQLite connection.
        interval_seconds: The configured interval, or None when disabled.

    Returns:
        False when disabled or the interval has not yet elapsed since the
        last send; True when never sent before (so a freshly-enabled
        interval fires on its first check rather than waiting a full
        interval) or when the interval has elapsed.
    """
    if interval_seconds is None:
        return False
    last = _digest_watermark(db)
    if last is None:
        return True
    return (datetime.now(UTC) - last).total_seconds() >= interval_seconds


def maybe_send_scheduled_digest(db: sqlite3.Connection | None = None) -> bool:
    """Send the digest via ``notifications.notify`` if the interval elapsed.

    Called once per :mod:`remind_me_mcp.scheduler` poll tick (every
    ``REMIND_ME_REMINDER_POLL_INTERVAL`` seconds, 60s by default) --
    deliberately cheap when disabled (the default): checking
    ``config.DIGEST_INTERVAL_SECONDS`` is a single attribute read, so a
    zero-config server pays nothing extra per tick, never touching the
    database.

    The watermark is claimed *before* building/sending, mirroring
    ``maintenance._due``/``sync._notify_sync_fault``'s discipline: bounds a
    persisting build/send failure to one retry per interval rather than
    every tick, and (unlike those two, which use an in-memory timer) is
    persisted so a server restart mid-interval does not immediately re-fire.

    Args:
        db: Connection to use; defaults to the shared per-thread connection.

    Returns:
        True if a digest was due and (best-effort) sent this call, False if
        disabled or not yet due.
    """
    interval_seconds = config.DIGEST_INTERVAL_SECONDS
    if interval_seconds is None:
        return False

    db = db if db is not None else _get_db()
    if not is_digest_due(db, interval_seconds):
        return False

    _mark_digest_sent(db)
    try:
        markdown = build_digest_markdown(db)
        notifications.notify("remind_me: digest", markdown)
    except Exception as e:  # noqa: BLE001 — a scheduler tick must never raise over this
        log.warning("Scheduled digest failed: %s", e)
    return True


__all__ = [
    "DEFAULT_SINCE_DAYS",
    "MAX_DIGEST_REMINDERS",
    "MAX_RECENT_MEMORIES",
    "build_digest_data",
    "build_digest_markdown",
    "is_digest_due",
    "maybe_send_scheduled_digest",
    "render_digest_markdown",
]
