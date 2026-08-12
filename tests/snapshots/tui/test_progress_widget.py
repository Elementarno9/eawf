"""Pilot tests for the ProgressBar widget over the unified cell-bar.

Mounts a lone :class:`~eawf.surfaces.tui.widgets.progress.ProgressBar` in a
bare host carrying the ``render_mode`` reactive (the same seam the live app
exposes) and asserts the W10 reuse: the progress widget paints the *unified*
block cell-bar (:func:`~eawf.surfaces.tui.widgets.eu_bar.render_completion_bar`
-- a :data:`~eawf.surfaces.tui.widgets.eu_bar.COMPLETION_FULL` run over a
:data:`~eawf.surfaces.tui.widgets.eu_bar.COMPLETION_REMAINDER` track with the
``done/total`` counter), the no-data path (``total <= 0``) renders the
:data:`~eawf.surfaces.tui.widgets.eu_bar.EMPTY_STATE` sentinel rather than a
fabricated bar, and a render-mode flip swaps the glyph set to the ASCII
fallback.
"""

from __future__ import annotations

import asyncio

import pytest
from textual.app import App, ComposeResult
from textual.reactive import reactive

from eawf.surfaces.tui.snapshot import settle_screen
from eawf.surfaces.tui.widgets.eu_bar import (
    COMPLETION_FULL,
    COMPLETION_REMAINDER,
    EMPTY_STATE,
    RenderMode,
)
from eawf.surfaces.tui.widgets.progress import ProgressBar


class _ProgressHostApp(App[None]):
    """Bare host carrying the ``render_mode`` reactive + one ProgressBar.

    Mirrors :class:`~eawf.surfaces.tui.app.EaApp`'s ``render_mode`` reactive
    so the mounted bar seeds + mirrors the active glyph mode exactly the way
    the live app drives it; the painted fill is a pure function of the bound
    ``done`` / ``total`` pair + bar width.
    """

    render_mode: reactive[RenderMode] = reactive[RenderMode]("unicode")

    def __init__(self, *, width: int) -> None:
        super().__init__()
        self._width = width

    def compose(self) -> ComposeResult:
        yield ProgressBar(width=self._width, id="prog")


def _painted_text(app: App[None]) -> str:
    """Return the ProgressBar's painted text."""
    bar = app.query_one("#prog", ProgressBar)
    return str(bar.render())


def test_progress_bar_renders_unified_cell_bar() -> None:
    """A populated done/total pair paints the unified block cell-bar + counter."""

    async def body() -> None:
        app = _ProgressHostApp(width=10)
        async with app.run_test(size=(40, 4)) as pilot:
            await settle_screen(pilot)
            bar = app.query_one("#prog", ProgressBar)
            bar.set_progress(3, 6)
            await settle_screen(pilot)
            painted = _painted_text(app)
            # The unified completion bar paints a COMPLETION_FULL run over a
            # COMPLETION_REMAINDER track with the ``done/total`` counter.
            assert COMPLETION_FULL in painted, f"no full-block glyph: {painted!r}"
            assert COMPLETION_REMAINDER in painted, f"no remainder track: {painted!r}"
            assert "3/6" in painted, f"no done/total counter: {painted!r}"

    asyncio.run(body())


def test_progress_bar_no_data_renders_empty_state_sentinel() -> None:
    """A zero-total pair renders the EMPTY_STATE sentinel, not a fabricated bar."""

    async def body() -> None:
        app = _ProgressHostApp(width=10)
        async with app.run_test(size=(40, 4)) as pilot:
            await settle_screen(pilot)
            bar = app.query_one("#prog", ProgressBar)
            bar.set_progress(0, 0)
            await settle_screen(pilot)
            painted = _painted_text(app)
            assert painted == EMPTY_STATE, f"expected empty-state sentinel: {painted!r}"
            assert COMPLETION_FULL not in painted, f"fabricated a bar: {painted!r}"
            assert COMPLETION_REMAINDER not in painted, f"fabricated a track: {painted!r}"

    asyncio.run(body())


def test_progress_bar_seeds_empty_state_on_mount() -> None:
    """An unset bar (total defaults to 0) paints the empty-state sentinel on mount."""

    async def body() -> None:
        app = _ProgressHostApp(width=8)
        async with app.run_test(size=(40, 4)) as pilot:
            await settle_screen(pilot)
            assert _painted_text(app) == EMPTY_STATE

    asyncio.run(body())


def test_progress_bar_render_mode_flip_swaps_to_ascii() -> None:
    """Flipping the app render mode swaps the bar to the ASCII fallback glyph set."""

    async def body() -> None:
        app = _ProgressHostApp(width=10)
        async with app.run_test(size=(40, 4)) as pilot:
            await settle_screen(pilot)
            bar = app.query_one("#prog", ProgressBar)
            bar.set_progress(3, 6)
            await settle_screen(pilot)
            assert COMPLETION_FULL in _painted_text(app)
            app.render_mode = "ascii"
            await settle_screen(pilot)
            painted = _painted_text(app)
            assert COMPLETION_FULL not in painted, f"unicode glyph survived flip: {painted!r}"
            assert "#" in painted, f"no ASCII fill glyph: {painted!r}"
            assert "3/6" in painted, f"no done/total counter: {painted!r}"

    asyncio.run(body())


def test_progress_bar_repaints_on_progress_change() -> None:
    """Reassigning the done/total pair repaints the bar to the new fill."""

    async def body() -> None:
        app = _ProgressHostApp(width=10)
        async with app.run_test(size=(40, 4)) as pilot:
            await settle_screen(pilot)
            bar = app.query_one("#prog", ProgressBar)
            bar.set_progress(1, 10)
            await settle_screen(pilot)
            assert "1/10" in _painted_text(app)
            bar.set_progress(9, 10)
            await settle_screen(pilot)
            assert "9/10" in _painted_text(app)

    asyncio.run(body())


def test_progress_bar_rejects_non_positive_width() -> None:
    """The constructor rejects a non-positive width."""
    with pytest.raises(ValueError, match="width must be positive"):
        ProgressBar(width=0)
