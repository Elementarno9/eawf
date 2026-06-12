"""Windows named-pipe listener bridged into the asyncio loop.

A dedicated OS thread runs the blocking pywin32 pipe-accept loop and
hands each accepted frame to the asyncio event loop via
``loop.call_soon_threadsafe``. The asyncio loop dequeues the frame,
invokes the JSON-RPC handler (same dispatcher the POSIX UDS listener
uses), and posts the response bytes back through a reply callback so
the listener thread can ``WriteFile`` to the pipe.

The listener thread owns the pipe lifecycle and never awaits
coroutines, while the asyncio task owns the dispatch and never blocks
on Windows I/O.
Cross-thread coordination uses :class:`threading.Event` for shutdown
+ per-frame ``done`` signalling, and :func:`asyncio.Queue` for the
work hand-off.

Import-guarded on non-Windows so the POSIX dev loop is not
contaminated by pywin32 attribute access at import time. Callers
gate ``import eawf.runtime.daemon.windows_pipe`` behind ``sys.platform ==
"win32"`` to avoid the :class:`ImportError`.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import getpass
import json
import logging
import queue as _queue
import sys
import threading
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

if sys.platform != "win32":
    raise ImportError("eawf.runtime.daemon.windows_pipe is win32-only")

if sys.platform == "win32":  # pragma: no cover - win32-only branch
    import pywintypes
    import win32file
    import win32pipe
    import winerror

logger = logging.getLogger(__name__)


# Per-read chunk size handed to ``win32file.ReadFile``. A message-mode pipe
# whose message exceeds this chunk does NOT truncate: ``ReadFile`` returns
# ``ERROR_MORE_DATA`` and the rest of the message stays queued for the next
# read. The reassembly loops (:func:`_read_full_message` server-side,
# :func:`pipe_client_call` client-side) drain that tail. The value matches
# the per-instance buffer the listener requests from ``CreateNamedPipe`` --
# it is a chunking hint, NOT a frame ceiling.
_PIPE_BUFFER_BYTES = 65536

# Default wait (ms) for ``WaitNamedPipe`` when a client opens the pipe. The
# server may be mid-accept between connections; the wait blocks until an
# instance is free rather than failing fast with ERROR_PIPE_BUSY. Critically,
# ``WaitNamedPipe`` does NOT consume a pipe instance -- it only blocks until
# one is available, so a readiness probe through it never steals the slot a
# real RPC needs.
_DEFAULT_WAIT_MS = 5000


FrameHandler = Callable[[bytes], Awaitable[bytes]]
ReplyCallback = Callable[[bytes], None]

# Heartbeat cadence (seconds) for the subscription idle reap. Between
# ``event.push`` frames the streamer wakes every heartbeat to ``PeekNamedPipe``
# the client side: if the peer has closed (or the daemon is shutting down) the
# stream tears down promptly instead of blocking forever on a silent bus.
_IDLE_HEARTBEAT_SECONDS = 5.0

# The subscribe RPC method names that switch the listener from
# request/response into streaming mode. Mirrors
# ``state_subscribe.SUBSCRIBE_METHODS`` but is duplicated here as a literal so
# this win32-only module never imports the POSIX dispatch graph at module top.
_SUBSCRIBE_METHODS = frozenset({"event.subscribe", "state.subscribe"})

if sys.platform == "win32":  # pragma: no cover - win32-only branch
    # ``CancelIoEx`` is the only reliable way to unblock a pending blocking
    # ``ReadFile`` on a named pipe (gotcha 1: ``CloseHandle`` alone does NOT
    # unblock it -- the read stays parked until data or a real cancel). The
    # client uses this from ``disconnect()`` so a TUI teardown returns
    # promptly instead of hanging on the streaming read. ``argtypes`` is set
    # once at module load (W05 contract) so every call marshals the handle
    # correctly.
    _CancelIoEx = ctypes.windll.kernel32.CancelIoEx
    _CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _CancelIoEx.restype = ctypes.c_bool


class SubscriptionFeed(Protocol):
    """A thread-pullable source of ``event.push`` frames for one subscriber.

    The Windows listener thread cannot ``await`` the asyncio
    :class:`~eawf.runtime.daemon.bus.EventBus`, so the daemon main bridges a
    bus subscriber into this thread-safe surface: :meth:`next_frame` blocks
    (up to a timeout) for the next push frame, and :meth:`close` unregisters
    the subscriber so :attr:`EventBus.active_subscriptions` drops when the
    client leaves.
    """

    def next_frame(self, timeout: float) -> bytes | None:
        """Return the next push frame, or ``None`` on *timeout* expiry.

        Args:
            timeout: Seconds to block for the next frame.

        Returns:
            One newline-terminated ``event.push`` frame, or ``None`` when
            no frame arrived within *timeout* (the caller then heartbeats).
        """
        ...

    def close(self) -> None:
        """Unregister the underlying subscriber (idempotent)."""
        ...


#: Builds a :class:`SubscriptionFeed` from a parsed subscribe params dict, or
#: returns ``None`` to reject the subscribe (e.g. no bus). The daemon main
#: wires this to the live :class:`EventBus`; tests pass a fake feed.
SubscribeRouter = Callable[[dict[str, Any]], "SubscriptionFeed | None"]


def cancel_pending_read(pipe: Any) -> bool:
    """Cancel an in-flight blocking ``ReadFile`` on *pipe* via ``CancelIoEx``.

    The teardown primitive for the streaming client: a subscription reader
    blocked in ``ReadFile`` does not unblock on ``CloseHandle`` alone, so
    ``disconnect()`` calls this to cancel the pending I/O. The blocked read
    then returns with ``ERROR_OPERATION_ABORTED`` and the reader thread
    exits, after which the caller closes the handle.

    Args:
        pipe: The connected pipe handle whose pending read should cancel.

    Returns:
        ``True`` when ``CancelIoEx`` accepted the cancel; ``False`` when
        there was no pending I/O to cancel (already idle / closed).
    """
    try:
        handle = int(pipe)
    except TypeError, ValueError:
        handle = pipe
    return bool(_CancelIoEx(ctypes.c_void_p(handle), None))


def _peer_still_connected(pipe: Any) -> bool:
    """Return whether the streaming peer is still attached, via PeekNamedPipe.

    ``PeekNamedPipe`` is non-destructive: it reports buffered bytes without
    consuming them, and raises ``ERROR_BROKEN_PIPE`` once the client closes
    its handle. The streamer calls this each heartbeat so a client that
    left without a clean unsubscribe is reaped instead of stranding the
    subscriber on the bus.

    Args:
        pipe: The connected pipe handle to probe.

    Returns:
        ``True`` while the peer is connected; ``False`` once it closed.
    """
    try:
        win32pipe.PeekNamedPipe(pipe, 0)
    except pywintypes.error:
        return False
    return True


def _read_first_chunk(pipe: Any) -> tuple[bytes, bool]:
    """Read ONE bounded chunk and report whether the message continues.

    Used server-side to read the first ``_PIPE_BUFFER_BYTES`` of a client
    message BEFORE impersonating the peer for the SID check. Two reasons
    this single bounded read precedes the SID verify:

    1. ``ImpersonateNamedPipeClient`` requires the server to have read
       from the pipe first, so the client's security context is the one
       impersonated -- impersonating before any read is unreliable.
    2. The DACL (owner-only) is the primary gate; this read is bounded to
       one chunk so an unauthenticated peer can move at most
       ``_PIPE_BUFFER_BYTES`` of bytes through the server before the SID
       check runs (the accepted residual the W07 threat-model note
       records). The MORE_DATA tail is drained only AFTER the SID passes.

    Args:
        pipe: Connected named-pipe handle.

    Returns:
        ``(chunk, more)`` -- the first chunk's bytes and ``True`` when the
        message continues (an ``ERROR_MORE_DATA`` tail remains to drain).

    Raises:
        pywintypes.error: For any pipe error other than ``ERROR_MORE_DATA``.
    """
    try:
        hr, segment = win32file.ReadFile(pipe, _PIPE_BUFFER_BYTES)
    except pywintypes.error as exc:
        if exc.winerror == winerror.ERROR_MORE_DATA:
            return b"", True
        raise
    return bytes(segment), hr == winerror.ERROR_MORE_DATA


def _read_full_message(pipe: Any) -> bytes:
    """Read one complete pipe message, draining the ``ERROR_MORE_DATA`` tail.

    A message-mode named pipe delivers one logical message per send, but a
    single ``ReadFile`` only returns up to its chunk size. When the message
    is larger, ``ReadFile`` raises ``pywintypes.error`` with
    ``winerror.ERROR_MORE_DATA`` and leaves the remainder queued; the loop
    keeps reading until a call returns without that code, which marks the
    message boundary. Without this loop a frame over ``_PIPE_BUFFER_BYTES``
    (e.g. a large ``state.mutate`` payload) would be silently truncated.

    Args:
        pipe: Connected named-pipe handle (server or client side).

    Returns:
        The full message bytes, concatenated across every MORE_DATA chunk.

    Raises:
        pywintypes.error: For any pipe error other than ``ERROR_MORE_DATA``
            (e.g. the peer closed the handle).
    """
    chunks: list[bytes] = []
    while True:
        try:
            hr, segment = win32file.ReadFile(pipe, _PIPE_BUFFER_BYTES)
        except pywintypes.error as exc:
            if exc.winerror == winerror.ERROR_MORE_DATA:
                continue
            raise
        chunks.append(bytes(segment))
        # hr == ERROR_MORE_DATA means the message continues; 0 means done.
        if hr != winerror.ERROR_MORE_DATA:
            break
    return b"".join(chunks)


def pipe_ready(pipe_name: str, wait_ms: int = 0) -> bool:
    r"""Return True when a pipe instance for *pipe_name* is available.

    A readiness probe for the spawn poll loop. ``WaitNamedPipe`` blocks up
    to *wait_ms* for a free instance and -- critically -- does NOT open or
    consume one, so probing readiness never steals the slot a real RPC
    needs. A bound pipe with a free instance returns True; an unbound pipe
    (``ERROR_FILE_NOT_FOUND``) or no instance free within the wait
    (``ERROR_SEM_TIMEOUT``) returns False so the caller keeps polling.

    Args:
        pipe_name: The ``\\.\pipe\eawfd-<user>`` path to probe.
        wait_ms: Milliseconds to wait for a free instance (0 = poll once).

    Returns:
        True when a pipe instance is available; False otherwise.
    """
    try:
        return bool(win32pipe.WaitNamedPipe(pipe_name, wait_ms))
    except pywintypes.error:
        return False


def pipe_client_call(
    pipe_name: str,
    payload: bytes,
    *,
    wait_ms: int = _DEFAULT_WAIT_MS,
) -> bytes:
    r"""Open *pipe_name*, write *payload*, read one full response message.

    The synchronous client transport the CLI ``DaemonClient`` uses on
    Windows. Procedure:

    1. ``WaitNamedPipe`` until an instance is free (does NOT consume one).
    2. ``CreateFile`` the pipe and switch it to message read-mode so a
       single logical response maps to one message.
    3. ``WriteFile`` the request frame.
    4. Drain the response via :func:`_read_full_message` so a reply larger
       than the pipe buffer reassembles rather than truncating.

    Args:
        pipe_name: The ``\\.\pipe\eawfd-<user>`` path the daemon bound.
        payload: One JSON-RPC request frame (newline-terminated by the
            caller, matching the POSIX UDS wire format).
        wait_ms: Milliseconds to wait for a free pipe instance.

    Returns:
        The full response message bytes (one JSON-RPC response frame).

    Raises:
        pywintypes.error: When the pipe cannot be opened or the round-trip
            fails for a reason other than MORE_DATA chunking.
    """
    win32pipe.WaitNamedPipe(pipe_name, wait_ms)
    handle = win32file.CreateFile(
        pipe_name,
        win32file.GENERIC_READ | win32file.GENERIC_WRITE,
        0,
        None,
        win32file.OPEN_EXISTING,
        0,
        None,
    )
    try:
        win32pipe.SetNamedPipeHandleState(
            handle,
            win32pipe.PIPE_READMODE_MESSAGE,
            None,
            None,
        )
        win32file.WriteFile(handle, payload)
        return _read_full_message(handle)
    finally:
        win32file.CloseHandle(handle)


def open_subscription_pipe(
    pipe_name: str,
    request_frame: bytes,
    *,
    wait_ms: int = _DEFAULT_WAIT_MS,
) -> Any:
    r"""Open a persistent pipe for a streaming subscription and send the request.

    Unlike :func:`pipe_client_call` (which closes after one round-trip), a
    subscription holds the handle open for the lifetime of the stream. This
    opens the pipe, switches it to message read-mode, writes the subscribe
    *request_frame*, and returns the handle. The caller reads the ack +
    streams ``event.push`` frames via :class:`PipeReader`, cancels a blocked
    read with :func:`cancel_pending_read`, and finally closes the handle
    with :func:`close_pipe`. Keeping every pywin32 reference inside this
    win32-only module lets the TUI binding stay free of guarded imports.

    Args:
        pipe_name: The per-user pipe path the daemon bound.
        request_frame: The newline-terminated ``state.subscribe`` frame.
        wait_ms: Milliseconds to wait for a free pipe instance.

    Returns:
        The connected pipe handle (held open for the stream).
    """
    win32pipe.WaitNamedPipe(pipe_name, wait_ms)
    handle = win32file.CreateFile(
        pipe_name,
        win32file.GENERIC_READ | win32file.GENERIC_WRITE,
        0,
        None,
        win32file.OPEN_EXISTING,
        0,
        None,
    )
    win32pipe.SetNamedPipeHandleState(handle, win32pipe.PIPE_READMODE_MESSAGE, None, None)
    win32file.WriteFile(handle, request_frame)
    return handle


def close_pipe(handle: Any) -> None:
    """Close a pipe handle, tolerating an already-closed handle.

    Args:
        handle: The pipe handle to close.
    """
    with contextlib.suppress(pywintypes.error):
        win32file.CloseHandle(handle)


class PipeReader:
    """Drain full messages from a connected pipe handle, reassembly-safe.

    A thin object wrapper over :func:`_read_full_message` so the streaming
    subscription path (W03) and any caller that holds a long-lived
    connected pipe can pull one complete message at a time without each
    re-implementing the ``ERROR_MORE_DATA`` loop. The reader does NOT own
    the handle lifecycle -- the caller opens and closes the pipe.

    Attributes:
        pipe: The connected named-pipe handle to read from.
    """

    def __init__(self, pipe: Any) -> None:
        """Bind the reader to a connected pipe handle.

        Args:
            pipe: Connected named-pipe handle.
        """
        self._pipe = pipe

    def read_message(self) -> bytes:
        """Return the next complete message, draining the MORE_DATA tail.

        Returns:
            Full message bytes across every ``ERROR_MORE_DATA`` chunk.

        Raises:
            pywintypes.error: For any pipe error other than ``ERROR_MORE_DATA``.
        """
        return _read_full_message(self._pipe)


class _BusSubscriptionFeed:
    """Bridge one asyncio :class:`EventBus` subscriber to the listener thread.

    The listener thread cannot ``await`` the bus, so this feed runs an
    asyncio task on the daemon loop that pulls
    :meth:`EventBus.iter_subscriber_pushes`, frames each envelope as an
    ``event.push`` notification, and hands the frames to the listener
    thread through a thread-safe :class:`queue.Queue`. :meth:`next_frame`
    blocks on that queue; :meth:`close` unregisters the subscriber (so
    :attr:`EventBus.active_subscriptions` drops) and stops the task.

    Implements the :class:`SubscriptionFeed` protocol.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        bus: Any,
        subscriber: Any,
        connection_id: str,
    ) -> None:
        """Wire the queue and start the asyncio pump task.

        Args:
            loop: The daemon asyncio loop owning the bus.
            bus: The live :class:`EventBus`.
            subscriber: The registered subscriber to drain.
            connection_id: The id to unregister on close.
        """
        self._loop = loop
        self._bus = bus
        self._subscriber = subscriber
        self._connection_id = connection_id
        self._frames: _queue.Queue[bytes] = _queue.Queue()
        self._closed = threading.Event()
        self._pump = asyncio.run_coroutine_threadsafe(self._pump_loop(), loop)

    async def _pump_loop(self) -> None:
        """Drain the bus subscriber, framing each push for the thread queue."""
        async for envelope in self._bus.iter_subscriber_pushes(self._subscriber):
            notification = {
                "jsonrpc": "2.0",
                "method": "event.push",
                "params": {"event": envelope.model_dump(mode="json")},
            }
            self._frames.put(json.dumps(notification).encode("utf-8") + b"\n")

    def next_frame(self, timeout: float) -> bytes | None:
        """Return the next push frame, or ``None`` on *timeout* expiry."""
        try:
            return self._frames.get(timeout=timeout)
        except _queue.Empty:
            return None

    def close(self) -> None:
        """Unregister the subscriber and stop the pump task. Idempotent."""
        if self._closed.is_set():
            return
        self._closed.set()
        self._loop.call_soon_threadsafe(self._bus.unregister, self._connection_id)
        self._pump.cancel()


def make_bus_subscribe_router(
    loop: asyncio.AbstractEventLoop,
    ctx: Any,
) -> SubscribeRouter:
    """Build a :class:`SubscribeRouter` bound to *ctx*'s live event bus.

    The daemon main passes the returned router to
    :class:`WindowsPipeServer` so a subscribe frame on the pipe registers
    a bus subscriber and streams ``event.push`` frames. The router runs
    the registration on the asyncio loop (the bus is loop-owned) and
    wraps the result in a :class:`_BusSubscriptionFeed`. Returns a router
    that yields ``None`` when ``ctx.bus`` is not a live bus, so the
    listener falls back to the rejecting request/reply handler.

    Args:
        loop: The daemon asyncio loop.
        ctx: The :class:`~eawf.runtime.daemon.methods.MethodContext` carrying
            ``bus`` + ``event_path``.

    Returns:
        A router callable mapping subscribe params to a feed (or ``None``).
    """
    import uuid

    from eawf.runtime.daemon.bus import EventBus
    from eawf.runtime.daemon.methods.event import subscribe as run_subscribe

    def _router(params: dict[str, Any]) -> SubscriptionFeed | None:
        if not isinstance(ctx.bus, EventBus):
            return None
        connection_id = f"winpipe-{uuid.uuid4().hex[:12]}"

        async def _register() -> Any:
            # Registration creates the subscriber's asyncio.Event and
            # mutates the bus dict, both of which MUST run on the loop
            # thread that owns the bus.
            sub, _backlog = run_subscribe(
                ctx.bus,
                connection_id=connection_id,
                params=params,
                event_path=ctx.event_path,
            )
            return sub

        future = asyncio.run_coroutine_threadsafe(_register(), loop)
        subscriber = future.result()
        return _BusSubscriptionFeed(loop, ctx.bus, subscriber, connection_id)

    return _router


def default_pipe_name(username: str | None = None) -> str:
    r"""Return the canonical pipe path ``\\.\pipe\eawfd-<username>``.

    Args:
        username: Override the resolved user (tests). When ``None`` the
            current login is read via :func:`getpass.getuser`.

    Returns:
        Per-user pipe path the daemon binds to.
    """
    if username is None:
        username = getpass.getuser()
    return rf"\\.\pipe\eawfd-{username}"


class WindowsPipeServer:
    """Named-pipe listener + asyncio dispatch bridge.

    The constructor wires the queue + threading event but does NOT
    open the pipe — call :meth:`start` to launch the listener thread
    and asyncio dispatch task. Tests build the server without
    starting it to assert the constructor wiring contract.

    Attributes:
        loop: The asyncio loop that owns the dispatch task. Threads
            call :func:`loop.call_soon_threadsafe` to deliver work.
        handler: Async callable that consumes one JSON-RPC frame and
            returns the response bytes. Matches the dispatch shape
            the POSIX listener uses.
        pipe_name: Path the listener thread binds to.
        verify_sid: Optional per-connection SID verification target.
            ``None`` disables the post-connect check (tests only).
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        handler: FrameHandler,
        *,
        pipe_name: str | None = None,
        verify_sid: Any | None = None,
        verify_sid_enabled: bool = True,
        subscribe_router: SubscribeRouter | None = None,
    ) -> None:
        """Wire the queue + shutdown event.

        Args:
            loop: asyncio loop running the daemon.
            handler: Frame handler — same dispatcher the POSIX
                listener uses.
            pipe_name: Override the resolved pipe path (tests). Falls
                back to :func:`default_pipe_name` for the current user.
            verify_sid: Pinned expected SID for the post-connect
                check; when ``None`` the helper resolves the current
                user at verification time.
            verify_sid_enabled: When False, skip the SID check (tests
                without an impersonable pipe client).
            subscribe_router: Builds a :class:`SubscriptionFeed` from a
                parsed subscribe params dict so the listener can stream
                ``event.push`` frames on a kept-open pipe. ``None``
                disables the streaming path (a subscribe frame then falls
                through to the request/response handler, which rejects it).
        """
        self._loop = loop
        self._handler = handler
        self._pipe_name = pipe_name or default_pipe_name()
        self._verify_sid = verify_sid
        self._verify_sid_enabled = verify_sid_enabled
        self._subscribe_router = subscribe_router
        self._queue: asyncio.Queue[tuple[bytes, ReplyCallback]] = asyncio.Queue()
        self._shutdown = threading.Event()
        self._listener_thread: threading.Thread | None = None
        self._dispatch_task: asyncio.Task[None] | None = None

    @property
    def pipe_path(self) -> str:
        """Return the pipe path the listener will bind / has bound to."""
        return self._pipe_name

    def is_listening(self) -> bool:
        """Return True when the listener thread is alive."""
        return self._listener_thread is not None and self._listener_thread.is_alive()

    def start(self) -> None:
        """Launch the listener thread + asyncio dispatch task.

        The asyncio task is scheduled on the loop passed to
        :meth:`__init__`; the listener thread is a daemon thread so a
        terminal SIGINT does not hang the process.

        Raises:
            RuntimeError: When called twice on the same instance.
        """
        if self._listener_thread is not None:
            raise RuntimeError("windows pipe server already started")
        logger.info(f"start pipe={self._pipe_name!r}")
        self._listener_thread = threading.Thread(
            target=self._listen_loop,
            name="eawfd-pipe-listener",
            daemon=True,
        )
        self._listener_thread.start()
        self._dispatch_task = self._loop.create_task(self._dispatch_loop())

    def stop(self) -> None:
        """Signal the listener + dispatch task to exit.

        Sets the shutdown event so the listener loop returns on its
        next iteration; opens a no-op client connection to the pipe
        so :func:`win32pipe.ConnectNamedPipe` unblocks immediately
        rather than waiting for a real client. The asyncio dispatch
        task drains any in-flight frames before returning.
        """
        logger.info("stop signal-sent")
        self._shutdown.set()
        self._wake_listener()
        if self._dispatch_task is not None:
            self._dispatch_task.cancel()

    def _wake_listener(self) -> None:
        """Open + close a client pipe handle so the accept loop unblocks."""
        try:
            handle = win32file.CreateFile(
                self._pipe_name,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0,
                None,
                win32file.OPEN_EXISTING,
                0,
                None,
            )
            win32file.CloseHandle(handle)
        except pywintypes.error:
            # Listener already exited before we connected — fine.
            logger.debug("_wake_listener listener-already-stopped")

    def _listen_loop(self) -> None:
        """Blocking accept loop owned by the listener thread.

        Each iteration:
          1. Build a fresh pipe instance with the user-restricted DACL.
          2. Wait for a client connection.
          3. Read the FIRST bounded chunk of the request (so the peer's
             security context exists to impersonate, and so an
             unauthenticated peer moves at most one chunk pre-verify).
          4. Verify the peer SID (read-before-impersonate order); on
             mismatch write the ``-32000`` envelope and loop.
          5. Drain any ``ERROR_MORE_DATA`` tail of the request only AFTER
             the SID passes.
          6. Hand the full frame to asyncio via :func:`Queue.put_nowait`
             through :func:`loop.call_soon_threadsafe`.
          7. Wait on a per-frame :class:`threading.Event` for the reply.
          8. ``WriteFile`` the reply, close the pipe instance.

        Exceptions in any step are logged at WARNING; the listener
        attempts to close the current pipe instance and loop back so a
        single malformed client does not take down the daemon.
        """
        from eawf.runtime.daemon.windows_security import (
            WindowsAuthError,
            build_user_only_security_attributes,
            verify_peer_sid,
        )

        sec_attrs = build_user_only_security_attributes()

        while not self._shutdown.is_set():
            pipe = None
            try:
                pipe = win32pipe.CreateNamedPipe(
                    self._pipe_name,
                    win32pipe.PIPE_ACCESS_DUPLEX,
                    win32pipe.PIPE_TYPE_MESSAGE
                    | win32pipe.PIPE_READMODE_MESSAGE
                    | win32pipe.PIPE_WAIT,
                    win32pipe.PIPE_UNLIMITED_INSTANCES,
                    _PIPE_BUFFER_BYTES,
                    _PIPE_BUFFER_BYTES,
                    0,
                    sec_attrs,
                )
                win32pipe.ConnectNamedPipe(pipe, None)
                if self._shutdown.is_set():
                    break

                # Read-before-impersonate: read ONE bounded chunk so the
                # client's security context is established for the SID check.
                # This is the single pre-verify read the W07 threat-model
                # note accepts; the DACL is the primary gate.
                first_chunk, more = _read_first_chunk(pipe)

                if self._verify_sid_enabled:
                    try:
                        verify_peer_sid(pipe, self._verify_sid)
                    except WindowsAuthError as exc:
                        logger.warning(f"_listen_loop reject reason={exc!s}")
                        self._write_unauthorized(pipe)
                        continue

                # Drain the MORE_DATA tail only after the SID passed.
                payload = first_chunk
                if more:
                    payload = first_chunk + _read_full_message(pipe)

                # Subscribe frames switch the pipe into streaming mode and
                # keep it open; every other frame is one-shot request/reply.
                feed = self._maybe_open_subscription(payload)
                if feed is not None:
                    self._stream_subscription(pipe, feed)
                    # _stream_subscription owns the pipe to teardown; the
                    # finally below still closes the handle defensively.
                    continue

                response = self._await_handler(payload)
                win32file.WriteFile(pipe, response)
            except pywintypes.error as exc:
                logger.warning(f"_listen_loop pywin32 err={exc!s}")
            except Exception:
                logger.exception("_listen_loop unhandled")
            finally:
                if pipe is not None:
                    with contextlib.suppress(pywintypes.error):
                        win32file.CloseHandle(pipe)
        logger.info("_listen_loop exit")

    def _await_handler(self, payload: bytes) -> bytes:
        """Hand *payload* to the asyncio loop and block for the reply.

        Args:
            payload: Raw bytes read from the pipe (one JSON-RPC frame).

        Returns:
            Response bytes produced by :attr:`handler`.
        """
        done = threading.Event()
        holder: dict[str, bytes] = {"reply": b""}

        def _reply(reply_bytes: bytes) -> None:
            holder["reply"] = reply_bytes
            done.set()

        self._loop.call_soon_threadsafe(
            self._queue.put_nowait,
            (payload, _reply),
        )
        done.wait()
        return holder["reply"]

    def _write_unauthorized(self, pipe: Any) -> None:
        """Write a JSON-RPC ``-32000`` envelope before closing *pipe*.

        Args:
            pipe: The pipe handle to write the envelope to.
        """
        envelope = b'{"jsonrpc":"2.0","id":null,"error":{"code":-32000,"message":"unauthorized"}}\n'
        try:
            win32file.WriteFile(pipe, envelope)
        except Exception:
            logger.debug("_write_unauthorized write-failed")

    def _maybe_open_subscription(self, payload: bytes) -> SubscriptionFeed | None:
        """Open a streaming feed when *payload* is a subscribe frame.

        Sniffs the frame's ``method``; for a subscribe verb (with a router
        wired) it asks the router to build a :class:`SubscriptionFeed`
        bound to the bus. A non-subscribe frame, no router, or an
        unparseable frame returns ``None`` so the caller falls back to the
        one-shot request/reply path.

        Args:
            payload: The full request frame bytes.

        Returns:
            A live feed to stream from, or ``None`` for the non-streaming
            path.
        """
        if self._subscribe_router is None:
            return None
        try:
            frame = json.loads(payload.rstrip(b"\n"))
        except ValueError, json.JSONDecodeError:
            return None
        if not isinstance(frame, dict) or frame.get("method") not in _SUBSCRIBE_METHODS:
            return None
        params = frame.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        return self._subscribe_router(params)

    def _stream_subscription(self, pipe: Any, feed: SubscriptionFeed) -> None:
        """Stream ``event.push`` frames to *pipe* until the peer leaves.

        Writes one ack frame, then pumps frames from *feed*: each heartbeat
        with no frame triggers a :func:`_peer_still_connected` probe so a
        client that left without a clean close is reaped. The subscriber is
        always unregistered via ``feed.close()`` in the finally block, so
        :attr:`EventBus.active_subscriptions` drops once the client leaves.
        The listener thread stays free to accept and serve other pipe
        instances while this stream runs because each accept builds a fresh
        instance (``PIPE_UNLIMITED_INSTANCES``).

        Args:
            pipe: The connected pipe handle held open for the stream.
            feed: The thread-pullable subscription feed.
        """
        try:
            win32file.WriteFile(pipe, b'{"jsonrpc":"2.0","id":null,"result":{"ok":true}}\n')
            while not self._shutdown.is_set():
                frame = feed.next_frame(_IDLE_HEARTBEAT_SECONDS)
                if frame is None:
                    if not _peer_still_connected(pipe):
                        logger.info("_stream_subscription peer-gone reaped")
                        break
                    continue
                win32file.WriteFile(pipe, frame)
        except pywintypes.error as exc:
            logger.info(f"_stream_subscription pipe-closed err={exc!s}")
        finally:
            feed.close()

    async def _dispatch_loop(self) -> None:
        """Drain the queue and feed frames into :attr:`handler`.

        Cancelled by :meth:`stop`; surfaces handler exceptions by
        replying with an internal-error envelope so the listener
        thread does not deadlock waiting on a reply that will never
        arrive.
        """
        while True:
            try:
                payload, reply = await self._queue.get()
            except asyncio.CancelledError:
                logger.info("_dispatch_loop cancelled")
                return
            try:
                response = await self._handler(payload)
            except asyncio.CancelledError:
                reply(
                    b'{"jsonrpc":"2.0","id":null,"error":{"code":-32603,"message":"cancelled"}}\n'
                )
                logger.info("_dispatch_loop cancelled-mid-frame")
                return
            except Exception as exc:
                logger.exception("_dispatch_loop handler-failed")
                msg = f"internal error: {exc}".replace('"', "'")
                reply(
                    b'{"jsonrpc":"2.0","id":null,'
                    b'"error":{"code":-32603,"message":"' + msg.encode("utf-8") + b'"}}\n'
                )
            else:
                reply(response)


__all__ = [
    "PipeReader",
    "SubscribeRouter",
    "SubscriptionFeed",
    "WindowsPipeServer",
    "cancel_pending_read",
    "close_pipe",
    "default_pipe_name",
    "make_bus_subscribe_router",
    "open_subscription_pipe",
    "pipe_client_call",
    "pipe_ready",
]
