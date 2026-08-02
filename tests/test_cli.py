"""
Behavior tests for remind_me_mcp.cli — the `add`/`search`/`list` subcommands
(issue #189).

Every test drives the real remind_me_mcp.__main__.main() entry point via
sys.argv (matching test_main.py's own convention), against a real WAL-mode
SQLite file under a per-test temp REMIND_ME_MCP_DIR — not the db_conn
in-memory fixture — so the CLI's own DB-open/migrate/close path (via
db._get_db()/db._close_db(), no FastMCP lifespan involved) is exercised for
real, and so the single-instance-lock-file test below can use the genuine
config.MCP_PID_FILE mechanism.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import TYPE_CHECKING

import pytest

import remind_me_mcp.__main__ as main_mod
import remind_me_mcp.config as cfg
import remind_me_mcp.db as db_mod
import remind_me_mcp.pid as pid_mod

if TYPE_CHECKING:
    from pathlib import Path


def _run_main(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    """Invoke main() with the given CLI arguments; return the exit code."""
    monkeypatch.setattr("sys.argv", ["remind-me-mcp", *argv])
    with pytest.raises(SystemExit) as excinfo:
        main_mod.main()
    return 0 if excinfo.value.code is None else int(excinfo.value.code)


@pytest.fixture()
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mock_embedder) -> Path:
    """Point every REMIND_ME_MCP_DIR-derived path at a fresh per-test temp
    dir (not the session-scoped tmp_memory_dir, which every other test
    module shares) and force a fresh DB connection under it.

    A stale thread-local connection left open by a previous test (real
    _get_db() caches one connection per thread, keyed only by a generation
    counter -- see db.py) would otherwise silently keep pointing at the
    *previous* test's database file even after DB_PATH is repointed here;
    db_mod._close_db() bumps that generation counter, forcing the next
    _get_db() call (inside the CLI command under test) to open a fresh
    connection against the newly-patched path.
    """
    mem_dir = tmp_path / "remind_me_cli_home"
    mem_dir.mkdir()
    db_path = mem_dir / "memory.db"
    pid_file = mem_dir / "server.pid"
    mcp_pid_file = mem_dir / "mcp_server.pid"

    monkeypatch.setattr(cfg, "MEMORY_DIR", mem_dir)
    monkeypatch.setattr(cfg, "DB_PATH", db_path)
    monkeypatch.setattr(cfg, "PID_FILE", pid_file)
    monkeypatch.setattr(cfg, "MCP_PID_FILE", mcp_pid_file)
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    monkeypatch.setattr(pid_mod, "DB_PATH", db_path)
    monkeypatch.setattr(pid_mod, "PID_FILE", pid_file)

    db_mod._close_db()
    yield mem_dir
    db_mod._close_db()


def _query_memories(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM memories").fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


def test_add_writes_a_retrievable_memory(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """`add` persists a memory to REMIND_ME_MCP_DIR's DB, printing its id."""
    code = _run_main(monkeypatch, "add", "buy oat milk on the way home")
    out = capsys.readouterr().out
    assert code == 0
    assert "stored with id" in out

    rows = _query_memories(cli_env / "memory.db")
    assert len(rows) == 1
    assert rows[0]["content"] == "buy oat milk on the way home"
    assert rows[0]["category"] == "general"
    assert json.loads(rows[0]["tags"]) == []


def test_add_respects_category_and_tags(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """`--category`/`--tags` are threaded through to the stored memory."""
    code = _run_main(
        monkeypatch,
        "add",
        "the deploy key rotates every 90 days",
        "--category",
        "ops",
        "--tags",
        "infra, security , rotation",
    )
    assert code == 0

    rows = _query_memories(cli_env / "memory.db")
    assert rows[0]["category"] == "ops"
    assert json.loads(rows[0]["tags"]) == ["infra", "security", "rotation"]


def test_add_then_findable_via_search(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A memory written by `add` is findable by keyword via `search`."""
    code = _run_main(monkeypatch, "add", "the wifi password is on the fridge")
    assert code == 0
    capsys.readouterr()

    code = _run_main(monkeypatch, "search", "wifi password")
    out = capsys.readouterr().out
    assert code == 0
    assert "wifi password" in out


def test_add_then_findable_via_list(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A memory written by `add` shows up in `list`."""
    code = _run_main(monkeypatch, "add", "recycling goes out on Tuesdays")
    assert code == 0
    capsys.readouterr()

    code = _run_main(monkeypatch, "list")
    out = capsys.readouterr().out
    assert code == 0
    assert "recycling goes out on Tuesdays" in out


def test_add_maps_tool_error_string_to_nonzero_exit(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """memory_add reports failures (e.g. a sqlite3.IntegrityError/
    OperationalError) as an 'Error: ...' string rather than raising --
    _cmd_add must map that to a non-zero exit rather than reporting success."""
    import remind_me_mcp.tools.crud as crud_mod

    async def _fake_memory_add(params):
        return "Error: Could not add memory — a memory with this content may already exist."

    monkeypatch.setattr(crud_mod, "memory_add", _fake_memory_add)

    code = _run_main(monkeypatch, "add", "content that triggers a simulated failure")
    out = capsys.readouterr().out
    assert code != 0
    assert "Error" in out


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_finds_previously_added_memory_by_keyword(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _run_main(monkeypatch, "add", "Bailey prefers dark roast coffee in the morning")
    _run_main(monkeypatch, "add", "the quarterly report is due next Friday")
    capsys.readouterr()

    code = _run_main(monkeypatch, "search", "dark roast coffee")
    out = capsys.readouterr().out
    assert code == 0
    # With only two documents in the corpus, hybrid search's semantic tier
    # can surface the unrelated memory too (nothing in a 2-row vault gives
    # it a genuinely *low* semantic score) -- assert the true keyword match
    # is present rather than requiring the unrelated memory to be absent,
    # which would make this test depend on exact RRF/embedding behavior
    # rather than on "search finds the memory it should."
    assert "dark roast coffee" in out


def test_search_no_match_reports_no_results_and_exits_zero(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    code = _run_main(monkeypatch, "search", "nonexistent topic entirely")
    out = capsys.readouterr().out
    assert code == 0
    assert "No memories found" in out


def test_search_json_output_is_valid_and_shaped(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _run_main(monkeypatch, "add", "the api key lives in the vault", "--category", "secrets")
    capsys.readouterr()

    code = _run_main(monkeypatch, "search", "api key vault", "--json")
    out = capsys.readouterr().out
    assert code == 0

    payload = json.loads(out)
    assert "memories" in payload
    assert isinstance(payload["memories"], list)
    assert payload["memories"][0]["content"] == "the api key lives in the vault"
    assert payload["memories"][0]["category"] == "secrets"


def test_search_respects_limit(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    for i in range(5):
        _run_main(monkeypatch, "add", f"widget number {i} needs restocking")
    capsys.readouterr()

    code = _run_main(monkeypatch, "search", "widget restocking", "--limit", "2", "--json")
    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out)
    assert len(payload["memories"]) <= 2


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_returns_recently_added_memories(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _run_main(monkeypatch, "add", "first memory")
    _run_main(monkeypatch, "add", "second memory")
    capsys.readouterr()

    code = _run_main(monkeypatch, "list", "--json")
    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out)
    assert payload["count"] == 2
    contents = {m["content"] for m in payload["memories"]}
    assert contents == {"first memory", "second memory"}


def test_list_respects_category_filter(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _run_main(monkeypatch, "add", "a work item", "--category", "work")
    _run_main(monkeypatch, "add", "a personal item", "--category", "personal")
    capsys.readouterr()

    code = _run_main(monkeypatch, "list", "--category", "work", "--json")
    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out)
    assert payload["count"] == 1
    assert payload["memories"][0]["category"] == "work"


def test_list_respects_limit(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    for i in range(5):
        _run_main(monkeypatch, "add", f"note {i}")
    capsys.readouterr()

    code = _run_main(monkeypatch, "list", "--limit", "2", "--json")
    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out)
    assert len(payload["memories"]) == 2


def test_list_markdown_output_by_default(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _run_main(monkeypatch, "add", "a markdown-rendered memory")
    capsys.readouterr()

    code = _run_main(monkeypatch, "list")
    out = capsys.readouterr().out
    assert code == 0
    assert "### Memory" in out


# ---------------------------------------------------------------------------
# Argument errors — clear message, non-zero exit, no traceback
# ---------------------------------------------------------------------------


def test_add_missing_content_arg_exits_nonzero_without_traceback(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    code = _run_main(monkeypatch, "add")
    err = capsys.readouterr().err
    assert code != 0
    assert "Traceback" not in err


def test_unknown_subcommand_falls_through_to_flag_parser_and_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A typo'd subcommand isn't in cli.SUBCOMMANDS, so it falls through to
    the existing flag parser, which rejects it as an unrecognized argument
    (argparse's own well-formed error, not a traceback)."""
    code = _run_main(monkeypatch, "addd", "oops")
    err = capsys.readouterr().err
    assert code != 0
    assert "Traceback" not in err


def test_search_bad_limit_reports_validation_error_not_traceback(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """MemorySearchInput.limit is bounded (ge=1, le=100, see models.py) --
    a value outside that range surfaces as a clean CLI error, not a
    raised ValidationError traceback."""
    code = _run_main(monkeypatch, "search", "anything", "--limit", "0")
    captured = capsys.readouterr()
    assert code != 0
    assert "Traceback" not in captured.err
    assert "Error" in captured.err


def test_list_bad_limit_reports_validation_error_not_traceback(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """MemoryListInput.limit is bounded (ge=1, le=100) the same way."""
    code = _run_main(monkeypatch, "list", "--limit", "0")
    captured = capsys.readouterr()
    assert code != 0
    assert "Traceback" not in captured.err
    assert "Error" in captured.err


def test_unexpected_exception_is_reported_cleanly_not_as_a_traceback(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """run_cli's outer try/except is the last line of defense: any exception
    a handler doesn't already catch (e.g. a genuine DB error) must still
    become a clean 'Error: ...' message and exit 1, never a raw traceback
    reaching the terminal."""
    import remind_me_mcp.tools.crud as crud_mod

    async def _boom(params):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(crud_mod, "memory_add", _boom)

    code = _run_main(monkeypatch, "add", "content that triggers a raised exception")
    captured = capsys.readouterr()
    assert code != 0
    assert "Traceback" not in captured.err
    assert "Error" in captured.err
    assert "database is locked" in captured.err


def test_add_content_too_long_reports_validation_error_not_traceback(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Pydantic input validation (MemoryAddInput.content max_length=50000)
    surfaces as a clean CLI error, not a raised ValidationError traceback."""
    code = _run_main(monkeypatch, "add", "x" * 50001)
    captured = capsys.readouterr()
    assert code != 0
    assert "Traceback" not in captured.err
    assert "Error" in captured.err


# ---------------------------------------------------------------------------
# Single-instance lock (issue #126) is NOT acquired by the CLI
# ---------------------------------------------------------------------------


def test_cli_does_not_acquire_the_mcp_server_lock(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Simulate a server already holding MCP_PID_FILE's single-instance lock
    (issue #126) — this process's own PID, which _pid_is_alive() will report
    as alive — and confirm every CLI subcommand still works normally. The
    CLI must never call pid._acquire_mcp_lock / touch MCP_PID_FILE at all."""
    mcp_pid_file = cfg.MCP_PID_FILE
    mcp_pid_file.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "host": "127.0.0.1",
                "port": 8767,
                "url": "http://127.0.0.1:8767",
                "started_at": "2026-01-01T00:00:00+00:00",
            }
        )
    )

    code = _run_main(monkeypatch, "add", "written while a server holds the lock")
    assert code == 0
    capsys.readouterr()

    code = _run_main(monkeypatch, "list", "--json")
    out = capsys.readouterr().out
    assert code == 0
    assert json.loads(out)["count"] == 1

    # The CLI must not have touched the lock file at all.
    data = json.loads(mcp_pid_file.read_text())
    assert data["pid"] == os.getpid()
    assert data["port"] == 8767


def test_cli_never_calls_acquire_mcp_lock(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Belt-and-suspenders: patch _acquire_mcp_lock to fail the test outright
    if the CLI path ever calls it."""
    monkeypatch.setattr(
        main_mod,
        "_acquire_mcp_lock",
        lambda *a, **kw: pytest.fail("CLI must never acquire the MCP server lock"),
    )

    code = _run_main(monkeypatch, "add", "a memory added with the lock function trapped")
    assert code == 0
