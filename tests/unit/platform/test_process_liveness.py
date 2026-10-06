"""Unit tests for :mod:`eawf.platform.process_liveness`.

The win32 liveness probe must never deliver a signal. ``os.kill(pid, 0)`` on
win32 is ``GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)``, a console Ctrl+C
that reaches every process sharing the caller's console, so a regression to
it would interrupt the test runner itself. The probe tests therefore run the
caller in a child with its own console and assert both the answers and that
the child survived to report them.
"""

from __future__ import annotations

import ast
import os
import subprocess  # noqa: EAWF024 spawns the real win32 child the backend acts on
import sys
import textwrap
from pathlib import Path

import pytest

from eawf.platform import process_liveness
from eawf.platform.process_liveness import (
    held_process_active,
    hold_process_handle,
    pid_alive,
    process_group_id,
    release_process_handle,
)
from eawf.runtime.runtimes import cancel

_SRC = Path(__file__).resolve().parents[3] / "src" / "eawf"

_STILL_ACTIVE = 259


class _FakeKernel:
    """Stand-in for the kernel32 handle primitives the registry calls."""

    def __init__(self, exit_codes: dict[int, int | None]) -> None:
        self.exit_codes = exit_codes
        self.opened: list[int] = []
        self.closed: list[int] = []

    def open(self, pid: int) -> int | None:
        self.opened.append(pid)
        return pid + 1000 if pid in self.exit_codes else None

    def exit_code(self, handle: int) -> int | None:
        return self.exit_codes[handle - 1000]

    def close(self, handle: int) -> None:
        self.closed.append(handle)


@pytest.fixture
def kernel(monkeypatch: pytest.MonkeyPatch) -> _FakeKernel:
    """Install an empty handle registry and fake kernel32 primitives."""
    fake = _FakeKernel({})
    monkeypatch.setattr(process_liveness, "_HELD_HANDLES", {})
    monkeypatch.setattr(process_liveness, "_open_process_handle", fake.open)
    monkeypatch.setattr(process_liveness, "_handle_exit_code", fake.exit_code)
    monkeypatch.setattr(process_liveness, "_close_process_handle", fake.close)
    return fake


@pytest.fixture
def taskkill_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record every ``subprocess.run`` the win32 kill makes; never spawn."""
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def _hold_on_win32(pid: int) -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sys, "platform", "win32")
        hold_process_handle(pid)


@pytest.mark.parametrize("pgid", [4242, 1, 0, -1])
def test_killpg_refuses_a_pgid_without_a_held_handle(
    kernel: _FakeKernel, taskkill_calls: list[list[str]], pgid: int
) -> None:
    with pytest.raises(ProcessLookupError):
        cancel._win32_killpg(pgid, cancel._HARD_SIGNAL)
    assert taskkill_calls == []


@pytest.mark.parametrize("code", [0, 1, 258, 260, None])
def test_killpg_refuses_a_held_handle_that_is_not_still_active(
    kernel: _FakeKernel,
    taskkill_calls: list[list[str]],
    code: int | None,
) -> None:
    kernel.exit_codes[4242] = code
    process_liveness._HELD_HANDLES[4242] = 5242
    with pytest.raises(ProcessLookupError):
        cancel._win32_killpg(4242, cancel._HARD_SIGNAL)
    assert taskkill_calls == []


def test_killpg_runs_taskkill_for_a_still_active_held_handle(
    kernel: _FakeKernel, taskkill_calls: list[list[str]]
) -> None:
    kernel.exit_codes[4242] = _STILL_ACTIVE
    process_liveness._HELD_HANDLES[4242] = 5242
    cancel._win32_killpg(4242, cancel._HARD_SIGNAL)
    cancel._win32_killpg(4242, int(cancel._SOFT_SIGNAL))
    assert taskkill_calls == [
        ["taskkill", "/PID", "4242", "/T", "/F"],
        ["taskkill", "/PID", "4242", "/T"],
    ]


def test_handle_for_another_pid_does_not_authorise_the_kill(
    kernel: _FakeKernel, taskkill_calls: list[list[str]]
) -> None:
    kernel.exit_codes[4242] = _STILL_ACTIVE
    process_liveness._HELD_HANDLES[4242] = 5242
    with pytest.raises(ProcessLookupError):
        cancel._win32_killpg(4243, cancel._HARD_SIGNAL)
    assert taskkill_calls == []


def test_cancel_process_group_reports_an_unheld_pgid_as_not_delivered(
    kernel: _FakeKernel, taskkill_calls: list[list[str]]
) -> None:
    result = cancel.cancel_process_group(4242, hard=True, killpg=cancel._win32_killpg)
    assert result.delivered is False
    assert taskkill_calls == []


def test_hold_is_a_no_op_off_win32(kernel: _FakeKernel, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    kernel.exit_codes[4242] = _STILL_ACTIVE
    hold_process_handle(4242)
    assert kernel.opened == []
    assert process_liveness._HELD_HANDLES == {}


def test_hold_registers_and_release_closes_the_handle(kernel: _FakeKernel) -> None:
    kernel.exit_codes[4242] = _STILL_ACTIVE
    _hold_on_win32(4242)
    assert process_liveness._HELD_HANDLES == {4242: 5242}
    assert held_process_active(4242) is True
    release_process_handle(4242)
    assert kernel.closed == [5242]
    assert process_liveness._HELD_HANDLES == {}
    assert held_process_active(4242) is False


def test_hold_registers_nothing_when_open_process_refuses(kernel: _FakeKernel) -> None:
    _hold_on_win32(4242)
    assert kernel.opened == [4242]
    assert process_liveness._HELD_HANDLES == {}
    assert held_process_active(4242) is False


def test_release_of_an_unheld_pid_closes_nothing(kernel: _FakeKernel) -> None:
    release_process_handle(4242)
    release_process_handle(0)
    assert kernel.closed == []


def test_release_twice_closes_the_handle_once(kernel: _FakeKernel) -> None:
    kernel.exit_codes[4242] = _STILL_ACTIVE
    _hold_on_win32(4242)
    release_process_handle(4242)
    release_process_handle(4242)
    assert kernel.closed == [5242]


def test_held_process_active_on_an_empty_registry(kernel: _FakeKernel) -> None:
    assert held_process_active(0) is False
    assert held_process_active(-1) is False
    assert held_process_active(4242) is False


@pytest.mark.skipif(sys.platform == "win32", reason="kernel32 exists on win32")
def test_kernel32_is_refused_off_win32() -> None:
    with pytest.raises(NotImplementedError):
        process_liveness._kernel32()


def test_non_positive_pid_is_not_alive() -> None:
    assert pid_alive(0) is False
    assert pid_alive(-1) is False


def test_own_pid_is_alive() -> None:
    assert pid_alive(os.getpid()) is True


@pytest.mark.skipif(sys.platform != "win32", reason="win32 process-group stand-in")
def test_process_group_id_is_the_pid_on_win32() -> None:
    assert process_group_id(4242) == 4242


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX getpgid passthrough")
def test_process_group_id_matches_getpgid_on_posix() -> None:
    assert process_group_id(os.getpid()) == os.getpgid(os.getpid())


@pytest.mark.skipif(sys.platform != "win32", reason="console Ctrl+C hazard is win32-only")
def test_probe_answers_without_signalling_the_console(tmp_path: Path) -> None:
    script = tmp_path / "probe.py"
    script.write_text(
        textwrap.dedent(
            """
            import subprocess, sys
            from eawf.platform.process_liveness import pid_alive

            sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                alive = pid_alive(sleeper.pid)
                running = sleeper.poll() is None
            finally:
                sleeper.kill()
                sleeper.wait()
            gone = pid_alive(sleeper.pid)
            print(f"alive={alive} running={running} gone={gone}")
            """
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        timeout=60,
        check=False,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert result.stdout.decode().strip() == "alive=True running=True gone=False"


def _kill_zero_calls(path: Path) -> list[int]:
    """Return the line numbers of ``os.kill(<pid>, 0)`` calls in *path*."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "kill"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
            and len(node.args) == 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == 0
        ):
            lines.append(node.lineno)
    return lines


def test_every_kill_zero_probe_has_a_win32_branch_first() -> None:
    """Each ``os.kill(pid, 0)`` sits in a function that routes win32 elsewhere.

    The guard is textual: the enclosing function must test
    ``sys.platform == "win32"`` before the call.
    """
    offenders = []
    for path in sorted(_SRC.rglob("*.py")):
        calls = _kill_zero_calls(path)
        if not calls:
            continue
        source = path.read_text(encoding="utf-8").splitlines()
        for line in calls:
            head = "\n".join(source[max(0, line - 25) : line])
            if 'sys.platform == "win32"' not in head and 'sys.platform != "win32"' not in head:
                offenders.append(f"{path.relative_to(_SRC)}:{line}")
    assert offenders == [], offenders
