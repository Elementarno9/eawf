"""Live-layout regression tests for the W24 degraded-banner dock fix.

In a daemon-down session the read-only binder trips ``app.degraded`` true and
the App surfaces a transport-degraded banner. The banner used to ``dock: top``,
which stacked it ahead of the Header on row 0 and squeezed the brand +
breadcrumb + runtime row off the top of the screen -- a daemon-down operator
lost the whole header. This suite pins the fix: the banner now bottom-docks
(mirroring the stale-schema banner), so the header stays on row 0 while the
banner stays clearly surfaced just above the footer.

The daemon-DOWN layout is exercised EXPLICITLY here; the golden-suite quiesce
that forces ``degraded=False`` at capture is deliberately NOT relied on. Each
test flips ``_on_degraded(True)`` and syncs the real banner, so the frame under
assertion is the live degraded frame a daemon-down operator actually sees.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

from textual.pilot import Pilot

from eawf.surfaces.tui.app import DEGRADED_BANNER_ID, EaApp
from eawf.surfaces.tui.snapshot.pilot_harness import capture_screen_text, settle_screen
from eawf.surfaces.tui.widgets.header import CRUMB_SEP

#: A populated repo state (phase / iter / wave active) so the header renders a
#: full brand + breadcrumb + runtime row -- the chrome the fix must preserve.
_REPO_STATE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "states"
    / "valid"
    / "03-phase-iter-wave-active.json"
)
_SIZE = (120, 40)

#: A substring of the degraded banner's leading copy (the FAIL sigil + calm
#: "daemon unreachable, reconnecting" line) the tests key on to locate it.
_BANNER_MARKER = "daemon unreachable"


def _row_index(rows: list[str], needle: str) -> int:
    """Return the index of the first row containing *needle*, or ``-1``."""
    for index, row in enumerate(rows):
        if needle in row:
            return index
    return -1


async def _capture_daemon_down(pilot: Pilot[object]) -> list[str]:
    """Force the live daemon-DOWN degraded frame and return its raw rows.

    ``settle_screen`` runs the golden-suite quiesce, which forces
    ``degraded=False`` -- so this flips it back true and syncs the REAL banner
    afterwards, capturing the frame a live daemon-down operator sees rather than
    the deterministic non-degraded golden shape. The capture is RAW (not
    normalized) because the normaliser drops the banner line, and these tests
    assert the banner IS present.
    """
    app = cast(EaApp, pilot.app)
    await settle_screen(pilot)
    await app._on_degraded(True)
    app._sync_degraded_banner()
    await pilot.pause()
    return capture_screen_text(app).splitlines()


def test_degraded_banner_does_not_cover_header() -> None:
    # Daemon-down: the header brand + breadcrumb + runtime row all survive on
    # row 0 (the pre-fix top dock pushed the whole header off the screen).
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as raw_pilot:
            pilot = cast("Pilot[object]", raw_pilot)
            rows = await _capture_daemon_down(pilot)

            header_index = _row_index(rows, "Eä")
            assert header_index == 0, f"header brand not on row 0: {rows[:2]!r}"
            header_row = rows[header_index]
            assert "Eä" in header_row  # brand wordmark
            assert CRUMB_SEP in header_row  # scope breadcrumb separator
            assert "runtime:" in header_row  # runtime row cell

            banner_index = _row_index(rows, _BANNER_MARKER)
            assert banner_index != -1, "degraded banner not surfaced in daemon-down frame"
            # The banner sits BELOW the header -- bottom-docked, never covering it.
            assert banner_index > header_index

    asyncio.run(body())


def test_degraded_banner_bottom_docked_above_footer() -> None:
    # The banner mirrors the stale-schema banner: it surfaces at the very bottom
    # of the frame (just above / over the footer's burn row), not at row 0.
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as raw_pilot:
            pilot = cast("Pilot[object]", raw_pilot)
            rows = await _capture_daemon_down(pilot)

            banner_index = _row_index(rows, _BANNER_MARKER)
            assert banner_index == len(rows) - 1  # last row, bottom-docked

            banner = next(iter(app.screen.query(f"#{DEGRADED_BANNER_ID}")))
            assert banner.region.height == 1  # a real, visible one-row region
            assert banner.region.y == len(rows) - 1  # docked to the bottom edge

    asyncio.run(body())


def test_non_degraded_banner_costs_no_row() -> None:
    # Boundary case: with the daemon reachable the banner is hidden and collapses
    # to a zero-size region, so it costs no layout row and the header stays row 0.
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as raw_pilot:
            pilot = cast("Pilot[object]", raw_pilot)
            await settle_screen(pilot)  # quiesce leaves degraded=False
            app._sync_degraded_banner()  # mount the banner in its hidden class
            await pilot.pause()

            banner = next(iter(app.screen.query(f"#{DEGRADED_BANNER_ID}")))
            assert banner.region.height == 0  # hidden -> collapsed, no row cost
            rows = capture_screen_text(app).splitlines()
            assert _row_index(rows, "Eä") == 0  # header still owns row 0

    asyncio.run(body())
