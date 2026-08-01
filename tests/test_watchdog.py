"""
Tests for remind_me_mcp.watchdog — the slow-call stack-dump watchdog (issue #128).

A stuck tool call previously had no visible symptom beyond "the MCP call
timed out" -- nothing in the server said where it was stuck, and diagnosing
a real incident required manually installing and running py-spy against the
live process. faulthandler.dump_traceback_later closes that gap for free
(stdlib, no new dependency) by dumping every thread's stack from a separate
OS thread -- which is why it works even against a thread blocked in
synchronous CPU-bound code, unlike an asyncio-based timer.

These tests stub faulthandler itself (arming a *real* timer per test would
be slow, flaky, and pollute other tests' stderr) -- the behavior under test
is the reference-counting logic around it, not faulthandler's own
correctness.
"""

from __future__ import annotations

from typing import Any

import pytest

from remind_me_mcp import watchdog


@pytest.fixture(autouse=True)
def _reset_watchdog_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test gets a clean slate: usable=True, threshold on, counter at 0."""
    monkeypatch.setattr(watchdog, "_usable", True)
    monkeypatch.setattr(watchdog, "_inflight", 0)
    monkeypatch.setattr(watchdog, "SLOW_CALL_SECONDS", 30.0)


@pytest.fixture()
def fake_faulthandler(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {"armed": [], "canceled": []}
    monkeypatch.setattr(
        watchdog.faulthandler,
        "dump_traceback_later",
        lambda seconds, **kw: calls["armed"].append((seconds, kw)),
    )
    monkeypatch.setattr(
        watchdog.faulthandler,
        "cancel_dump_traceback_later",
        lambda: calls["canceled"].append(True),
    )
    return calls


def test_arm_starts_the_watchdog_on_first_call(fake_faulthandler: dict) -> None:
    watchdog.arm()
    assert len(fake_faulthandler["armed"]) == 1
    assert fake_faulthandler["armed"][0][0] == 30.0
    assert fake_faulthandler["armed"][0][1]["repeat"] is True


def test_disarm_cancels_once_the_last_call_finishes(fake_faulthandler: dict) -> None:
    watchdog.arm()
    watchdog.disarm()
    assert fake_faulthandler["canceled"] == [True]


def test_a_faster_concurrent_call_does_not_cancel_a_slower_ones_watchdog(
    fake_faulthandler: dict,
) -> None:
    """Regression guard: two calls in flight, the second (faster) one
    finishing first must not cancel the watchdog the first call still needs.
    """
    watchdog.arm()  # slow call starts
    watchdog.arm()  # fast call starts
    assert len(fake_faulthandler["armed"]) == 1  # only armed once (0->1 transition)

    watchdog.disarm()  # fast call finishes
    assert fake_faulthandler["canceled"] == []  # NOT canceled -- slow call still running

    watchdog.disarm()  # slow call finishes
    assert fake_faulthandler["canceled"] == [True]  # now it's safe to cancel


def test_disarm_without_a_matching_arm_does_not_go_negative(
    fake_faulthandler: dict,
) -> None:
    """Defensive floor: an unbalanced disarm (e.g. from a bug elsewhere)
    must not leave the counter negative, which would require two arms to
    ever reach zero again."""
    watchdog.disarm()
    assert watchdog._inflight == 0
    watchdog.arm()
    watchdog.disarm()
    assert fake_faulthandler["canceled"] == [True]


def test_disabled_when_threshold_is_zero(
    fake_faulthandler: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watchdog, "SLOW_CALL_SECONDS", 0.0)
    watchdog.arm()
    watchdog.disarm()
    assert fake_faulthandler["armed"] == []
    assert fake_faulthandler["canceled"] == []


def test_disabled_when_stderr_has_no_usable_fileno(
    fake_faulthandler: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Must never be the reason a tool call fails: an environment where
    faulthandler can't attach to stderr (some test harnesses' captured
    streams) is a silent no-op, not an error."""
    monkeypatch.setattr(watchdog, "_usable", False)
    watchdog.arm()
    watchdog.disarm()
    assert fake_faulthandler["armed"] == []


def test_a_faulthandler_error_never_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """arm()/disarm() must never raise, even if faulthandler itself does --
    a debugging aid must never be the reason a real tool call fails."""
    monkeypatch.setattr(
        watchdog.faulthandler,
        "dump_traceback_later",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(
        watchdog.faulthandler,
        "cancel_dump_traceback_later",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    watchdog.arm()  # must not raise
    watchdog.disarm()  # must not raise


def test_status_reports_enabled_threshold_and_inflight_count(
    fake_faulthandler: dict,
) -> None:
    assert watchdog.status() == {
        "enabled": True,
        "threshold_seconds": 30.0,
        "calls_in_flight": 0,
    }
    watchdog.arm()
    assert watchdog.status()["calls_in_flight"] == 1
    watchdog.disarm()


def test_status_reports_disabled_when_threshold_is_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(watchdog, "SLOW_CALL_SECONDS", 0.0)
    assert watchdog.status()["enabled"] is False


def test_usable_check_is_cached_after_the_first_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Avoids re-probing sys.stderr.fileno() on every single tool call."""
    monkeypatch.setattr(watchdog, "_usable", None)
    probes = []

    class _FakeStderr:
        def fileno(self) -> int:
            probes.append(1)
            return 2

    monkeypatch.setattr(watchdog.sys, "stderr", _FakeStderr())

    assert watchdog._watchdog_usable() is True
    assert watchdog._watchdog_usable() is True
    assert len(probes) == 1
