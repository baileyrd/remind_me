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
    _get_origin_url,
    _is_remind_me_repo_root,
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


def test_is_remind_me_repo_root_true_for_matching_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "remind-me-mcp"\nversion = "1.0.0"\n'
    )
    assert _is_remind_me_repo_root(tmp_path) is True


def test_is_remind_me_repo_root_false_for_unrelated_project(tmp_path: Path) -> None:
    """A .git dir belonging to some other project must not be mistaken for
    this package's repo root -- self-update would otherwise git pull/pip
    install against a completely unrelated repository."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "some-other-package"\nversion = "3.2.1"\n'
    )
    assert _is_remind_me_repo_root(tmp_path) is False


def test_is_remind_me_repo_root_false_when_no_pyproject(tmp_path: Path) -> None:
    assert _is_remind_me_repo_root(tmp_path) is False


def test_is_remind_me_repo_root_false_for_malformed_toml(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("this is not [ valid toml")
    assert _is_remind_me_repo_root(tmp_path) is False


def test_find_repo_root_skips_unrelated_git_dir(tmp_path: Path) -> None:
    """A nested unrelated repo's .git (e.g. a venv living inside some other
    project) must not stop the upward walk -- it should keep looking for a
    .git whose pyproject.toml actually identifies as remind-me-mcp."""
    from remind_me_mcp import updater

    outer = tmp_path / "someones-other-project"
    outer.mkdir()
    (outer / ".git").mkdir()
    (outer / "pyproject.toml").write_text('[project]\nname = "someones-other-project"\n')

    nested = outer / "subdir" / "remind_me_mcp"
    nested.mkdir(parents=True)
    fake_file = nested / "updater.py"
    fake_file.touch()

    original_file = updater.__file__
    try:
        updater.__file__ = str(fake_file)
        # No genuine remind-me-mcp .git anywhere up the tree -> None, not
        # the unrelated outer repo.
        assert _find_repo_root() is None
    finally:
        updater.__file__ = original_file


# ---------------------------------------------------------------------------
# _get_origin_url
# ---------------------------------------------------------------------------


def test_get_origin_url_reads_remote(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example.com/some/repo.git"],
        cwd=tmp_path,
        check=True,
    )
    assert _get_origin_url(tmp_path) == "https://example.com/some/repo.git"


def test_get_origin_url_empty_when_no_remote(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert _get_origin_url(tmp_path) == ""


def test_get_origin_url_empty_on_nonexistent_path(tmp_path: Path) -> None:
    assert _get_origin_url(tmp_path / "does-not-exist") == ""


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


def test_perform_update_refuses_on_origin_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """SEC-05: when REMIND_ME_UPDATE_EXPECTED_ORIGIN is set, a repointed
    origin remote (compromise, a stray `git remote set-url`) is refused
    before anything is fetched or pulled."""
    import remind_me_mcp.config as cfg

    monkeypatch.setattr(cfg, "UPDATE_EXPECTED_ORIGIN", "https://github.com/expected/repo.git")

    call_log: list[tuple] = []

    def fake_git(*args, repo_path):
        call_log.append(args)
        return MagicMock(stdout="")

    with (
        patch("remind_me_mcp.updater._find_repo_root", return_value=Path("/fake/repo")),
        patch("remind_me_mcp.updater._get_origin_url", return_value="https://evil.example/repo.git"),
        patch("remind_me_mcp.updater._run_git", side_effect=fake_git),
    ):
        result = perform_update()

    assert not result.success
    assert "evil.example" in result.error
    assert "expected/repo.git" in result.error
    assert result.origin_url == "https://evil.example/repo.git"
    # Refused before ever touching git -- no fetch/pull/status call happened.
    assert call_log == []


def test_perform_update_proceeds_when_origin_matches_expected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A matching origin is not blocked by the SEC-05 trust pin."""
    import remind_me_mcp.config as cfg

    monkeypatch.setattr(cfg, "UPDATE_EXPECTED_ORIGIN", "https://github.com/expected/repo.git")

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
        patch("remind_me_mcp.updater._get_origin_url", return_value="https://github.com/expected/repo.git"),
        patch("remind_me_mcp.updater._run_git", side_effect=fake_git),
        patch("remind_me_mcp.updater._run_pip", return_value=MagicMock(stdout="installed")),
        patch("importlib.metadata.version", return_value="1.1.0"),
    ):
        result = perform_update()

    assert result.success
    assert result.origin_url == "https://github.com/expected/repo.git"


def test_perform_update_pip_failure_rolls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """When pip install fails after a successful git pull, the source tree
    is reset back to previous_commit (SEC-06) instead of being left ahead
    of the installed package's metadata/dependencies."""
    import remind_me_mcp.config as cfg

    monkeypatch.setattr(cfg, "UPDATE_EXPECTED_ORIGIN", None)

    reset_calls: list[tuple] = []

    def fake_git(*args, repo_path):
        if args[0] == "reset":
            reset_calls.append(args)
        result = MagicMock()
        if args[0] == "rev-parse":
            result.stdout = "abc123\n"
        elif args[0] == "status":
            result.stdout = "\n"
        elif args[0] == "pull":
            result.stdout = "Updating abc123..def456\n"
        return result

    with (
        patch("remind_me_mcp.updater._find_repo_root", return_value=Path("/fake/repo")),
        patch("remind_me_mcp.updater._get_origin_url", return_value=""),
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
    assert result.rolled_back is True
    assert "abc123" in result.error
    assert reset_calls == [("reset", "--hard", "abc123")]


def test_perform_update_pip_failure_rollback_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the rollback itself fails, the error message says so explicitly
    instead of silently claiming a clean recovery."""
    import remind_me_mcp.config as cfg

    monkeypatch.setattr(cfg, "UPDATE_EXPECTED_ORIGIN", None)

    def fake_git(*args, repo_path):
        result = MagicMock()
        if args[0] == "reset":
            raise subprocess.CalledProcessError(1, "git reset", stderr="reset failed")
        if args[0] == "rev-parse":
            result.stdout = "abc123\n"
        elif args[0] == "status":
            result.stdout = "\n"
        elif args[0] == "pull":
            result.stdout = "Updating abc123..def456\n"
        return result

    with (
        patch("remind_me_mcp.updater._find_repo_root", return_value=Path("/fake/repo")),
        patch("remind_me_mcp.updater._get_origin_url", return_value=""),
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
    assert result.rolled_back is False
    assert "manual" in result.error.lower() or "reset --hard" in result.error


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


def test_start_background_check_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """SE-06: REMIND_ME_AUTO_UPDATE_CHECK=false skips the startup update check entirely.

    With config.AUTO_UPDATE_CHECK False, start_background_check must spawn no
    thread and never call check_for_update (i.e. no `git fetch` at startup).
    """
    import remind_me_mcp.config as cfg
    import remind_me_mcp.updater as mod

    monkeypatch.setattr(cfg, "AUTO_UPDATE_CHECK", False)

    with patch(
        "remind_me_mcp.updater.check_for_update",
        side_effect=AssertionError("check_for_update must not run when opted out"),
    ):
        start_background_check()

        update_threads = [t for t in threading.enumerate() if t.name == "update-check"]
        assert update_threads == [], "no update-check thread may be spawned when opted out"

    assert pop_update_notice() is None
    # The module-level notice must be untouched
    with mod._notice_lock:
        assert mod._update_notice is None


def test_auto_update_check_default_enabled() -> None:
    """SE-06: with the env var unset, the startup check stays enabled (default true)."""
    import remind_me_mcp.config as cfg

    assert cfg.AUTO_UPDATE_CHECK is True


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
async def test_check_update_tool_shows_origin_when_present() -> None:
    from remind_me_mcp.tools import remind_me_check_update

    fake_status = UpdateStatus(
        installed_version="1.0.0",
        local_commit="abc123",
        remote_commit="abc123",
        update_available=False,
        commits_behind=0,
        origin_url="https://github.com/example/remind_me.git",
    )

    with patch("remind_me_mcp.updater.check_for_update", return_value=fake_status):
        result = await remind_me_check_update()

    assert "https://github.com/example/remind_me.git" in result


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
