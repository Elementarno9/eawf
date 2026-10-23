"""Daemon push transport feeding the TUI's reactive data.

The operator surface rides one daemon subscription: the binder subscribes
when the daemon socket answers, delivers each pushed frame to the App's
callbacks, and resumes from its cursor after a drop. A stream that comes
back unable to replay what it missed owes a full re-read, which the
``on_resync`` callback performs.

``on_degraded`` is driven purely by the daemon-socket probe crossing its
failure threshold: ``True`` while the daemon cannot be reached, ``False``
once a subscription connects again.

Why a callback rather than a Textual ``reactive`` on this class: a
``reactive`` descriptor only fires watchers on a ``DOMNode`` (App /
Widget / Screen). :class:`StateBinding` is a plain helper, so the App
owns its reactive attributes and this binder pushes into the App through
:class:`StateBindingCallbacks`. The binder never writes: mutation stays
with the daemon.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import orjson
from pydantic import ValidationError

from eawf.kernel.projection.compute import KeyedPatch
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.runtime.daemon.read_contract import (
    CATCH_UP_TOO_LARGE,
    PROJECTION_PUSH_METHOD,
    PROJECTION_SUBSCRIBE_METHOD,
)
from eawf.runtime.daemon.runtime_dir import runtime_dir
from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

#: Minimum number of consecutive socket-probe failures before the binding
#: enters degraded mode.
DEFAULT_DAEMON_FAILURE_THRESHOLD: int = 3

#: Default cadence in seconds for repeated daemon-socket reconnect probes.
DEFAULT_DAEMON_PROBE_INTERVAL_S: float = 0.5

#: The subscribe verb the epoch-1 feed rides. A binder constructed without an
#: explicit verb sends this one, so the console's projection feed can take the
#: same transport without moving the feed that was already on it.
DEFAULT_SUBSCRIBE_METHOD: str = "state.subscribe"

#: Minimum seconds between subscription (re)connect attempts. A stream the
#: daemon repeatedly drops must NOT be re-subscribed at the probe cadence
#: (~2/s), which floods the daemon with full re-subscribes (the storm this
#: throttle fixes). The ``since`` cursor makes each reconnect cheap. Overridable via
#: ``EAWF_RECONNECT_MIN_INTERVAL_S`` so a test can drive a tight loop.
DEFAULT_RECONNECT_MIN_INTERVAL_S: float = 5.0

#: Seconds ``disconnect`` waits for the subscription thread to return after
#: its socket is shut down. The woken read returns at once, so this bounds
#: only a thread stuck elsewhere; past it the task is cancelled regardless.
_SUBSCRIPTION_EXIT_GRACE_S: float = 0.5


@dataclass(frozen=True)
class StateBindingCallbacks:
    """Async callbacks the App registers to receive binding updates.

    Attributes:
        on_degraded: Awaited with the degraded flag whenever it flips.
            ``True`` means the daemon cannot be reached.
        on_event: Awaited with each live daemon envelope the stream pushes.
        on_patch: Awaited with each keyed projection patch the daemon pushes.
            Optional and ``None`` for the epoch-1 feed, which subscribes with a
            verb that produces no patch frame at all.
        on_resync: Awaited once a re-established stream could not replay what
            it missed, so everything the feed fed must be read again.
    """

    on_degraded: Callable[[bool], Awaitable[None]]
    on_event: Callable[[Envelope], Awaitable[None]] | None = None
    on_patch: Callable[[KeyedPatch], Awaitable[None]] | None = None
    on_resync: Callable[[], Awaitable[None]] | None = None


class StateBinding:
    """Read-only daemon push binder feeding the App's reactive data.

    The binder owns the subscription and the probe task; the App owns the
    Textual ``reactive`` attributes and supplies
    :class:`StateBindingCallbacks` to receive updates.
    """

    def __init__(
        self,
        callbacks: StateBindingCallbacks,
        *,
        daemon_probe_interval_s: float | None = None,
        daemon_failure_threshold: int = DEFAULT_DAEMON_FAILURE_THRESHOLD,
        daemon_client_factory: Callable[[], DaemonClient] | None = None,
        subscribe_method: str = DEFAULT_SUBSCRIBE_METHOD,
    ) -> None:
        """Construct the binder.

        Args:
            callbacks: App-supplied async hooks for pushed frames and
                degraded updates.
            daemon_client_factory: Test seam for the JSON-RPC client. The
                production default uses :class:`DaemonClient`.
            subscribe_method: The verb the push stream subscribes with. The
                default keeps the epoch-1 feed exactly where it was; the
                console's projection seam passes the projection verb so its
                feed rides this same socket rather than opening a second.
        """
        self._callbacks = callbacks
        self._subscribe_method = subscribe_method
        self._subscribe_task: asyncio.Task[None] | None = None
        self._probe_task: asyncio.Task[None] | None = None
        self._client_factory = daemon_client_factory or (
            lambda: DaemonClient(call_timeout_seconds=1.0)
        )
        self._stopping = False
        self._is_degraded = False
        self._consecutive_failures = 0
        # Windows: the live subscription holds a persistent pipe handle the
        # streaming read blocks on. ``disconnect`` cancels that read via
        # ``CancelIoEx`` (CloseHandle alone does not unblock it), so the
        # handle is retained here for teardown.
        self._win_pipe: Any = None
        # POSIX: the subscription thread parks in ``readline()`` on this socket,
        # which cancelling the asyncio task cannot interrupt, so ``disconnect``
        # shuts it down to wake the read instead of waiting out the call timeout.
        self._subscription_sock: socket.socket | None = None
        self._daemon_failure_threshold = max(1, int(daemon_failure_threshold))
        probe_env = os.environ.get("EAWF_DAEMON_PROBE_INTERVAL_S")
        if daemon_probe_interval_s is not None:
            self._daemon_probe_interval = daemon_probe_interval_s
        elif probe_env is not None:
            self._daemon_probe_interval = float(probe_env)
        else:
            self._daemon_probe_interval = DEFAULT_DAEMON_PROBE_INTERVAL_S
        # Subscription cursor + reconnect throttle: the id of the last
        # event the push stream delivered, resumed via ``since_event_id=`` on
        # every (re)subscribe so the daemon replays what the gap missed; and the
        # monotonic timestamp of the last connect attempt so a repeatedly-
        # dropping stream reconnects on a bounded cadence, not per probe tick.
        self._last_event_id: str | None = None
        # Whether a stream was ever established, and whether the next one owes a
        # full re-read because the gap before it cannot be replayed.
        self._subscribed_once = False
        self._resync_owed = False
        # The projection feed's own resume cursor. Event ids order nothing
        # across two records, so a console resumes from the workspace-global
        # ordinal the daemon allocated instead; 0 means nothing acknowledged.
        self._last_canonical_sequence = 0
        self._last_subscribe_monotonic = 0.0
        reconnect_env = os.environ.get("EAWF_RECONNECT_MIN_INTERVAL_S")
        if reconnect_env is not None:
            self._reconnect_min_interval = float(reconnect_env)
        else:
            self._reconnect_min_interval = DEFAULT_RECONNECT_MIN_INTERVAL_S

    async def connect(self) -> None:
        """Probe the daemon, subscribe when it answers, and keep probing."""
        await self._process_daemon_probe()
        await self._start_probe_loop()

    def _daemon_socket_available(self) -> bool:
        """Return whether a daemon transport is up for a cheap push attempt.

        On Windows there is no UDS, so this probes the per-tree named pipe
        via ``pipe_ready`` (a ``WaitNamedPipe`` check that never consumes a
        pipe instance). On POSIX it probes the UDS socket.
        """
        if os.name == "nt":
            from eawf.runtime.daemon.windows_pipe import default_pipe_name, pipe_ready

            ready = pipe_ready(default_pipe_name(runtime_dir()), 0)
            logger.debug(f"_daemon_socket_available win-pipe ready={ready}")
            return ready
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
        await self._set_degraded(True)

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

    async def _start_subscribe_loop(self) -> None:
        """Start the daemon subscription loop, holding ONE durable subscription.

        A restart is refused while the subscription task is live (so a healthy
        stream is never duplicated) AND throttled to at most once per
        :attr:`_reconnect_min_interval`: a stream the daemon repeatedly drops
        would otherwise be re-subscribed on every probe tick (~2/s), flooding
        the daemon with full re-subscribes -- the storm this throttle fixes.
        The ``since`` cursor makes each reconnect cheap.
        """
        if self._subscribe_task is not None and not self._subscribe_task.done():
            return
        now = time.monotonic()
        if now - self._last_subscribe_monotonic < self._reconnect_min_interval:
            return
        self._last_subscribe_monotonic = now
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
                await self._set_degraded(True)
        else:
            if not self._stopping:
                self._consecutive_failures = 0

    def _subscribe_params(self) -> dict[str, object]:
        """Build the ``state.subscribe`` params for the EVENT push stream.

        The subscription is deliberately NOT narrowed to the bound state's
        URN: fleet lifecycle events are wave- and iter-scoped, so a daemon
        bus filter keyed on the state URN would drop every one of them and
        the feed would never see a claim / close / spawn. The App fans each
        delivered envelope out to the panes, which filter client-side (the
        agent-watch zoom narrows to one session).
        """
        kinds = [StoreKind.EVENT.value]
        if self._subscribe_method == PROJECTION_SUBSCRIBE_METHOD:
            # a Run's appended lines and a Batch's proof move no projection row, so the
            # projection feed takes their notices beside its patches to show them live
            kinds.append(StoreKind.RUN_EVENTS_APPENDED.value)
            kinds.append(StoreKind.BATCH_PROOF_NOTED.value)
        params: dict[str, object] = {"kinds": kinds}
        # Resume from the last delivered event so a reconnect continues the
        # stream instead of re-requesting the full backlog (since_event_id=None).
        if self._last_event_id is not None:
            params["since_event_id"] = self._last_event_id
        return params

    def _run_subscription(self, loop: asyncio.AbstractEventLoop) -> None:
        """Subscribe and stream pushes, starting afresh when the cursor cannot resume.

        A daemon that cannot replay from the cursor -- the log no longer holds it,
        or the gap exceeds the catch-up bound -- refuses the subscribe. The stream
        is then opened again from no cursor, owing a full re-read, so the bound
        state is never left standing before the gap.
        """
        stream = self._stream_windows if os.name == "nt" else self._stream_posix
        try:
            stream(loop, self._subscribe_params())
        except DaemonRpcError as exc:
            if exc.code != CATCH_UP_TOO_LARGE or self._last_event_id is None:
                raise
            logger.info(
                f"_run_subscription unreplayable cursor={self._last_event_id!r} cause={exc}"
            )
            self._last_event_id = None
            self._resync_owed = True
            stream(loop, self._subscribe_params())

    def _stream_posix(self, loop: asyncio.AbstractEventLoop, params: dict[str, object]) -> None:
        """Stream pushes over the daemon socket until it ends or the binder stops.

        The client's call timeout bounds the subscribe round-trip only. It is
        cleared for the stream, because a socket read that times out marks its
        reader dead ("cannot read from timed out object") and an idle stream
        would die with it; ``disconnect`` wakes the untimed read by shutting the
        socket down instead.
        """
        with self._client_factory() as client:
            sock = getattr(client, "_sock", None)
            self._subscription_sock = sock
            try:
                # Published before this check so a ``disconnect`` racing the
                # connect either sees the socket to shut down or is seen here.
                if self._stopping:
                    return
                client.call(self._subscribe_method, params)
                if sock is not None:
                    sock.settimeout(None)
                self._announce_connected(loop, params)
                while not self._stopping:
                    reader = getattr(client, "_reader", None)
                    if reader is None:
                        raise RuntimeError("daemon reader unavailable")
                    line = reader.readline()
                    if not line:
                        raise RuntimeError("daemon subscribe stream ended")
                    self._handle_push_line(loop, line)
            finally:
                self._subscription_sock = None

    def _announce_connected(
        self, loop: asyncio.AbstractEventLoop, params: dict[str, object]
    ) -> None:
        """Record an acknowledged subscribe and schedule its delivery-side effects.

        A stream re-established without a cursor resumes nothing, so whatever was
        committed while the link was down is owed as a full re-read; the first
        stream owes none, since the caller has only just read the state it binds.
        """
        resync = self._resync_owed or (self._subscribed_once and "since_event_id" not in params)
        self._resync_owed = False
        self._subscribed_once = True
        self._consecutive_failures = 0
        asyncio.run_coroutine_threadsafe(self._on_subscription_connected(resync=resync), loop)

    def _stream_windows(self, loop: asyncio.AbstractEventLoop, params: dict[str, object]) -> None:
        """Stream ``state.subscribe`` pushes over a persistent named pipe.

        Unlike the connectionless request/reply pipe path, a subscription
        holds the pipe handle open: it opens the pipe + sends the subscribe
        frame (:func:`open_subscription_pipe`), reads the ack, then loops
        reading ``event.push`` frames via :class:`PipeReader`. The handle is
        stored on ``self._win_pipe`` so :meth:`disconnect` can ``CancelIoEx``
        the blocked read -- the only reliable way to unblock a pending
        ``ReadFile`` (``CloseHandle`` alone does not). The cancel makes the
        blocked read raise, which exits the loop cleanly under
        ``self._stopping``. Every pywin32 reference lives in the win32-only
        ``windows_pipe`` module so this binding stays import-clean on POSIX.

        Raises:
            DaemonRpcError: The daemon refused the subscribe, as the socket
                client raises it, so a cursor it cannot replay is handled once.
        """
        from eawf.runtime.daemon.windows_pipe import (
            PipeReader,
            close_pipe,
            default_pipe_name,
            open_subscription_pipe,
        )

        request = orjson.dumps(
            {
                "jsonrpc": "2.0",
                "id": "tui-subscribe",
                "method": self._subscribe_method,
                "params": params,
            }
        )
        handle = open_subscription_pipe(
            default_pipe_name(runtime_dir()), request + b"\n", wait_ms=1000
        )
        self._win_pipe = handle
        reader = PipeReader(handle)
        try:
            # First message is the subscribe ack, or the refusal in its place.
            error = orjson.loads(reader.read_message()).get("error")
            if isinstance(error, dict):
                raise DaemonRpcError(int(error.get("code", 0)), str(error.get("message", "")))
            self._announce_connected(loop, params)
            while not self._stopping:
                line = reader.read_message()
                if not line:
                    raise RuntimeError("daemon subscribe stream ended")
                self._handle_push_line(loop, line)
        finally:
            self._win_pipe = None
            close_pipe(handle)

    async def _on_subscription_connected(self, *, resync: bool) -> None:
        """Clear failures + degraded once push connects, and owe a re-read if asked.

        Args:
            resync: Whether the stream came back unable to replay its gap, so
                everything it feeds is read again before the pushes after it.
        """
        self._consecutive_failures = 0
        await self._set_degraded(False)
        if resync and self._callbacks.on_resync is not None:
            await self._callbacks.on_resync()

    def _handle_push_line(self, loop: asyncio.AbstractEventLoop, line: bytes) -> None:
        """Decode one push frame and schedule delivery for the feed it belongs to.

        The two feeds share this socket and differ only in the frame the daemon
        writes, so the method name is what selects the leg. A frame belonging to
        neither is dropped, which is what keeps an epoch-1 binder unaffected by a
        projection frame it did not subscribe for.
        """
        try:
            frame = orjson.loads(line)
            method = frame.get("method")
            params = frame.get("params")
            if not isinstance(params, dict):
                return
            if method == PROJECTION_PUSH_METHOD:
                self._handle_patch_frame(loop, params)
                return
            if method != "event.push":
                return
            envelope = Envelope.model_validate(params.get("event"))
        except (orjson.JSONDecodeError, ValidationError, ValueError) as exc:
            logger.debug(f"_handle_push_line skip cause={exc!r}")
            return
        if self._subscribe_method == PROJECTION_SUBSCRIBE_METHOD:
            # a notice on the projection feed is no state change: it reaches the event
            # sink alone, and the resume cursor stays the last patch's event
            asyncio.run_coroutine_threadsafe(self._deliver_notice(envelope), loop)
            return
        # Advance the resume cursor so a later reconnect resumes past this event.
        self._last_event_id = envelope.id
        asyncio.run_coroutine_threadsafe(self._handle_push(envelope), loop)

    def _handle_patch_frame(self, loop: asyncio.AbstractEventLoop, params: dict[str, Any]) -> None:
        """Validate one keyed patch, advance the resume cursors and deliver it.

        A malformed patch is dropped rather than raised: the stream carries every
        subscriber's patches, so one bad frame must not end the feed. The cursors
        do not advance over it, so a later reconnect asks from the last patch
        actually applied: the ordinal for the console's own reconnect, and the
        envelope the patch came from for the stream's.
        """
        if self._callbacks.on_patch is None:
            return
        try:
            patch = KeyedPatch.model_validate(params.get("patch"))
        except (ValidationError, ValueError) as exc:
            logger.debug(f"_handle_patch_frame skip cause={exc!r}")
            return
        self._last_canonical_sequence = max(self._last_canonical_sequence, patch.canonical_sequence)
        event_id = params.get("event_id")
        if isinstance(event_id, str):
            self._last_event_id = event_id
        asyncio.run_coroutine_threadsafe(self._deliver_patch(patch), loop)

    async def _deliver_notice(self, envelope: Envelope) -> None:
        """Hand one notice from the projection feed to the event callback, if one is set."""
        if self._callbacks.on_event is not None:
            await self._callbacks.on_event(envelope)

    async def _deliver_patch(self, patch: KeyedPatch) -> None:
        """Hand one keyed patch to the projection callback, if one is registered."""
        if self._callbacks.on_patch is not None:
            await self._callbacks.on_patch(patch)

    @property
    def resume_cursor(self) -> int:
        """Return the ordinal the projection feed last acknowledged; ``0`` when none."""
        return self._last_canonical_sequence

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Run one request/response RPC over this binder's daemon transport.

        The seam calls the daemon through the binder rather than holding a client
        of its own, so the console keeps one transport to open, authorise, probe
        and reconnect. The call is blocking, so it runs off the event loop.

        Args:
            method: The dotted JSON-RPC method name.
            params: The request parameters.

        Returns:
            The result mapping the daemon answered with.
        """

        def _blocking_call() -> dict[str, Any]:
            with self._client_factory() as client:
                return client.call(method, params)

        return await asyncio.to_thread(_blocking_call)

    async def _handle_push(self, envelope: Envelope) -> None:
        """Forward one live event envelope to the event callback, if one is set."""
        if self._callbacks.on_event is not None:
            await self._callbacks.on_event(envelope)

    def _cancel_win_pipe_read(self) -> None:
        """Cancel the streaming-pipe read on Windows via ``CancelIoEx``.

        No-op on POSIX (no ``_win_pipe`` handle) and when no subscription
        pipe is open. Tolerant of a handle already closed by the
        subscription thread's own teardown -- the goal is only to unblock
        a still-pending ``ReadFile`` so ``disconnect`` returns promptly.
        """
        pipe = self._win_pipe
        if pipe is None:
            return
        from eawf.runtime.daemon.windows_pipe import cancel_pending_read

        with contextlib.suppress(Exception):
            cancel_pending_read(pipe)

    def _shutdown_subscription_sock(self) -> bool:
        """Shut the POSIX subscription socket down to wake its blocked read.

        Returns:
            Whether a socket was open to shut down. ``False`` on Windows and
            when no subscription holds a socket. A socket the subscription
            thread already closed raises ``OSError``, which is ignored: the
            read it would have woken has already ended.
        """
        sock = self._subscription_sock
        if sock is None:
            return False
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
        return True

    async def disconnect(self) -> None:
        """Cancel background tasks on app teardown.

        The live subscription thread is parked in a blocking read that
        cancelling the asyncio task does NOT unblock: a ``ReadFile`` on the
        Windows streaming pipe, a ``readline()`` on the POSIX socket. So
        this first ``CancelIoEx``-cancels the pending pipe read (via
        :func:`cancel_pending_read`) or shuts the socket down, so the read
        returns promptly and the thread exits, then cancels the task. After
        a socket shutdown the task is given a moment to finish on its own,
        so the thread has exited by the time this returns.
        """
        self._stopping = True
        self._cancel_win_pipe_read()
        woken = self._shutdown_subscription_sock()
        if self._subscribe_task is not None:
            if woken:
                await asyncio.wait({self._subscribe_task}, timeout=_SUBSCRIPTION_EXIT_GRACE_S)
            self._subscribe_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._subscribe_task
            self._subscribe_task = None
        if self._probe_task is not None:
            self._probe_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._probe_task
            self._probe_task = None


__all__ = [
    "DEFAULT_SUBSCRIBE_METHOD",
    "StateBinding",
    "StateBindingCallbacks",
]
