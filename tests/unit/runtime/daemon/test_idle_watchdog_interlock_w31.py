"""The idle watchdog counts a live research Campaign drive as in-flight work.

A research Campaign drive runs in the background without refreshing
``ctx.last_activity`` or incrementing ``in_flight_mutations``; without the
interlock a subscriber-less drive trips the idle watchdog and the daemon exits
mid-round.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from eawf.runtime.daemon.main import _build_watchdog
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


def _campaign_live(monkeypatch: pytest.MonkeyPatch, *, live: bool) -> None:
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.campaign_run.campaign_drive_in_flight",
        lambda campaign_key=None: live,
    )


def test_in_flight_counts_live_campaign_drive(monkeypatch: pytest.MonkeyPatch) -> None:
    """A live research Campaign drive counts as one in-flight unit."""
    _campaign_live(monkeypatch, live=True)
    assert _build_watchdog(_ctx(), 60.0).in_flight() == 1


def test_in_flight_sums_mutations_and_campaign_drive(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe adds the live drive atop the mutation counter."""
    _campaign_live(monkeypatch, live=True)
    assert _build_watchdog(_ctx(in_flight_mutations=2), 60.0).in_flight() == 3


def test_in_flight_counts_mutations_without_a_drive(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single mutation with no drive reports exactly one unit."""
    _campaign_live(monkeypatch, live=False)
    assert _build_watchdog(_ctx(in_flight_mutations=1), 60.0).in_flight() == 1


def test_in_flight_is_zero_when_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    """No background work and no mutations reports zero so the watchdog can trip."""
    _campaign_live(monkeypatch, live=False)
    assert _build_watchdog(_ctx(), 60.0).in_flight() == 0
