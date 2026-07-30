"""
remind_me_mcp.updater — Version checking and self-update logic.

Provides functions to check whether the local git clone is behind origin/main
and to pull updates + reinstall the package. Also manages a background startup
check that surfaces a one-shot update notice on the first MCP tool response.
"""

from __future__ import annotations

import contextlib
import logging
import os
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
    origin_url: str = ""
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
    origin_url: str = ""
    error: str | None = None
    restart_required: bool = False
    rolled_back: bool = False


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


_PACKAGE_NAME = "remind-me-mcp"

# One import-spec-detectable module per optional-dependencies group in
# pyproject.toml, used to figure out which extras are currently installed
# so a reinstall doesn't silently drop them (see _installed_extras).
_EXTRA_MARKERS = {
    "semantic": "sqlite_vec",
    "mempalace": "chromadb",
    "otel": "opentelemetry.sdk",
    "ann": "usearch",
}


def _installed_extras() -> list[str]:
    """Return the optional-dependency extras currently installed, by name.

    ``pip install -e .`` only pulls the base ``dependencies`` list -- it
    knows nothing about which optional extras (e.g. ``[semantic]``) were
    installed previously, so a plain reinstall silently drops them. Checking
    each extra's marker module lets the reinstall step re-request the same
    extras instead of degrading the running install.
    """
    import importlib.util

    def _has(marker: str) -> bool:
        try:
            return importlib.util.find_spec(marker) is not None
        except ModuleNotFoundError:
            # find_spec raises rather than returning None for a dotted name
            # (e.g. "opentelemetry.sdk") when the parent package itself
            # isn't importable.
            return False

    return [extra for extra, marker in _EXTRA_MARKERS.items() if _has(marker)]


def _is_remind_me_repo_root(candidate: Path) -> bool:
    """Return True when *candidate* looks like this package's own repo root.

    Checks ``pyproject.toml``'s ``[project].name`` rather than trusting any
    ``.git`` directory found by walking upward -- a non-editable install
    placed inside an unrelated git-tracked directory (e.g. a venv nested in
    a user's own project repo) would otherwise cause self-update to
    ``git pull``/``pip install -e .`` against the wrong repository entirely.
    """
    pyproject = candidate / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        import tomllib

        with pyproject.open("rb") as f:
            data = tomllib.load(f)
        return data.get("project", {}).get("name") == _PACKAGE_NAME
    except (OSError, ValueError):
        return False


def _find_repo_root() -> Path | None:
    """Locate this package's git repository root from its installed location.

    Walks upward from this file's directory looking for a ``.git`` directory
    whose sibling ``pyproject.toml`` identifies as this package (SEC-05) --
    the first ``.git`` found isn't necessarily the right one.

    Returns:
        Path to the repo root, or None if not inside this package's git
        repository.
    """
    current = Path(__file__).resolve().parent
    for parent in (current, *current.parents):
        if (parent / ".git").is_dir() and _is_remind_me_repo_root(parent):
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


def _get_origin_url(repo_path: Path) -> str:
    """Return the ``origin`` remote's URL, or "" if it can't be read.

    Reading this is purely local (no network) -- ``git config --get`` reads
    ``.git/config`` directly, unlike ``git remote get-url`` which behaves
    the same but is documented as network-safe either way. Best-effort: an
    unreadable/missing origin must never break a status check.
    """
    try:
        return subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def _find_uv() -> str | None:
    """Locate the ``uv`` executable, including installs outside ``PATH``.

    ``shutil.which`` alone is not enough. An MCP server is launched by a
    client (or a systemd unit), and frequently inherits a minimal ``PATH``
    that omits ``~/.local/bin`` -- which is exactly where uv's own
    installer puts it. That matters because a uv-tool-managed install of
    this package has no ``pip`` at all, so uv is the *only* way to
    reinstall it; failing to find uv turns a working update into a
    "No module named pip" dead end.

    Returns:
        Absolute path to the uv executable, or None if it can't be found.
    """
    import shutil
    import sys

    found = shutil.which("uv")
    if found:
        return found

    exe = "uv.exe" if sys.platform == "win32" else "uv"
    home = Path.home()
    candidates = (
        home / ".local" / "bin" / exe,  # uv standalone installer (default)
        home / ".cargo" / "bin" / exe,  # cargo install uv
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _run_pip(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a pip install command against *this server's* environment.

    Prefers ``uv pip`` (faster, and the only option for uv-tool-managed
    installs), falling back to ``python -m pip``.

    Args:
        *args: Pip subcommand and arguments (e.g. ``"install"``, ``"-e"``, ``"."``).

    Returns:
        CompletedProcess with captured stdout/stderr.

    Raises:
        RuntimeError: If neither uv nor pip is available to do the install.
        subprocess.TimeoutExpired: If the command takes longer than 120 seconds.
        subprocess.CalledProcessError: If the install command exits with non-zero status.
    """
    import importlib.util
    import sys

    uv = _find_uv()
    if uv:
        # --python pins the target to the interpreter actually running this
        # server. Without it, `uv pip` resolves against the ambient
        # VIRTUAL_ENV / discovered .venv, which for a uv-tool-managed install
        # is a different environment entirely -- the update would report
        # success while installing somewhere that is never imported.
        subcommand, *rest = args
        return subprocess.run(
            [uv, "pip", subcommand, "--python", sys.executable, *rest],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )

    if importlib.util.find_spec("pip") is None:
        raise RuntimeError(
            "Cannot reinstall: neither 'uv' nor 'pip' is available. The "
            f"running interpreter ({sys.executable}) has no pip, which is "
            "normal for a 'uv tool install' -- but uv was not found on PATH "
            "or in ~/.local/bin either. Install uv, or make it visible to "
            "the server process, then retry."
        )

    return subprocess.run(
        [sys.executable, "-m", "pip", *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )


# ---------------------------------------------------------------------------
# Cross-process update lock
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _update_lock(repo: Path):
    """Best-effort exclusive lock so overlapping self-updates can't race.

    Multiple MCP server processes (e.g. one per open client session) commonly
    share the same repo/venv. Without this, two concurrent
    ``perform_update()`` calls can interleave their git pull + pip/uv install
    steps -- one process's reinstall can land in the middle of another's,
    leaving the venv's installed metadata out of sync with what either call
    reported (observed: a call reporting a successful upgrade to a version
    the venv was never actually left at).

    POSIX-only (``fcntl``) and best-effort throughout: any failure to open
    the lock file itself (missing repo dir, read-only filesystem, no
    ``fcntl`` on this platform) falls back to running unlocked rather than
    blocking an update that would otherwise have worked fine standalone.

    Raises:
        RuntimeError: if another update currently holds the lock.
    """
    try:
        import fcntl
    except ImportError:
        yield
        return

    try:
        fd = os.open(repo / ".git" / "remind_me_update.lock", os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        yield
        return

    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(
                "Another self-update is already running for this repo. "
                "Wait for it to finish, then retry."
            ) from None
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


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

    origin_url = _get_origin_url(repo)

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
            origin_url=origin_url,
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
            origin_url=origin_url,
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
            origin_url=origin_url,
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
        origin_url=origin_url,
    )


def perform_update(force: bool = False) -> UpdateResult:
    """Pull the latest changes and reinstall the package.

    Checks for a dirty working tree (uncommitted changes) before pulling.
    If ``force`` is True, skips the dirty-tree check. If
    ``config.UPDATE_EXPECTED_ORIGIN`` is set, refuses to proceed unless the
    local ``origin`` remote matches it exactly (SEC-05) -- opt-in, since
    there's no single correct origin for every fork of this package.

    Args:
        force: If True, proceed even with uncommitted changes.

    Returns:
        UpdateResult describing what happened.
    """
    from remind_me_mcp import __version__, config

    repo = _find_repo_root()
    if repo is None:
        return UpdateResult(
            success=False,
            error="Not installed from a git repository.",
        )

    previous_version = __version__
    origin_url = _get_origin_url(repo)

    expected_origin = config.UPDATE_EXPECTED_ORIGIN
    if expected_origin and origin_url != expected_origin:
        return UpdateResult(
            success=False,
            previous_version=previous_version,
            origin_url=origin_url,
            error=(
                f"Refusing to update: origin is {origin_url!r}, expected "
                f"{expected_origin!r} (REMIND_ME_UPDATE_EXPECTED_ORIGIN). "
                "If this remote change is intentional, update the env var "
                "to match."
            ),
        )

    # Get current commit
    try:
        previous_commit = _run_git(
            "rev-parse", "--short", "HEAD", repo_path=repo,
        ).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        previous_commit = "unknown"

    # Everything below mutates the repo/venv (dirty-tree check through
    # reinstall) and must run under the cross-process lock -- see
    # _update_lock's docstring for why (SE-updater-race).
    try:
        with _update_lock(repo):
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
                            origin_url=origin_url,
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
                        origin_url=origin_url,
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
                    origin_url=origin_url,
                    error=f"git pull failed: {exc.stderr.strip() or exc.stdout.strip()}",
                )
            except subprocess.TimeoutExpired:
                return UpdateResult(
                    success=False,
                    previous_commit=previous_commit,
                    previous_version=previous_version,
                    origin_url=origin_url,
                    error="git pull timed out.",
                )

            # pip install -e ., re-requesting whichever extras were already
            # installed so the update doesn't silently drop them (see
            # _installed_extras).
            extras = _installed_extras()
            install_target = f"{repo}[{','.join(extras)}]" if extras else str(repo)
            try:
                pip_result = _run_pip("install", "-e", install_target)
                pip_output = pip_result.stdout.strip()
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError) as exc:
                # The source tree already advanced past previous_commit at this
                # point -- left as-is, the checked-out code and installed package
                # metadata/dependencies would silently diverge (SEC-06). Since the
                # dirty-tree check above already guarantees nothing uncommitted
                # exists to lose, resetting back to previous_commit is safe and
                # restores a fully consistent state.
                rolled_back = _rollback(repo, previous_commit)
                if isinstance(exc, subprocess.TimeoutExpired):
                    reason = "pip install timed out."
                elif isinstance(exc, RuntimeError):
                    # No installer available at all (see _run_pip) -- already a
                    # fully-formed operator-facing message.
                    reason = str(exc)
                else:
                    reason = f"pip install failed: {exc.stderr.strip() or exc.stdout.strip()}"
                return UpdateResult(
                    success=False,
                    previous_commit=previous_commit,
                    previous_version=previous_version,
                    origin_url=origin_url,
                    rolled_back=rolled_back,
                    error=(
                        f"{reason} "
                        + (
                            f"Rolled the source tree back to {previous_commit} -- "
                            "nothing changed overall."
                            if rolled_back
                            else "Automatic rollback ALSO failed -- the source tree "
                            f"is now ahead of the installed package (still at "
                            f"{previous_commit}'s dependencies/metadata). Run "
                            f"'git reset --hard {previous_commit}' manually, then "
                            "reinstall."
                        )
                    ),
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
                origin_url=origin_url,
                restart_required=True,
            )
    except RuntimeError as exc:
        return UpdateResult(
            success=False,
            previous_commit=previous_commit,
            previous_version=previous_version,
            origin_url=origin_url,
            error=str(exc),
        )


def _rollback(repo: Path, previous_commit: str) -> bool:
    """Best-effort ``git reset --hard`` back to *previous_commit*.

    Only ever called after a successful ``git pull`` followed by a failed
    ``pip install``, to restore a consistent state (SEC-06). Returns False
    (never raises) if the reset itself fails -- the caller must surface
    that so the operator knows manual recovery is needed.
    """
    if previous_commit in ("", "unknown"):
        return False
    try:
        _run_git("reset", "--hard", previous_commit, repo_path=repo)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


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

    SE-06: honors REMIND_ME_AUTO_UPDATE_CHECK=false as an opt-out for the
    startup ``git fetch``; the manual check/update tools are unaffected.
    """
    from remind_me_mcp import config

    if not config.AUTO_UPDATE_CHECK:
        log.info("Startup update check disabled (REMIND_ME_AUTO_UPDATE_CHECK)")
        return
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
