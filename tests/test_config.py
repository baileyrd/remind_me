"""
Tests for remind_me_mcp.config robustness (HY-06).

Covers guarded integer environment parsing (_env_int) and the guarantee that
importing the package does not call logging.basicConfig (root logging setup
belongs to the __main__ entrypoint).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from remind_me_mcp.config import _env_int

if TYPE_CHECKING:
    import pytest

# ---------------------------------------------------------------------------
# _env_int — guarded integer environment parsing
# ---------------------------------------------------------------------------


def test_env_int_unset_returns_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset variable returns the default."""
    monkeypatch.delenv("REMIND_ME_TEST_INT", raising=False)
    assert _env_int("REMIND_ME_TEST_INT", 42) == 42


def test_env_int_valid_value_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid integer string is parsed."""
    monkeypatch.setenv("REMIND_ME_TEST_INT", "1234")
    assert _env_int("REMIND_ME_TEST_INT", 42) == 1234


def test_env_int_blank_returns_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blank/whitespace value returns the default."""
    monkeypatch.setenv("REMIND_ME_TEST_INT", "   ")
    assert _env_int("REMIND_ME_TEST_INT", 42) == 42


def test_env_int_garbage_falls_back_with_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A malformed value logs a warning and returns the default (HY-06)."""
    monkeypatch.setenv("REMIND_ME_TEST_INT", "not-a-number")
    with caplog.at_level(logging.WARNING, logger="remind_me_mcp.config"):
        assert _env_int("REMIND_ME_TEST_INT", 42) == 42
    assert any("REMIND_ME_TEST_INT" in rec.message for rec in caplog.records)


def test_env_int_negative_value_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative integers parse fine (validation is the consumer's concern)."""
    monkeypatch.setenv("REMIND_ME_TEST_INT", "-5")
    assert _env_int("REMIND_ME_TEST_INT", 42) == -5


# ---------------------------------------------------------------------------
# HY-06: importing the package must not configure root logging
# ---------------------------------------------------------------------------


def test_config_module_does_not_call_basicconfig() -> None:
    """config.py must not invoke logging.basicConfig at import time (HY-06).

    Source-level check: the runtime root logger may legitimately have handlers
    from pytest or the host application, so the assertion targets the module
    text rather than global logging state.
    """
    import inspect

    import remind_me_mcp.config as cfg

    assert "logging.basicConfig(" not in inspect.getsource(cfg)
