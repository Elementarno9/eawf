"""Measure the daemon's readiness budget in the perf lane.

The blocking suite proves the ordering: an offloaded read leaves a ping
answered while the read is held, and a probe facing a held loop decides
before the hold ends. These tests put the absolute number on it: how long the
ping and the probe take against :data:`READINESS_BUDGET_SECONDS`.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

import eawf.runtime.daemon.server as server_mod
import eawf.runtime.daemon.spawn as spawn_mod
from eawf.runtime.daemon.limits import READINESS_BUDGET_SECONDS
from tests.fixtures.migration.epoch1_daemon_tree import write_state
from tests.integration.kernel.migration.test_loop_occupancy import (
    CLIENT_TIMEOUT_SECONDS,
    client,
    install_read_hold,
    read_response,
    request_frame,
    serve_on_thread,
    short_socket_path,
)
from tests.integration.kernel.migration.test_readiness_under_load import runtime_dir
from tests.integration.runtime.daemon._method_context import build_ctx

#: How far past the budget a probe may run before it has decided: one budget
#: for the round trip it gives up on, plus the PID-file read after it.
PROBE_SLACK_SECONDS = 2.0

_skip_perf = pytest.mark.skipif(
    os.environ.get("EAWF_SKIP_PERF") == "1",
    reason="EAWF_SKIP_PERF=1 - perf gate skipped",
)


@_skip_perf
def test_an_offloaded_read_leaves_the_ping_inside_the_readiness_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    write_state(state_path)
    ctx = build_ctx(tmp_path, state_path)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    install_read_hold(monkeypatch, started, release, finished)

    with (
        serve_on_thread(ctx, short_socket_path("perf-ping")) as sock_path,
        client(sock_path) as holder,
    ):
        try:
            holder.sendall(request_frame("state.read"))
            assert started.wait(timeout=CLIENT_TIMEOUT_SECONDS), "the held read never started"
            with client(sock_path) as prober:
                before = time.monotonic()
                prober.sendall(request_frame("daemon.ping"))
                ping = read_response(prober)
                latency = time.monotonic() - before
        finally:
            release.set()
        assert "result" in read_response(holder)

    assert "result" in ping, ping
    assert latency < READINESS_BUDGET_SECONDS, (
        f"ping took {latency:.3f}s while an offloaded read was held"
    )


@_skip_perf
def test_a_probe_facing_a_held_loop_decides_shortly_after_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    write_state(state_path)
    ctx = build_ctx(tmp_path, state_path)
    monkeypatch.setattr(server_mod, "OFFLOADED_METHODS", frozenset())
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    install_read_hold(monkeypatch, started, release, finished)

    with (
        runtime_dir(pid=os.getpid()) as rt_dir,
        serve_on_thread(ctx, str(rt_dir / "eawfd.sock")) as sock_path,
        client(sock_path) as holder,
    ):
        try:
            holder.sendall(request_frame("state.read"))
            assert started.wait(timeout=CLIENT_TIMEOUT_SECONDS), "the held read never started"
            before = time.monotonic()
            result = spawn_mod.daemon_pid_if_ready(rt_dir)
            elapsed = time.monotonic() - before
        finally:
            release.set()

    assert result == os.getpid()
    assert elapsed < READINESS_BUDGET_SECONDS + PROBE_SLACK_SECONDS, (
        f"the probe took {elapsed:.3f}s to decide on a busy daemon"
    )
