"""Readiness under load: busy is not absent, and one budget says so.

Two numbers used to answer the same question. The readiness probe waited
0.2 s for ``daemon.ping``; the dispatch path had no occupancy ceiling at
all. So a daemon busy with concurrent reads answered late, the probe mapped
the timeout to "no daemon", and the operator was told a live process was
dead — the exact failure this suite pins, from both ends:

* the probe concludes absence only from a failed *connect*, so a
  connected-but-slow daemon resolves to its PID;
* the loop-lag monitor warns when something holds the loop longer than the
  same budget, which is the condition that made the probe late.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import eawf.runtime.daemon.limits as limits_mod
import eawf.runtime.daemon.main as main_mod
import eawf.runtime.daemon.server as server_mod
import eawf.runtime.daemon.spawn as spawn_mod
from eawf.runtime.daemon.limits import READINESS_BUDGET_SECONDS
from tests.integration.runtime.daemon.test_close_lock_split import _build_ctx, _write_state
from tests.integration.runtime.daemon.test_loop_occupancy import (
    CLIENT_TIMEOUT_SECONDS,
    client,
    install_read_hold,
    request_frame,
    serve_on_thread,
)

pytestmark = pytest.mark.integration

#: Hold long enough that the readiness probe is guaranteed to time out on
#: the round trip and still have the daemon busy when it decides.
LOAD_HOLD_SECONDS = 3.0

#: A PID no process can hold, for the stale-PID-file path.
DEAD_PID = 2147483647


@contextlib.contextmanager
def runtime_dir(*, pid: int | None) -> Iterator[Path]:
    """Yield a short-pathed daemon runtime dir, optionally with a PID file.

    Short-pathed because ``tmp_path`` plus ``eawfd.sock`` can exceed the
    AF_UNIX path ceiling (macOS: 104 bytes).
    """
    path = Path(tempfile.mkdtemp(prefix="eawf-ready-"))
    if pid is not None:
        (path / "eawfd.pid").write_text(f"{pid}\n", encoding="utf-8")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _probe_under_read_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    pid_file_pid: int | None,
) -> tuple[int | None, float]:
    """Probe a daemon whose loop is held by a ``state.read``.

    Returns:
        ``(probe_result, elapsed_seconds)``.
    """
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path)
    ctx = _build_ctx(tmp_path, state_path)
    # Hold the loop rather than a worker thread: the probe has to face a
    # daemon that cannot answer, which is what "under load" meant in the
    # incident.
    monkeypatch.setattr(server_mod, "OFFLOADED_METHODS", frozenset())
    started = threading.Event()
    install_read_hold(monkeypatch, started, hold_seconds=LOAD_HOLD_SECONDS)

    with (
        runtime_dir(pid=pid_file_pid) as rt_dir,
        serve_on_thread(ctx, str(rt_dir / "eawfd.sock")) as sock_path,
        client(sock_path) as holder,
    ):
        holder.sendall(request_frame("state.read"))
        assert started.wait(timeout=CLIENT_TIMEOUT_SECONDS), "the held read never started"
        before = time.monotonic()
        result = spawn_mod.daemon_pid_if_ready(rt_dir)
        elapsed = time.monotonic() - before
    return result, elapsed


def test_probe_reports_present_for_a_daemon_under_state_read_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR RUN-055: the round trip times out, the connect did not, so the
    daemon is reported present — with its PID — instead of absent."""
    result, elapsed = _probe_under_read_load(tmp_path, monkeypatch, pid_file_pid=os.getpid())

    assert result == os.getpid()
    assert elapsed >= READINESS_BUDGET_SECONDS, (
        f"the probe gave up after {elapsed:.3f}s, short of the {READINESS_BUDGET_SECONDS}s budget"
    )
    assert elapsed < READINESS_BUDGET_SECONDS + 2.0


def test_probe_reports_absent_when_the_busy_daemon_pid_is_dead(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Error path: a listener answered the connect but the PID file names a
    dead process, so the probe cannot name the daemon and says absent rather
    than inventing a PID."""
    result, _elapsed = _probe_under_read_load(tmp_path, monkeypatch, pid_file_pid=DEAD_PID)

    assert result is None


def test_probe_reports_absent_when_the_busy_daemon_has_no_pid_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boundary: no PID file at all is the same unnameable case."""
    result, _elapsed = _probe_under_read_load(tmp_path, monkeypatch, pid_file_pid=None)

    assert result is None


def test_probe_reports_absent_when_nothing_is_bound() -> None:
    """Boundary: a runtime dir with a PID file but no socket is absent, and
    says so immediately rather than burning the budget."""
    with runtime_dir(pid=os.getpid()) as rt_dir:
        before = time.monotonic()
        result = spawn_mod.daemon_pid_if_ready(rt_dir)
        elapsed = time.monotonic() - before

    assert result is None
    assert elapsed < READINESS_BUDGET_SECONDS


def test_probe_reports_absent_for_a_stale_socket_node() -> None:
    """Error path: a socket node left by an unclean exit refuses the
    connect, which is the one shape that does mean absent."""
    with runtime_dir(pid=os.getpid()) as rt_dir:
        (rt_dir / "eawfd.sock").write_bytes(b"")
        result = spawn_mod.daemon_pid_if_ready(rt_dir)

    assert result is None


def test_readiness_budget_is_stated_once() -> None:
    """CR RUN-055: handler occupancy and probe readiness read the same
    object, so neither side can drift by re-declaring its own literal."""
    assert spawn_mod.READINESS_BUDGET_SECONDS is limits_mod.READINESS_BUDGET_SECONDS
    assert main_mod.READINESS_BUDGET_SECONDS is limits_mod.READINESS_BUDGET_SECONDS


def test_loop_lag_monitor_warns_when_the_loop_is_held(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CR RUN-055: a handler holding the loop past the budget is exactly the
    condition that times a probe out, so the daemon says so out loud."""

    async def body() -> int:
        stop = asyncio.Event()
        task = asyncio.create_task(main_mod.run_loop_lag_monitor(stop, budget_seconds=0.25))
        await asyncio.sleep(0.05)
        time.sleep(1.0)
        await asyncio.sleep(0.2)
        stop.set()
        return await asyncio.wait_for(task, timeout=CLIENT_TIMEOUT_SECONDS)

    with caplog.at_level(logging.WARNING, logger="eawf.runtime.daemon.main"):
        over_budget = asyncio.run(body())

    assert over_budget >= 1
    assert any("loop-held" in record.message for record in caplog.records)


def test_loop_lag_monitor_stays_silent_on_an_idle_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Boundary: an unheld loop produces no observations, so the signal
    stays worth reading."""

    async def body() -> int:
        stop = asyncio.Event()
        task = asyncio.create_task(main_mod.run_loop_lag_monitor(stop, budget_seconds=0.25))
        await asyncio.sleep(0.6)
        stop.set()
        return await asyncio.wait_for(task, timeout=CLIENT_TIMEOUT_SECONDS)

    with caplog.at_level(logging.WARNING, logger="eawf.runtime.daemon.main"):
        over_budget = asyncio.run(body())

    assert over_budget == 0
    assert not any("loop-held" in record.message for record in caplog.records)


@pytest.mark.parametrize("budget", [0.0, -1.0])
def test_loop_lag_monitor_rejects_a_non_positive_budget(budget: float) -> None:
    """Error path: a zero or negative budget is a sampling interval of zero
    or less, so it is refused at the boundary."""

    async def body() -> int:
        return await main_mod.run_loop_lag_monitor(asyncio.Event(), budget_seconds=budget)

    with pytest.raises(ValueError, match="budget_seconds must be positive"):
        asyncio.run(body())
