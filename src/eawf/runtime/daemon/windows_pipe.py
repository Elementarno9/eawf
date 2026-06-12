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
            tail = exc.strerror if isinstance(exc.strerror, bytes) else b""
            return tail, True
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
                # pywin32 surfaces the partial buffer on the exception in
                # some builds; defensively append it before continuing.
                tail = exc.strerror if isinstance(exc.strerror, bytes) else b""
                if tail:
                    chunks.append(tail)
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
    "WindowsPipeServer",
    "default_pipe_name",
    "pipe_client_call",
    "pipe_ready",
]
