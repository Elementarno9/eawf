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

                if self._verify_sid_enabled:
                    try:
                        verify_peer_sid(pipe, self._verify_sid)
                    except WindowsAuthError as exc:
                        logger.warning(f"_listen_loop reject reason={exc!s}")
                        self._write_unauthorized(pipe)
                        continue

                _hr, payload = win32file.ReadFile(pipe, _PIPE_BUFFER_BYTES)
                response = self._await_handler(bytes(payload))
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
