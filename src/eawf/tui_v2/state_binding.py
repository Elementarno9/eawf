"""Read-only bridge from ``state.json`` into the TUI's reactive data.

The operator surface binds the daemon ``state.json`` into the
:class:`~eawf.tui_v2.app.EaApp` reactive layer. The long-term shape is
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
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import orjson
from pydantic import ValidationError

from eawf.state.models import State

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

#: Default mtime-poll cadence in seconds for the daemonless fallback
#: leg. Overridable via ``EAWF_POLL_INTERVAL_S`` so tests can drive a
#: tight loop without sleeping for the full production interval.
DEFAULT_POLL_INTERVAL_S: float = 2.0


@dataclass(frozen=True)
class StateBindingCallbacks:
    """Async callbacks the App registers to receive binding updates.

    Attributes:
        on_state: Awaited with the freshly loaded :class:`State` each
            time the binder reads a new ``state.json`` revision.
        on_degraded: Awaited with the degraded flag whenever the binder
            flips between daemon-push and mtime-poll mode. ``True`` means
            the mtime-poll fallback is active (daemon unreachable).
    """

    on_state: Callable[[State], Awaitable[None]]
    on_degraded: Callable[[bool], Awaitable[None]]


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
    ) -> None:
        """Construct the binder.

        Args:
            state_path: Path to the scope's ``state.json`` (read-only).
            callbacks: App-supplied async hooks for state / degraded
                updates.
            poll_interval_s: Override the mtime-poll cadence. Defaults to
                ``EAWF_POLL_INTERVAL_S`` then :data:`DEFAULT_POLL_INTERVAL_S`.
        """
        self._state_path = state_path
        self._callbacks = callbacks
        self._poll_task: asyncio.Task[None] | None = None
        env_interval = os.environ.get("EAWF_POLL_INTERVAL_S")
        if poll_interval_s is not None:
            self._poll_interval = poll_interval_s
        elif env_interval is not None:
            self._poll_interval = float(env_interval)
        else:
            self._poll_interval = DEFAULT_POLL_INTERVAL_S
        self._last_mtime = 0.0

    async def connect(self) -> None:
        """Load the initial state and start the mtime-poll fallback.

        The daemon-push leg (``event.subscribe``) slots in here in a
        later wave; today the binder reads ``state.json`` once and then
        polls. Marks the binding degraded because the push leg is not
        yet wired — the App surfaces the degraded banner accordingly.
        """
        initial = load_state(self._state_path)
        if initial is not None:
            await self._callbacks.on_state(initial)
        if self._state_path is not None and self._state_path.is_file():
            self._last_mtime = self._state_path.stat().st_mtime
        await self._callbacks.on_degraded(True)
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self) -> None:
        """Re-read ``state.json`` whenever its mtime advances.

        Sleeps :attr:`_poll_interval` between probes so an idle terminal
        does not busy-loop. A failed read is logged and skipped — the
        next tick retries.
        """
        while True:
            await asyncio.sleep(self._poll_interval)
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
        """Cancel the poll task on app teardown."""
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None


__all__ = [
    "DEFAULT_POLL_INTERVAL_S",
    "StateBinding",
    "StateBindingCallbacks",
    "load_state",
]
