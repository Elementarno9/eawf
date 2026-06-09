"""Pilot tests for the VarianceTile over the unified cell-bar (P30-I02-W10).

Mounts a lone :class:`~eawf.surfaces.tui.widgets.variance_tile.VarianceTile`
in a bare host carrying the ``render_mode`` reactive (the same seam the live
app exposes) and asserts the W10 reuse: the variance tile paints the
*unified* block cell-bar
(:func:`~eawf.surfaces.tui.widgets.eu_bar.render_bar_markup`, the
block-eighths fill) keyed to the variance magnitude, the no-data path
(``variance_pct is None``) renders the
:data:`~eawf.surfaces.tui.widgets.variance_tile.EMPTY_STATE` sentinel rather
than a fabricated bar, and a render-mode flip swaps the glyph set to the
ASCII fallback.
"""

from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.reactive import reactive

from eawf.surfaces.render.bars import BLOCK_EIGHTHS
from eawf.surfaces.tui.snapshot import settle_screen
from eawf.surfaces.tui.theme import EA_THEMES, LOGICAL_THEMES
from eawf.surfaces.tui.widgets.eu_bar import RenderMode
from eawf.surfaces.tui.widgets.variance_tile import EMPTY_STATE, VarianceTile


def _has_block(text: str) -> bool:
    """Return ``True`` if *text* carries any block-eighths glyph."""
    return any(ch in BLOCK_EIGHTHS for ch in text)


class _VarianceHostApp(App[None]):
    """Bare themed host carrying the ``render_mode`` reactive + one tile.

    Mirrors :class:`~eawf.surfaces.tui.app.EaApp`'s theme bootstrap + the
    ``render_mode`` reactive so the mounted tile seeds + mirrors the active
    glyph mode the way the live app drives it, and the ``$ok`` / ``$warn`` /
    ``$err`` content-markup palette vars resolve at render time.
    """

    render_mode: reactive[RenderMode] = reactive[RenderMode]("unicode")

    def __init__(self) -> None:
        super().__init__()
        for theme in EA_THEMES:
            self.register_theme(theme)
        self.theme = LOGICAL_THEMES["dark"]

    def compose(self) -> ComposeResult:
        yield VarianceTile(id="var")


def _painted_text(app: App[None]) -> str:
    """Return the VarianceTile's painted text."""
    tile = app.query_one("#var", VarianceTile)
    return str(tile.render())


def test_variance_tile_renders_unified_cell_bar() -> None:
    """A populated variance paints the unified block cell-bar + signed-percent label."""

    async def body() -> None:
        app = _VarianceHostApp()
        async with app.run_test(size=(40, 4)) as pilot:
            await settle_screen(pilot)
            tile = app.query_one("#var", VarianceTile)
            tile.set_variance(30.0)
            await settle_screen(pilot)
            painted = _painted_text(app)
            assert _has_block(painted), f"no block-eighths glyph: {painted!r}"
            assert "+30.0%" in painted, f"no signed-percent label: {painted!r}"

    asyncio.run(body())


def test_variance_tile_no_data_renders_empty_state_sentinel() -> None:
    """A None variance renders the EMPTY_STATE sentinel, not a fabricated bar."""

    async def body() -> None:
        app = _VarianceHostApp()
        async with app.run_test(size=(40, 4)) as pilot:
            await settle_screen(pilot)
            tile = app.query_one("#var", VarianceTile)
            tile.set_variance(None)
            await settle_screen(pilot)
            painted = _painted_text(app)
            assert EMPTY_STATE in painted, f"expected empty-state sentinel: {painted!r}"
            assert not _has_block(painted), f"fabricated a bar: {painted!r}"

    asyncio.run(body())


def test_variance_tile_seeds_empty_state_on_mount() -> None:
    """An unset tile (variance defaults to None) paints the empty-state sentinel."""

    async def body() -> None:
        app = _VarianceHostApp()
        async with app.run_test(size=(40, 4)) as pilot:
            await settle_screen(pilot)
            painted = _painted_text(app)
            assert EMPTY_STATE in painted, f"expected empty-state sentinel: {painted!r}"
            assert not _has_block(painted), f"fabricated a bar: {painted!r}"

    asyncio.run(body())


def test_variance_tile_render_mode_flip_swaps_to_ascii() -> None:
    """Flipping the app render mode swaps the bar to the ASCII fallback glyph set."""

    async def body() -> None:
        app = _VarianceHostApp()
        async with app.run_test(size=(40, 4)) as pilot:
            await settle_screen(pilot)
            tile = app.query_one("#var", VarianceTile)
            tile.set_variance(30.0)
            await settle_screen(pilot)
            assert _has_block(_painted_text(app))
            app.render_mode = "ascii"
            await settle_screen(pilot)
            painted = _painted_text(app)
            assert not _has_block(painted), f"unicode glyph survived flip: {painted!r}"
            assert "#" in painted, f"no ASCII fill glyph: {painted!r}"
            assert "+30.0%" in painted, f"no signed-percent label: {painted!r}"

    asyncio.run(body())


def test_variance_tile_repaints_on_variance_change() -> None:
    """Reassigning the variance repaints the tile to the new value + magnitude."""

    async def body() -> None:
        app = _VarianceHostApp()
        async with app.run_test(size=(40, 4)) as pilot:
            await settle_screen(pilot)
            tile = app.query_one("#var", VarianceTile)
            tile.set_variance(10.0)
            await settle_screen(pilot)
            assert "+10.0%" in _painted_text(app)
            tile.set_variance(-75.0)
            await settle_screen(pilot)
            assert "-75.0%" in _painted_text(app)

    asyncio.run(body())
