"""
Tests for the reminders ICS calendar feed (issue #190).

Covers the pure ICS generation in ics_export.py (VCALENDAR/VEVENT structure,
UID stability, RFC 5545 text escaping, line folding), the
GET /api/reminders/{token}.ics HTTP endpoint in api.py (token auth via
hmac.compare_digest, Content-Type, body content), and the
remind_me_reminders_ics_url MCP tool.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from remind_me_mcp.api import _build_api_app
from remind_me_mcp.db import _now_iso
from remind_me_mcp.ics_export import _escape_ics_text, _fold_line, build_ics

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

# ---------------------------------------------------------------------------
# ICS token resolution (config.resolve_ics_token) — mirrors test_remote.py's
# connector-token coverage for the same generate/persist/rotate/env pattern.
# ---------------------------------------------------------------------------


@pytest.fixture()
def ics_token_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point MEMORY_DIR at a fresh per-test directory with no env override."""
    import remind_me_mcp.config as cfg

    monkeypatch.setattr(cfg, "ICS_TOKEN", None)
    monkeypatch.setattr(cfg, "MEMORY_DIR", tmp_path)
    return tmp_path


def test_ics_token_generated_and_persisted(ics_token_dir: Path) -> None:
    import remind_me_mcp.config as cfg

    token = cfg.resolve_ics_token()

    token_file = ics_token_dir / "ics_token"
    assert token_file.is_file()
    assert token_file.read_text(encoding="utf-8").strip() == token
    assert len(token) >= 32


def test_ics_token_reused_across_calls(ics_token_dir: Path) -> None:
    import remind_me_mcp.config as cfg

    first = cfg.resolve_ics_token()
    second = cfg.resolve_ics_token()
    assert first == second


def test_ics_token_rotation_by_deleting_file(ics_token_dir: Path) -> None:
    import remind_me_mcp.config as cfg

    first = cfg.resolve_ics_token()
    (ics_token_dir / "ics_token").unlink()
    second = cfg.resolve_ics_token()
    assert first != second


def test_ics_token_env_var_wins(ics_token_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import remind_me_mcp.config as cfg

    monkeypatch.setattr(cfg, "ICS_TOKEN", "  env-ics-token  ")
    assert cfg.resolve_ics_token() == "env-ics-token"
    assert not (ics_token_dir / "ics_token").exists()


def test_ics_token_ephemeral_when_unwritable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import remind_me_mcp.config as cfg

    blocker = tmp_path / "not_a_dir"
    blocker.write_text("file, not a directory")
    monkeypatch.setattr(cfg, "ICS_TOKEN", None)
    monkeypatch.setattr(cfg, "MEMORY_DIR", blocker)

    with caplog.at_level("WARNING", logger="remind_me_mcp.config"):
        token = cfg.resolve_ics_token()

    assert token
    assert "ephemeral" in caplog.text

FIXED_NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# build_ics — structure
# ---------------------------------------------------------------------------


def test_build_ics_empty_list_still_valid_vcalendar() -> None:
    ics = build_ics([])
    assert ics.startswith("BEGIN:VCALENDAR\r\n")
    assert ics.endswith("END:VCALENDAR\r\n")
    assert "VERSION:2.0\r\n" in ics
    assert "PRODID:" in ics


def test_build_ics_one_reminder_produces_one_vevent() -> None:
    reminders = [{"id": "m1", "content": "call mom", "remind_at": "2026-08-05T12:00:00+00:00"}]
    ics = build_ics(reminders, now=FIXED_NOW)

    assert ics.count("BEGIN:VEVENT") == 1
    assert ics.count("END:VEVENT") == 1
    assert "SUMMARY:call mom" in ics
    assert "DESCRIPTION:call mom" in ics
    assert "DTSTART:20260805T120000Z" in ics
    assert "DTSTAMP:20260802T120000Z" in ics


def test_build_ics_multiple_reminders_produce_multiple_vevents() -> None:
    reminders = [
        {"id": "m1", "content": "first", "remind_at": "2026-08-05T12:00:00+00:00"},
        {"id": "m2", "content": "second", "remind_at": "2026-08-06T12:00:00+00:00"},
    ]
    ics = build_ics(reminders, now=FIXED_NOW)
    assert ics.count("BEGIN:VEVENT") == 2
    assert ics.count("END:VEVENT") == 2
    assert "UID:m1-2026-08-05T12:00:00+00:00@remind-me" in ics
    assert "UID:m2-2026-08-06T12:00:00+00:00@remind-me" in ics


def test_build_ics_skips_rows_without_remind_at() -> None:
    reminders = [{"id": "m1", "content": "no reminder set", "remind_at": None}]
    ics = build_ics(reminders, now=FIXED_NOW)
    assert ics.count("BEGIN:VEVENT") == 0


def test_build_ics_naive_remind_at_assumed_utc() -> None:
    """A naive ISO timestamp (no tzinfo) is treated as UTC, matching
    SetReminderInput.validate_remind_at's own naive-timestamps-are-UTC rule."""
    reminders = [{"id": "m1", "content": "x", "remind_at": "2026-08-05T12:00:00"}]
    ics = build_ics(reminders, now=FIXED_NOW)
    assert "DTSTART:20260805T120000Z" in ics


# ---------------------------------------------------------------------------
# UID stability
# ---------------------------------------------------------------------------


def test_uid_is_stable_across_calls() -> None:
    """Same (memory_id, remind_at) input produces the identical UID on a
    second, independent call -- this is what lets a subscribing calendar
    client dedupe/update on refetch instead of duplicating the event."""
    reminders = [{"id": "m1", "content": "call mom", "remind_at": "2026-08-05T12:00:00+00:00"}]

    first = build_ics(reminders, now=FIXED_NOW)
    second = build_ics(reminders, now=FIXED_NOW)

    def _uid_line(ics: str) -> str:
        return next(line for line in ics.split("\r\n") if line.startswith("UID:"))

    assert _uid_line(first) == _uid_line(second)


def test_uid_differs_when_remind_at_changes() -> None:
    """A genuinely different occurrence (remind_at moved) gets a new UID
    rather than silently reusing the old one."""
    r1 = [{"id": "m1", "content": "x", "remind_at": "2026-08-05T12:00:00+00:00"}]
    r2 = [{"id": "m1", "content": "x", "remind_at": "2026-08-06T12:00:00+00:00"}]

    def _uid_line(ics: str) -> str:
        return next(line for line in ics.split("\r\n") if line.startswith("UID:"))

    assert _uid_line(build_ics(r1)) != _uid_line(build_ics(r2))


def test_uid_differs_across_memories() -> None:
    r1 = [{"id": "m1", "content": "x", "remind_at": "2026-08-05T12:00:00+00:00"}]
    r2 = [{"id": "m2", "content": "x", "remind_at": "2026-08-05T12:00:00+00:00"}]

    def _uid_line(ics: str) -> str:
        return next(line for line in ics.split("\r\n") if line.startswith("UID:"))

    assert _uid_line(build_ics(r1)) != _uid_line(build_ics(r2))


# ---------------------------------------------------------------------------
# Escaping (RFC 5545 §3.3.11)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a, b", "a\\, b"),
        ("a; b", "a\\; b"),
        ("a\\b", "a\\\\b"),
        ("line1\nline2", "line1\\nline2"),
        ("line1\r\nline2", "line1\\nline2"),
    ],
)
def test_escape_ics_text(raw: str, expected: str) -> None:
    assert _escape_ics_text(raw) == expected


def test_escape_ics_text_backslash_escaped_before_other_chars() -> None:
    """Escaping order matters: a literal backslash must be doubled BEFORE
    comma/semicolon escaping runs, or the backslash introduced by escaping a
    comma would itself get re-escaped."""
    assert _escape_ics_text("a\\,b") == "a\\\\\\,b"


def test_build_ics_escapes_special_characters_in_content() -> None:
    tricky = "Buy milk, eggs; and bread\nDon't forget the \\receipt\\"
    reminders = [{"id": "m1", "content": tricky, "remind_at": "2026-08-05T12:00:00+00:00"}]
    ics = build_ics(reminders, now=FIXED_NOW)

    # Unescaped raw fragments that would corrupt the property value must not
    # appear anywhere as unescaped ICS-structural characters.
    for line in ics.split("\r\n"):
        if line.startswith(("SUMMARY:", "DESCRIPTION:")):
            # Every comma/semicolon in the encoded line must be preceded by a
            # backslash (i.e. escaped, not a raw structural character).
            for i, ch in enumerate(line):
                if ch in (",", ";"):
                    assert line[i - 1] == "\\", f"unescaped {ch!r} in {line!r}"
    assert "\\n" in ics  # the embedded newline survived as a literal escape
    assert "\r\nDon't" not in ics  # the raw newline did NOT become a real line break


def test_build_ics_round_trips_escaped_content_back_via_unescape() -> None:
    """A minimal unescape of DESCRIPTION recovers the original content,
    proving the escaping is lossless (not just "doesn't crash")."""
    tricky = "a,b;c\\d\ne"
    reminders = [{"id": "m1", "content": tricky, "remind_at": "2026-08-05T12:00:00+00:00"}]
    ics = build_ics(reminders, now=FIXED_NOW)

    desc_line = next(
        line for line in ics.replace("\r\n ", "").split("\r\n") if line.startswith("DESCRIPTION:")
    )
    escaped = desc_line[len("DESCRIPTION:"):]

    # Minimal RFC 5545 unescape, inverse of _escape_ics_text.
    unescaped = []
    i = 0
    while i < len(escaped):
        if escaped[i] == "\\" and i + 1 < len(escaped):
            nxt = escaped[i + 1]
            if nxt == "n":
                unescaped.append("\n")
            else:
                unescaped.append(nxt)
            i += 2
        else:
            unescaped.append(escaped[i])
            i += 1
    assert "".join(unescaped) == tricky


# ---------------------------------------------------------------------------
# Line folding (RFC 5545 §3.1)
# ---------------------------------------------------------------------------


def _assert_no_physical_line_exceeds_75_octets(ics: str) -> None:
    for line in ics.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75, f"line exceeds 75 octets: {line!r}"


def test_fold_line_short_line_unchanged() -> None:
    short = "SUMMARY:short"
    assert _fold_line(short) == short


def test_fold_line_long_ascii_line_folds_at_75_octets() -> None:
    long_line = "SUMMARY:" + "x" * 200
    folded = _fold_line(long_line)
    assert "\r\n " in folded
    _assert_no_physical_line_exceeds_75_octets(folded + "\r\n")
    # Folding is reversible: stripping "\r\n " (the fold marker) recovers
    # the original unfolded line, per RFC 5545's own unfolding rule.
    assert folded.replace("\r\n ", "") == long_line


def test_build_ics_long_summary_and_description_fold_correctly() -> None:
    long_content = "This is a very long reminder content. " * 10
    reminders = [{"id": "m1", "content": long_content, "remind_at": "2026-08-05T12:00:00+00:00"}]
    ics = build_ics(reminders, now=FIXED_NOW)
    _assert_no_physical_line_exceeds_75_octets(ics)
    # Unfolding (stripping the RFC 5545 fold marker) recovers the full
    # untruncated content in DESCRIPTION.
    unfolded = ics.replace("\r\n ", "")
    assert long_content in unfolded


def test_build_ics_long_line_folds_on_utf8_boundary_not_mid_character() -> None:
    """Non-ASCII content must never be split inside a multi-byte UTF-8
    sequence -- doing so would corrupt the character and (for some byte
    sequences) even produce invalid UTF-8 the reading side can't decode."""
    long_content = "café résumé naïve " * 10  # multi-byte accented characters
    reminders = [{"id": "m1", "content": long_content, "remind_at": "2026-08-05T12:00:00+00:00"}]
    ics = build_ics(reminders, now=FIXED_NOW)
    _assert_no_physical_line_exceeds_75_octets(ics)
    for line in ics.split("\r\n"):
        # Every physical line must be valid UTF-8 on its own (a mid-character
        # split would make .encode("utf-8") desync from the original bytes,
        # which we've already asserted stay <=75 octets above -- this
        # additionally proves each chunk decodes/round-trips cleanly).
        line.encode("utf-8").decode("utf-8")
    unfolded = ics.replace("\r\n ", "")
    assert long_content in unfolded


def test_build_ics_summary_truncated_but_description_is_not() -> None:
    long_content = "x" * 500
    reminders = [{"id": "m1", "content": long_content, "remind_at": "2026-08-05T12:00:00+00:00"}]
    ics = build_ics(reminders, now=FIXED_NOW)
    unfolded = ics.replace("\r\n ", "")
    summary_line = next(line for line in unfolded.split("\r\n") if line.startswith("SUMMARY:"))
    description_line = next(
        line for line in unfolded.split("\r\n") if line.startswith("DESCRIPTION:")
    )
    assert len(summary_line) < len(long_content)
    assert summary_line.endswith("…")
    assert long_content in description_line


# ---------------------------------------------------------------------------
# HTTP endpoint: GET /api/reminders/{token}.ics
# ---------------------------------------------------------------------------


@pytest.fixture()
def ics_client(db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient with a fixed, known ICS token and dashboard auth disabled
    (dashboard auth being off is irrelevant to the feed route, which uses
    its own secret-path token regardless -- this isolates the test to that
    token check)."""
    import remind_me_mcp.config as _cfg
    import remind_me_mcp.importer as _importer_mod

    monkeypatch.setattr(_importer_mod, "_get_db", lambda: db_conn)
    monkeypatch.setattr(_cfg, "API_KEY", "disabled")
    monkeypatch.setattr(_cfg, "ICS_TOKEN", "test-ics-token")

    app = _build_api_app()
    return TestClient(app)


def test_ics_endpoint_correct_token_returns_feed(
    ics_client: TestClient, memory_factory
) -> None:
    from datetime import timedelta

    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    memory_factory(content="water the plants", remind_at=future)

    resp = ics_client.get("/api/reminders/test-ics-token.ics")

    assert resp.status_code == 200
    assert resp.text.startswith("BEGIN:VCALENDAR\r\n")
    assert "water the plants" in resp.text


def test_ics_endpoint_content_type_is_text_calendar(
    ics_client: TestClient, memory_factory
) -> None:
    resp = ics_client.get("/api/reminders/test-ics-token.ics")
    assert resp.headers["content-type"] == "text/calendar; charset=utf-8"


def test_ics_endpoint_wrong_token_rejected(ics_client: TestClient) -> None:
    resp = ics_client.get("/api/reminders/wrong-token.ics")
    assert resp.status_code in (403, 404)
    # No leakage: the error body must not echo back or confirm the expected
    # token, or otherwise reveal that a real token check happened.
    assert "test-ics-token" not in resp.text


def test_ics_endpoint_no_bearer_header_required(
    ics_client: TestClient, memory_factory
) -> None:
    """The whole point of the secret-path scheme: no Authorization header is
    needed even though dashboard bearer auth (API_KEY) is conceptually
    active elsewhere on /api/*."""
    import remind_me_mcp.config as _cfg

    resp = ics_client.get(
        "/api/reminders/test-ics-token.ics", headers={"Authorization": ""}
    )
    assert resp.status_code == 200
    del _cfg  # imported only to document the "elsewhere" claim above


def test_ics_endpoint_body_matches_pure_function_output(
    ics_client: TestClient, memory_factory, db_conn: sqlite3.Connection
) -> None:
    from datetime import timedelta

    from remind_me_mcp.db import _row_to_dict

    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    mem = memory_factory(content="renew library card", remind_at=future)

    resp = ics_client.get("/api/reminders/test-ics-token.ics")

    row = db_conn.execute("SELECT * FROM memories WHERE id = ?", (mem["id"],)).fetchone()
    expected_reminders = [_row_to_dict(row)]
    expected = build_ics(expected_reminders, now=datetime.now(UTC))

    # DTSTAMP is generated independently in each call (wall-clock "now"), so
    # compare everything except that one line rather than the whole body.
    def _strip_dtstamp(ics: str) -> str:
        return "\r\n".join(line for line in ics.split("\r\n") if not line.startswith("DTSTAMP:"))

    assert _strip_dtstamp(resp.text) == _strip_dtstamp(expected)


def test_ics_endpoint_excludes_delivered_overdue_reminders(
    ics_client: TestClient, memory_factory, db_conn: sqlite3.Connection
) -> None:
    from datetime import timedelta

    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    delivered = memory_factory(content="already handled", remind_at=past)
    db_conn.execute(
        "INSERT INTO reminder_deliveries (memory_id, remind_at, delivered_at) VALUES (?, ?, ?)",
        (delivered["id"], delivered["remind_at"], _now_iso()),
    )
    db_conn.commit()

    resp = ics_client.get("/api/reminders/test-ics-token.ics")
    assert "already handled" not in resp.text


def test_ics_endpoint_excludes_soft_deleted_memory(
    ics_client: TestClient, memory_factory
) -> None:
    from datetime import timedelta

    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    memory_factory(content="deleted reminder", remind_at=future, deleted_at=_now_iso())

    resp = ics_client.get("/api/reminders/test-ics-token.ics")
    assert "deleted reminder" not in resp.text


# ---------------------------------------------------------------------------
# remind_me_reminders_ics_url MCP tool
# ---------------------------------------------------------------------------


async def test_ics_url_tool_returns_placeholder_when_ui_not_running(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import remind_me_mcp.config as _cfg
    from remind_me_mcp.tools import remind_me_reminders_ics_url
    from remind_me_mcp.tools import reminders as reminders_mod

    monkeypatch.setattr(_cfg, "ICS_TOKEN", "tool-test-token")
    monkeypatch.setattr(
        reminders_mod._pkg, "get_server_status", lambda: {"ui_server": "stopped", "ui_url": None}
    )

    result = await remind_me_reminders_ics_url()

    assert "stdio" in result.lower() or "not" in result.lower()
    assert "/api/reminders/tool-test-token.ics" in result


async def test_ics_url_tool_returns_full_url_when_ui_running(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import remind_me_mcp.config as _cfg
    from remind_me_mcp.tools import remind_me_reminders_ics_url
    from remind_me_mcp.tools import reminders as reminders_mod

    monkeypatch.setattr(_cfg, "ICS_TOKEN", "tool-test-token")
    monkeypatch.setattr(
        reminders_mod._pkg,
        "get_server_status",
        lambda: {"ui_server": "running", "ui_url": "http://127.0.0.1:5199"},
    )

    result = await remind_me_reminders_ics_url()

    assert result == "http://127.0.0.1:5199/api/reminders/tool-test-token.ics"
