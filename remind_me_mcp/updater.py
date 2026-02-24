"""
remind_me_mcp.updater — Version checking and self-update logic.

Provides functions to check whether the local git clone is behind origin/main
and to pull updates + reinstall the package. Also manages a background startup
check that surfaces a one-shot update notice on the first MCP tool response.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("remind_me_mcp.updater")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UpdateStatus:
    """Result of a version/update check against the remote repository."""

    installed_version: str
    local_commit: str
    remote_commit: str
    update_available: bool
    commits_behind: int
    commit_messages: list[str] = field(default_factory=list)
    repo_path: str = ""
    error: str | None = None


@dataclass(frozen=True)
class UpdateResult:
    """Result of a self-update operation."""

    success: bool
    previous_commit: str = ""
    new_commit: str = ""
    previous_version: str = ""
    new_version: str = ""
    pip_output: str = ""
    error: str | None = None
    restart_required: bool = False


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def _find_repo_root() -> Path | None:
    """Locate the git repository root from the package's installed location.

    Walks upward from this file's directory looking for a ``.git`` directory.

    Returns:
        Path to the repo root, or None if not inside a git repository.
    """
    current = Path(__file__).resolve().parent
    for parent in (current, *current.parents):
        if (parent / ".git").is_dir():
            return parent
    return None


def _run_git(*args: str, repo_path: Path) -> subprocess.CompletedProcess[str]:
    """Run a git command in the given repository.

    Args:
        *args: Git subcommand and arguments (e.g. ``"log"``, ``"--oneline"``).
        repo_path: Working directory for the git process.

    Returns:
        CompletedProcess with captured stdout/stderr.

    Raises:
        subprocess.TimeoutExpired: If the command takes longer than 60 seconds.
        subprocess.CalledProcessError: If git exits with non-zero status.
    """
    return subprocess.run(
        ["git", *args],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )


def _run_pip(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a pip command.

    Args:
        *args: Pip subcommand and arguments.

    Returns:
        CompletedProcess with captured stdout/stderr.

    Raises:
        subprocess.TimeoutExpired: If the command takes longer than 120 seconds.
        subprocess.CalledProcessError: If pip exits with non-zero status.
    """
    import sys

    return subprocess.run(
        [sys.executable, "-m", "pip", *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def check_for_update() -> UpdateStatus:
    """Check whether the local clone is behind origin/main.

    Fetches from origin, compares local HEAD against ``origin/main``, and
    returns an ``UpdateStatus`` with the version info and any available commits.

    Returns:
        UpdateStatus describing the current state versus remote.
    """
    from remind_me_mcp import __version__

    repo = _find_repo_root()
    if repo is None:
        return UpdateStatus(
            installed_version=__version__,
            local_commit="",
            remote_commit="",
            update_available=False,
            commits_behind=0,
            error="Not installed from a git repository.",
        )

    try:
        _run_git("fetch", "origin", "--quiet", repo_path=repo)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return UpdateStatus(
            installed_version=__version__,
            local_commit="",
            remote_commit="",
            update_available=False,
            commits_behind=0,
            repo_path=str(repo),
            error=f"Failed to fetch from origin: {exc}",
        )

    try:
        local_commit = _run_git(
            "rev-parse", "HEAD", repo_path=repo,
        ).stdout.strip()
        remote_commit = _run_git(
            "rev-parse", "origin/main", repo_path=repo,
        ).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return UpdateStatus(
            installed_version=__version__,
            local_commit="",
            remote_commit="",
            update_available=False,
            commits_behind=0,
            repo_path=str(repo),
            error=f"Failed to read commit info: {exc}",
        )

    if local_commit == remote_commit:
        return UpdateStatus(
            installed_version=__version__,
            local_commit=local_commit[:12],
            remote_commit=remote_commit[:12],
            update_available=False,
            commits_behind=0,
            repo_path=str(repo),
        )

    # Count commits behind
    try:
        behind_output = _run_git(
            "rev-list", "--count", "HEAD..origin/main", repo_path=repo,
        ).stdout.strip()
        commits_behind = int(behind_output)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        commits_behind = 0

    # Get commit messages for what's new
    commit_messages: list[str] = []
    try:
        log_output = _run_git(
            "log", "--oneline", "HEAD..origin/main", "--max-count=10", repo_path=repo,
        ).stdout.strip()
        if log_output:
            commit_messages = log_output.splitlines()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass

    return UpdateStatus(
        installed_version=__version__,
        local_commit=local_commit[:12],
        remote_commit=remote_commit[:12],
        update_available=commits_behind > 0,
        commits_behind=commits_behind,
        commit_messages=commit_messages,
        repo_path=str(repo),
    )


def perform_update(force: bool = False) -> UpdateResult:
    """Pull the latest changes and reinstall the package.

    Checks for a dirty working tree (uncommitted changes) before pulling.
    If ``force`` is True, skips the dirty-tree check.

    Args:
        force: If True, proceed even with uncommitted changes.

    Returns:
        UpdateResult describing what happened.
    """
    from remind_me_mcp import __version__

    repo = _find_repo_root()
    if repo is None:
        return UpdateResult(
            success=False,
            error="Not installed from a git repository.",
        )

    previous_version = __version__

    # Get current commit
    try:
        previous_commit = _run_git(
            "rev-parse", "--short", "HEAD", repo_path=repo,
        ).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        previous_commit = "unknown"

    # Check for dirty working tree
    if not force:
        try:
            status_output = _run_git(
                "status", "--porcelain", repo_path=repo,
            ).stdout.strip()
            if status_output:
                return UpdateResult(
                    success=False,
                    previous_commit=previous_commit,
                    previous_version=previous_version,
                    error=(
                        "Working tree has uncommitted changes. "
                        "Commit or stash them first, or use force=True to override."
                    ),
                )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            return UpdateResult(
                success=False,
                previous_commit=previous_commit,
                previous_version=previous_version,
                error=f"Failed to check working tree status: {exc}",
            )

    # git pull --ff-only
    try:
        _run_git("pull", "--ff-only", "origin", "main", repo_path=repo)
    except subprocess.CalledProcessError as exc:
        return UpdateResult(
            success=False,
            previous_commit=previous_commit,
            previous_version=previous_version,
            error=f"git pull failed: {exc.stderr.strip() or exc.stdout.strip()}",
        )
    except subprocess.TimeoutExpired:
        return UpdateResult(
            success=False,
            previous_commit=previous_commit,
            previous_version=previous_version,
            error="git pull timed out.",
        )

    # pip install -e .
    try:
        pip_result = _run_pip("install", "-e", str(repo))
        pip_output = pip_result.stdout.strip()
    except subprocess.CalledProcessError as exc:
        return UpdateResult(
            success=False,
            previous_commit=previous_commit,
            previous_version=previous_version,
            error=f"pip install failed: {exc.stderr.strip() or exc.stdout.strip()}",
        )
    except subprocess.TimeoutExpired:
        return UpdateResult(
            success=False,
            previous_commit=previous_commit,
            previous_version=previous_version,
            error="pip install timed out.",
        )

    # Get new commit
    try:
        new_commit = _run_git(
            "rev-parse", "--short", "HEAD", repo_path=repo,
        ).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        new_commit = "unknown"

    # Read new version from metadata (cache may be stale, so re-read)
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version

    try:
        new_version = _pkg_version("remind-me-mcp")
    except PackageNotFoundError:
        new_version = "unknown"

    return UpdateResult(
        success=True,
        previous_commit=previous_commit,
        new_commit=new_commit,
        previous_version=previous_version,
        new_version=new_version,
        pip_output=pip_output,
        restart_required=True,
    )


# ---------------------------------------------------------------------------
# Background startup check and notification state
# ---------------------------------------------------------------------------

_update_notice: str | None = None
_notice_lock = threading.Lock()


def _background_check() -> None:
    """Run the update check and set the notice if an update is available."""
    global _update_notice
    try:
        status = check_for_update()
        if status.update_available:
            parts = [
                f"**Update available** for remind-me-mcp "
                f"({status.commits_behind} commit{'s' if status.commits_behind != 1 else ''} behind)",
                f"Installed: `{status.installed_version}` (commit `{status.local_commit}`)",
                f"Latest: commit `{status.remote_commit}`",
            ]
            if status.commit_messages:
                parts.append("\nRecent changes:")
                for msg in status.commit_messages[:5]:
                    parts.append(f"- `{msg}`")
            parts.append(
                "\nRun `remind_me_self_update` to update, "
                "or `remind_me_check_update` for details."
            )
            with _notice_lock:
                _update_notice = "\n".join(parts)
    except Exception:  # Broad catch intentional: background check must never crash the server (graceful-degradation boundary)
        log.debug("Background update check failed", exc_info=True)


def start_background_check() -> None:
    """Start the update check in a background daemon thread.

    Called from ``app_lifespan`` at server startup. Non-blocking — the thread
    runs in the background and sets the notice state if an update is found.
    """
    thread = threading.Thread(target=_background_check, daemon=True, name="update-check")
    thread.start()


def pop_update_notice() -> str | None:
    """Return and clear the cached update notice.

    Returns the notice string exactly once, then clears it so subsequent
    calls return None. Thread-safe.

    Returns:
        The update notice string, or None if no notice is pending.
    """
    global _update_notice
    with _notice_lock:
        notice = _update_notice
        _update_notice = None
    return notice


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "UpdateStatus",
    "UpdateResult",
    "check_for_update",
    "perform_update",
    "start_background_check",
    "pop_update_notice",
]
