"""Sidecar processes that live and die with the MCP server.

Ensures the hub SSH tunnel (and optionally the dashboard UI) are running,
spawning them into a Windows Job object with KILL_ON_JOB_CLOSE so they are
torn down by the OS when this server process exits — no exit hooks needed,
robust even against hard crashes. Called from the sync loop, so a sidecar
lost to another server's exit is respawned within one sync interval.

Config (env):
  REMIND_ME_TUNNEL      full command line for the tunnel (e.g. ssh -N -L ...).
                        Unset = no tunnel management.
  REMIND_ME_SIDECAR_UI  "1" to also keep the dashboard UI alive on UI_PORT.
"""

from __future__ import annotations

import logging
import os
import shlex
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse

log = logging.getLogger("remind_me_mcp.sidecars")

TUNNEL_CMD = os.environ.get("REMIND_ME_TUNNEL", "")
SIDECAR_UI = os.environ.get("REMIND_ME_SIDECAR_UI", "").lower() in ("true", "1", "yes")

_procs: dict[str, subprocess.Popen] = {}
_job_handle = None  # keep alive for the life of the process


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _job():
    """Lazily create a Job object that kills its processes when we die."""
    global _job_handle
    if sys.platform != "win32" or _job_handle is not None:
        return _job_handle
    import ctypes
    from ctypes import wintypes

    # Names below mirror the Win32 API exactly (winnt.h / jobapi2.h) rather
    # than Python naming conventions, so they stay greppable against MSDN.
    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):  # noqa: N801
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):  # noqa: N801
        _fields_ = [(n, ctypes.c_uint64) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):  # noqa: N801
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000  # noqa: N806
    JobObjectExtendedLimitInformation = 9  # noqa: N806
    k32 = ctypes.windll.kernel32
    h = k32.CreateJobObjectW(None, None)
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    k32.SetInformationJobObject(h, JobObjectExtendedLimitInformation,
                                ctypes.byref(info), ctypes.sizeof(info))
    _job_handle = h
    return h


def _spawn(name: str, cmd: list[str], env: dict | None = None) -> None:
    prev = _procs.get(name)
    if prev is not None and prev.poll() is None:
        return  # still starting up / running
    # Claude Desktop launches MCP servers with a minimal env. Windows OpenSSH
    # exits 255 with no output if ProgramData is unset, so repair the basics.
    full_env = dict(os.environ if env is None else env)
    sysdrive = full_env.get("SYSTEMDRIVE", "C:")
    full_env.setdefault("ProgramData", rf"{sysdrive}\ProgramData")
    full_env.setdefault("ALLUSERSPROFILE", full_env["ProgramData"])
    sysroot = full_env.get("SYSTEMROOT", rf"{sysdrive}\WINDOWS")
    full_env.setdefault("ComSpec", rf"{sysroot}\system32\cmd.exe")
    proc = subprocess.Popen(
        cmd, env=full_env,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )

    def _drain(p=proc, n=name):
        for line in iter(p.stderr.readline, b""):
            log.info("Sidecar %s stderr: %s", n, line.decode(errors="replace").rstrip())

    import threading
    threading.Thread(target=_drain, daemon=True, name=f"sidecar-{name}-stderr").start()
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.AssignProcessToJobObject(_job(), int(proc._handle))
    _procs[name] = proc
    log.info("Sidecar %s started (pid %d)", name, proc.pid)


def ensure_sidecars() -> None:
    """Idempotent: start any configured sidecar whose port is not answering."""
    from remind_me_mcp.config import HUB_URL, UI_PORT

    if TUNNEL_CMD and HUB_URL:
        u = urlparse(HUB_URL)
        port = u.port or (443 if u.scheme == "https" else 80)
        host = u.hostname or "127.0.0.1"
        if not _port_open(port, host):
            _spawn("tunnel", shlex.split(TUNNEL_CMD, posix=sys.platform != "win32"))
            # give ssh a moment so the first sync after startup succeeds
            for _ in range(20):
                if _port_open(port, host):
                    break
                rc = _procs["tunnel"].poll()
                if rc is not None:
                    log.warning("Sidecar tunnel exited rc=%s (0x%x)", rc, rc & 0xFFFFFFFF)
                    break
                time.sleep(0.5)

    if SIDECAR_UI and not _port_open(UI_PORT):
        env = {k: v for k, v in os.environ.items() if k != "REMIND_ME_HUB_URL"}
        env["REMIND_ME_SIDECAR_UI"] = "0"
        _spawn("ui", [sys.executable, "-m", "remind_me_mcp",
                      "--serve-ui", "--ui-host", "127.0.0.1", "--ui-port", str(UI_PORT)],
               env=env)
    # ponytail: sidecars ride this server's job — if this server exits first,
    # a surviving server respawns them on its next sync tick (≤ SYNC_INTERVAL gap).
