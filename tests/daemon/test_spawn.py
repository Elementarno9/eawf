"""Tests for :func:`eawf.daemon.spawn.auto_spawn_daemon`.

These tests avoid actually fork-execing the daemon process for the
core idempotency / stale-PID checks by monkeypatching the spawn
primitive — we drive ``auto_spawn_daemon`` with synthetic PID files
and process-liveness probes so the suite stays hermetic. Real
fork+exec coverage lives in the cold-spawn benchmark under
``benches/daemon_cold_spawn.py``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from eawf.daemon import spawn as spawn_mod
from eawf.daemon.spawn import DaemonSpawnTimeout, auto_spawn_daemon

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="POSIX-only spawn tests; windows path is covered in W09",
)


def _write_pid_file(runtime_dir: Path, pid: int) -> Path:
    pid_file = runtime_dir / "eawfd.pid"
    pid_file.write_text(f"{pid}\n1\n2026-05-19T00:00:00+00:00\n", encoding="utf-8")
    return pid_file


def test_auto_spawn_returns_existing_pid_when_alive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PID file exists + holder alive + owned by current UID → no spawn."""
    runtime_dir = tmp_path / "rt"
    runtime_dir.mkdir()
    # Use the test process's own PID so kill(0) reports alive without
    # actually killing anything.
    _write_pid_file(runtime_dir, os.getpid())

    spawn_called: list[bool] = []

    def _fake_spawn(_runtime_dir: Path) -> None:
        spawn_called.append(True)

    monkeypatch.setattr(spawn_mod, "_spawn_posix", _fake_spawn)
    monkeypatch.setattr(spawn_mod, "_wait_for_socket", lambda *_args, **_kwargs: True)
    pid = auto_spawn_daemon(runtime_dir)
    assert pid == os.getpid()
    assert spawn_called == []


def test_auto_spawn_detects_stale_pid_and_respawns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PID file present, holder dead → stale; spawn helper is called."""
    runtime_dir = tmp_path / "rt"
    runtime_dir.mkdir()
    # Use a PID astronomically unlikely to exist; ``kill(0)`` will
    # raise :class:`ProcessLookupError` so the helper treats it as stale.
    stale_pid = 999_999_999
    _write_pid_file(runtime_dir, stale_pid)

    spawn_called: list[bool] = []

    def _fake_spawn(_runtime_dir: Path) -> None:
        spawn_called.append(True)
        # Simulate the freshly spawned daemon writing its pid file +
        # opening its socket. The test does not actually need a live
        # socket — we override ``_wait_for_socket`` so the helper
        # short-circuits.
        _write_pid_file(runtime_dir, os.getpid())

    monkeypatch.setattr(spawn_mod, "_spawn_posix", _fake_spawn)
    monkeypatch.setattr(spawn_mod, "_wait_for_socket", lambda *_args, **_kwargs: True)
    pid = auto_spawn_daemon(runtime_dir)
    assert pid == os.getpid()
    assert spawn_called == [True]


def test_auto_spawn_no_pid_file_triggers_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing PID file → cold spawn fires."""
    runtime_dir = tmp_path / "rt"
    # Deliberately do not mkdir; the helper must materialise it.
    spawn_called: list[bool] = []

    def _fake_spawn(_runtime_dir: Path) -> None:
        spawn_called.append(True)
        _write_pid_file(runtime_dir, os.getpid())

    monkeypatch.setattr(spawn_mod, "_spawn_posix", _fake_spawn)
    monkeypatch.setattr(spawn_mod, "_wait_for_socket", lambda *_args, **_kwargs: True)
    pid = auto_spawn_daemon(runtime_dir)
    assert pid == os.getpid()
    assert spawn_called == [True]


def test_auto_spawn_timeout_raises_when_socket_never_opens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spawn helper runs but socket never appears → DaemonSpawnTimeout."""
    runtime_dir = tmp_path / "rt"

    def _fake_spawn(_runtime_dir: Path) -> None:
        # No-op: deliberately do NOT write the pid file or open a
        # socket so ``_wait_for_socket`` fails.
        return

    monkeypatch.setattr(spawn_mod, "_spawn_posix", _fake_spawn)
    monkeypatch.setattr(spawn_mod, "_wait_for_socket", lambda *_args, **_kwargs: False)
    with pytest.raises(DaemonSpawnTimeout):
        auto_spawn_daemon(runtime_dir)


def test_auto_spawn_silent_unless_verbose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Cold-spawn writes nothing to stderr unless ``EAWF_VERBOSE=1``."""
    runtime_dir = tmp_path / "rt"

    def _fake_spawn(_runtime_dir: Path) -> None:
        _write_pid_file(runtime_dir, os.getpid())

    monkeypatch.setattr(spawn_mod, "_spawn_posix", _fake_spawn)
    monkeypatch.setattr(spawn_mod, "_wait_for_socket", lambda *_args, **_kwargs: True)
    monkeypatch.delenv("EAWF_VERBOSE", raising=False)

    auto_spawn_daemon(runtime_dir)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_auto_spawn_verbose_prints_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``EAWF_VERBOSE=1`` prints a one-line spawn notice to stderr."""
    runtime_dir = tmp_path / "rt"

    def _fake_spawn(_runtime_dir: Path) -> None:
        _write_pid_file(runtime_dir, os.getpid())

    monkeypatch.setattr(spawn_mod, "_spawn_posix", _fake_spawn)
    monkeypatch.setattr(spawn_mod, "_wait_for_socket", lambda *_args, **_kwargs: True)
    monkeypatch.setenv("EAWF_VERBOSE", "1")

    auto_spawn_daemon(runtime_dir)
    captured = capsys.readouterr()
    assert "spawning eawfd" in captured.err
    assert captured.out == ""


def test_pid_alive_helper_returns_false_for_missing_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_pid_alive`` returns False for nonexistent PIDs (POSIX gate)."""
    assert spawn_mod._pid_alive(999_999_999) is False


def test_pid_alive_helper_returns_true_for_self() -> None:
    assert spawn_mod._pid_alive(os.getpid()) is True


def test_read_pid_file_handles_missing_file(tmp_path: Path) -> None:
    assert spawn_mod._read_pid_file(tmp_path / "nope.pid") is None


def test_read_pid_file_handles_garbage(tmp_path: Path) -> None:
    pid_file = tmp_path / "eawfd.pid"
    pid_file.write_text("not-an-int\n", encoding="utf-8")
    assert spawn_mod._read_pid_file(pid_file) is None
