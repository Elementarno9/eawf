"""Tests for the W04 subscription cursor + reconnect throttle (P30-I21-W04).

The TUI state binding used to re-subscribe on every daemon-probe tick with
``since=None`` (a full re-subscribe), so a stream the daemon repeatedly dropped
flooded the daemon with ~2 subscribes/second. These tests pin the two fixes:

* :meth:`StateBinding._subscribe_params` carries a ``since=<last event id>``
  cursor once an event has been delivered, so a reconnect resumes rather than
  re-requesting the backlog; and
* :meth:`StateBinding._start_subscribe_loop` throttles (re)connects to at most
  once per ``_reconnect_min_interval``, so a dropping stream reconnects on a
  bounded cadence instead of per probe tick.
"""

from __future__ import annotations

import asyncio

from eawf.kernel.state.enums import StoreKind
from eawf.surfaces.tui.state_binding import StateBinding, StateBindingCallbacks


def _binding() -> StateBinding:
    async def _noop_state(_s: object) -> None:
        return None

    async def _noop_degraded(_d: bool) -> None:
        return None

    callbacks = StateBindingCallbacks(on_state=_noop_state, on_degraded=_noop_degraded)
    return StateBinding(None, callbacks)


def test_subscribe_params_omit_since_before_first_event() -> None:
    """Before any event, the subscribe carries no cursor (initial full subscribe)."""
    binding = _binding()
    binding._scope_id = "urn:eawf:v1:state:QR"
    params = binding._subscribe_params()
    assert params["scope_id"] == "urn:eawf:v1:state:QR"
    assert "since" not in params
    assert params["kinds"] == [StoreKind.EVENT.value]


def test_subscribe_params_include_since_cursor_after_event() -> None:
    """Once an event id is recorded, the (re)subscribe resumes from it."""
    binding = _binding()
    binding._scope_id = "urn:eawf:v1:state:QR"
    binding._last_event_id = "EV-abc123"
    assert binding._subscribe_params()["since"] == "EV-abc123"


def test_start_subscribe_loop_throttles_reconnect() -> None:
    """A second (re)connect within the min interval is refused (no storm)."""

    async def body() -> None:
        binding = _binding()
        binding._reconnect_min_interval = 60.0  # long: the throttle must bite

        started: list[object] = []

        async def _fake_loop() -> None:
            started.append(object())

        binding._subscribe_loop = _fake_loop  # type: ignore[method-assign]

        # First connect is allowed (last attempt at t=0, now >> interval).
        await binding._start_subscribe_loop()
        first_task = binding._subscribe_task
        assert first_task is not None
        await asyncio.sleep(0)  # let the fake loop run + the task complete
        assert len(started) == 1

        # The stream "dropped" (task done); a reconnect within the interval is
        # throttled -- no new task, no second subscribe.
        assert binding._subscribe_task is not None and binding._subscribe_task.done()
        await binding._start_subscribe_loop()
        await asyncio.sleep(0)
        assert binding._subscribe_task is first_task  # unchanged: throttled
        assert len(started) == 1

    asyncio.run(body())


def test_start_subscribe_loop_reconnects_after_interval() -> None:
    """Once the throttle window passes, a dropped stream does reconnect."""

    async def body() -> None:
        binding = _binding()
        binding._reconnect_min_interval = 0.0  # no throttle: reconnect freely

        started: list[object] = []

        async def _fake_loop() -> None:
            started.append(object())

        binding._subscribe_loop = _fake_loop  # type: ignore[method-assign]

        await binding._start_subscribe_loop()
        await asyncio.sleep(0)
        first_task = binding._subscribe_task

        await binding._start_subscribe_loop()  # task done + interval elapsed
        await asyncio.sleep(0)
        assert binding._subscribe_task is not first_task  # a fresh reconnect
        assert len(started) == 2

    asyncio.run(body())
