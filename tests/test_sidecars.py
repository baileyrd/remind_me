"""
Tests for remind_me_mcp.sidecars — Windows Job Object creation and process
lifecycle for sidecar processes (the SSH tunnel, optionally the dashboard UI).

Windows-only module in practice (``_job()`` no-ops on other platforms), so
these tests skip outright when not running on win32.
"""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only job-object logic")

from remind_me_mcp import sidecars  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_job_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test gets a clean _job_handle so _job() actually runs its body."""
    monkeypatch.setattr(sidecars, "_job_handle", None)


# ---------------------------------------------------------------------------
# _job() — issue #138: restype/argtypes and return-value checks
# ---------------------------------------------------------------------------


def test_job_returns_a_real_handle_on_success() -> None:
    """The happy path: a real Win32 call, not mocked, since CreateJobObjectW
    is safe and side-effect-free to call directly in a test process."""
    handle = sidecars._job()
    assert handle is not None
    assert int(handle) != 0


def test_job_caches_the_handle_across_calls() -> None:
    first = sidecars._job()
    second = sidecars._job()
    assert first == second


def test_job_returns_none_and_logs_when_create_job_object_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression guard for issue #138: CreateJobObjectW returning a falsy
    handle (failure) must be detected and reported, not silently stored as
    a garbage handle.

    Patches just the one function name on the real kernel32 proxy (not the
    whole object): production code does
    ``k32.CreateJobObjectW.restype = ...``, which requires the attribute to
    be a real function object (assignable .restype/.argtypes), not a bound
    method on a plain mock class.
    """
    import ctypes

    def fake_create_job_object_w(*a):
        return 0

    monkeypatch.setattr(ctypes.windll.kernel32, "CreateJobObjectW", fake_create_job_object_w)
    monkeypatch.setattr(ctypes, "GetLastError", lambda: 5)  # ERROR_ACCESS_DENIED

    with caplog.at_level("WARNING"):
        handle = sidecars._job()

    assert handle is None
    assert sidecars._job_handle is None
    assert any("CreateJobObjectW failed" in r.message for r in caplog.records)


def test_job_warns_but_still_returns_handle_when_set_information_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A successful CreateJobObjectW but failed SetInformationJobObject
    still returns the handle (the job object exists, just without
    KILL_ON_JOB_CLOSE) rather than treating the whole thing as fatal --
    but it must be logged, not silently swallowed as it was before #138."""
    import ctypes

    def fake_set_information_job_object(*a):
        return 0  # BOOL failure

    monkeypatch.setattr(
        ctypes.windll.kernel32, "SetInformationJobObject", fake_set_information_job_object
    )
    monkeypatch.setattr(ctypes, "GetLastError", lambda: 87)  # ERROR_INVALID_PARAMETER

    with caplog.at_level("WARNING"):
        handle = sidecars._job()

    assert handle is not None
    assert any("SetInformationJobObject failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _spawn() — AssignProcessToJobObject failure must not crash sidecar startup
# ---------------------------------------------------------------------------


def test_spawn_warns_but_does_not_raise_when_assign_to_job_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression guard for issue #138: a failed AssignProcessToJobObject
    (e.g. this process already sits in a job without breakaway rights) must
    log a warning and let the sidecar keep running -- not raise or silently
    proceed as if the kill-on-exit guarantee held."""
    import ctypes
    from ctypes import wintypes

    monkeypatch.setattr(sidecars, "_procs", {})
    monkeypatch.setattr(sidecars, "_job", lambda: wintypes.HANDLE(1))

    def fake_assign_process_to_job_object(*a):
        return 0

    monkeypatch.setattr(
        ctypes.windll.kernel32, "AssignProcessToJobObject", fake_assign_process_to_job_object
    )
    monkeypatch.setattr(ctypes, "GetLastError", lambda: 5)

    try:
        with caplog.at_level("WARNING"):
            sidecars._spawn("test-sidecar", [sys.executable, "-c", "pass"])
        assert any("AssignProcessToJobObject failed" in r.message for r in caplog.records)
        assert "test-sidecar" in sidecars._procs
    finally:
        spawned = sidecars._procs.pop("test-sidecar", None)
        if spawned is not None:
            spawned.kill()
            spawned.wait(timeout=5)


# ---------------------------------------------------------------------------
# _spawn() — issue #139: reap exited processes, close stderr, no leak on respawn
# ---------------------------------------------------------------------------


def test_spawn_reaps_and_closes_stderr_of_a_prior_exited_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard for issue #139.

    A sidecar that exits between ensure_sidecars ticks used to be respawned
    without ever wait()ing the old Popen or closing its stderr pipe -- a
    persistently-failing sidecar (bad command, unreachable host) leaked one
    zombie/handle every tick, forever. The fixed _spawn must reap and close
    the prior process before starting a new one.
    """
    monkeypatch.setattr(sidecars, "_procs", {})
    monkeypatch.setattr(sidecars, "_job", lambda: None)  # skip job-object wiring

    # A process that exits almost immediately, standing in for "the tunnel
    # command failed" (bad SSH key, unreachable host, etc.).
    sidecars._spawn("flaky", [sys.executable, "-c", "pass"])
    old_proc = sidecars._procs["flaky"]
    old_proc.wait(timeout=5)  # let it actually exit before the next _spawn call
    assert old_proc.poll() is not None

    try:
        sidecars._spawn("flaky", [sys.executable, "-c", "import time; time.sleep(2)"])

        # The prior process must have been reaped (no zombie) and its
        # stderr pipe closed, not merely abandoned.
        assert old_proc.returncode is not None
        assert old_proc.stderr is None or old_proc.stderr.closed

        # A genuinely new process replaced it in _procs.
        assert sidecars._procs["flaky"] is not old_proc
        assert sidecars._procs["flaky"].poll() is None
    finally:
        current = sidecars._procs.pop("flaky", None)
        if current is not None:
            current.kill()
            current.wait(timeout=5)


def test_drain_thread_closes_stderr_when_the_process_exits_on_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even without a follow-up _spawn call to reap it, the drain thread
    itself closes stderr once the process's output pipe hits EOF -- so a
    sidecar that's never respawned (e.g. the server shuts down first)
    doesn't sit holding an open pipe handle for the whole gap."""
    monkeypatch.setattr(sidecars, "_procs", {})
    monkeypatch.setattr(sidecars, "_job", lambda: None)

    sidecars._spawn("short-lived", [sys.executable, "-c", "pass"])
    proc = sidecars._procs.pop("short-lived")
    proc.wait(timeout=5)

    import time as _time

    deadline = _time.time() + 5
    while proc.stderr is not None and not proc.stderr.closed and _time.time() < deadline:
        _time.sleep(0.05)

    assert proc.stderr is None or proc.stderr.closed
