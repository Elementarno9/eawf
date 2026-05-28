"""Read-only bridge from ``state.json`` into the TUI's reactive data.

The operator surface binds the daemon ``state.json`` into the
:class:`~eawf.surfaces.tui.app.EaApp` reactive layer. The long-term shape is
*daemon-push primary + mtime-poll fallback*: the TUI subscribes to the
daemon ``event.subscribe`` stream when the daemon is up and falls back
to a 2 s mtime poll when it is not (the daemonless carve-out). This
module ships the **fallback leg**
that works today — a direct, read-only load of ``state.json`` plus an
mtime-poll loop — and exposes the seams (``connect`` / ``disconnect``,
the ``on_state`` / ``on_degraded`` callbacks) the daemon-push leg slots
into in a later wave.

Why a callback rather than a Textual ``reactive`` on this class: a
``reactive`` descriptor only fires watchers on a ``DOMNode`` (App /
Widget / Screen). :class:`StateBinding` is a plain helper, so the App
owns the reactive ``state`` attribute and this binder pushes fresh state
into the App through :class:`StateBindingCallbacks`. That keeps the
single source of mutation (the daemon, per AGENTS rule 4) untouched —
this path is read-only and never writes ``state.json``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import orjson
from pydantic import ValidationError

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.store.envelope import Envelope
from eawf.runtime.daemon.runtime_dir import runtime_dir
from eawf.surfaces.cli._daemon_client import DaemonClient

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

#: Default mtime-poll cadence in seconds for the daemonless fallback
#: leg. Overridable via ``EAWF_POLL_INTERVAL_S`` so tests can drive a
#: tight loop without sleeping for the full production interval.
DEFAULT_POLL_INTERVAL_S: float = 2.0

#: Minimum number of consecutive socket-probe failures before the binding
#: enters degraded mode and starts the mtime-poll fallback.
DEFAULT_DAEMON_FAILURE_THRESHOLD: int = 3

#: Default cadence in seconds for repeated daemon-socket reconnect probes.
DEFAULT_DAEMON_PROBE_INTERVAL_S: float = 0.5


@dataclass(frozen=True)
class StateBindingCallbacks:
    """Async callbacks the App registers to receive binding updates.

    Attributes:
        on_state: Awaited with the freshly loaded :class:`State` each
            time the binder reads a new ``state.json`` revision.
        on_degraded: Awaited with the degraded flag whenever the binder
            flips between daemon-push and mtime-poll mode. ``True`` means
            the mtime-poll fallback is active (daemon unreachable).
        on_event: Awaited with live daemon envelopes before the matching
            state refresh is delivered. Optional because older tests only
            care about state/degraded callbacks.
    """

    on_state: Callable[[State], Awaitable[None]]
    on_degraded: Callable[[bool], Awaitable[None]]
    on_event: Callable[[Envelope], Awaitable[None]] | None = None


def load_state(state_path: Path | None) -> State | None:
    """Read + validate ``state.json`` into a typed :class:`State`.

    Read-only: this never mutates the file. Returns ``None`` when the
    path is unset, missing, unreadable, or fails schema validation so
    the TUI degrades to an empty-scope placeholder rather than crashing
    on a fresh or corrupt workspace.

    Args:
        state_path: Path to ``<scope>/.ea/state.json``. ``None`` short-
            circuits to ``None`` (no scope resolved).

    Returns:
        The validated :class:`State`, or ``None`` when no usable state
        is on disk.
    """
    if state_path is None or not state_path.is_file():
        return None
    try:
        payload = orjson.loads(state_path.read_bytes())
        return State.model_validate(payload)
    except (orjson.JSONDecodeError, OSError, ValidationError) as exc:
        logger.warning(f"load_state path={state_path!s} unreadable cause={exc!r}")
        return None


class StateBinding:
    """Read-only mtime-poll binder feeding the App's reactive state.

    The binder owns the disk read and the poll task; the App owns the
    Textual ``reactive`` attributes and supplies
    :class:`StateBindingCallbacks` to receive updates. Mutation of
    ``state.json`` stays with the daemon (AGENTS rule 4) — this class
    only reads.
    """

    def __init__(
        self,
        state_path: Path | None,
        callbacks: StateBindingCallbacks,
        *,
        poll_interval_s: float | None = None,
        daemon_probe_interval_s: float | None = None,
        daemon_failure_threshold: int = DEFAULT_DAEMON_FAILURE_THRESHOLD,
        daemon_client_factory: Callable[[], DaemonClient] | None = None,
    ) -> None:
        """Construct the binder.

        Args:
            state_path: Path to the scope's ``state.json`` (read-only).
            callbacks: App-supplied async hooks for state / degraded
                updates.
            poll_interval_s: Override the mtime-poll cadence. Defaults to
                ``EAWF_POLL_INTERVAL_S`` then :data:`DEFAULT_POLL_INTERVAL_S`.
            daemon_client_factory: Test seam for the JSON-RPC client. The
                production default uses :class:`DaemonClient`.
        """
        self._state_path = state_path
        self._callbacks = callbacks
        self._poll_task: asyncio.Task[None] | None = None
        self._subscribe_task: asyncio.Task[None] | None = None
        self._probe_task: asyncio.Task[None] | None = None
        self._client_factory = daemon_client_factory or (
            lambda: DaemonClient(call_timeout_seconds=1.0)
        )
        self._stopping = False
        self._scope_id: str | None = None
        self._is_degraded = False
        self._consecutive_failures = 0
        self._daemon_failure_threshold = max(1, int(daemon_failure_threshold))
        env_interval = os.environ.get("EAWF_POLL_INTERVAL_S")
        probe_env = os.environ.get("EAWF_DAEMON_PROBE_INTERVAL_S")
        if poll_interval_s is not None:
            self._poll_interval = poll_interval_s
        elif env_interval is not None:
            self._poll_interval = float(env_interval)
        else:
            self._poll_interval = DEFAULT_POLL_INTERVAL_S
        if daemon_probe_interval_s is not None:
            self._daemon_probe_interval = daemon_probe_interval_s
        elif probe_env is not None:
            self._daemon_probe_interval = float(probe_env)
        else:
            self._daemon_probe_interval = DEFAULT_DAEMON_PROBE_INTERVAL_S
        self._last_mtime = 0.0

    async def connect(self) -> None:
        """Load initial state, then prefer daemon push with poll fallback."""
        initial = load_state(self._state_path)
        if initial is not None:
            self._scope_id = initial.urn
            await self._callbacks.on_state(initial)
        if self._state_path is not None and self._state_path.is_file():
            with contextlib.suppress(OSError):
                self._last_mtime = self._state_path.stat().st_mtime
        await self._process_daemon_probe()
        await self._start_probe_loop()

    def _daemon_socket_available(self) -> bool:
        """Return whether a daemon socket exists for a cheap push attempt."""
        if os.name == "nt":
            return False
        sock_path = runtime_dir() / "eawfd.sock"
        eawf_runtime_dir = os.environ.get("EAWF_RUNTIME_DIR")
        xdg_runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
        logger.debug(
            f"_daemon_socket_available probing path={sock_path!s}"
            f" EAWF_RUNTIME_DIR={eawf_runtime_dir!r}"
            f" XDG_RUNTIME_DIR={xdg_runtime_dir!r}"
        )
        if not sock_path.exists():
            logger.debug(f"_daemon_socket_available socket_missing path={sock_path!s}")
            return False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.25)
                probe.connect(str(sock_path))
            return True
        except OSError as exc:
            logger.debug(f"_daemon_socket_available probe failed path={sock_path!s} cause={exc!r}")
            return False

    async def _process_daemon_probe(self) -> None:
        """Evaluate one socket-probe tick and transition degraded state."""
        if self._daemon_socket_available():
            self._consecutive_failures = 0
            await self._start_subscribe_loop()
            return
        self._consecutive_failures += 1
        logger.debug(
            f"_process_daemon_probe failure={self._consecutive_failures}"
            f" threshold={self._daemon_failure_threshold}"
        )
        if self._consecutive_failures < self._daemon_failure_threshold:
            return
        await self._start_poll_fallback()

    async def _start_probe_loop(self) -> None:
        """Start periodic socket probes and reconnect attempts."""
        if self._probe_task is not None and not self._probe_task.done():
            return
        self._probe_task = asyncio.create_task(self._probe_loop())

    async def _probe_loop(self) -> None:
        """Repeat socket probe/reconnect attempts on a short backoff."""
        while True:
            if self._stopping:
                return
            await asyncio.sleep(self._daemon_probe_interval)
            if self._stopping:
                return
            await self._process_daemon_probe()

    async def _start_poll_fallback(self) -> None:
        """Mark degraded and start the mtime-poll loop once."""
        await self._set_degraded(True)
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_loop())

    async def _stop_poll_loop(self) -> None:
        if self._poll_task is None:
            return
        self._poll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._poll_task
        self._poll_task = None

    async def _start_subscribe_loop(self) -> None:
        """Start the daemon subscription loop when not already running."""
        if self._subscribe_task is not None and not self._subscribe_task.done():
            return
        self._subscribe_task = asyncio.create_task(self._subscribe_loop())

    async def _set_degraded(self, degraded: bool) -> None:
        """Emit ``on_degraded`` only when the degraded flag changes."""
        if self._is_degraded == degraded:
            return
        self._is_degraded = degraded
        await self._callbacks.on_degraded(degraded)

    async def _subscribe_loop(self) -> None:
        """Run blocking daemon subscription off-thread and fall back on error."""
        loop = asyncio.get_running_loop()
        try:
            await asyncio.to_thread(self._run_subscription, loop)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(f"_subscribe_loop cause={exc!r}")
            if self._stopping:
                return
            self._consecutive_failures += 1
            logger.debug(
                f"_subscribe_loop failure={self._consecutive_failures}"
                f" threshold={self._daemon_failure_threshold}"
            )
            if self._consecutive_failures >= self._daemon_failure_threshold:
                await self._start_poll_fallback()
        else:
            if not self._stopping:
                self._consecutive_failures = 0
                await self._stop_poll_loop()

    def _run_subscription(self, loop: asyncio.AbstractEventLoop) -> None:
        """Subscribe to ``state.subscribe`` with ``DaemonClient``."""
        params: dict[str, object] = {"kinds": [StoreKind.EVENT.value]}
        if self._scope_id is not None:
            params["scope_id"] = self._scope_id
        with self._client_factory() as client:
            client.call("state.subscribe", params)
            self._consecutive_failures = 0
            asyncio.run_coroutine_threadsafe(self._on_subscription_connected(), loop)
            while not self._stopping:
                reader = getattr(client, "_reader", None)
                if reader is None:
                    raise RuntimeError("daemon reader unavailable")
                try:
                    line = reader.readline()
                except TimeoutError:
                    continue
                if not line:
                    raise RuntimeError("daemon subscribe stream ended")
                self._handle_push_line(loop, line)

    async def _on_subscription_connected(self) -> None:
        self._consecutive_failures = 0
        await self._stop_poll_loop()
        await self._set_degraded(False)

    def _handle_push_line(self, loop: asyncio.AbstractEventLoop, line: bytes) -> None:
        """Decode one ``event.push`` frame and schedule delivery."""
        try:
            frame = orjson.loads(line)
            if frame.get("method") != "event.push":
                return
            params = frame.get("params")
            if not isinstance(params, dict):
                return
            envelope = Envelope.model_validate(params.get("event"))
        except (orjson.JSONDecodeError, ValidationError, ValueError) as exc:
            logger.debug(f"_handle_push_line skip cause={exc!r}")
            return
        asyncio.run_coroutine_threadsafe(self._handle_push(envelope), loop)

    async def _handle_push(self, envelope: Envelope) -> None:
        """Forward live event envelope and refresh bound state from disk."""
        if self._callbacks.on_event is not None:
            await self._callbacks.on_event(envelope)
        refreshed = load_state(self._state_path)
        if refreshed is None:
            return
        if self._state_path is not None and self._state_path.is_file():
            with contextlib.suppress(OSError):
                self._last_mtime = self._state_path.stat().st_mtime
        await self._callbacks.on_state(refreshed)

    async def _poll_loop(self) -> None:
        """Re-read ``state.json`` whenever its mtime advances.

        Sleeps :attr:`_poll_interval` between probes so an idle terminal
        does not busy-loop. A failed read is logged and skipped — the
        next tick retries.
        """
        while True:
            await asyncio.sleep(self._poll_interval)
            if self._stopping:
                return
            if self._state_path is None or not self._state_path.is_file():
                continue
            try:
                mtime = self._state_path.stat().st_mtime
            except OSError:
                # TOCTOU: the file vanished (or became unreadable) between the
                # is_file() check and the stat(); skip this tick rather than
                # letting the exception kill the poll loop.
                continue
            if mtime <= self._last_mtime:
                continue
            self._last_mtime = mtime
            refreshed = load_state(self._state_path)
            if refreshed is not None:
                await self._callbacks.on_state(refreshed)

    async def disconnect(self) -> None:
        """Cancel background tasks on app teardown."""
        self._stopping = True
        if self._subscribe_task is not None:
            self._subscribe_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._subscribe_task
            self._subscribe_task = None
        if self._probe_task is not None:
            self._probe_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._probe_task
            self._probe_task = None
        if self._poll_task is not None:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None


__all__ = [
    "DEFAULT_POLL_INTERVAL_S",
    "StateBinding",
    "StateBindingCallbacks",
    "load_state",
]
