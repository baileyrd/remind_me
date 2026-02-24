"""
Tests for remind_me_mcp.updater — version checking, self-update, and startup notification.

All subprocess calls are mocked so tests never touch git or pip.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from remind_me_mcp.updater import (
    UpdateResult,
    UpdateStatus,
    _find_repo_root,
    check_for_update,
    perform_update,
    pop_update_notice,
    start_background_check,
)

# ---------------------------------------------------------------------------
# _find_repo_root
# ---------------------------------------------------------------------------


def test_find_repo_root_inside_git_repo() -> None:
    """_find_repo_root should find the .git directory above the package."""
    root = _find_repo_root()
    # The test is running inside the remind_me repo, so it should find a root.
    assert root is not None
    assert (root / ".git").is_dir()


def test_find_repo_root_outside_git_repo(tmp_path: Path) -> None:
    """_find_repo_root should return None when not in a git repo."""
    with patch("remind_me_mcp.updater.Path") as mock_path_cls:
        # Make __file__ resolve to a temp dir with no .git anywhere
        fake_file = tmp_path / "updater.py"
        fake_file.touch()
        mock_resolved = MagicMock()
        mock_resolved.parent = tmp_path
        mock_resolved.parents = tmp_path.parents
        mock_path_cls.return_value.resolve.return_value = mock_resolved

        # Check that .git is not in tmp_path (it shouldn't be)
        _find_repo_root()
        # Since we patched Path(__file__), the function will walk tmp_path
        # which has no .git, so it returns None — unless the real __file__
        # is used. Let's do this more directly:

    # More direct approach: check a path with no .git
    from remind_me_mcp import updater

    original_file = updater.__file__
    try:
        updater.__file__ = str(tmp_path / "fake_updater.py")
        (tmp_path / "fake_updater.py").touch()
        assert _find_repo_root() is None
    finally:
        updater.__file__ = original_file


# ---------------------------------------------------------------------------
# check_for_update
# ---------------------------------------------------------------------------


def test_check_for_update_no_repo() -> None:
    """check_for_update returns error when not in a git repo."""
    with patch("remind_me_mcp.updater._find_repo_root", return_value=None):
        status = check_for_update()

    assert not status.update_available
    assert status.error is not None
    assert "git repository" in status.error


def test_check_for_update_up_to_date() -> None:
    """check_for_update reports up-to-date when local == remote."""
    commit = "abc123def456"

    def fake_git(*args, repo_path):
        result = MagicMock()
        if args[0] == "fetch":
            result.stdout = ""
        elif args[0] == "rev-parse":
            result.stdout = commit + "\n"
        return result

    with (
        patch("remind_me_mcp.updater._find_repo_root", return_value=Path("/fake/repo")),
        patch("remind_me_mcp.updater._run_git", side_effect=fake_git),
    ):
        status = check_for_update()

    assert not status.update_available
    assert status.commits_behind == 0
    assert status.error is None
    assert status.local_commit == commit[:12]
    assert status.remote_commit == commit[:12]


def test_check_for_update_update_available() -> None:
    """check_for_update detects when remote is ahead."""
    def fake_git(*args, repo_path):
        result = MagicMock()
        if args[0] == "fetch":
            result.stdout = ""
        elif args == ("rev-parse", "HEAD"):
            result.stdout = "aaa111222333\n"
        elif args == ("rev-parse", "origin/main"):
            result.stdout = "bbb444555666\n"
        elif args[0] == "rev-list":
            result.stdout = "3\n"
        elif args[0] == "log":
            result.stdout = "bbb4445 Add feature X\nccc5556 Fix bug Y\nddd6667 Update docs\n"
        return result

    with (
        patch("remind_me_mcp.updater._find_repo_root", return_value=Path("/fake/repo")),
        patch("remind_me_mcp.updater._run_git", side_effect=fake_git),
    ):
        status = check_for_update()

    assert status.update_available
    assert status.commits_behind == 3
    assert len(status.commit_messages) == 3
    assert status.error is None


def test_check_for_update_fetch_failure() -> None:
    """check_for_update returns error on git fetch failure."""
    def fake_git(*args, repo_path):
        if args[0] == "fetch":
            raise subprocess.CalledProcessError(1, "git fetch", stderr="network error")
        return MagicMock(stdout="")

    with (
        patch("remind_me_mcp.updater._find_repo_root", return_value=Path("/fake/repo")),
        patch("remind_me_mcp.updater._run_git", side_effect=fake_git),
    ):
        status = check_for_update()

    assert not status.update_available
    assert status.error is not None
    assert "fetch" in status.error.lower()


# ---------------------------------------------------------------------------
# perform_update
# ---------------------------------------------------------------------------


def test_perform_update_no_repo() -> None:
    """perform_update returns error when not in a git repo."""
    with patch("remind_me_mcp.updater._find_repo_root", return_value=None):
        result = perform_update()

    assert not result.success
    assert "git repository" in result.error


def test_perform_update_dirty_tree_rejected() -> None:
    """perform_update refuses when working tree is dirty and force=False."""
    def fake_git(*args, repo_path):
        result = MagicMock()
        if args[0] == "rev-parse":
            result.stdout = "abc123\n"
        elif args[0] == "status":
            result.stdout = " M some_file.py\n"
        return result

    with (
        patch("remind_me_mcp.updater._find_repo_root", return_value=Path("/fake/repo")),
        patch("remind_me_mcp.updater._run_git", side_effect=fake_git),
    ):
        result = perform_update(force=False)

    assert not result.success
    assert "uncommitted" in result.error.lower()


def test_perform_update_dirty_tree_forced() -> None:
    """perform_update proceeds with dirty tree when force=True."""
    call_log: list[tuple] = []

    def fake_git(*args, repo_path):
        call_log.append(args)
        result = MagicMock()
        if args[0] == "rev-parse":
            result.stdout = "abc123\n" if len(call_log) <= 1 else "def456\n"
        elif args[0] == "pull":
            result.stdout = "Updating abc123..def456\n"
        elif args[0] == "status":
            result.stdout = " M dirty_file.py\n"
        return result

    with (
        patch("remind_me_mcp.updater._find_repo_root", return_value=Path("/fake/repo")),
        patch("remind_me_mcp.updater._run_git", side_effect=fake_git),
        patch("remind_me_mcp.updater._run_pip", return_value=MagicMock(stdout="installed")),
        patch("importlib.metadata.version", return_value="1.1.0"),
    ):
        result = perform_update(force=True)

    assert result.success
    # Should NOT have called 'status' (skipped dirty check)
    assert not any(args[0] == "status" for args in call_log)


def test_perform_update_pull_failure() -> None:
    """perform_update returns error when git pull fails."""
    def fake_git(*args, repo_path):
        result = MagicMock()
        if args[0] == "rev-parse":
            result.stdout = "abc123\n"
        elif args[0] == "status":
            result.stdout = "\n"
        elif args[0] == "pull":
            raise subprocess.CalledProcessError(
                1, "git pull", output="", stderr="merge conflict"
            )
        return result

    with (
        patch("remind_me_mcp.updater._find_repo_root", return_value=Path("/fake/repo")),
        patch("remind_me_mcp.updater._run_git", side_effect=fake_git),
    ):
        result = perform_update()

    assert not result.success
    assert "pull" in result.error.lower()


def test_perform_update_pip_failure() -> None:
    """perform_update returns error when pip install fails."""
    def fake_git(*args, repo_path):
        result = MagicMock()
        if args[0] == "rev-parse":
            result.stdout = "abc123\n"
        elif args[0] == "status":
            result.stdout = "\n"
        elif args[0] == "pull":
            result.stdout = "Already up to date.\n"
        return result

    with (
        patch("remind_me_mcp.updater._find_repo_root", return_value=Path("/fake/repo")),
        patch("remind_me_mcp.updater._run_git", side_effect=fake_git),
        patch(
            "remind_me_mcp.updater._run_pip",
            side_effect=subprocess.CalledProcessError(
                1, "pip install", output="", stderr="build error"
            ),
        ),
    ):
        result = perform_update()

    assert not result.success
    assert "pip" in result.error.lower()


def test_perform_update_success() -> None:
    """perform_update succeeds end-to-end with mocked subprocess."""
    call_count = 0

    def fake_git(*args, repo_path):
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        if args[0] == "rev-parse" and "--short" in args:
            result.stdout = "abc123\n" if call_count <= 2 else "def456\n"
        elif args[0] == "status":
            result.stdout = "\n"
        elif args[0] == "pull":
            result.stdout = "Updating abc123..def456\n"
        return result

    with (
        patch("remind_me_mcp.updater._find_repo_root", return_value=Path("/fake/repo")),
        patch("remind_me_mcp.updater._run_git", side_effect=fake_git),
        patch("remind_me_mcp.updater._run_pip", return_value=MagicMock(stdout="Successfully installed")),
        patch("importlib.metadata.version", return_value="1.1.0"),
    ):
        result = perform_update()

    assert result.success
    assert result.restart_required
    assert result.error is None


# ---------------------------------------------------------------------------
# pop_update_notice
# ---------------------------------------------------------------------------


def test_pop_update_notice_returns_once_then_none() -> None:
    """pop_update_notice returns the notice once, then None."""
    import remind_me_mcp.updater as mod

    # Set notice directly
    with mod._notice_lock:
        mod._update_notice = "Test notice"

    first = pop_update_notice()
    second = pop_update_notice()

    assert first == "Test notice"
    assert second is None


def test_pop_update_notice_none_when_empty() -> None:
    """pop_update_notice returns None when no notice is set."""
    import remind_me_mcp.updater as mod

    with mod._notice_lock:
        mod._update_notice = None

    assert pop_update_notice() is None


# ---------------------------------------------------------------------------
# start_background_check
# ---------------------------------------------------------------------------


def test_start_background_check_sets_notice() -> None:
    """start_background_check sets the update notice when an update is available."""
    import remind_me_mcp.updater as mod

    # Clear any existing notice
    with mod._notice_lock:
        mod._update_notice = None

    fake_status = UpdateStatus(
        installed_version="1.0.0",
        local_commit="abc123",
        remote_commit="def456",
        update_available=True,
        commits_behind=2,
        commit_messages=["def456 Add feature", "ccc333 Fix bug"],
        repo_path="/fake/repo",
    )

    with patch("remind_me_mcp.updater.check_for_update", return_value=fake_status):
        start_background_check()

    # Wait for the background thread to complete
    for t in threading.enumerate():
        if t.name == "update-check":
            t.join(timeout=5)

    notice = pop_update_notice()
    assert notice is not None
    assert "Update available" in notice
    assert "2 commits behind" in notice


def test_start_background_check_no_notice_when_up_to_date() -> None:
    """start_background_check sets no notice when already up-to-date."""
    import remind_me_mcp.updater as mod

    with mod._notice_lock:
        mod._update_notice = None

    fake_status = UpdateStatus(
        installed_version="1.0.0",
        local_commit="abc123",
        remote_commit="abc123",
        update_available=False,
        commits_behind=0,
    )

    with patch("remind_me_mcp.updater.check_for_update", return_value=fake_status):
        start_background_check()

    for t in threading.enumerate():
        if t.name == "update-check":
            t.join(timeout=5)

    assert pop_update_notice() is None


# ---------------------------------------------------------------------------
# MCP tool wrappers (integration-style)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_update_tool_returns_markdown() -> None:
    """remind_me_check_update MCP tool returns markdown status."""
    from remind_me_mcp.tools import remind_me_check_update

    fake_status = UpdateStatus(
        installed_version="1.0.0",
        local_commit="abc123",
        remote_commit="def456",
        update_available=True,
        commits_behind=3,
        commit_messages=["def456 New feature"],
        repo_path="/fake/repo",
    )

    with patch("remind_me_mcp.updater.check_for_update", return_value=fake_status):
        result = await remind_me_check_update()

    assert "Version Status" in result
    assert "1.0.0" in result
    assert "Update available" in result
    assert "3 commit" in result


@pytest.mark.asyncio
async def test_self_update_tool_returns_restart_notice() -> None:
    """remind_me_self_update MCP tool returns restart notice on success."""
    from remind_me_mcp.tools import remind_me_self_update

    fake_result = UpdateResult(
        success=True,
        previous_commit="abc123",
        new_commit="def456",
        previous_version="1.0.0",
        new_version="1.1.0",
        pip_output="installed",
        restart_required=True,
    )

    with patch("remind_me_mcp.updater.perform_update", return_value=fake_result):
        result = await remind_me_self_update()

    assert "Update Successful" in result
    assert "1.0.0" in result
    assert "1.1.0" in result
    assert "Restart required" in result
