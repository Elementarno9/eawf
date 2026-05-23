"""Unit + Pilot tests for the C06 ``EUBar`` widget (P26-W17).

Covers the pure render helpers (cell-fill maths, colour band selection,
markup string shape) and a Pilot-driven mount that confirms the bar
paints under a real app loading the W16 ``theme.tcss`` palette.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.app import ComposeResult

from eawf.tui.widgets.eu_bar import (
    BAR_CELLS,
    EMPTY_STATE,
    GLYPH_EMPTY,
    GLYPH_FULL,
    EUBar,
    band_var,
    render_bar_markup,
    render_completion_bar,
    render_eu_bar_plain,
    render_size_bar,
)

from ._palette_harness import PaletteHarnessApp

_THEME = Path(__file__).resolve().parents[2] / "src" / "eawf" / "tui" / "theme.tcss"


class _Harness(PaletteHarnessApp):
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


# --------------------------------------------------------------------------
# render_completion_bar — iter / phase closed-ratio bar (W05)
# --------------------------------------------------------------------------


def test_render_completion_bar_zero_done_all_empty() -> None:
    bar = render_completion_bar(0, 6)
    assert bar == f"{GLYPH_EMPTY * 10}  0/6"


def test_render_completion_bar_full_when_done_equals_total() -> None:
    bar = render_completion_bar(6, 6)
    assert bar == f"{GLYPH_FULL * 10}  6/6"


def test_render_completion_bar_clamps_done_over_total() -> None:
    # done > total clamps both the fill and the count suffix to total.
    bar = render_completion_bar(9, 6)
    assert bar == f"{GLYPH_FULL * 10}  6/6"


def test_render_completion_bar_clamps_negative_done_to_zero() -> None:
    bar = render_completion_bar(-3, 6)
    assert bar == f"{GLYPH_EMPTY * 10}  0/6"


def test_render_completion_bar_zero_total_empty_state() -> None:
    assert render_completion_bar(0, 0) == EMPTY_STATE


def test_render_completion_bar_negative_total_empty_state() -> None:
    assert render_completion_bar(2, -1) == EMPTY_STATE


def test_render_completion_bar_half_ratio_fills_half() -> None:
    bar = render_completion_bar(3, 6)
    assert bar == f"{GLYPH_FULL * 5}{GLYPH_EMPTY * 5}  3/6"
    # 3/6 == 0.5 over a 10-cell bar -> exactly 5 filled cells.
    fraction = 3 / 6
    assert fraction == pytest.approx(0.5)


def test_render_completion_bar_custom_width() -> None:
    bar = render_completion_bar(1, 4, width=4)
    assert bar == f"{GLYPH_FULL * 1}{GLYPH_EMPTY * 3}  1/4"


# --------------------------------------------------------------------------
# render_size_bar — wave effort-bucket bar (W05)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bucket", "filled"),
    [("XS", 1), ("S", 2), ("M", 3), ("L", 4), ("XL", 5)],
)
def test_render_size_bar_each_bucket(bucket: str, filled: int) -> None:
    bar = render_size_bar(bucket)
    assert bar == f"{GLYPH_FULL * filled}{GLYPH_EMPTY * (5 - filled)}  {bucket}"


def test_render_size_bar_xs_lights_one_cell() -> None:
    assert render_size_bar("XS").startswith(GLYPH_FULL + GLYPH_EMPTY)


def test_render_size_bar_xl_fills_all_cells() -> None:
    assert render_size_bar("XL").startswith(GLYPH_FULL * 5)


def test_render_size_bar_unknown_bucket_empty_state() -> None:
    assert render_size_bar("ZZ") == EMPTY_STATE


def test_render_size_bar_empty_bucket_empty_state() -> None:
    assert render_size_bar("") == EMPTY_STATE


def test_render_size_bar_custom_width_caps_fill() -> None:
    # XL maps to 5 cells but a width=3 bar caps the fill at the bar width.
    bar = render_size_bar("XL", width=3)
    assert bar == f"{GLYPH_FULL * 3}  XL"


# --------------------------------------------------------------------------
# render_eu_bar_plain — guarded EU / token row (W05)
# --------------------------------------------------------------------------


def test_render_eu_bar_plain_zero_total_empty_state() -> None:
    assert render_eu_bar_plain(0.0, 0.0) == EMPTY_STATE


def test_render_eu_bar_plain_negative_total_empty_state() -> None:
    assert render_eu_bar_plain(5.0, -1.0) == EMPTY_STATE


def test_render_eu_bar_plain_nonzero_total_renders_bar() -> None:
    bar = render_eu_bar_plain(2.0, 4.0)
    assert "50%" in bar
    assert EMPTY_STATE not in bar
    # 2/4 == 0.5 of the 5-cell EU bar -> 3 filled (round-half-up).
    assert bar.count(GLYPH_FULL) == 3
    assert pytest.approx(0.5) == (2.0 / 4.0)


def test_empty_state_constant_is_stable() -> None:
    # W06 imports this sentinel; the exact glyph is part of the contract.
    assert EMPTY_STATE == "— no data"
