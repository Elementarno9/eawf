"""Determinism tests for :func:`quiesce_volatile_chrome`.

The snapshot harness freezes two timer-driven chrome elements before every
golden capture so a slow host cannot drift a golden:

* the daemon-degraded flip -- ``app.degraded`` trips true ~1.5 s after mount
  when no daemon answers, top-docking the degraded banner OVER the Header row
  (``normalize_snapshot`` then drops the banner line, losing the ``Eä`` brand
  and shrinking a 40-row frame to 39); and
* the footer heartbeat pulse -- the ``•`` dot blanks to a bare space every
  1.0 s, and ``capture_screen_text`` rstrips the blank cell, dropping the
  trailing bullet.

These tests force both elements into their volatile phase and assert the
harness capture is byte-identical to the settled non-degraded frame -- the
regression that reddened the macos-15 CI job the goldens were captured on.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest
from textual.app import App
from textual.pilot import Pilot

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.snapshot.pilot_harness import (
    capture_screen_text,
    normalize_snapshot,
    quiesce_volatile_chrome,
    settle_screen,
)
from eawf.surfaces.tui.widgets.heartbeat import Heartbeat

#: The real banner-sync method, captured at import BEFORE the autouse conftest
#: fixture (``_suppress_daemon_degraded_banner``) no-ops it for the golden
#: suite. The CI-1 test restores it so the degraded banner genuinely mounts and
#: covers the Header -- the exact condition the quiesce must reverse.
_REAL_SYNC_DEGRADED_BANNER = EaApp._sync_degraded_banner

_REPO_STATE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "states"
    / "valid"
    / "03-phase-iter-wave-active.json"
)
_SIZE = (120, 40)


def _blank_every_heartbeat(app: App[object]) -> None:
    """Drive every mounted heartbeat into its blank (unlit) pulse phase."""
    for heartbeat in app.query(Heartbeat):
        heartbeat._lit = False
        heartbeat._repaint()


def test_quiesce_reverts_degraded_flip_and_heartbeat_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Restore the real banner sync so the degraded flip actually top-docks the
    # banner over the Header (the autouse fixture otherwise suppresses it).
    monkeypatch.setattr(EaApp, "_sync_degraded_banner", _REAL_SYNC_DEGRADED_BANNER)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as raw_pilot:
            pilot = cast("Pilot[object]", raw_pilot)
            baseline = await settle_screen(pilot)
            # Sanity: the settled frame carries the Header brand + the footer
            # heartbeat bullet, and is the full 40-row frame.
            assert "Eä" in baseline
            assert "•" in baseline
            assert len(baseline.splitlines()) == 40

            # Force both volatile elements into the phase a slow CI host holds:
            # degraded flipped true (banner covers the Header) + heartbeat blank.
            await app._on_degraded(True)
            _blank_every_heartbeat(app)
            await pilot.pause()
            drifted = normalize_snapshot(capture_screen_text(app))
            # Pre-condition proof: the un-quiesced capture LOST the Header (the
            # banner covers it, then normalize drops the banner line) and the
            # footer bullet -- exactly the macos-15 drift.
            assert "Eä" not in drifted
            assert "•" not in drifted
            assert len(drifted.splitlines()) == 39

            # The harness quiesce (inside settle_screen) must revert both.
            requiesced = await settle_screen(pilot)
            assert "Eä" in requiesced
            assert "•" in requiesced  # heartbeat bullet restored
            assert app.degraded is False
            # Byte-identical to the settled non-degraded frame regardless of the
            # degraded / heartbeat phase at capture entry.
            assert requiesced == baseline

    asyncio.run(body())


def test_quiesce_forces_degraded_false_and_heartbeat_lit_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Exercise quiesce_volatile_chrome as its own unit: after a forced flip +
    # blank, the direct call leaves degraded False and every heartbeat lit.
    monkeypatch.setattr(EaApp, "_sync_degraded_banner", _REAL_SYNC_DEGRADED_BANNER)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as raw_pilot:
            pilot = cast("Pilot[object]", raw_pilot)
            await settle_screen(pilot)
            await app._on_degraded(True)
            _blank_every_heartbeat(app)
            assert app.degraded is True
            assert all(not hb._lit for hb in app.query(Heartbeat))

            await quiesce_volatile_chrome(pilot)

            assert app.degraded is False
            assert all(hb._lit for hb in app.query(Heartbeat))
            frame = normalize_snapshot(capture_screen_text(app))
            assert "Eä" in frame
            assert "•" in frame

    asyncio.run(body())


def test_quiesce_is_noop_on_bare_host_without_eaapp_seams() -> None:
    # Boundary case: a bare Textual App carries none of the EaApp seams
    # (degraded reactive, _sync_degraded_banner, _feed_listeners, Heartbeat).
    # Every guard is a soft getattr, so the quiesce is a clean no-op that never
    # raises -- the harness must tolerate a non-EaApp host under a Pilot.
    async def body() -> None:
        app: App[None] = App()
        async with app.run_test(size=_SIZE) as raw_pilot:
            pilot = cast("Pilot[object]", raw_pilot)
            await quiesce_volatile_chrome(pilot)  # must not raise
            assert not app.query(Heartbeat)

    asyncio.run(body())


def test_settle_screen_is_deterministic_under_forced_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two settle_screen captures taken around a forced degraded flip + heartbeat
    # blank must be byte-identical: the quiesce makes the returned frame
    # independent of the volatile phase the host happened to be in.
    monkeypatch.setattr(EaApp, "_sync_degraded_banner", _REAL_SYNC_DEGRADED_BANNER)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as raw_pilot:
            pilot = cast("Pilot[object]", raw_pilot)
            first = await settle_screen(pilot)
            await app._on_degraded(True)
            _blank_every_heartbeat(app)
            second = await settle_screen(pilot)
            assert first == second

    asyncio.run(body())
