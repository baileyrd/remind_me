"""
Tests for remind_me_mcp.watcher — the watched-folder source connector (FT-03).

All scan behavior is exercised deterministically through FolderWatcher.scan_once()
(no timing loops): debounce is driven by explicit os.utime mtimes relative to
the WATCH_GRACE window, and the thread lifecycle test only checks start/stop
join behavior. Follows test_importer.py / test_document_import.py patterns:
db_conn fixture for an isolated in-memory database, _embed_and_store_rows
monkeypatched to a no-op.
"""

from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING

import pytest

import remind_me_mcp.config as _cfg
import remind_me_mcp.importer as _importer_mod
import remind_me_mcp.watcher as watcher_mod
from remind_me_mcp.watcher import (
    FolderWatcher,
    get_watch_status,
    start_watcher,
    stop_watcher,
    validate_watch_dirs,
)

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def watch_dir(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    """An empty watch directory wired to the in-memory test database.

    Patches the _get_db bindings the watcher and importer use, and no-ops
    embedding so no model is ever loaded.
    """
    monkeypatch.setattr(watcher_mod, "_get_db", lambda: db_conn)
    monkeypatch.setattr(_importer_mod, "_embed_and_store_rows", lambda rows: len(rows))
    d = tmp_path / "notes"
    d.mkdir()
    return d


def _write(path: Path, text: str, age: float = 3600.0) -> None:
    """Write *text* to *path* with an mtime *age* seconds in the past.

    An old mtime puts the file outside the debounce grace window, so a single
    scan pass ingests it deterministically.
    """
    path.write_text(text, encoding="utf-8")
    ts = time.time() - age
    os.utime(path, (ts, ts))


def _memories(db: sqlite3.Connection) -> list[dict]:
    """Return all memory rows with parsed metadata."""
    rows = db.execute(
        "SELECT id, content, source, category, metadata, superseded_by FROM memories"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["metadata"] = json.loads(d["metadata"] or "{}")
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# scan_once — ingest / dedup / debounce
# ---------------------------------------------------------------------------


def test_new_file_ingested(db_conn: sqlite3.Connection, watch_dir: Path) -> None:
    """A new (settled) notes file is ingested on the first scan pass."""
    _write(watch_dir / "note.md", "# Plans\n\nRemember the launch date.")
    watcher = FolderWatcher([watch_dir])

    counts = watcher.scan_once()

    assert counts["ingested"] == 1
    assert counts["debounced"] == 0
    mems = _memories(db_conn)
    assert len(mems) == 1
    assert mems[0]["source"] == "document_import"
    assert "launch date" in mems[0]["content"]
    status = watcher.status()
    assert status["files_ingested"] == 1
    assert status["scans"] == 1
    assert status["last_scan_at"] is not None


def test_scan_once_wraps_pass_in_telemetry_span(
    db_conn: sqlite3.Connection, watch_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 7a: each scan pass is wrapped in a 'watcher.scan' OTEL span (a
    no-op unless REMIND_ME_OTEL_ENABLED is set)."""
    _write(watch_dir / "note.md", "# Plans\n\nRemember the launch date.")
    watcher = FolderWatcher([watch_dir])

    spans: list[str] = []
    real_maybe_span = watcher_mod.maybe_span

    def spy_maybe_span(name, **attrs):
        spans.append(name)
        return real_maybe_span(name, **attrs)

    monkeypatch.setattr(watcher_mod, "maybe_span", spy_maybe_span)

    watcher.scan_once()

    assert spans == ["watcher.scan"]


def test_unchanged_file_not_reimported(db_conn: sqlite3.Connection, watch_dir: Path) -> None:
    """An unchanged file is not touched again — not even re-hashed/skipped."""
    _write(watch_dir / "note.md", "# Plans\n\nRemember the launch date.")
    watcher = FolderWatcher([watch_dir])
    watcher.scan_once()

    counts = watcher.scan_once()

    assert counts == {"ingested": 0, "skipped": 0, "debounced": 0, "superseded": 0, "errors": 0}
    imports = db_conn.execute("SELECT COUNT(*) AS c FROM chat_imports").fetchone()["c"]
    assert imports == 1


def test_restart_skips_via_hash_dedup_and_learns_import_id(
    db_conn: sqlite3.Connection, watch_dir: Path
) -> None:
    """A fresh watcher (restart) skips already-imported content via hash dedup
    and adopts the existing import_id for later supersession."""
    path = watch_dir / "note.md"
    _write(path, "# Plans\n\nRemember the launch date.")
    FolderWatcher([watch_dir]).scan_once()
    old_import_id = db_conn.execute("SELECT import_id FROM chat_imports").fetchone()["import_id"]

    restarted = FolderWatcher([watch_dir])
    counts = restarted.scan_once()

    assert counts["skipped"] == 1
    assert counts["ingested"] == 0
    assert restarted._import_ids[path] == old_import_id
    assert restarted.status()["files_skipped"] == 1


def test_changed_file_reingested_and_old_import_superseded(
    db_conn: sqlite3.Connection, watch_dir: Path
) -> None:
    """A changed file imports fresh; the previous import's memories are marked
    superseded (superseded_by = new import_id) so stale chunks leave search."""
    path = watch_dir / "note.md"
    _write(path, "# Plans\n\nOld plan: ship in June.")
    watcher = FolderWatcher([watch_dir])
    watcher.scan_once()
    old_import_id = watcher._import_ids[path]

    _write(path, "# Plans\n\nNew plan: ship in July instead.")
    counts = watcher.scan_once()

    assert counts["ingested"] == 1
    assert counts["superseded"] == 1
    new_import_id = watcher._import_ids[path]
    assert new_import_id != old_import_id
    old = [m for m in _memories(db_conn) if m["metadata"].get("import_id") == old_import_id]
    new = [m for m in _memories(db_conn) if m["metadata"].get("import_id") == new_import_id]
    assert old and all(m["superseded_by"] == new_import_id for m in old)
    assert new and all(m["superseded_by"] is None for m in new)
    assert watcher.status()["memories_superseded"] == 1


def test_changed_file_supersedes_across_restart(
    db_conn: sqlite3.Connection, watch_dir: Path
) -> None:
    """Supersession works even when the original import happened before a
    restart: the import_id is learned from the dedup skip, then used."""
    path = watch_dir / "note.md"
    _write(path, "First version of the note.")
    FolderWatcher([watch_dir]).scan_once()

    restarted = FolderWatcher([watch_dir])
    restarted.scan_once()  # learns the existing import_id via the skip
    old_import_id = restarted._import_ids[path]
    _write(path, "Second version of the note, fully rewritten.")
    counts = restarted.scan_once()

    assert counts["ingested"] == 1
    assert counts["superseded"] == 1
    old = [m for m in _memories(db_conn) if m["metadata"].get("import_id") == old_import_id]
    assert old and all(m["superseded_by"] is not None for m in old)


def test_unsupported_and_hidden_files_ignored(
    db_conn: sqlite3.Connection, watch_dir: Path
) -> None:
    """Unsupported extensions, hidden files, and hidden dirs are never scanned;
    supported files in (non-hidden) subdirectories are found recursively."""
    _write(watch_dir / "script.py", "print('not a note')")
    _write(watch_dir / "binary.pdf", "%PDF-fake")
    _write(watch_dir / ".secret.md", "hidden note")
    hidden_dir = watch_dir / ".obsidian"
    hidden_dir.mkdir()
    _write(hidden_dir / "config.json", '{"role": "user", "content": "x"}')
    sub = watch_dir / "projects"
    sub.mkdir()
    _write(sub / "ok.txt", "A visible note in a subdirectory.")
    watcher = FolderWatcher([watch_dir])

    counts = watcher.scan_once()

    assert counts["ingested"] == 1
    mems = _memories(db_conn)
    assert len(mems) == 1
    assert mems[0]["metadata"]["filename"] == "ok.txt"


def test_fresh_file_debounced_until_signature_stable(
    db_conn: sqlite3.Connection, watch_dir: Path
) -> None:
    """A file modified inside the grace window waits for a stable (mtime, size)
    signature across two scans before it is ingested."""
    path = watch_dir / "draft.md"
    _write(path, "still being written...", age=0.0)  # mtime ≈ now → too fresh
    watcher = FolderWatcher([watch_dir], grace=3600.0)

    first = watcher.scan_once()
    assert first["debounced"] == 1
    assert first["ingested"] == 0
    assert _memories(db_conn) == []
    assert watcher.status()["files_pending"] == 1

    # Still changing: a new signature re-arms the debounce.
    _write(path, "still being written... and more text now", age=0.0)
    second = watcher.scan_once()
    assert second["debounced"] == 1
    assert second["ingested"] == 0

    # Signature unchanged since the last scan → stable → ingested.
    third = watcher.scan_once()
    assert third["ingested"] == 1
    assert len(_memories(db_conn)) == 1


def test_broken_file_error_recorded_and_not_retried(
    db_conn: sqlite3.Connection, watch_dir: Path
) -> None:
    """A file the importer cannot parse records an error and is not retried
    until its content changes."""
    path = watch_dir / "broken.json"
    _write(path, "{not valid json")
    watcher = FolderWatcher([watch_dir])

    counts = watcher.scan_once()
    assert counts["errors"] == 1
    assert _memories(db_conn) == []
    assert watcher.status()["recent_errors"]

    again = watcher.scan_once()
    assert again == {"ingested": 0, "skipped": 0, "debounced": 0, "superseded": 0, "errors": 0}


def test_deleted_file_state_pruned(db_conn: sqlite3.Connection, watch_dir: Path) -> None:
    """Scan state for vanished files is pruned, but the import_id is kept so a
    recreated, changed file still supersedes its predecessor."""
    path = watch_dir / "note.md"
    _write(path, "Version one.")
    watcher = FolderWatcher([watch_dir])
    watcher.scan_once()
    old_import_id = watcher._import_ids[path]

    path.unlink()
    watcher.scan_once()
    assert path not in watcher._ingested
    assert watcher._import_ids[path] == old_import_id

    _write(path, "Version two, recreated.")
    counts = watcher.scan_once()
    assert counts["ingested"] == 1
    assert counts["superseded"] == 1


# ---------------------------------------------------------------------------
# Config validation — IMPORT_ROOTS containment
# ---------------------------------------------------------------------------


def test_watch_dir_outside_import_roots_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Directories outside IMPORT_ROOTS are rejected with a reason."""
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    monkeypatch.setattr(_cfg, "IMPORT_ROOTS", [allowed])

    accepted, rejected = validate_watch_dirs([allowed / "notes", outside])

    assert accepted == [(allowed / "notes").resolve()]
    assert len(rejected) == 1
    assert str(outside.resolve()) in rejected[0]


def test_start_watcher_refuses_when_all_dirs_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """start_watcher() is a no-op when every configured dir fails containment."""
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    monkeypatch.setattr(_cfg, "IMPORT_ROOTS", [allowed])
    monkeypatch.setattr(_cfg, "WATCH_DIRS", [outside])

    assert start_watcher() is None
    assert get_watch_status()["enabled"] is False


def test_start_watcher_disabled_without_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no REMIND_ME_WATCH_DIRS the watcher never starts (default-off)."""
    monkeypatch.setattr(_cfg, "WATCH_DIRS", [])

    assert start_watcher() is None
    status = get_watch_status()
    assert status["enabled"] is False
    assert status["running"] is False
    assert "REMIND_ME_WATCH_DIRS" in status["hint"]


# ---------------------------------------------------------------------------
# Thread lifecycle
# ---------------------------------------------------------------------------


def test_thread_start_stop_joins_promptly(watch_dir: Path) -> None:
    """start() spawns the loop thread; stop() signals it and joins quickly."""
    watcher = FolderWatcher([watch_dir], interval=3600)  # never wakes on its own
    thread = watcher.start()
    assert thread.is_alive()
    assert watcher.status()["running"] is True
    # Idempotent start returns the same running thread.
    assert watcher.start() is thread

    started = time.monotonic()
    watcher.stop(timeout=10.0)
    elapsed = time.monotonic() - started

    assert not thread.is_alive()
    assert elapsed < 5.0
    assert watcher.status()["running"] is False


def test_global_start_stop_lifecycle(
    monkeypatch: pytest.MonkeyPatch, watch_dir: Path
) -> None:
    """start_watcher()/stop_watcher() manage the module-level singleton the
    server lifespan uses, and get_watch_status() reflects it."""
    monkeypatch.setattr(_cfg, "WATCH_DIRS", [watch_dir])
    monkeypatch.setattr(_cfg, "IMPORT_ROOTS", [watch_dir.parent])

    watcher = start_watcher()
    try:
        assert watcher is not None
        status = get_watch_status()
        assert status["enabled"] is True
        assert status["running"] is True
        assert status["watch_dirs"] == [str(watch_dir.resolve())]
        # Idempotent: a second start returns the running watcher.
        assert start_watcher() is watcher
    finally:
        stop_watcher(timeout=10.0)

    assert watcher._thread is not None and not watcher._thread.is_alive()
    assert get_watch_status()["enabled"] is False
