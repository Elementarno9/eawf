"""Test: a paused fleet drive persists ONCE on the transition, not per hold tick.

The smoke-surfaced W16 defect: ``_Loop.run_to_terminal``'s PAUSED branch called
``_persist`` on EVERY 1 s hold tick, rewriting ``state.json`` ~1 Hz forever while
the run sat paused with nothing changing. ``_drain_in_flight_no_claim`` blocks
until each lane finishes, so the drain only needs to run once -- on the
transition INTO paused. This drives the loop through several PAUSED hold ticks
(disk stays PAUSED, then flips to HALTED to exit) and asserts the persist that
carries the PAUSED state fires exactly once, not per tick.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from eawf.kernel.state.models import FleetRun, FleetRunState
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.fleet import _Loop

pytestmark = pytest.mark.integration


def _ctx() -> MethodContext:
    # A stateless context: every disk / persist seam is stubbed on the loop, so
    # no on-disk state is read or written.
    return MethodContext(
        started_at="2026-06-11T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.6.0",
    )


def _loop() -> _Loop:
    run = FleetRun(run_state=FleetRunState.DRAINING, armed_at=datetime.now(UTC))
    return _Loop(
        ctx=_ctx(),
        run=run,
        spawn=lambda _ctx, _wave_id: "unused",
        watch=lambda _ctx, _lane: "closed",
    )


def test_paused_hold_persists_once_not_per_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """The PAUSED hold persists once on the transition, then sleeps each tick.

    The disk run-state reads PAUSED for four hold ticks then flips to HALTED to
    end the loop. With the fix the persist carrying the PAUSED state fires
    exactly once (the transition into paused); the pre-fix per-tick persist
    would record PAUSED four times. The drain is likewise a one-shot.
    """
    loop = _loop()

    # Disk stays PAUSED for four ticks, then HALTED ends the hold. Each read pops
    # the head so the loop observes the same sequence the daemon would on disk.
    disk_states = [
        FleetRunState.PAUSED,
        FleetRunState.PAUSED,
        FleetRunState.PAUSED,
        FleetRunState.PAUSED,
        FleetRunState.HALTED,
    ]
    disk_iter = iter(disk_states)

    persisted_states: list[FleetRunState] = []
    drain_states: list[FleetRunState] = []
    sleeps = 0

    def _fake_disk_run_state() -> FleetRunState:
        return next(disk_iter)

    def _fake_persist() -> None:
        persisted_states.append(loop.run.run_state)

    def _fake_drain() -> None:
        drain_states.append(loop.run.run_state)

    def _fake_sleep(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1

    monkeypatch.setattr(loop, "_disk_run_state", _fake_disk_run_state)
    monkeypatch.setattr(loop, "_persist", _fake_persist)
    monkeypatch.setattr(loop, "_drain_in_flight_no_claim", _fake_drain)
    monkeypatch.setattr(loop, "_cancelled", lambda: False)
    # fleet.py calls ``time.sleep`` on the shared stdlib module object, so
    # patching the module's ``sleep`` here neutralises the hold delay there too.
    monkeypatch.setattr(time, "sleep", _fake_sleep)

    result = loop.run_to_terminal()

    # The run drained the halt to DONE (the HALTED branch's terminal transition).
    assert result.run_state is FleetRunState.DONE
    # The PAUSED state was persisted exactly ONCE -- the transition into paused,
    # not once per hold tick (four PAUSED reads occurred).
    assert persisted_states.count(FleetRunState.PAUSED) == 1
    # The drain while PAUSED likewise ran once, on the transition (it blocks
    # until the lanes finish, so re-running it every hold tick is redundant).
    # The halt branch's own drain is a separate, legitimate drain.
    assert drain_states.count(FleetRunState.PAUSED) == 1
    # The loop genuinely HELD across the four PAUSED ticks (a sleep per hold),
    # so the single persist really is amortised across many ticks.
    assert sleeps == 4
