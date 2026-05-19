"""End-to-end coverage for the W04 Windows Service framework subclass.

Every test is gated on ``sys.platform == "win32"`` because the only
meaningful verification of the SCM-asyncio bridge requires the real
pywin32 framework to be importable. On macOS / Linux the suite
reports SKIPPED for each case.

Coverage focus is the XB14 fix (C02 §5.13) — SCM thread invokes
:meth:`EawfdService.SvcStop`, asyncio loop runs on a different thread,
:func:`loop.call_soon_threadsafe` is the only safe bridge.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows Service framework is win32-only",
)


def test_svc_stop_bridges_into_asyncio_loop_thread_safely() -> None:
    """``SvcStop`` from a non-loop thread sets the asyncio stop event.

    Simulates the SCM contract: the SCM thread calls ``SvcStop`` while
    the asyncio loop runs on a different thread. The bridge must
    deliver the stop signal via :func:`loop.call_soon_threadsafe` so
    the loop wakes from ``await self._stop_event.wait()`` cleanly.
    """
    from eawf.daemon.win_service import EawfdService

    service = EawfdService.__new__(EawfdService)
    # Skip super().__init__ — the ServiceFramework constructor needs
    # an SCM context we cannot fabricate in-process.
    service._loop = None
    service._stop_event = None

    loop = asyncio.new_event_loop()
    stop_event = asyncio.Event()
    service._loop = loop
    service._stop_event = stop_event

    # ``ReportServiceStatus`` is provided by ``ServiceFramework`` but
    # we bypassed __init__; stub it so SvcStop does not blow up.
    service.ReportServiceStatus = lambda _state: None  # type: ignore[assignment]

    # Stub the SCM-event setter; it is irrelevant to the asyncio
    # bridge contract we are exercising.
    class _StubEvent:
        def __init__(self) -> None:
            self.signalled = False

    service._scm_stop_event = _StubEvent()

    def _set_scm_event(_event: object) -> None:
        service._scm_stop_event.signalled = True

    # Monkey-patch the imported helper inside the module's namespace
    # for the duration of this test.
    import eawf.daemon.win_service as win_service_mod

    original = win_service_mod.win32event.SetEvent
    win_service_mod.win32event.SetEvent = _set_scm_event  # type: ignore[assignment]
    try:
        loop_done = threading.Event()
        loop_thread_id: dict[str, int] = {}

        def _runner() -> None:
            loop_thread_id["id"] = threading.get_ident()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(stop_event.wait())
            finally:
                loop_done.set()

        loop_thread = threading.Thread(target=_runner, name="asyncio-loop")
        loop_thread.start()

        # Give the loop thread a moment to enter run_until_complete.
        time.sleep(0.05)
        assert not stop_event.is_set()

        # SvcStop runs on *this* thread, which is NOT the loop thread.
        scm_thread_id = threading.get_ident()
        service.SvcStop()
        assert scm_thread_id != loop_thread_id["id"], (
            "test invariant: SvcStop must run on a thread separate from "
            "the loop thread for the bridge contract to be exercised"
        )

        # Loop should observe the cross-thread signal and exit.
        loop_done.wait(timeout=2.0)
        assert loop_done.is_set(), "asyncio loop did not exit after SvcStop"
        assert stop_event.is_set()
        # The SCM-side event should also have been signalled.
        assert service._scm_stop_event.signalled is True

        loop_thread.join(timeout=1.0)
        assert not loop_thread.is_alive()
    finally:
        win_service_mod.win32event.SetEvent = original  # type: ignore[assignment]
        loop.close()


def test_svc_stop_is_safe_when_loop_not_yet_started() -> None:
    """Calling ``SvcStop`` before ``SvcDoRun`` must not raise."""
    from eawf.daemon.win_service import EawfdService

    service = EawfdService.__new__(EawfdService)
    service._loop = None
    service._stop_event = None
    service.ReportServiceStatus = lambda _state: None  # type: ignore[assignment]

    class _StubEvent:
        signalled = False

    service._scm_stop_event = _StubEvent()

    import eawf.daemon.win_service as win_service_mod

    original = win_service_mod.win32event.SetEvent

    def _set_scm_event(_event: object) -> None:
        service._scm_stop_event.signalled = True

    win_service_mod.win32event.SetEvent = _set_scm_event  # type: ignore[assignment]
    try:
        # Must not raise even though there is no loop / event yet.
        service.SvcStop()
        assert service._scm_stop_event.signalled is True
    finally:
        win_service_mod.win32event.SetEvent = original  # type: ignore[assignment]


def test_service_metadata_matches_spec() -> None:
    """Service registration metadata uses the names C02 §5.10.3 specifies."""
    from eawf.daemon.win_service import EawfdService

    assert EawfdService._svc_name_ == "eawfd"
    assert EawfdService._svc_display_name_ == "eawf coordinator daemon"
    assert "coordinator" in EawfdService._svc_description_.lower()
