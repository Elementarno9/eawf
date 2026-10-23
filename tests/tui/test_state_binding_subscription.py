"""Tests for the W04 subscription cursor + reconnect throttle.

The TUI state binding used to re-subscribe on every daemon-probe tick with
``since_event_id=None`` (a full re-subscribe), so a stream the daemon repeatedly
dropped flooded the daemon with ~2 subscribes/second. These tests pin the fixes:

* :meth:`StateBinding._subscribe_params` carries a ``since_event_id=<last event
  id>`` cursor once an event has been delivered, so a reconnect resumes rather
  than re-requesting the backlog. The wire key MUST be ``since_event_id`` -- the
  daemon's :class:`~eawf.runtime.daemon.methods.event.SubscribeParams` is an
  ``extra="forbid"`` model whose field is ``since_event_id``, so the historical
  ``since`` key was silently rejected and Watch fell back to mtime-polling; and
* :meth:`StateBinding._start_subscribe_loop` throttles (re)connects to at most
  once per ``_reconnect_min_interval``, so a dropping stream reconnects on a
  bounded cadence instead of per probe tick.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import threading
import time
from typing import TYPE_CHECKING, Any, cast

import orjson
import pytest

from eawf.kernel.state.enums import StoreKind
from eawf.surfaces.tui.chassis.state_binding import StateBinding, StateBindingCallbacks

if TYPE_CHECKING:
    from eawf.surfaces.cli._daemon_client import DaemonClient


def _binding() -> StateBinding:
    async def _noop_degraded(_d: bool) -> None:
        return None

    callbacks = StateBindingCallbacks(on_degraded=_noop_degraded)
    return StateBinding(callbacks)


def _event_push_frame(event_id: str) -> bytes:
    """Serialize one ``event.push`` frame carrying an EVENT envelope.

    Mirrors the wire shape :meth:`StateBinding._handle_push_line` decodes, so
    feeding it drives the real cursor-advance path rather than poking the field.
    """
    return orjson.dumps(
        {
            "method": "event.push",
            "params": {
                "event": {
                    "schema_version": "1.0",
                    "id": event_id,
                    "kind": StoreKind.EVENT.value,
                    "scope_id": None,
                    "created_at": "2026-07-15T00:00:00Z",
                    "summary": f"event {event_id}",
                    "payload": {},
                }
            },
        }
    )


def test_subscribe_params_omit_since_before_first_event() -> None:
    """Before any event, the subscribe carries no cursor and no scope narrowing.

    The EVENT subscription is deliberately un-narrowed: fleet lifecycle
    events are wave-/iter-scoped, so a daemon-side ``scope_id`` filter keyed on
    the state URN would drop every one of them. The subscribe therefore omits
    ``scope_id`` and lets the panes filter client-side.
    """
    binding = _binding()
    params = binding._subscribe_params()
    assert "scope_id" not in params
    assert "since" not in params
    assert "since_event_id" not in params
    assert params["kinds"] == [StoreKind.EVENT.value]


def test_subscribe_params_include_since_cursor_after_event() -> None:
    """Once an event id is recorded, the (re)subscribe resumes from it.

    The wire key is ``since_event_id`` (the daemon's forbid-extra field), never
    the historical ``since`` alias the daemon rejected.
    """
    binding = _binding()
    binding._last_event_id = "EV-abc123"
    params = binding._subscribe_params()
    assert "since" not in params
    assert params["since_event_id"] == "EV-abc123"


def test_reconnect_resumes_from_last_seen_event_ordered_no_gap() -> None:
    """Disconnect -> events land -> reconnect resumes from the last seen id.

    The cold subscribe omits the cursor (the daemon would send the full
    backlog). After the push stream delivers an ordered run of events, the
    binder advances its resume cursor to the LAST delivered id. A reconnect
    therefore asks the daemon to catch up from that exact id via
    ``since_event_id`` -- the daemon resumes past it with no gap and no
    full-backlog replay.
    """

    async def body() -> None:
        binding = _binding()
        loop = asyncio.get_running_loop()

        # Cold start (disconnect): no cursor -> daemon would send full backlog.
        cold = binding._subscribe_params()
        assert "since_event_id" not in cold

        # The stream delivers an ordered run of events (write-events phase);
        # each push advances the resume cursor through the real handler.
        for event_id in ("EV-001", "EV-002", "EV-003"):
            binding._handle_push_line(loop, _event_push_frame(event_id))
        await asyncio.sleep(0)  # drain the scheduled push handlers

        # Reconnect: the subscribe resumes from the LAST seen id, so catch-up
        # is ordered and gapless -- not a replay from the head of the backlog.
        resumed = binding._subscribe_params()
        assert "since" not in resumed
        assert resumed["since_event_id"] == "EV-003"
        assert binding._last_event_id == "EV-003"

    asyncio.run(body())


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


class _BlockingClient:
    """A daemon client whose subscription read blocks until its socket is shut down.

    The peer end of a socket pair is held open and never written, and the socket
    carries no timeout, so ``readline()`` returns only once ``disconnect`` wakes it.
    """

    def __init__(self) -> None:
        self._sock, self._peer = socket.socketpair()
        self._reader: Any = None
        self.subscribed = threading.Event()
        self.calls: list[str] = []

    def __enter__(self) -> _BlockingClient:
        self._reader = self._sock.makefile("rb")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def call(self, method: str, _params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(method)
        self.subscribed.set()
        return {}

    def close(self) -> None:
        # The peer goes first: its EOF ends a still-blocked read, which holds
        # the buffered reader's lock that closing the reader would wait on.
        self._peer.close()
        if self._reader is not None:
            with contextlib.suppress(OSError):
                self._reader.close()
        self._sock.close()


def _blocking_binding(client: _BlockingClient) -> tuple[StateBinding, threading.Event]:
    """Bind ``client`` and report when the subscription thread has returned."""

    async def _noop_degraded(_d: bool) -> None:
        return None

    callbacks = StateBindingCallbacks(on_degraded=_noop_degraded)
    binding = StateBinding(callbacks, daemon_client_factory=lambda: cast("DaemonClient", client))
    exited = threading.Event()
    run = binding._run_subscription

    def _tracked(loop: asyncio.AbstractEventLoop) -> None:
        try:
            run(loop)
        finally:
            exited.set()

    binding._run_subscription = _tracked  # type: ignore[method-assign]
    return binding, exited


@pytest.mark.skipif(os.name == "nt", reason="the POSIX socket read; Windows uses CancelIoEx")
def test_disconnect_wakes_a_read_that_blocks_indefinitely() -> None:
    """``disconnect`` returns in under a second and the subscription thread has exited."""
    client = _BlockingClient()

    async def body() -> None:
        binding, exited = _blocking_binding(client)
        try:
            binding._subscribe_task = asyncio.create_task(binding._subscribe_loop())
            assert await asyncio.to_thread(client.subscribed.wait, 5.0)
            await asyncio.sleep(0.05)  # let the thread park in readline()
            assert not exited.is_set()

            started = time.monotonic()
            await binding.disconnect()
            elapsed = time.monotonic() - started

            assert elapsed < 1.0
            assert exited.is_set()
            assert binding._subscription_sock is None
            assert binding._subscribe_task is None
        finally:
            # A regression must fail the test, not hang the executor shutdown.
            client.close()

    asyncio.run(body())


def test_disconnect_without_subscription_has_no_socket_to_shut() -> None:
    """With no subscription running, there is no socket and nothing to wait on."""

    async def body() -> None:
        binding = _binding()
        assert binding._shutdown_subscription_sock() is False
        started = time.monotonic()
        await binding.disconnect()
        assert time.monotonic() - started < 0.5

    asyncio.run(body())


def test_shutdown_tolerates_a_socket_the_thread_already_closed() -> None:
    """A socket closed by the thread's own teardown is ignored, not raised."""
    binding = _binding()
    sock, peer = socket.socketpair()
    sock.close()
    peer.close()
    binding._subscription_sock = sock
    assert binding._shutdown_subscription_sock() is True


def test_subscription_stopped_before_subscribe_never_calls() -> None:
    """A disconnect that lands while connecting stops the thread before it subscribes."""
    client = _BlockingClient()
    binding, exited = _blocking_binding(client)
    binding._stopping = True
    loop = asyncio.new_event_loop()
    try:
        binding._run_subscription(loop)
    finally:
        loop.close()
    assert client.calls == []
    assert exited.is_set()
    assert binding._subscription_sock is None
