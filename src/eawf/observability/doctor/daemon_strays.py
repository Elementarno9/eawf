"""Find and stop eawfd processes that no client can reach any more.

A daemon is reachable only through the socket it bound in its runtime dir.
One whose runtime dir was removed under it (a test's tmp dir), whose socket
no longer answers as it, or that still sits on the per-user address every
tree shared before runtime dirs were keyed per tree, keeps running while no
client will ever dial it again. The doctor lists those processes and
``eawf doctor --fix`` stops them. A daemon whose live socket sits under
another HOME's runtime base belongs to that HOME's clients and is left alone.

POSIX only: the scan reads ``ps`` for the process table and ``lsof`` for
the socket each process has bound. Windows daemons listen on a named pipe
and are not scanned.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.observability.doctor.models import CheckResult
from eawf.runtime.daemon.runtime_dir import TREES_DIRNAME, runtime_base_dir, runtime_dir
from eawf.runtime.daemon.spawn import daemon_pid_if_ready

logger = logging.getLogger(__name__)

#: The module a spawned daemon runs as (``python -m``).
DAEMON_MODULE = "eawf.runtime.daemon.main"

#: The console script a supervised daemon runs as.
DAEMON_SCRIPT = "eawfd"

#: The socket name every daemon binds in its runtime dir.
DAEMON_SOCKET_NAME = "eawfd.sock"

#: Names a runtime base dir takes: ``~/.eawfd`` and ``$XDG_RUNTIME_DIR/eawfd``.
_RUNTIME_BASE_NAMES = frozenset({".eawfd", "eawfd"})

#: A daemon younger than this may still be replaying its WAL before it binds,
#: so an unbound one is not yet a stray.
STARTUP_GRACE_SECONDS = 60.0

#: How long a stray gets to exit on SIGTERM before it is killed.
STOP_TIMEOUT_SECONDS = 5.0

_STOP_POLL_SECONDS = 0.05

#: Timeout for one ``ps`` or ``lsof`` call.
_SCAN_TIMEOUT_SECONDS = 10.0

StrayReason = Literal["unbound", "address_gone", "address_lost", "superseded_address"]

_REASON_TEXT: dict[StrayReason, str] = {
    "unbound": "holds no daemon socket",
    "address_gone": "its socket path no longer exists",
    "address_lost": "its socket no longer answers as this process",
    "superseded_address": "bound to the per-user address clients no longer dial",
}


class StrayDaemon(BaseModel):
    """One eawfd process no client can reach.

    Attributes:
        pid: The process id.
        started: The process start time as ``ps`` prints it; stopping
            re-reads it so a reused pid is never signalled.
        socket: The daemon socket the process has bound, if any.
        reason: Why no client reaches it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    pid: int = Field(gt=0)
    started: str = Field(min_length=1)
    socket: str | None = None
    reason: StrayReason


class _DaemonProcess(BaseModel):
    """One row of the process table that runs the daemon."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pid: int = Field(gt=0)
    age_seconds: float = Field(ge=0.0)
    started: str = Field(min_length=1)


def _run(argv: list[str]) -> str | None:
    """Return *argv*'s stdout, or ``None`` when the tool is missing or times out."""
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            check=False,
            text=True,
            timeout=_SCAN_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    return completed.stdout


def _etime_seconds(etime: str) -> float:
    """Return the seconds in a ``ps`` elapsed time, ``[[dd-]hh:]mm:ss``."""
    days, _, clock = etime.rpartition("-")
    seconds = 0.0
    for part in clock.split(":"):
        seconds = seconds * 60 + float(part)
    return seconds + (int(days) * 86400 if days else 0)


def _is_daemon_command(command: str) -> bool:
    """Return whether *command* is a daemon launch, not a tool that names the daemon.

    The daemon starts as ``<python> -m eawf.runtime.daemon.main``, as the ``eawfd``
    console script run directly, or as that script run by its interpreter. A
    ``tail -f eawfd`` or ``pgrep eawfd`` only mentions it.
    """
    argv = command.split()
    if not argv:
        return False
    if Path(argv[0]).name == DAEMON_SCRIPT:
        return True
    if not Path(argv[0]).name.lower().startswith("python"):
        return False
    rest = argv[1:]
    return rest[:2] == ["-m", DAEMON_MODULE] or (bool(rest) and Path(rest[0]).name == DAEMON_SCRIPT)


def _daemon_processes() -> list[_DaemonProcess] | None:
    """Return this user's daemon processes, or ``None`` when ``ps`` cannot run."""
    out = _run(["ps", "-A", "-o", "pid=,uid=,etime=,lstart=,command="])
    if out is None:
        return None
    uid = os.getuid()
    processes: list[_DaemonProcess] = []
    for line in out.splitlines():
        # ``lstart`` prints five fields, e.g. ``Wed Sep 30 08:47:05 2026``.
        fields = line.split(None, 8)
        if len(fields) < 9 or not _is_daemon_command(fields[8]):
            continue
        try:
            pid, owner = int(fields[0]), int(fields[1])
            age = _etime_seconds(fields[2])
        except ValueError:
            continue
        if owner != uid or pid == os.getpid():
            continue
        processes.append(_DaemonProcess(pid=pid, age_seconds=age, started=" ".join(fields[3:8])))
    return processes


def _bound_sockets(pids: list[int]) -> dict[int, str] | None:
    """Return the daemon socket each of *pids* has bound, or ``None`` without ``lsof``."""
    if not pids:
        return {}
    out = _run(["lsof", "-a", "-p", ",".join(str(pid) for pid in pids), "-U", "-Fpn"])
    if out is None:
        return None
    sockets: dict[int, str] = {}
    current: int | None = None
    for line in out.splitlines():
        if line.startswith("p"):
            current = int(line[1:])
            continue
        if current is None or not line.startswith("n"):
            continue
        # Linux lsof appends `` type=STREAM`` to a socket's name.
        name = line[1:].split(" type=")[0]
        if Path(name).name == DAEMON_SOCKET_NAME:
            sockets[current] = name
    return sockets


def _under_another_base(runtime: Path) -> bool:
    """Return whether *runtime* is a runtime dir under another HOME's runtime base."""
    base = runtime.parent.parent if runtime.parent.name == TREES_DIRNAME else runtime
    # resolve both sides: lsof reports /private/tmp where HOME may say /tmp
    return base.name in _RUNTIME_BASE_NAMES and base.resolve() != runtime_base_dir().resolve()


def _stray_reason(process: _DaemonProcess, socket: str | None) -> StrayReason | None:
    """Return why *process* is unreachable, or ``None`` when a client reaches it."""
    if socket is None:
        return None if process.age_seconds < STARTUP_GRACE_SECONDS else "unbound"
    path = Path(socket)
    if not path.exists():
        return "address_gone"
    # another HOME's clients still dial a socket that exists; stopping its
    # daemon from this HOME would cut a live session off
    if _under_another_base(path.parent):
        return None
    if daemon_pid_if_ready(path.parent) != process.pid:
        return "address_lost"
    # runtime_dir() is the doctor's own EAWF_RUNTIME_DIR pin when set, so an
    # operator pinned to the base dir keeps its daemon. The daemon's own
    # environment cannot say this: the spawn pins every daemon to its dir.
    if path.parent == runtime_base_dir() and runtime_dir() != path.parent:
        return "superseded_address"
    return None


def find_stray_daemons() -> list[StrayDaemon] | None:
    """Return this user's unreachable daemon processes, ordered by pid.

    Returns:
        The strays, or ``None`` when the platform or a missing ``ps`` or
        ``lsof`` leaves the process table unreadable.
    """
    if sys.platform == "win32":
        return None
    processes = _daemon_processes()
    if processes is None:
        return None
    sockets = _bound_sockets([process.pid for process in processes])
    if sockets is None:
        return None
    strays: list[StrayDaemon] = []
    for process in sorted(processes, key=lambda row: row.pid):
        socket = sockets.get(process.pid)
        reason = _stray_reason(process, socket)
        if reason is not None:
            strays.append(
                StrayDaemon(pid=process.pid, started=process.started, socket=socket, reason=reason)
            )
    logger.info(f"find_stray_daemons scanned={len(processes)} strays={len(strays)}")
    return strays


def _pid_alive(pid: int) -> bool:
    """Return whether *pid* still names a live process of this user."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_exit(pid: int, timeout_seconds: float) -> bool:
    """Return whether *pid* exited within *timeout_seconds*."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(_STOP_POLL_SECONDS)
    return not _pid_alive(pid)


def stop_stray_daemon(stray: StrayDaemon) -> bool:
    """Stop *stray* if the process table still shows it as the same stray.

    The daemon gets SIGTERM so it drains and removes its own entries, and
    SIGKILL only when it has not exited within :data:`STOP_TIMEOUT_SECONDS`;
    its WAL replays whatever the kill interrupts on the next boot.

    Args:
        stray: A stray from :func:`find_stray_daemons`.

    Returns:
        ``True`` when the process was stopped, ``False`` when it had
        already gone or is no longer the same stray.
    """
    current = find_stray_daemons() or []
    if stray not in current:
        logger.info(f"stop_stray_daemon skipped pid={stray.pid} reason=gone_or_changed")
        return False
    os.kill(stray.pid, signal.SIGTERM)
    if not _wait_for_exit(stray.pid, STOP_TIMEOUT_SECONDS):
        logger.warning(f"stop_stray_daemon sigkill pid={stray.pid}")
        os.kill(stray.pid, signal.SIGKILL)
        _wait_for_exit(stray.pid, STOP_TIMEOUT_SECONDS)
    logger.info(f"stop_stray_daemon stopped pid={stray.pid} reason={stray.reason!r}")
    return True


def describe_stray(stray: StrayDaemon) -> str:
    """Return one operator-facing line naming *stray* and why it is one."""
    return f"pid {stray.pid} (started {stray.started}): {_REASON_TEXT[stray.reason]}"


def check_stray_daemons() -> CheckResult:
    """Report eawfd processes no client can reach.

    Returns:
        ``warn`` naming each stray and the repair, else ``ok``.
    """
    name = "stray_daemons"
    strays = find_stray_daemons()
    if strays is None:
        return CheckResult(
            name=name,
            status="ok",
            detail="stray daemon scan unavailable (needs ps and lsof on POSIX)",
        )
    if not strays:
        return CheckResult(name=name, status="ok", detail="no stray eawfd processes")
    listed = "; ".join(describe_stray(stray) for stray in strays)
    return CheckResult(
        name=name,
        status="warn",
        detail=f"{len(strays)} stray eawfd process(es): {listed}; run `eawf doctor --fix`",
    )


__all__ = [
    "StrayDaemon",
    "check_stray_daemons",
    "describe_stray",
    "find_stray_daemons",
    "stop_stray_daemon",
]
