"""Deterministic tests for the timing-decided branches of the portalock lock.

The hold-ceiling, ticker-join and retry branches in
:mod:`eawf.runtime.lock.portalock` are otherwise reached only when a real
ticker thread or a real contender happens to line up with the host clock, so
the lock package's branch rate moved with host speed. Each test here pins one
branch with doubles instead: a stop event that reports a scripted number of
ticks, a scripted monotonic clock, a thread that never finishes, and a
``portalocker.lock`` that refuses or fails on cue.

The clock and the thread doubles replace portalock's own ``time`` and
``threading`` names, never the shared modules, so nothing else running in the
test process sees them.
"""

from __future__ import annotations

import io
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import IO

import portalocker
import pytest

from eawf.runtime.lock import portalock

_LOGGER = "eawf.runtime.lock.portalock"


class _ScriptedStopEvent(threading.Event):
    """A stop event whose ``wait`` times out *ticks* times, then reports the stop."""

    def __init__(self, ticks: int) -> None:
        super().__init__()
        self._remaining = ticks
        self.waits: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        if self._remaining == 0:
            return True
        self._remaining -= 1
        return False


class _FakeTime:
    """Stands in for the ``time`` module inside portalock.

    ``monotonic`` returns the scripted readings in order and then keeps
    returning the last one; ``sleep`` only records the requested delay.
    """

    def __init__(self, *readings: float) -> None:
        self._readings = list(readings)
        self.monotonic_calls = 0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        self.monotonic_calls += 1
        if len(self._readings) > 1:
            return self._readings.pop(0)
        return self._readings[0]

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


class _CountingHandle(portalock.LockHandle):
    """A lock handle whose heartbeat only counts, or fails, on each call."""

    def __init__(self, path: Path, *, fail: bool = False) -> None:
        super().__init__(target=path, path=path, fh=io.StringIO())
        self.beats = 0
        self._fail = fail

    def heartbeat(self) -> None:
        self.beats += 1
        if self._fail:
            raise OSError("heartbeat write refused")


class _StubThread:
    """A ticker thread that never runs its target and reports a fixed liveness."""

    def __init__(
        self,
        *,
        alive: bool,
        target: Callable[..., None],
        args: tuple[object, ...],
        kwargs: dict[str, object],
        name: str,
        daemon: bool,
    ) -> None:
        self.alive = alive
        self.target = target
        self.args = args
        self.kwargs = kwargs
        self.name = name
        self.daemon = daemon
        self.started = False
        self.join_timeouts: list[float | None] = []

    def start(self) -> None:
        self.started = True

    def join(self, timeout: float | None = None) -> None:
        self.join_timeouts.append(timeout)

    def is_alive(self) -> bool:
        return self.alive


def _install_stub_threads(monkeypatch: pytest.MonkeyPatch, *, alive: bool) -> list[_StubThread]:
    """Make portalock build :class:`_StubThread` tickers; return the built ones."""
    built: list[_StubThread] = []

    def _factory(
        *,
        target: Callable[..., None],
        args: tuple[object, ...],
        kwargs: dict[str, object],
        name: str,
        daemon: bool,
    ) -> _StubThread:
        thread = _StubThread(
            alive=alive, target=target, args=args, kwargs=kwargs, name=name, daemon=daemon
        )
        built.append(thread)
        return thread

    monkeypatch.setattr(
        portalock, "threading", SimpleNamespace(Event=threading.Event, Thread=_factory)
    )
    return built


def _messages(caplog: pytest.LogCaptureFixture, needle: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if needle in r.getMessage()]


# --------------------------------------------------------------------------
# _heartbeat_loop
# --------------------------------------------------------------------------


def test_heartbeat_loop_exits_without_a_tick_when_stop_is_already_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _FakeTime(0.0)
    monkeypatch.setattr(portalock, "time", clock)
    handle = _CountingHandle(tmp_path / "state.json.lock")
    stop = _ScriptedStopEvent(ticks=0)

    portalock._heartbeat_loop(handle, stop, interval=7.0, ceiling=10.0, start_monotonic=0.0)

    assert handle.beats == 0
    assert stop.waits == [7.0]
    assert clock.monotonic_calls == 0


def test_heartbeat_loop_warns_once_when_the_hold_crosses_the_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Tick 1 is under the ceiling, tick 2 crosses it, tick 3 skips the check.
    clock = _FakeTime(5.0, 15.0)
    monkeypatch.setattr(portalock, "time", clock)
    handle = _CountingHandle(tmp_path / "state.json.lock")
    stop = _ScriptedStopEvent(ticks=3)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        portalock._heartbeat_loop(handle, stop, interval=1.0, ceiling=10.0, start_monotonic=0.0)

    assert handle.beats == 3
    assert clock.monotonic_calls == 2
    ceiling = _messages(caplog, "hold_ceiling_exceeded")
    assert len(ceiling) == 1
    assert "duration_s=15.0" in ceiling[0]
    assert "ceiling_s=10.0" in ceiling[0]


def test_heartbeat_loop_does_not_warn_at_exactly_the_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(portalock, "time", _FakeTime(10.0))
    handle = _CountingHandle(tmp_path / "state.json.lock")

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        portalock._heartbeat_loop(
            handle, _ScriptedStopEvent(ticks=1), interval=1.0, ceiling=10.0, start_monotonic=0.0
        )

    assert handle.beats == 1
    assert _messages(caplog, "hold_ceiling_exceeded") == []


def test_heartbeat_loop_keeps_ticking_after_a_failed_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(portalock, "time", _FakeTime(1.0))
    handle = _CountingHandle(tmp_path / "state.json.lock", fail=True)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        portalock._heartbeat_loop(
            handle, _ScriptedStopEvent(ticks=2), interval=1.0, ceiling=10.0, start_monotonic=0.0
        )

    assert handle.beats == 2
    assert len(_messages(caplog, "heartbeat refresh-failed")) == 2
    assert _messages(caplog, "hold_ceiling_exceeded") == []


# --------------------------------------------------------------------------
# _heartbeat_ticker
# --------------------------------------------------------------------------


def test_heartbeat_ticker_warns_when_the_ticker_outlives_the_join(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    built = _install_stub_threads(monkeypatch, alive=True)
    handle = _CountingHandle(tmp_path / "state.json.lock")

    with (
        caplog.at_level(logging.WARNING, logger=_LOGGER),
        portalock._heartbeat_ticker(handle, interval=1.0, ceiling=2.0),
    ):
        pass

    [thread] = built
    assert thread.join_timeouts == [5.0]
    stop = thread.args[1]
    assert isinstance(stop, threading.Event)
    assert stop.is_set()
    assert len(_messages(caplog, "heartbeat-ticker-still-alive")) == 1


def test_heartbeat_ticker_uses_module_defaults_and_stays_quiet_after_a_clean_join(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    built = _install_stub_threads(monkeypatch, alive=False)
    monkeypatch.setattr(portalock, "time", _FakeTime(42.0))
    handle = _CountingHandle(tmp_path / "state.json.lock")

    with (
        caplog.at_level(logging.WARNING, logger=_LOGGER),
        portalock._heartbeat_ticker(handle, interval=None, ceiling=None),
    ):
        [thread] = built
        assert thread.started
        assert thread.daemon
        assert thread.name == "eawf-lock-heartbeat-state.json.lock"
        assert thread.target is portalock._heartbeat_loop
        assert thread.args[0] is handle
        assert thread.kwargs == {
            "interval": portalock.HEARTBEAT_INTERVAL_SECONDS,
            "ceiling": portalock.HOLD_CEILING_SECONDS,
            "start_monotonic": 42.0,
        }

    assert thread.join_timeouts == [5.0]
    assert _messages(caplog, "heartbeat-ticker-still-alive") == []


def test_heartbeat_ticker_stops_and_joins_when_the_body_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _install_stub_threads(monkeypatch, alive=False)
    handle = _CountingHandle(tmp_path / "state.json.lock")

    with (
        pytest.raises(RuntimeError, match="body failed"),
        portalock._heartbeat_ticker(handle, interval=1.0, ceiling=2.0),
    ):
        raise RuntimeError("body failed")

    [thread] = built
    assert thread.join_timeouts == [5.0]
    stop = thread.args[1]
    assert isinstance(stop, threading.Event)
    assert stop.is_set()


# --------------------------------------------------------------------------
# acquire
# --------------------------------------------------------------------------


def _target(tmp_path: Path) -> Path:
    target = tmp_path / "state.json"
    target.write_text("{}")
    return target


def test_acquire_retries_after_a_refused_lock_before_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _FakeTime(0.0)
    monkeypatch.setattr(portalock, "time", clock)
    real_lock = portalocker.lock
    seen: list[IO[str]] = []

    def _refuse_once(fh: IO[str], flags: portalocker.LockFlags) -> None:
        seen.append(fh)
        if len(seen) == 1:
            raise portalocker.LockException("held by a contender")
        real_lock(fh, flags)

    monkeypatch.setattr(portalocker, "lock", _refuse_once)

    with portalock.acquire(_target(tmp_path), timeout=1.0, heartbeat_interval=3600.0) as handle:
        assert len(seen) == 2
        assert seen[0].closed
        assert handle.fh is seen[1]
        assert not handle.fh.closed

    assert clock.sleeps == [0.05]


def test_acquire_times_out_at_the_deadline_without_sleeping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The deadline is 0.0 + 1.0; the first refusal already reads 1.0.
    clock = _FakeTime(0.0, 1.0)
    monkeypatch.setattr(portalock, "time", clock)
    seen: list[IO[str]] = []

    def _refuse(fh: IO[str], flags: portalocker.LockFlags) -> None:
        seen.append(fh)
        raise portalocker.LockException("held by a contender")

    monkeypatch.setattr(portalocker, "lock", _refuse)

    with (
        pytest.raises(portalock.LockTimeout, match=r"within 1\.0s"),
        portalock.acquire(_target(tmp_path), timeout=1.0),
    ):
        pytest.fail("the lock must not be yielded")

    assert len(seen) == 1
    assert seen[0].closed
    assert clock.sleeps == []


def test_acquire_closes_the_handle_and_reraises_a_non_lock_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _FakeTime(0.0)
    monkeypatch.setattr(portalock, "time", clock)
    seen: list[IO[str]] = []

    def _fail(fh: IO[str], flags: portalocker.LockFlags) -> None:
        seen.append(fh)
        raise OSError("lock syscall failed")

    monkeypatch.setattr(portalocker, "lock", _fail)

    with (
        pytest.raises(OSError, match="lock syscall failed"),
        portalock.acquire(_target(tmp_path), timeout=1.0),
    ):
        pytest.fail("the lock must not be yielded")

    assert len(seen) == 1
    assert seen[0].closed
    assert clock.sleeps == []


def test_acquire_propagates_an_open_failure_without_locking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locks: list[IO[str]] = []

    def _deny_open(*_args: object, **_kwargs: object) -> IO[str]:
        raise PermissionError("lockfile not writable")

    monkeypatch.setattr(portalock, "open", _deny_open, raising=False)
    monkeypatch.setattr(portalocker, "lock", lambda fh, _flags: locks.append(fh))

    with (
        pytest.raises(PermissionError, match="lockfile not writable"),
        portalock.acquire(_target(tmp_path), timeout=1.0),
    ):
        pytest.fail("the lock must not be yielded")

    assert locks == []
