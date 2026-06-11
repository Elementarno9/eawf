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
import getpass
import logging
import sys
import threading
from collections.abc import Awaitable, Callable
from typing import Any

if sys.platform != "win32":
    raise ImportError("eawf.runtime.daemon.windows_pipe is win32-only")

if sys.platform == "win32":  # pragma: no cover - win32-only branch
    import pywintypes
    import win32file
    import win32pipe

logger = logging.getLogger(__name__)


# Per-frame buffer cap matches the JSON-RPC frame ceiling we enforce on
# the POSIX listener.
_PIPE_BUFFER_BYTES = 65536


FrameHandler = Callable[[bytes], Awaitable[bytes]]
ReplyCallback = Callable[[bytes], None]


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


#: ``ERROR_PIPE_BUSY`` — every pipe instance is serving another client; the
#: caller waits on :func:`win32pipe.WaitNamedPipe` and retries.
_ERROR_PIPE_BUSY = 231
#: ``ERROR_FILE_NOT_FOUND`` — the pipe does not exist yet (daemon still booting).
_ERROR_FILE_NOT_FOUND = 2


#: ``ERROR_SEM_TIMEOUT`` — the pipe exists but every instance is busy; still
#: proves the listener is up.
_ERROR_SEM_TIMEOUT = 121


def pipe_probe(pipe_name: str) -> bool:
    """Return True when the daemon pipe exists, without consuming a connection.

    Uses :func:`win32pipe.WaitNamedPipe` (which only checks for an
    available instance, never opens one) so the spawn-readiness poll does
    not flood the daemon listener with zero-byte connect/disconnect
    cycles. ``ERROR_SEM_TIMEOUT`` (exists-but-busy) still counts as up;
    ``ERROR_FILE_NOT_FOUND`` means "not created yet".
    """
    try:
        win32pipe.WaitNamedPipe(pipe_name, 50)
    except pywintypes.error as exc:
        return exc.winerror == _ERROR_SEM_TIMEOUT
    return True


#: Methods that switch a pipe connection into long-lived push-streaming mode.
_SUBSCRIBE_METHODS = frozenset({"event.subscribe", "state.subscribe"})

#: Idle heartbeat (seconds) between client-liveness probes while a subscription
#: stream is waiting for the next push.
_SUBSCRIBE_HEARTBEAT_S = 5.0


def _pipe_alive(pipe: Any) -> bool:
    """Return False once the client end of *pipe* has disconnected.

    PeekNamedPipe(pipe, 0) succeeds (avail=0) on a live-but-idle server pipe
    and raises ERROR_BROKEN_PIPE once the client closes its handle.
    """
    try:
        win32pipe.PeekNamedPipe(pipe, 0)
    except pywintypes.error:
        return False
    return True


def _is_subscribe_frame(payload: bytes) -> bool:
    """Return True when *payload* is an event/state.subscribe request frame."""
    import orjson

    try:
        req = orjson.loads(payload.rstrip(b"\n"))
    except orjson.JSONDecodeError:
        return False
    return isinstance(req, dict) and req.get("method") in _SUBSCRIBE_METHODS


def pipe_open(pipe_name: str, *, timeout_ms: int = 30000) -> Any:
    """Open a connected message-mode handle to the daemon pipe.

    Waits out ``ERROR_PIPE_BUSY`` (all instances busy) and
    ``ERROR_FILE_NOT_FOUND`` (daemon still booting) up to *timeout_ms*,
    then switches the handle to message-read mode. The caller owns the
    returned handle and must ``win32file.CloseHandle`` it.

    Raises:
        TimeoutError: When the pipe never became connectable in time.
        OSError: When a Windows pipe API fails for any other reason.
    """
    import time

    deadline = time.monotonic() + (timeout_ms / 1000.0)
    handle = None
    last_err: pywintypes.error | None = None
    while time.monotonic() < deadline:
        try:
            handle = win32file.CreateFile(
                pipe_name,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0,
                None,
                win32file.OPEN_EXISTING,
                0,
                None,
            )
            break
        except pywintypes.error as exc:
            last_err = exc
            if exc.winerror == _ERROR_PIPE_BUSY:
                with contextlib.suppress(pywintypes.error):
                    win32pipe.WaitNamedPipe(pipe_name, 1000)
                continue
            if exc.winerror == _ERROR_FILE_NOT_FOUND:
                time.sleep(0.05)
                continue
            raise OSError(f"pipe connect failed: {exc}") from exc
    if handle is None:
        raise TimeoutError(f"pipe {pipe_name!r} not connectable within {timeout_ms}ms (last={last_err})")
    win32pipe.SetNamedPipeHandleState(handle, win32pipe.PIPE_READMODE_MESSAGE, None, None)
    return handle


def pipe_client_call(pipe_name: str, request: bytes, *, timeout_ms: int = 30000) -> bytes:
    """Send one framed JSON-RPC request to the daemon pipe; return the reply frame.

    Synchronous single-shot transport mirroring the POSIX UDS round-trip:
    open the per-user pipe, ``WriteFile`` the request as one message, and
    ``ReadFile`` the single response message back.

    Args:
        pipe_name: ``\\\\.\\pipe\\eawfd-<user>`` path the daemon bound.
        request: Newline-framed request bytes (``orjson.dumps(req) + b"\\n"``).
        timeout_ms: Total budget for connecting + the round-trip.

    Returns:
        The raw response frame bytes (newline-terminated, as the server writes).

    Raises:
        TimeoutError: When the pipe never became connectable within the budget.
        OSError: When a Windows pipe API fails for any other reason.
    """
    handle = pipe_open(pipe_name, timeout_ms=timeout_ms)
    try:
        win32file.WriteFile(handle, request)
        _hr, data = win32file.ReadFile(handle, _PIPE_BUFFER_BYTES)
        return bytes(data)
    finally:
        with contextlib.suppress(pywintypes.error):
            win32file.CloseHandle(handle)


class PipeReader:
    """``readline()``-compatible adapter over a message-mode pipe handle.

    Each :meth:`readline` returns one whole pipe message (the daemon writes
    one JSON-RPC frame per message), or ``b""`` once the pipe closes —
    matching the EOF contract the binding's subscribe loop expects from a
    socket ``makefile("rb")`` reader. Owns the handle; :meth:`close` shuts it.
    """

    def __init__(self, handle: Any) -> None:
        self._handle = handle

    def readline(self) -> bytes:
        try:
            _hr, data = win32file.ReadFile(self._handle, _PIPE_BUFFER_BYTES)
            return bytes(data)
        except pywintypes.error as exc:
            # 109 broken pipe / 232 no data / 233 not connected / 995 the read
            # was cancelled by close() via CancelIoEx -> all mean "EOF".
            if exc.winerror in (109, 232, 233, 995):
                return b""
            raise

    def close(self) -> None:
        # CloseHandle alone does NOT unblock a synchronous ReadFile pending in
        # another thread, and pywin32 lacks CancelIoEx/CancelSynchronousIo, so
        # cancel the in-flight read via kernel32.CancelIoEx (cancels I/O on the
        # handle regardless of issuing thread) before closing. The blocked
        # ReadFile then returns ERROR_OPERATION_ABORTED (995) -> readline EOF.
        with contextlib.suppress(Exception):
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
            kernel32.CancelIoEx.restype = wintypes.BOOL
            kernel32.CancelIoEx(int(self._handle), None)
        with contextlib.suppress(pywintypes.error):
            win32file.CloseHandle(self._handle)


def pipe_subscribe(pipe_name: str, request: bytes, *, timeout_ms: int = 30000) -> tuple[PipeReader, bytes]:
    """Open a PERSISTENT pipe connection for a subscribe stream.

    Opens the pipe, writes the subscribe *request*, reads the first frame
    (the subscribe ack), and returns ``(reader, ack_frame)`` where *reader*
    is a :class:`PipeReader` over the same held handle for the caller to
    drain subsequent ``event.push`` frames via ``reader.readline()``.
    """
    handle = pipe_open(pipe_name, timeout_ms=timeout_ms)
    try:
        win32file.WriteFile(handle, request)
        _hr, ack = win32file.ReadFile(handle, _PIPE_BUFFER_BYTES)
    except BaseException:
        with contextlib.suppress(pywintypes.error):
            win32file.CloseHandle(handle)
        raise
    return PipeReader(handle), bytes(ack)


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
        ctx: Any | None = None,
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
        """
        self._loop = loop
        self._handler = handler
        # MethodContext for the subscribe-streaming path (ctx.bus + event_path).
        # None disables streaming (request/response only) — kept None in the
        # unit-test constructor-wiring contract.
        self._ctx = ctx
        self._pipe_name = pipe_name or default_pipe_name()
        self._verify_sid = verify_sid
        self._verify_sid_enabled = verify_sid_enabled
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
          3. Read one frame.
          4. Hand off to asyncio via :func:`Queue.put_nowait` through
             :func:`loop.call_soon_threadsafe`.
          5. Wait on a per-frame :class:`threading.Event` for the
             reply bytes.
          6. ``WriteFile`` the reply, close the pipe instance.

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

                # ImpersonateNamedPipeClient (inside verify_peer_sid) fails
                # with ERROR_CANNOT_IMPERSONATE (1368) unless the server has
                # already read from the pipe, so READ the frame first, then
                # verify the peer SID before dispatching it.
                _hr, payload = win32file.ReadFile(pipe, _PIPE_BUFFER_BYTES)

                if self._verify_sid_enabled:
                    try:
                        verify_peer_sid(pipe, self._verify_sid)
                    except WindowsAuthError as exc:
                        logger.warning(f"_listen_loop reject reason={exc!s}")
                        self._write_unauthorized(pipe)
                        continue

                # event.subscribe / state.subscribe is a long-lived multi-frame
                # push stream: hand this pipe instance to a dedicated thread
                # that streams pushes until the client disconnects, so the
                # single listener thread stays free to accept other RPCs.
                if self._ctx is not None and _is_subscribe_frame(bytes(payload)):
                    sub_thread = threading.Thread(
                        target=self._stream_subscription,
                        args=(pipe, bytes(payload)),
                        name="eawfd-pipe-subscriber",
                        daemon=True,
                    )
                    sub_thread.start()
                    pipe = None  # ownership transferred to the streamer thread
                    continue

                response = self._await_handler(bytes(payload))
                win32file.WriteFile(pipe, response)
            except pywintypes.error as exc:
                # A client that connects then disconnects without a full
                # round-trip (e.g. a liveness probe) yields ERROR_BROKEN_PIPE
                # (109) / ERROR_NO_DATA (232) / ERROR_PIPE_NOT_CONNECTED (233);
                # those are benign, log at debug. Anything else is a warning.
                if exc.winerror in (109, 232, 233):
                    logger.debug(f"_listen_loop client-disconnect err={exc!s}")
                else:
                    logger.warning(f"_listen_loop pywin32 err={exc!s}")
            except Exception:
                logger.exception("_listen_loop unhandled")
            finally:
                if pipe is not None:
                    with contextlib.suppress(pywintypes.error):
                        win32file.CloseHandle(pipe)
        logger.info("_listen_loop exit")

    def _stream_subscription(self, pipe: Any, payload: bytes) -> None:
        """Own *pipe*, register a subscriber, and stream event.push frames.

        Mirrors the POSIX ``_handle_subscribe`` / ``_stream_subscriber`` path
        but drives the asyncio :class:`EventBus` from this dedicated thread via
        :func:`asyncio.run_coroutine_threadsafe`: register + flush catch-up
        backlog, then pull pushes one at a time and ``WriteFile`` each as an
        ``event.push`` frame. Owns the pipe handle and closes it on exit. The
        loop ends on client disconnect (``WriteFile`` raises) or shutdown.

        Known PoC limitation: a client that disconnects while the stream is
        idle (blocked waiting for the next push) is only noticed on the next
        push attempt; the POSIX path watches the reader for EOF in parallel.
        """
        import uuid as _uuid

        import orjson

        from eawf.runtime.daemon.methods.event import subscribe as run_subscribe
        from eawf.runtime.daemon.server import _frame, _push_frame, _success

        ctx = self._ctx
        loop = self._loop
        req = orjson.loads(payload.rstrip(b"\n"))
        req_id = req.get("id")
        params = req.get("params") or {}
        connection_id = _uuid.uuid4().hex

        async def _register() -> Any:
            return run_subscribe(
                ctx.bus, connection_id=connection_id, params=params, event_path=ctx.event_path
            )

        try:
            try:
                sub, backlog = asyncio.run_coroutine_threadsafe(_register(), loop).result()
            except Exception as exc:
                logger.warning(f"_stream_subscription register-failed err={exc!s}")
                with contextlib.suppress(pywintypes.error):
                    win32file.WriteFile(pipe, _frame(_success(req_id, {"ok": False})))
                return

            logger.info(
                f"_stream_subscription start connection={connection_id!r} backlog={len(backlog)}"
            )

            # Drive the subscriber primitives directly (NOT the async generator):
            # cancelling iter_subscriber_pushes().__anext__ on a heartbeat timeout
            # would terminate the generator and kill the stream. Waiting on
            # sub.event with a timeout is harmless — queued envelopes stay in
            # sub.queue and the Event flag is untouched by the cancelled wait.
            async def _wait_event() -> bool:
                try:
                    await asyncio.wait_for(sub.event.wait(), _SUBSCRIBE_HEARTBEAT_S)
                    return True
                except (TimeoutError, asyncio.TimeoutError):
                    return False

            async def _drain() -> list:
                out: list = []
                while sub.queue:
                    out.append(sub.queue.popleft())
                sub.event.clear()
                return out

            try:
                win32file.WriteFile(
                    pipe, _frame(_success(req_id, {"ok": True, "backlog_count": len(backlog)}))
                )
                for env in backlog:
                    win32file.WriteFile(pipe, _push_frame(env))
                while not self._shutdown.is_set():
                    got = asyncio.run_coroutine_threadsafe(_wait_event(), loop).result()
                    if sub.closed:
                        break
                    if not got:
                        # Idle heartbeat: reap the subscriber if the client left.
                        if not _pipe_alive(pipe):
                            logger.debug(
                                f"_stream_subscription peer-gone connection={connection_id!r}"
                            )
                            break
                        continue
                    for env in asyncio.run_coroutine_threadsafe(_drain(), loop).result():
                        win32file.WriteFile(pipe, _push_frame(env))
            except pywintypes.error as exc:
                logger.debug(
                    f"_stream_subscription client-disconnect connection={connection_id!r} err={exc!s}"
                )
            finally:

                async def _unregister() -> None:
                    ctx.bus.unregister(connection_id)

                with contextlib.suppress(Exception):
                    asyncio.run_coroutine_threadsafe(_unregister(), loop).result(timeout=2.0)
                logger.info(f"_stream_subscription end connection={connection_id!r}")
        finally:
            with contextlib.suppress(pywintypes.error):
                win32file.CloseHandle(pipe)

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
