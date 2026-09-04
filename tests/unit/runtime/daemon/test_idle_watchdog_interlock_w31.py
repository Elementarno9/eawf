"""P30-I21-W31 (G3): idle watchdog interlock with background drive / research.

A headless fleet drive (or research campaign) runs on a daemon thread that never
refreshes ``ctx.last_activity`` nor increments ``in_flight_mutations``. Before
this wave, a subscriber-less headless drive tripped the idle watchdog and the
daemon self-killed mid-spawn at the default 300s. This wave counts live drive +
research-run threads as in-flight so the watchdog never trips while background
work runs, and wires ``shutdown_drive`` into the serve-loop teardown so a
mid-drive shutdown cancel+joins the drive instead of abandoning it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from eawf.runtime.daemon.main import _build_watchdog, _shutdown_background_drives
from eawf.runtime.daemon.methods import MethodContext


def _ctx(*, in_flight_mutations: int = 0) -> MethodContext:
    """A duck-typed context exposing only the fields the watchdog reads."""
    stub = SimpleNamespace(
        in_flight_mutations=in_flight_mutations,
        bus=None,
        active_subscriptions=0,
        last_activity=0.0,
    )
    return cast(MethodContext, stub)


def test_in_flight_counts_live_drive(monkeypatch: pytest.MonkeyPatch) -> None:
    """A live background drive counts as in-flight so the watchdog holds off."""
    monkeypatch.setattr("eawf.runtime.daemon.methods.fleet.drive_in_flight", lambda: True)
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.research.research_run_in_flight",
        lambda campaign_id=None: False,
    )
    watchdog = _build_watchdog(_ctx(), 60.0)
    assert watchdog.in_flight() == 1


def test_in_flight_counts_live_research_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A live research campaign counts as in-flight (parity with the drive)."""
    monkeypatch.setattr("eawf.runtime.daemon.methods.fleet.drive_in_flight", lambda: False)
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.research.research_run_in_flight",
        lambda campaign_id=None: True,
    )
    watchdog = _build_watchdog(_ctx(), 60.0)
    assert watchdog.in_flight() == 1


def test_in_flight_sums_mutations_drive_and_research(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe adds live drive + research atop the mutation counter."""
    monkeypatch.setattr("eawf.runtime.daemon.methods.fleet.drive_in_flight", lambda: True)
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.research.research_run_in_flight",
        lambda campaign_id=None: True,
    )
    watchdog = _build_watchdog(_ctx(in_flight_mutations=2), 60.0)
    assert watchdog.in_flight() == 4


def test_in_flight_is_zero_when_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    """No background work + no mutations reports zero so the watchdog can trip."""
    monkeypatch.setattr("eawf.runtime.daemon.methods.fleet.drive_in_flight", lambda: False)
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.research.research_run_in_flight",
        lambda campaign_id=None: False,
    )
    watchdog = _build_watchdog(_ctx(), 60.0)
    assert watchdog.in_flight() == 0


def test_shutdown_background_drives_calls_shutdown_drive(monkeypatch: pytest.MonkeyPatch) -> None:
    """The serve-loop teardown signals + joins the active drive on shutdown."""
    called: list[bool] = []
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.fleet.shutdown_drive",
        lambda *a, **k: called.append(True),
    )
    _shutdown_background_drives()
    assert called == [True]
