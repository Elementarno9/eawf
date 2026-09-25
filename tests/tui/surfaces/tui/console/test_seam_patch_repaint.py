"""A keyed patch for a held route reaches the screen within one settle cycle.

The feed pushes a patch; the seam fans it out to every held route it names and tells
the app which routes changed; the app repaints when the route on screen is one of them.
The gate-fire proof runs the same check with the fan-out disabled and requires it to
fail on the stale frame, so the check cannot pass on a console that never repaints.
"""

from __future__ import annotations

import asyncio

import pytest

from eawf.kernel.projection.registers import ATTENTION_ROUTE
from eawf.surfaces.tui.console.app import Body, ConsoleApp
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import SIZES
from tests.tui.surfaces.tui.console.test_seam_pool import (
    CURSOR,
    FakeDaemon,
    patch,
    seam_over,
    settle,
)

#: The row the patch adds, absent from every read the stand-in daemon serves.
PATCHED_KEY = "TRK-7002"


def _painted(app: ConsoleApp) -> str:
    """Return the body rows the widget last painted, not just the composed frame."""
    return "\n".join(app.query_one("#body", Body).rows)


def check_patch_repaints_the_visible_route() -> None:
    """Load the home route, push a patch for it, and require the painted body to show it.

    Raises:
        AssertionError: The painted frame is stale after one settle cycle.
    """
    daemon = FakeDaemon()
    seam = seam_over(daemon)

    async def drive() -> tuple[str, str, int]:
        app = ConsoleApp(clock=FakeClock(), seam=seam)
        async with app.run_test(size=SIZES[1]) as pilot:
            await settle(app, pilot)
            before = _painted(app)
            renders = app.render_count
            await seam.apply_patch(patch("scope.home", key=PATCHED_KEY, sequence=CURSOR + 1))
            await pilot.pause()
            return before, _painted(app), app.render_count - renders

    before, after, repaints = asyncio.run(drive())
    assert PATCHED_KEY not in before
    assert PATCHED_KEY in after, "the visible frame is stale after the patch"
    assert repaints == 1
    assert daemon.reads == ["scope.home", ATTENTION_ROUTE]


def test_patch_repaints_held_route() -> None:
    check_patch_repaints_the_visible_route()


def test_patch_repaint_reds_with_fan_out_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate-fire proof: with no fan-out the same check fails on the stale frame."""
    monkeypatch.setattr(ProjectionSeam, "_fan_out", lambda self, patch: ())
    with pytest.raises(AssertionError, match="stale"):
        check_patch_repaints_the_visible_route()


def test_patch_to_a_hidden_held_route_repaints_nothing_and_is_drawn_on_return() -> None:
    """A background route is kept current without a repaint, and a return reads nothing."""
    daemon = FakeDaemon()
    seam = seam_over(daemon)

    async def drive() -> tuple[int, str]:
        app = ConsoleApp(clock=FakeClock(), seam=seam)
        async with app.run_test(size=SIZES[1]) as pilot:
            await settle(app, pilot)
            app.press_key("g")
            app.press_key("a")
            await settle(app, pilot)
            renders = app.render_count
            await seam.apply_patch(patch("scope.home", key=PATCHED_KEY, sequence=CURSOR + 1))
            await pilot.pause()
            quiet = app.render_count - renders
            app.press_key("g")
            app.press_key("h")
            await settle(app, pilot)
            return quiet, _painted(app)

    quiet, home = asyncio.run(drive())
    assert quiet == 0
    assert PATCHED_KEY in home
    assert daemon.reads == ["scope.home", ATTENTION_ROUTE, "activity"]


def test_an_attention_patch_repaints_every_route_for_the_header() -> None:
    """The header counts Attention on every route, so its patch repaints wherever it lands."""
    seam = seam_over(FakeDaemon())

    async def drive() -> int:
        app = ConsoleApp(clock=FakeClock(), seam=seam)
        async with app.run_test(size=SIZES[1]) as pilot:
            await settle(app, pilot)
            renders = app.render_count
            action = patch(
                ATTENTION_ROUTE, key="ACT-0001", sequence=CURSOR + 1, collection="pending_action"
            )
            await seam.apply_patch(action)
            await pilot.pause()
            return app.render_count - renders

    assert asyncio.run(drive()) == 1
    held = seam.projection_for(ATTENTION_ROUTE)
    assert held is not None
    assert [row.key for row in held.rows] == ["ACT-0001"]
