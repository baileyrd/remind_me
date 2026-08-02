"""
remind_me_mcp.ics_export — RFC 5545 ICS calendar feed generation (issue #190).

Pure, dependency-free ICS generation for the reminders calendar feed
(``GET /api/reminders/{token}.ics`` in api.py, ``remind_me_reminders_ics_url``
in tools/reminders.py). No third-party iCalendar library: pyproject.toml's
existing optional-extras pattern (``[semantic]``, ``[ann]``, ``[mempalace]``,
``[otel]``) already signals a bias toward not adding a dependency for
something this small, and the read-only subset of RFC 5545 needed here — one
VCALENDAR wrapping a VEVENT per reminder, each with UID/DTSTAMP/DTSTART/
SUMMARY/DESCRIPTION — is compact enough to implement directly and test
exhaustively.

Two RFC 5545 details matter enough to call out explicitly, since getting
either wrong produces a feed that *looks* fine until a real calendar client
chokes on it:

  - **Line folding (§3.1).** Content lines must not exceed 75 octets
    (excluding the CRLF terminator); longer lines are split with a bare
    ``CRLF SPACE`` inserted between two octets, which the reading side
    strips back out. Apple Calendar in particular is unforgiving of
    unfolded long lines. :func:`_fold_line` splits on UTF-8 byte boundaries
    (never inside a multi-byte character) so folding is safe for non-ASCII
    content too.
  - **Text escaping (§3.3.11).** Backslash, comma, semicolon, and newlines
    are meaningful to the ICS TEXT value type and MUST be escaped, or a
    single reminder containing one of those characters corrupts every
    VEVENT after it in the same document (an unescaped ``;`` or ``,``
    inside SUMMARY/DESCRIPTION shifts how a naive parser reads the rest of
    the line/property).

UIDs are deterministic (``{memory_id}-{remind_at}@remind-me``), not random
UUIDs: a subscribing calendar app re-fetches this feed on its own schedule,
and a stable UID is what lets it treat an unchanged reminder as the same
event (update-in-place) instead of creating a fresh duplicate on every poll.
Changing a reminder's ``remind_at`` deliberately mints a new UID, since
that's a genuinely different occurrence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

PRODID = "-//remind-me-mcp//Reminders//EN"
"""RFC 5545 PRODID identifying the product that generated the calendar."""

SUMMARY_MAX_CHARS = 100
"""Truncation length for VEVENT SUMMARY (a one-line calendar title).
DESCRIPTION always carries the untruncated content, so nothing is lost."""

_FOLD_LIMIT = 75
"""Max octets per physical ICS content line, per RFC 5545 §3.1."""


def _escape_ics_text(text: str) -> str:
    """Escape an ICS TEXT value per RFC 5545 §3.3.11.

    Order matters: backslash is escaped first so the backslashes introduced
    by the later comma/semicolon/newline escapes are not themselves
    re-escaped. Both CRLF and bare LF/CR collapse to the literal two-
    character escape sequence ``\\n``.
    """
    text = text.replace("\\", "\\\\")
    text = text.replace(",", "\\,")
    text = text.replace(";", "\\;")
    text = text.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    return text


def _fold_line(line: str) -> str:
    """Fold one unfolded ICS content line at 75 octets (RFC 5545 §3.1).

    The first physical line carries up to 75 octets of content. Each
    continuation line is prefixed with a single space (the RFC's folding
    marker) which itself counts toward that line's 75-octet budget, so
    continuation chunks are capped at 74 octets. Splits only ever land on
    UTF-8 character boundaries — never inside a multi-byte sequence — so
    folded non-ASCII content decodes cleanly on both sides of the join.

    Args:
        line: A single logical content line (e.g. ``"SUMMARY:..."``), not
            yet split and with no embedded CRLF.

    Returns:
        The line unchanged if it already fits in 75 octets, otherwise the
        same content joined by ``"\\r\\n "`` at each fold point.
    """
    encoded = line.encode("utf-8")
    if len(encoded) <= _FOLD_LIMIT:
        return line

    chunks: list[bytes] = []
    start = 0
    limit = _FOLD_LIMIT
    total = len(encoded)
    while start < total:
        end = min(start + limit, total)
        # Back off until `end` doesn't land inside a multi-byte UTF-8
        # sequence (continuation bytes have the high bits 10xxxxxx).
        while end < total and end > start and (encoded[end] & 0xC0) == 0x80:
            end -= 1
        chunks.append(encoded[start:end])
        start = end
        limit = _FOLD_LIMIT - 1  # subsequent lines lose 1 octet to the leading space
    return "\r\n ".join(chunk.decode("utf-8") for chunk in chunks)


def _format_utc_stamp(dt: datetime) -> str:
    """Render a timezone-aware datetime as a ``Z``-suffixed UTC ICS DATE-TIME."""
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def build_ics(reminders: list[dict[str, Any]], *, now: datetime | None = None) -> str:
    """Render reminders as an RFC 5545 ICS VCALENDAR document.

    Args:
        reminders: Memory rows with at least ``id``, ``content``, and
            ``remind_at`` (an ISO-8601 timestamp, canonically UTC — the
            shape ``_row_to_dict`` produces and ``remind_me_list_reminders``
            already queries). Rows missing ``remind_at`` are skipped rather
            than raising, so a caller can pass any memory-shaped dict
            without pre-filtering.
        now: DTSTAMP value shared by every VEVENT in this document (UTC,
            defaults to the current time). Pinning it makes output
            byte-identical across calls in tests; production callers should
            leave it unset.

    Returns:
        A CRLF-terminated ICS document string: ``BEGIN:VCALENDAR`` /
        ``VERSION:2.0`` / ``PRODID`` / one ``VEVENT`` per reminder (ordered
        as given) / ``END:VCALENDAR``.
    """
    stamp = _format_utc_stamp(now or datetime.now(UTC))
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
    ]
    for r in reminders:
        remind_at = r.get("remind_at")
        if not remind_at:
            continue
        memory_id = str(r["id"])
        content = str(r.get("content") or "")
        remind_at = str(remind_at)

        dt = datetime.fromisoformat(remind_at)
        dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt
        dtstart = _format_utc_stamp(dt)

        # Deterministic, not random (see module docstring): a subscribing
        # calendar client dedupes/updates on refetch instead of duplicating.
        uid = f"{memory_id}-{remind_at}@remind-me"
        summary = (
            content
            if len(content) <= SUMMARY_MAX_CHARS
            else content[: SUMMARY_MAX_CHARS - 1] + "…"
        )

        lines.append("BEGIN:VEVENT")
        lines.append(_fold_line(f"UID:{_escape_ics_text(uid)}"))
        lines.append(f"DTSTAMP:{stamp}")
        lines.append(f"DTSTART:{dtstart}")
        lines.append(_fold_line(f"SUMMARY:{_escape_ics_text(summary)}"))
        lines.append(_fold_line(f"DESCRIPTION:{_escape_ics_text(content)}"))
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "PRODID",
    "SUMMARY_MAX_CHARS",
    "build_ics",
]
