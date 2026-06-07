"""Pilot test for the determinate ProgressBar widget (P29-I13-W21).

Mounts a lone :class:`~eawf.surfaces.tui.widgets.progress.ProgressBar` in a
bare host and asserts the painted block-eighths fill matches the bound
completion ratio -- the determinate-progress contract: a known fraction
reads as a left-to-right block-eighths fill that tracks the bound value.
"""

from __future__ import annotations

import asyncio

import pytest
from textual.app import App, ComposeResult

from eawf.surfaces.render.bars import render_block_bar
from eawf.surfaces.tui.snapshot import settle_screen
from eawf.surfaces.tui.widgets.progress import ProgressBar


class _ProgressHarness(App[None]):
    """Bare host mounting one ProgressBar for the fill capture.

    The host exposes no ``state``; the frame is a pure function of the
    bound ratio + bar width, so the painted fill is deterministic.
    """

    def __init__(self, *, width: int) -> None:
        super().__init__()
        self._width = width

    def compose(self) -> ComposeResult:
        yield ProgressBar(width=self._width, id="prog")


def _painted_text(app: App[None]) -> str:
    """Return the ProgressBar's painted text (trailing space trimmed)."""
    bar = app.query_one("#prog", ProgressBar)
    return str(bar.render())


@pytest.mark.parametrize("ratio", [0.0, 0.25, 0.5, 0.75, 1.0])
def test_progress_bar_fill_matches_bound_ratio(ratio: float) -> None:
    """The painted fill matches the block-eighths render of the bound ratio."""

    async def body() -> None:
        app = _ProgressHarness(width=10)
        async with app.run_test(size=(40, 4)) as pilot:
            await settle_screen(pilot)
            bar = app.query_one("#prog", ProgressBar)
            bar.set_ratio(ratio)
            await settle_screen(pilot)
            assert _painted_text(app) == render_block_bar(ratio, width=10)

    asyncio.run(body())


def test_progress_bar_seeds_empty_on_mount() -> None:
    """An unset bar paints the all-empty fill on mount (ratio defaults to 0)."""

    async def body() -> None:
        app = _ProgressHarness(width=8)
        async with app.run_test(size=(40, 4)) as pilot:
            await settle_screen(pilot)
            assert _painted_text(app) == render_block_bar(0.0, width=8)

    asyncio.run(body())


def test_progress_bar_repaints_on_ratio_change() -> None:
    """Reassigning the ratio repaints the bar to the new fill."""

    async def body() -> None:
        app = _ProgressHarness(width=10)
        async with app.run_test(size=(40, 4)) as pilot:
            await settle_screen(pilot)
            bar = app.query_one("#prog", ProgressBar)
            bar.set_ratio(0.3)
            await settle_screen(pilot)
            assert _painted_text(app) == render_block_bar(0.3, width=10)
            bar.set_ratio(0.9)
            await settle_screen(pilot)
            assert _painted_text(app) == render_block_bar(0.9, width=10)

    asyncio.run(body())


def test_progress_bar_rejects_out_of_range_ratio() -> None:
    """``set_ratio`` rejects a ratio outside ``[0, 1]``."""

    async def body() -> None:
        app = _ProgressHarness(width=10)
        async with app.run_test(size=(40, 4)) as pilot:
            await settle_screen(pilot)
            bar = app.query_one("#prog", ProgressBar)
            with pytest.raises(ValueError, match="ratio out of range"):
                bar.set_ratio(1.5)

    asyncio.run(body())


def test_progress_bar_rejects_non_positive_width() -> None:
    """The constructor rejects a non-positive width."""
    with pytest.raises(ValueError, match="width must be positive"):
        ProgressBar(width=0)
