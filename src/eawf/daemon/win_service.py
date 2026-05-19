"""Windows Service framework subclass for the eawfd daemon.

A ``win32serviceutil.ServiceFramework`` subclass that hosts the asyncio
event loop and bridges Service Control Manager (SCM) callbacks into
the loop. The cross-thread bridge addresses the thread-affinity
mismatch: ``SvcStop`` callbacks fire on the SCM thread while the
asyncio loop owns its own thread; without :func:`loop.call_soon_threadsafe`
the stop signal never crosses thread boundaries and the daemon hangs.

The module is import-guarded on non-win32 hosts so the POSIX dev loop
is not contaminated by pywin32 attribute access at import time. Type-
checking falls back to the conditional ``TYPE_CHECKING`` guard so
mypy on POSIX can still resolve symbols without the unavailable stubs.

Operator entrypoint: ``python -m eawf.daemon.win_service install
--startup auto`` then ``... start``. The :mod:`eawf.daemon.service_install`
module wraps these invocations into the cross-OS install verbs.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import TYPE_CHECKING, Any

if sys.platform != "win32":
    raise ImportError("eawf.daemon.win_service is win32-only")

if sys.platform == "win32":  # pragma: no cover - win32-only branch
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil

if TYPE_CHECKING:  # pragma: no cover - type-only
    import win32serviceutil

logger = logging.getLogger(__name__)


class EawfdService(win32serviceutil.ServiceFramework):
    """Hosts the eawfd asyncio loop under the Windows Service framework.

    The SCM owns process lifecycle; ``SvcDoRun`` launches the asyncio
    loop and blocks until the loop returns. ``SvcStop`` runs on the
    SCM thread and must bridge into the loop via
    :func:`loop.call_soon_threadsafe` to cross the thread boundary.

    Attributes:
        _svc_name_: Internal service name registered with the SCM
            (matches the binary lookup key).
        _svc_display_name_: Human-readable display name shown in
            ``services.msc``.
        _svc_description_: Long-form description shown in the SCM
            properties dialog.
    """

    _svc_name_: str = "eawfd"
    _svc_display_name_: str = "eawf coordinator daemon"
    _svc_description_: str = (
        "Single-user coordinator for the eawf framework. "
        "Hosts the JSON-RPC listener that mediates state.json + "
        "config + event store mutations."
    )

    def __init__(self, args: list[str]) -> None:
        """Wire the SCM bridge plumbing.

        Args:
            args: Command-line arguments forwarded by the SCM (always
                a single-element list with the service name; held by
                the framework for diagnostic logging).
        """
        super().__init__(args)
        # Win32 manual-reset event for SCM-side waits; the asyncio
        # bridge mirrors this through ``self._stop_event``.
        self._scm_stop_event: Any = win32event.CreateEvent(None, 0, 0, None)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None

    def SvcStop(self) -> None:  # noqa: N802 — pywin32 framework API
        """Cross-thread shutdown bridge.

        The SCM invokes this callback on its own worker thread; the
        asyncio loop runs on a different thread. Setting an
        :class:`asyncio.Event` from the SCM thread directly is unsafe,
        so we route the set call through
        :func:`loop.call_soon_threadsafe`.

        After the bridge call returns, the SCM thread also signals
        the win32 stop event so any pywin32 ``WaitForSingleObject``
        consumers (none today, but future surfaces such as the legacy
        NSSM fallback) observe a consistent signal.
        """
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        logger.info("SvcStop scm-thread signal")
        if self._loop is not None and self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        win32event.SetEvent(self._scm_stop_event)

    def SvcDoRun(self) -> None:  # noqa: N802 — pywin32 framework API
        """SCM entry — boot the asyncio loop and run until stop.

        Logs a ``PYS_SERVICE_STARTED`` event so the Windows Event
        Viewer surface is populated, then drives the loop until
        :meth:`SvcStop` flips :attr:`_stop_event`.
        """
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._main())
        finally:
            loop.close()
            self._loop = None
            self._stop_event = None
            logger.info("SvcDoRun loop closed")

    async def _main(self) -> None:
        """Asyncio entry — start the daemon's transports and wait.

        Spins up the named-pipe listener via the same
        :class:`eawf.daemon.windows_pipe.WindowsPipeServer` W02 ships,
        wires the shared dispatcher, and awaits the cross-thread stop
        event. Teardown closes the listener cleanly before
        :meth:`SvcDoRun` exits.
        """
        # Import-local so non-win32 type-checking does not resolve the
        # win-only transport eagerly.
        from eawf import __version__
        from eawf.daemon import PROTOCOL_VERSION
        from eawf.daemon.methods import MethodContext
        from eawf.daemon.server import process_frame_bytes
        from eawf.daemon.windows_pipe import WindowsPipeServer

        self._stop_event = asyncio.Event()
        ctx = MethodContext(
            started_at=_now_iso(),
            pid=_current_pid(),
            protocol_version=PROTOCOL_VERSION,
            version=__version__,
            shutdown_event=self._stop_event,
        )

        loop = asyncio.get_running_loop()

        async def _handler(payload: bytes) -> bytes:
            return await process_frame_bytes(payload, ctx)

        pipe_server = WindowsPipeServer(loop, _handler)
        pipe_server.start()
        logger.info(f"_main bound pipe={pipe_server.pipe_path!r}")
        try:
            await self._stop_event.wait()
        finally:
            pipe_server.stop()
            logger.info("_main stopped")


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _current_pid() -> int:
    """Return the current process PID."""
    import os

    return os.getpid()


if __name__ == "__main__":  # pragma: no cover - SCM entry point
    win32serviceutil.HandleCommandLine(EawfdService)
