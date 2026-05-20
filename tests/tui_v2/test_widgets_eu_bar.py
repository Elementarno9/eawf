"""Unit + Pilot tests for the C06 ``EUBar`` widget (P26-W17).

Covers the pure render helpers (cell-fill maths, colour band selection,
markup string shape) and a Pilot-driven mount that confirms the bar
paints under a real app loading the W16 ``theme.tcss`` palette.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.app import App, ComposeResult

from eawf.tui_v2.widgets.eu_bar import (
    BAR_CELLS,
    GLYPH_EMPTY,
    GLYPH_FULL,
    EUBar,
    band_var,
    render_bar_markup,
)

_THEME = Path(__file__).resolve().parents[2] / "src" / "eawf" / "tui_v2" / "theme.tcss"


class _Harness(App[None]):
    """Production-style host app loading the real palette CSS."""

    CSS_PATH = str(_THEME)

    def compose(self) -> ComposeResult:
        yield EUBar(id="bar")


# --------------------------------------------------------------------------
# render_bar_markup — glyph fill + percent
# --------------------------------------------------------------------------


def test_render_bar_markup_empty_when_zero_consumed() -> None:
    markup = render_bar_markup(0.0, 5.0)
    assert GLYPH_EMPTY * BAR_CELLS in markup
    assert "0%" in markup


def test_render_bar_markup_full_when_consumed_equals_total() -> None:
    markup = render_bar_markup(5.0, 5.0)
    assert GLYPH_FULL * BAR_CELLS in markup
    assert "100%" in markup


def test_render_bar_markup_half() -> None:
    markup = render_bar_markup(2.5, 5.0)
    # 50% of 5 cells = 2.5 -> round-half-up -> 3 filled, 2 empty.
    assert f"{GLYPH_FULL * 3}{GLYPH_EMPTY * 2}" in markup
    assert "50%" in markup


def test_render_bar_markup_lights_first_cell_at_half_cell_boundary() -> None:
    # 5-cell bar: each cell = 20%; round-half-up lights cell 1 at >= 10%.
    just_below = render_bar_markup(0.45, 5.0)  # 9% -> 0 cells
    assert just_below.count(GLYPH_FULL) == 0
    at_boundary = render_bar_markup(0.5, 5.0)  # 10% -> 1 cell
    assert at_boundary.count(GLYPH_FULL) == 1


def test_render_bar_markup_zero_total_no_division_error() -> None:
    markup = render_bar_markup(3.0, 0.0)
    assert "0%" in markup
    assert GLYPH_EMPTY * BAR_CELLS in markup


def test_render_bar_markup_over_budget_pct_exceeds_100() -> None:
    markup = render_bar_markup(6.0, 5.0)
    assert "120%" in markup
    assert GLYPH_FULL * BAR_CELLS in markup  # clamped fill, err band


# --------------------------------------------------------------------------
# band_var — colour thresholds against the palette vars
# --------------------------------------------------------------------------


def test_band_var_ok_at_or_below_80pct() -> None:
    assert band_var(0.0) == "$ok"
    assert band_var(0.80) == "$ok"


def test_band_var_warn_between_80_and_100pct() -> None:
    assert band_var(0.81) == "$warn"
    assert band_var(1.00) == "$warn"


def test_band_var_err_over_100pct() -> None:
    assert band_var(1.01) == "$err"
    assert band_var(2.0) == "$err"


# --------------------------------------------------------------------------
# Pilot mount — paints under the real palette
# --------------------------------------------------------------------------


def test_eu_bar_paints_under_real_palette() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(40, 6)) as pilot:
            await pilot.pause()
            app.query_one("#bar", EUBar).set_eu(4.0, 5.0)
            await pilot.pause()
            rendered = app.export_screenshot()
            assert "80%" in rendered
            assert GLYPH_FULL in rendered

    asyncio.run(body())


def test_eu_bar_set_eu_updates_reactives() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(40, 6)) as pilot:
            await pilot.pause()
            bar = app.query_one("#bar", EUBar)
            bar.set_eu(3.0, 6.0)
            await pilot.pause()
            assert bar.consumed_eu == 3.0
            assert bar.total_eu == 6.0

    asyncio.run(body())
