"""Tests for the C06 standalone ``Heartbeat`` widget (P26-W21).

The standalone :class:`~eawf.tui.widgets.heartbeat.Heartbeat` (distinct
from the W18 footer-embedded one) — the pulse glyph + visible/hidden
toggle, the degraded colour-class flip, and the ``r`` force-refresh
:meth:`ack` lit-frame, driven through the Pilot harness.
"""

from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult

from eawf.tui.widgets.heartbeat import HEARTBEAT_GLYPH, Heartbeat


class _HeartbeatHost(App[None]):
    """Bare host mounting a single standalone Heartbeat for Pilot tests."""

    def compose(self) -> ComposeResult:
        yield Heartbeat(id="hb")


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------


def test_heartbeat_glyph_is_a_bullet() -> None:
    assert HEARTBEAT_GLYPH == "•"


# --------------------------------------------------------------------------
# Pulse + ack + degraded (Pilot)
# --------------------------------------------------------------------------


def test_heartbeat_renders_glyph_on_mount() -> None:
    async def body() -> None:
        async with _HeartbeatHost().run_test() as pilot:
            await pilot.pause()
            hb = pilot.app.query_one("#hb", Heartbeat)
            assert hb.render() == HEARTBEAT_GLYPH

    asyncio.run(body())


def test_heartbeat_pulse_toggles_visibility() -> None:
    async def body() -> None:
        async with _HeartbeatHost().run_test() as pilot:
            await pilot.pause()
            hb = pilot.app.query_one("#hb", Heartbeat)
            assert hb.render() == HEARTBEAT_GLYPH
            hb._pulse()
            await pilot.pause()
            # An unlit phase blanks the cell.
            assert hb.render() == " "
            hb._pulse()
            await pilot.pause()
            assert hb.render() == HEARTBEAT_GLYPH

    asyncio.run(body())


def test_heartbeat_ack_forces_lit_frame() -> None:
    async def body() -> None:
        async with _HeartbeatHost().run_test() as pilot:
            await pilot.pause()
            hb = pilot.app.query_one("#hb", Heartbeat)
            hb._pulse()  # drive to the unlit phase
            await pilot.pause()
            assert hb.render() == " "
            hb.ack()  # r force-refresh ack guarantees a lit frame
            await pilot.pause()
            assert hb.render() == HEARTBEAT_GLYPH

    asyncio.run(body())


def test_heartbeat_degraded_sets_class() -> None:
    async def body() -> None:
        async with _HeartbeatHost().run_test() as pilot:
            await pilot.pause()
            hb = pilot.app.query_one("#hb", Heartbeat)
            assert not hb.has_class("-degraded")
            hb.degraded = True
            await pilot.pause()
            assert hb.has_class("-degraded")
            hb.degraded = False
            await pilot.pause()
            assert not hb.has_class("-degraded")

    asyncio.run(body())
