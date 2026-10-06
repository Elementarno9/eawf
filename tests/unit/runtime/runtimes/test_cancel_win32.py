"""win32 process-tree cancel: the budget HALT and agent kill must stop a runaway.

win32 has no process groups. The spawn seam records a child's own pid as its
pgid, and the cancel ladder reaches the tree under it with ``taskkill /T``
(``/F`` on the hard rung) instead of raising ``NotImplementedError``.
"""

from __future__ import annotations

import signal
import subprocess  # noqa: EAWF024 spawns the real win32 child the backend acts on
import sys
import time

import pytest

from eawf.platform.process_liveness import (
    hold_process_handle,
    pid_alive,
    release_process_handle,
)
from eawf.runtime.runtimes import cancel

_WIN32_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="win32 cancel backend")

_PARENT = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "print(child.pid, flush=True)\n"
    "time.sleep(60)\n"
)


def _spawn_tree() -> tuple[subprocess.Popen[bytes], int]:
    """Start a parent that starts a grandchild; return the parent and grandchild pid."""
    parent = subprocess.Popen(
        [sys.executable, "-c", _PARENT],
        stdout=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
    )
    assert parent.stdout is not None
    grandchild = int(parent.stdout.readline())
    return parent, grandchild


def _wait_dead(pid: int, deadline_s: float = 10.0) -> bool:
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


@_WIN32_ONLY
def test_the_group_probe_is_bound_off_the_posix_stub() -> None:
    assert cancel._GETPGID is cancel._win32_getpgid
    assert cancel._KILLPG is cancel._win32_killpg
    assert cancel._HARD_SIGNAL == 9


@_WIN32_ONLY
def test_cancel_with_grace_stops_the_whole_tree() -> None:
    parent, grandchild = _spawn_tree()
    # The kill only reaches a tree whose root this process holds a handle to,
    # as the spawn seam does for every runtime child.
    hold_process_handle(parent.pid)
    try:
        result = cancel.cancel_with_grace(parent.pid, grace_seconds=0.5, poll_interval=0.05)
        assert result.sigterm_sent
        assert _wait_dead(parent.pid)
        assert _wait_dead(grandchild), "taskkill /T left the grandchild running"
    finally:
        for pid in (parent.pid, grandchild):
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        parent.wait(timeout=10)
        release_process_handle(parent.pid)


@_WIN32_ONLY
def test_a_gone_group_is_reported_not_raised() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"], creationflags=subprocess.CREATE_NO_WINDOW
    )
    proc.wait(timeout=30)
    assert _wait_dead(proc.pid)
    with pytest.raises(ProcessLookupError):
        cancel._win32_getpgid(proc.pid)
    with pytest.raises(ProcessLookupError):
        cancel._win32_killpg(proc.pid, cancel._HARD_SIGNAL)


class _Completed:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def _fake_taskkill(monkeypatch: pytest.MonkeyPatch, returncode: int) -> list[list[str]]:
    """Stub the handle check and ``subprocess.run``; return the argv log."""
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> _Completed:
        calls.append(argv)
        return _Completed(returncode)

    monkeypatch.setattr(cancel, "held_process_active", lambda _pid: True)
    monkeypatch.setattr(cancel.subprocess, "run", fake_run)
    return calls


def test_hermetic_soft_cancel_runs_taskkill_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_taskkill(monkeypatch, 0)
    assert cancel._win32_killpg(4242, signal.SIGTERM) is None
    assert calls == [["taskkill", "/PID", "4242", "/T"]]


def test_hermetic_hard_cancel_appends_force(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_taskkill(monkeypatch, 0)
    assert cancel._win32_killpg(4242, cancel._HARD_SIGNAL) is None
    assert calls == [["taskkill", "/PID", "4242", "/T", "/F"]]


def test_hermetic_exit_128_raises_process_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_taskkill(monkeypatch, 128)
    with pytest.raises(ProcessLookupError):
        cancel._win32_killpg(4242, cancel._HARD_SIGNAL)


def test_hermetic_unheld_pgid_never_reaches_taskkill(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_taskkill(monkeypatch, 0)
    monkeypatch.setattr(cancel, "held_process_active", lambda _pid: False)
    with pytest.raises(ProcessLookupError):
        cancel._win32_killpg(4242, signal.SIGTERM)
    assert calls == []
