"""Tests for :func:`eawf.runtime.daemon.spawn.auto_spawn_daemon`.

These tests avoid actually fork-execing the daemon process for the
core idempotency / stale-PID checks by monkeypatching the spawn
primitive — we drive ``auto_spawn_daemon`` with synthetic PID files
and process-liveness probes so the suite stays hermetic. Real
fork+exec coverage lives in the cold-spawn benchmark under
``benches/daemon_cold_spawn.py``.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import types
from collections.abc import Iterator
from pathlib import Path

import pytest

from eawf.runtime.daemon import spawn as spawn_mod
from eawf.runtime.daemon.spawn import DaemonSpawnTimeout, auto_spawn_daemon

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
    monkeypatch.setattr(spawn_mod, "daemon_singleton_locked", lambda _runtime_dir: False)
    monkeypatch.setattr(spawn_mod, "_ping_daemon_once", lambda _runtime_dir: os.getpid())
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
        # socket — we override ``_ping_daemon_once`` so the helper
        # short-circuits.
        _write_pid_file(runtime_dir, os.getpid())

    pings: list[int | None] = [None, os.getpid()]

    monkeypatch.setattr(spawn_mod, "_spawn_posix", _fake_spawn)
    monkeypatch.setattr(spawn_mod, "daemon_singleton_locked", lambda _runtime_dir: False)
    monkeypatch.setattr(spawn_mod, "_ping_daemon_once", lambda _runtime_dir: pings.pop(0))
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

    pings: list[int | None] = [None, os.getpid()]

    monkeypatch.setattr(spawn_mod, "_spawn_posix", _fake_spawn)
    monkeypatch.setattr(spawn_mod, "daemon_singleton_locked", lambda _runtime_dir: False)
    monkeypatch.setattr(spawn_mod, "_ping_daemon_once", lambda _runtime_dir: pings.pop(0))
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
        # socket so the readiness wait fails.
        return

    monkeypatch.setattr(spawn_mod, "_spawn_posix", _fake_spawn)
    monkeypatch.setattr(spawn_mod, "daemon_singleton_locked", lambda _runtime_dir: False)
    monkeypatch.setattr(spawn_mod, "_ping_daemon_once", lambda _runtime_dir: None)
    monkeypatch.setattr(spawn_mod, "SPAWN_POLL_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(spawn_mod, "SPAWN_POLL_INTERVAL_SECONDS", 0.001)
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

    pings: list[int | None] = [None, os.getpid()]

    monkeypatch.setattr(spawn_mod, "_spawn_posix", _fake_spawn)
    monkeypatch.setattr(spawn_mod, "daemon_singleton_locked", lambda _runtime_dir: False)
    monkeypatch.setattr(spawn_mod, "_ping_daemon_once", lambda _runtime_dir: pings.pop(0))
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

    pings: list[int | None] = [None, os.getpid()]

    monkeypatch.setattr(spawn_mod, "_spawn_posix", _fake_spawn)
    monkeypatch.setattr(spawn_mod, "daemon_singleton_locked", lambda _runtime_dir: False)
    monkeypatch.setattr(spawn_mod, "_ping_daemon_once", lambda _runtime_dir: pings.pop(0))
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


def test_auto_spawn_waits_on_singleton_locked_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock-held startup waits for readiness and does not fork again."""
    runtime_dir = tmp_path / "rt"
    spawn_called: list[bool] = []

    def _fake_spawn(_runtime_dir: Path) -> None:
        spawn_called.append(True)

    monkeypatch.setattr(spawn_mod, "daemon_singleton_locked", lambda _runtime_dir: True)
    monkeypatch.setattr(spawn_mod, "wait_for_daemon_ready", lambda _runtime_dir: 4242)
    monkeypatch.setattr(spawn_mod, "_spawn_posix", _fake_spawn)

    assert auto_spawn_daemon(runtime_dir) == 4242
    assert spawn_called == []


def test_auto_spawn_rechecks_readiness_under_spawn_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second CLI attaching after another starter avoids a second fork."""
    runtime_dir = tmp_path / "rt"
    spawn_called: list[bool] = []
    lock_events: list[str] = []

    @contextlib.contextmanager
    def _fake_spawn_lock(_runtime_dir: Path, *, timeout_seconds: float) -> Iterator[None]:
        assert timeout_seconds > 0
        lock_events.append("enter")
        yield
        lock_events.append("exit")

    def _fake_spawn(_runtime_dir: Path) -> None:
        spawn_called.append(True)

    monkeypatch.setattr(spawn_mod, "daemon_singleton_locked", lambda _runtime_dir: False)
    monkeypatch.setattr(spawn_mod, "acquire_spawn_lock", _fake_spawn_lock)
    monkeypatch.setattr(spawn_mod, "_ping_daemon_once", lambda _runtime_dir: 5150)
    monkeypatch.setattr(spawn_mod, "_spawn_posix", _fake_spawn)

    assert auto_spawn_daemon(runtime_dir) == 5150
    assert spawn_called == []
    assert lock_events == ["enter", "exit"]


def test_auto_spawn_live_pid_without_ping_does_not_return_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live PID file alone is not readiness."""
    runtime_dir = tmp_path / "rt"
    runtime_dir.mkdir()
    _write_pid_file(runtime_dir, os.getpid())
    spawn_called: list[bool] = []

    def _fake_spawn(_runtime_dir: Path) -> None:
        spawn_called.append(True)

    monkeypatch.setattr(spawn_mod, "daemon_singleton_locked", lambda _runtime_dir: False)
    monkeypatch.setattr(spawn_mod, "_ping_daemon_once", lambda _runtime_dir: None)
    monkeypatch.setattr(spawn_mod, "_spawn_posix", _fake_spawn)
    monkeypatch.setattr(spawn_mod, "SPAWN_POLL_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(spawn_mod, "SPAWN_POLL_INTERVAL_SECONDS", 0.001)

    with pytest.raises(DaemonSpawnTimeout):
        auto_spawn_daemon(runtime_dir)
    assert spawn_called == [True]


def test_read_pid_file_handles_missing_file(tmp_path: Path) -> None:
    assert spawn_mod._read_pid_file(tmp_path / "nope.pid") is None


def test_read_pid_file_handles_garbage(tmp_path: Path) -> None:
    pid_file = tmp_path / "eawfd.pid"
    pid_file.write_text("not-an-int\n", encoding="utf-8")
    assert spawn_mod._read_pid_file(pid_file) is None


def test_request_daemon_shutdown_missing_socket_does_not_spawn(tmp_path: Path) -> None:
    """Control shutdown returns absent instead of cold-spawning."""
    assert (
        spawn_mod.request_daemon_shutdown(
            tmp_path,
            drain=True,
            timeout_seconds=30,
        )
        is None
    )


@pytest.mark.parametrize("timeout_seconds", [-1, 601])
def test_request_daemon_shutdown_rejects_timeout_boundary(
    tmp_path: Path,
    timeout_seconds: int,
) -> None:
    """Control shutdown enforces daemon method bounds before transport."""
    with pytest.raises(ValueError, match="between 0 and 600"):
        spawn_mod.request_daemon_shutdown(
            tmp_path,
            drain=True,
            timeout_seconds=timeout_seconds,
        )


def test_default_spawn_poll_timeout_is_20s_on_win32() -> None:
    """Windows service cold-starts get a wider readiness window."""
    assert spawn_mod._default_spawn_poll_timeout_seconds("win32") == 20.0
    assert spawn_mod._default_spawn_poll_timeout_seconds("linux") == 5.0


def test_win32_ping_daemon_once_round_trips_over_pipe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows readiness must be a real ``daemon.ping`` pipe RPC."""
    calls: list[tuple[str, bytes, int]] = []
    pipe_name = r"\\.\pipe\eawfd-test"

    def _fake_pipe_client_call(name: str, payload: bytes, *, wait_ms: int) -> bytes:
        calls.append((name, payload, wait_ms))
        return b'{"jsonrpc":"2.0","id":"spawn-readiness","result":{"pid":4321}}\n'

    fake_windows_pipe = types.SimpleNamespace(
        default_pipe_name=lambda: pipe_name,
        pipe_client_call=_fake_pipe_client_call,
    )

    def _fail_wait_for_pipe(_runtime_dir: Path, _deadline: float) -> bool:
        raise AssertionError("_wait_for_pipe is not readiness")

    monkeypatch.setattr(spawn_mod.sys, "platform", "win32")
    monkeypatch.setattr(spawn_mod, "_wait_for_pipe", _fail_wait_for_pipe)
    monkeypatch.setitem(sys.modules, "eawf.runtime.daemon.windows_pipe", fake_windows_pipe)

    assert spawn_mod._ping_daemon_once(tmp_path) == 4321
    assert calls == [(pipe_name, calls[0][1], 50)]
    request = json.loads(calls[0][1].decode("utf-8"))
    assert request["method"] == "daemon.ping"
    assert request["params"] == {}


def test_win32_ping_daemon_once_rejects_non_pid_pipe_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows readiness ignores pipe responses that are not ping success."""

    def _fake_pipe_client_call(_name: str, _payload: bytes, *, wait_ms: int) -> bytes:
        assert wait_ms == 50
        return b'{"jsonrpc":"2.0","id":"spawn-readiness","result":{"ok":true}}\n'

    fake_windows_pipe = types.SimpleNamespace(
        default_pipe_name=lambda: r"\\.\pipe\eawfd-test",
        pipe_client_call=_fake_pipe_client_call,
    )
    monkeypatch.setattr(spawn_mod.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "eawf.runtime.daemon.windows_pipe", fake_windows_pipe)

    assert spawn_mod._ping_daemon_once(tmp_path) is None
