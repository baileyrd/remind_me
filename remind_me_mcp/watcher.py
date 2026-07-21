"""
remind_me_mcp.watcher — Watched-folder source connector (FT-03).

Polls the directories configured in REMIND_ME_WATCH_DIRS and auto-ingests
new or changed notes/docs files through the shared import pipeline
(:func:`remind_me_mcp.importer.import_chat_file`), reusing its hash-based
dedup. Polling (mtime/size scans on an interval) is used instead of OS
file-system events so no new dependency is needed and the watcher matches
the existing background-loop style (sync, updater).

Debounce: a file whose mtime is younger than WATCH_GRACE seconds is
deferred until a later scan observes the same (mtime, size) signature, so
partially-written files are never ingested mid-write. Files that were last
modified before the grace window (the common startup-backlog case) ingest
on first sight.

Changed-file behavior: a changed file has new content, hence a new hash,
so it imports fresh under a new import_id. The watcher then marks every
memory from the path's previous import as superseded (``superseded_by`` =
the new import_id), so stale chunks from the old version drop out of
search results (search filters ``superseded_by IS NULL``) while remaining
in the database for audit. The previous import_id is learned from the
watcher's own earlier ingest of the path or — after a server restart —
from the importer's 'already_imported' skip response on the first scan.

Lifecycle: :func:`start_watcher` is called from the server lifespan (gated
on REMIND_ME_WATCH_DIRS being set) and :func:`stop_watcher` signals the
thread and joins it before the database connections are closed (SE-07).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from remind_me_mcp import config
from remind_me_mcp.config import is_in_import_roots
from remind_me_mcp.db import _get_db, _now_iso
from remind_me_mcp.importer import import_chat_file
from remind_me_mcp.telemetry import maybe_span

log = logging.getLogger("remind_me_mcp.watcher")

WATCH_EXTENSIONS = (".md", ".markdown", ".txt", ".json", ".jsonl")
"""File extensions the watcher ingests — the same set the importer supports."""

_ERROR_HISTORY = 10
"""How many recent error messages the watcher keeps for the status surface."""


# ---------------------------------------------------------------------------
# Changed-file supersession
# ---------------------------------------------------------------------------


def _supersede_import(old_import_id: str, new_import_id: str) -> int:
    """Mark all memories from a prior import as superseded (FT-03).

    Sets ``superseded_by = new_import_id`` on every non-superseded memory
    whose metadata carries *old_import_id*. Search excludes superseded
    memories (``superseded_by IS NOT NULL``), so stale chunks from the old
    version of a changed file drop out of results while staying in the
    database for audit. Note: lifecycle consolidation stores a canonical
    *memory* id in ``superseded_by``; here the superseding *import* id is
    stored instead — both satisfy the IS-NULL search filter.

    Args:
        old_import_id: import_id of the file's previous import.
        new_import_id: import_id of the fresh import that replaces it.

    Returns:
        The number of memories marked superseded.
    """
    db = _get_db()
    cur = db.execute(
        """UPDATE memories
              SET superseded_by = ?, updated_at = ?
            WHERE superseded_by IS NULL
              AND json_extract(metadata, '$.import_id') = ?""",
        (new_import_id, _now_iso(), old_import_id),
    )
    db.commit()
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------


class FolderWatcher:
    """Polls watch directories and ingests new/changed files (FT-03).

    A single scan pass is exposed as :meth:`scan_once` so behavior is
    directly testable without timing loops; :meth:`start` runs scan passes
    in a daemon thread every ``interval`` seconds until :meth:`stop` is
    called. ``scan_once`` is not re-entrant — the background thread is its
    only production caller.
    """

    def __init__(
        self,
        watch_dirs: list[Path],
        interval: int = 60,
        grace: float = 5.0,
        category: str = "chat_import",
        tags: list[str] | None = None,
        extract_mode: str = "assistant_messages",
        max_length: int = 10000,
    ) -> None:
        """Create a watcher over already-validated directories.

        Args:
            watch_dirs: Directories to poll (validate containment with
                :func:`validate_watch_dirs` first).
            interval: Seconds between scan passes in the background thread.
            grace: Debounce window — files modified less than this many
                seconds ago wait for a stable (mtime, size) signature.
            category: Category passed to the importer (its 'chat_import'
                default becomes 'document' for document files).
            tags: Tags applied to every ingested memory.
            extract_mode: Chat-file extraction mode (documents ignore it).
            max_length: Max characters per memory chunk.
        """
        self.watch_dirs = [Path(d) for d in watch_dirs]
        self.interval = interval
        self.grace = grace
        self.category = category
        self.tags = list(tags or [])
        self.extract_mode = extract_mode
        self.max_length = max_length

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        # Per-path scan state. (mtime, size) signatures: _pending holds files
        # observed but not yet stable; _ingested holds the signature at the
        # last import attempt (successful, deduped, or failed — a failed file
        # is not retried until it changes). _import_ids remembers each path's
        # current import_id so a changed file can supersede its predecessor.
        self._pending: dict[Path, tuple[float, int]] = {}
        self._ingested: dict[Path, tuple[float, int]] = {}
        self._import_ids: dict[Path, str] = {}

        # Status counters (read from other threads via status()).
        self._scans = 0
        self._last_scan_at: str | None = None
        self._files_ingested = 0
        self._files_skipped = 0
        self._memories_superseded = 0
        self._errors: deque[str] = deque(maxlen=_ERROR_HISTORY)

    # -- scanning -----------------------------------------------------------

    def _candidate_files(self) -> list[Path]:
        """List supported, non-hidden files under all watch dirs (recursive).

        Hidden entries are skipped per path component *relative to the watch
        dir*, so a hidden watch dir itself (e.g. ``~/.notes``) still works.
        Missing watch dirs are skipped silently — they may appear later.
        """
        files: set[Path] = set()
        for root in self.watch_dirs:
            if not root.is_dir():
                log.debug("Watch dir missing, skipping: %s", root)
                continue
            for path in root.rglob("*"):
                if path.suffix.lower() not in WATCH_EXTENSIONS:
                    continue
                rel_parts = path.relative_to(root).parts
                if any(part.startswith(".") for part in rel_parts):
                    continue
                if path.is_file():
                    files.add(path)
        return sorted(files)

    def scan_once(self) -> dict[str, int]:
        """Run one scan pass: ingest stable new/changed files.

        Returns:
            Counts for this pass: ``{'ingested', 'skipped', 'debounced',
            'superseded', 'errors'}``. 'skipped' means the importer's hash
            dedup recognised already-imported content (e.g. the first scan
            after a restart); an unchanged signature is not counted at all.
        """
        with maybe_span("watcher.scan"):
            return self._scan_once_inner()

    def _scan_once_inner(self) -> dict[str, int]:
        """The actual scan pass — see :meth:`scan_once`, split out only so
        the OTEL span wraps the whole thing without reindenting its body."""
        counts = {"ingested": 0, "skipped": 0, "debounced": 0, "superseded": 0, "errors": 0}
        now = time.time()
        seen: set[Path] = set()

        for path in self._candidate_files():
            try:
                st = path.stat()
            except OSError:
                continue  # vanished mid-scan
            seen.add(path)
            sig = (st.st_mtime, st.st_size)

            if self._ingested.get(path) == sig:
                continue  # unchanged since last import attempt
            if (now - st.st_mtime) < self.grace and self._pending.get(path) != sig:
                # Too fresh and not yet seen with this signature — debounce.
                self._pending[path] = sig
                counts["debounced"] += 1
                continue
            self._ingest(path, sig, counts)
            self._pending.pop(path, None)

        # Forget scan state for files that vanished so a recreated file
        # debounces again. _import_ids is kept on purpose: a recreated,
        # changed file still supersedes its previous import's memories.
        for state in (self._pending, self._ingested):
            for gone in set(state) - seen:
                del state[gone]

        with self._lock:
            self._scans += 1
            self._last_scan_at = _now_iso()
            self._files_ingested += counts["ingested"]
            self._files_skipped += counts["skipped"]
            self._memories_superseded += counts["superseded"]
        return counts

    def _ingest(self, path: Path, sig: tuple[float, int], counts: dict[str, int]) -> None:
        """Import one stable file and reconcile per-path state.

        On success, memories from the path's previous import (if any) are
        marked superseded. On a dedup skip the existing import_id is adopted
        as the path's current import. On error the signature is still
        recorded so a broken file is not retried until it changes.
        """
        try:
            result = import_chat_file(
                file_path=str(path),
                category=self.category,
                tags=list(self.tags),
                extract_mode=self.extract_mode,
                max_length=self.max_length,
            )
        except Exception as e:
            self._ingested[path] = sig
            counts["errors"] += 1
            self._record_error(f"{path}: {e}")
            log.warning("Watcher failed to import %s: %s", path, e)
            return

        self._ingested[path] = sig
        status = result.get("status")
        if status == "ok":
            new_id = str(result.get("import_id"))
            old_id = self._import_ids.get(path)
            superseded = 0
            if old_id and old_id != new_id:
                superseded = _supersede_import(old_id, new_id)
            self._import_ids[path] = new_id
            counts["ingested"] += 1
            counts["superseded"] += superseded
            log.info(
                "Watcher ingested %s — %s memories created, %d superseded",
                path,
                result.get("memories_created"),
                superseded,
            )
        elif status == "skipped":
            existing = result.get("import_id")
            if existing:
                self._import_ids[path] = str(existing)
            counts["skipped"] += 1
            log.debug("Watcher skipped %s (already imported)", path)
        else:
            counts["errors"] += 1
            self._record_error(f"{path}: {result.get('reason', 'import error')}")
            log.warning("Watcher import error for %s: %s", path, result.get("reason"))

    def _record_error(self, message: str) -> None:
        """Append an error message to the bounded recent-errors history."""
        with self._lock:
            self._errors.append(message)

    # -- thread lifecycle -----------------------------------------------------

    def start(self) -> threading.Thread:
        """Start the polling loop in a daemon thread (idempotent).

        Returns:
            The running watcher thread.
        """
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="folder-watcher")
        self._thread.start()
        log.info(
            "Folder watcher started — dirs=%s interval=%ds",
            [str(d) for d in self.watch_dirs],
            self.interval,
        )
        return self._thread

    def stop(self, timeout: float = 10.0) -> None:
        """Signal the loop to stop and join the thread.

        Called from the server lifespan before the database connections are
        closed (SE-07), so an in-flight scan finishes its writes first.

        Args:
            timeout: Max seconds to wait for the thread to exit.
        """
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    def _run(self) -> None:
        """Thread body: scan immediately, then every ``interval`` seconds."""
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception as e:
                self._record_error(str(e))
                log.error("Watcher scan failed: %s", e, exc_info=True)
            self._stop.wait(self.interval)
        log.info("Folder watcher stopped")

    # -- status ---------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Return a snapshot of the watcher's state for the status surface."""
        with self._lock:
            return {
                "enabled": True,
                "running": self._thread is not None and self._thread.is_alive(),
                "watch_dirs": [str(d) for d in self.watch_dirs],
                "interval_seconds": self.interval,
                "scans": self._scans,
                "last_scan_at": self._last_scan_at,
                "files_tracked": len(self._ingested),
                "files_pending": len(self._pending),
                "files_ingested": self._files_ingested,
                "files_skipped": self._files_skipped,
                "memories_superseded": self._memories_superseded,
                "recent_errors": list(self._errors),
            }


# ---------------------------------------------------------------------------
# Module-level lifecycle (server lifespan + status tools)
# ---------------------------------------------------------------------------

_watcher: FolderWatcher | None = None
_watcher_lock = threading.Lock()


def validate_watch_dirs(dirs: list[Path] | None = None) -> tuple[list[Path], list[str]]:
    """Split configured watch dirs into accepted paths and rejection reasons.

    A directory is rejected when it lies outside IMPORT_ROOTS — the same
    SE-02 containment rule the import tools enforce. Missing directories are
    accepted (they may be created later); the scanner skips them until they
    exist. Reads config at call time so tests can monkeypatch
    ``config.WATCH_DIRS`` / ``config.IMPORT_ROOTS``.

    Args:
        dirs: Directories to validate; defaults to ``config.WATCH_DIRS``.

    Returns:
        ``(accepted, rejected)`` — resolved accepted paths and a
        human-readable reason string per rejected entry.
    """
    if dirs is None:
        dirs = config.WATCH_DIRS
    accepted: list[Path] = []
    rejected: list[str] = []
    for d in dirs:
        p = Path(d).expanduser().resolve()
        if not is_in_import_roots(p):
            rejected.append(f"watch dir not in allowed import roots: {p}")
            continue
        accepted.append(p)
    return accepted, rejected


def start_watcher() -> FolderWatcher | None:
    """Start the global folder watcher when REMIND_ME_WATCH_DIRS is configured.

    Gated on configuration: returns None (and starts nothing) when no watch
    dir is set or every configured dir is rejected by the IMPORT_ROOTS
    containment check. Rejected dirs are logged and surfaced in the watcher's
    recent errors. Idempotent — a second call returns the running watcher.

    Returns:
        The running :class:`FolderWatcher`, or None when disabled.
    """
    global _watcher
    accepted, rejected = validate_watch_dirs()
    for reason in rejected:
        log.warning("Folder watcher: %s", reason)
    if not accepted:
        if rejected:
            log.warning("Folder watcher not started — no valid watch dirs")
        return None
    with _watcher_lock:
        if _watcher is not None and _watcher.status()["running"]:
            return _watcher
        _watcher = FolderWatcher(
            accepted,
            interval=config.WATCH_INTERVAL,
            grace=float(config.WATCH_GRACE),
        )
        for reason in rejected:
            _watcher._record_error(reason)
        _watcher.start()
        return _watcher


def stop_watcher(timeout: float = 10.0) -> None:
    """Stop and discard the global watcher (no-op when not running).

    Called from the server lifespan shutdown *before* ``_close_db()`` so the
    watcher thread cannot write to closed connections (SE-07).

    Args:
        timeout: Max seconds to wait for the watcher thread to exit.
    """
    global _watcher
    with _watcher_lock:
        watcher = _watcher
        _watcher = None
    if watcher is not None:
        watcher.stop(timeout)


def get_watch_status() -> dict[str, Any]:
    """Return the global watcher's status for the MCP status tools.

    Returns:
        The watcher's :meth:`FolderWatcher.status` snapshot, or a disabled
        marker (with a configuration hint) when no watcher is running.
    """
    watcher = _watcher
    if watcher is None:
        return {
            "enabled": False,
            "running": False,
            "watch_dirs": [],
            "hint": "set REMIND_ME_WATCH_DIRS (colon-separated dirs inside the "
            "import roots) to enable the folder watcher",
        }
    return watcher.status()


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "WATCH_EXTENSIONS",
    "FolderWatcher",
    "validate_watch_dirs",
    "start_watcher",
    "stop_watcher",
    "get_watch_status",
    "_supersede_import",
]
