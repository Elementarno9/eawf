"""Unit tests for the Doctor-mode stdin-isolation guard (P30-I16-W29).

The Doctor-mode health gather fans out to blocking subprocesses (the
instrument version-probes + a per-wave ``git log`` drift scan) that run
``subprocess.run`` with the default ``stdin=None`` -- so each child inherits
the parent's fd 0 (the controlling TTY when the gather runs inside the live
TUI). On a graphics terminal a child that touches that TTY can trigger an
escape-sequence reply that leaks back into the App's stdin as a synthetic key
(``c`` -> config, ``?`` -> help) the instant the operator enters Doctor mode.

:func:`~eawf.observability.doctor.checks.detached_tty_stdin` closes that at the
root by pointing process fd 0 at ``/dev/null`` for the gather's duration, so
every probe child inherits a dead stdin and can solicit no TTY reply. These
tests pin the contract:

* inside the block, a ``subprocess.run`` child (default ``stdin=None``)
  inherits ``/dev/null`` -- a ``cat`` sees immediate EOF, not the test's stdin;
* the original fd 0 is restored after the block, including on error;
* the gather entry (:func:`gather_doctor_health`) runs its probe + drift fan-out
  under the guard (so the ``subprocess.run`` children never see a live stdin).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from eawf.observability.doctor import checks


def _stat_of_fd(fd: int) -> tuple[int, int]:
    """Return the ``(st_dev, st_ino)`` identity of an open fd."""
    st = os.fstat(fd)
    return (st.st_dev, st.st_ino)


def test_child_inherits_devnull_stdin_inside_block() -> None:
    """A default-``stdin`` child inside the block reads ``/dev/null`` (EOF).

    The behavioural heart of the fix: with fd 0 detached, a subprocess that
    reads its stdin gets an immediate EOF (empty read) rather than inheriting
    the parent's live stdin. We run ``cat`` with the DEFAULT ``stdin`` (no
    explicit ``stdin=`` kwarg) so the test exercises the exact inheritance
    path the doctor probes use.
    """
    with checks.detached_tty_stdin():
        proc = subprocess.run(
            [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    # /dev/null reads as empty: the child saw a dead stdin, not the parent's.
    assert proc.stdout == ""
    assert proc.returncode == 0


def test_fd0_points_at_devnull_inside_block() -> None:
    """Inside the block, process fd 0 is the ``/dev/null`` device."""
    devnull_id = _stat_of_fd(os.open(os.devnull, os.O_RDONLY))
    # The freshly opened fd above is distinct from fd 0; identity is by device.
    with checks.detached_tty_stdin():
        assert _stat_of_fd(0) == devnull_id


def test_fd0_restored_after_block() -> None:
    """The original fd 0 identity is restored once the block exits."""
    before = _stat_of_fd(0)
    with checks.detached_tty_stdin():
        # Detached to /dev/null in here (covered by the test above).
        pass
    assert _stat_of_fd(0) == before


def test_fd0_restored_on_error() -> None:
    """fd 0 is restored even when the block raises."""
    before = _stat_of_fd(0)
    with pytest.raises(RuntimeError, match="boom"), checks.detached_tty_stdin():
        raise RuntimeError("boom")
    assert _stat_of_fd(0) == before


def test_no_devnull_degrades_to_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host with no ``/dev/null`` degrades to a no-op rather than raising.

    The guard must never abort the render loop on an exotic host: when the
    ``/dev/null`` open fails, the block body still runs and fd 0 is untouched.
    """

    def fake_open(path: str, flags: int, *args: object) -> int:
        if path == os.devnull:
            raise OSError("no devnull on this host")
        return os.open(path, flags, *args)

    monkeypatch.setattr(checks.os, "open", fake_open)
    before = _stat_of_fd(0)
    ran = False
    with checks.detached_tty_stdin():
        ran = True
        # No detach happened, so fd 0 is the original.
        assert _stat_of_fd(0) == before
    assert ran is True
    assert _stat_of_fd(0) == before


def test_gather_runs_probe_fanout_under_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """:func:`gather_doctor_health` runs its subprocess fan-out under the guard.

    Proves the wiring, not just the helper: a stub ``run_all`` records whether
    fd 0 was detached to ``/dev/null`` at the moment the doctor checks run --
    the exact instant the real probes would spawn their TTY-touching children.
    """
    from eawf.surfaces.tui.modes.doctor import gather_doctor_health

    devnull_id = _stat_of_fd(os.open(os.devnull, os.O_RDONLY))
    seen: dict[str, Any] = {}

    def fake_run_all(*, workspace: Path | None) -> list[checks.CheckResult]:
        seen["fd0"] = _stat_of_fd(0)
        return [checks.CheckResult(name="tools_available", status="ok", detail="stub")]

    monkeypatch.setattr(checks, "run_all", fake_run_all)
    # Drift + events read no live state here; point at an empty tmp tree.
    health = gather_doctor_health(workspace=tmp_path, state_path=tmp_path / "state.json")

    assert seen["fd0"] == devnull_id, "run_all must execute with fd 0 detached to /dev/null"
    assert health.rows  # the fold still produced a view
