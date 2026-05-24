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

import eawf.tui.app as eaapp_mod
from eawf.tui.app import EaApp, probe_braille_coverage, resolve_render_mode
from eawf.tui.widgets.eu_bar import (
    BAR_CELLS,
    BRAILLE_BASE,
    BRAILLE_LEFT_COL,
    BRAILLE_RIGHT_COL,
    BRAILLE_SUBCOLS,
    EMPTY_STATE,
    GLYPH_EMPTY,
    GLYPH_FULL,
    EUBar,
    band_var,
    render_bar_braille,
    render_bar_markup,
    render_bar_plain,
    render_completion_bar,
    render_eu_bar_plain,
    render_size_bar,
)

from ._palette_harness import PaletteHarnessApp

_BRAILLE_FULL = chr(BRAILLE_BASE | BRAILLE_LEFT_COL | BRAILLE_RIGHT_COL)
_BRAILLE_LEFT = chr(BRAILLE_BASE | BRAILLE_LEFT_COL)
_BRAILLE_EMPTY = chr(BRAILLE_BASE)

_THEME = Path(__file__).resolve().parents[2] / "src" / "eawf" / "tui" / "theme.tcss"
_EMPTY_REPO = (
    Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)


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
            # The widget defaults to Braille fill; 80% lights full cells.
            assert _BRAILLE_FULL in rendered

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


def test_render_completion_bar_counter_width_zero_preserves_legacy_format() -> None:
    # counter_width=0 (the default) keeps the natural-width counter so the
    # string is byte-for-byte the pre-W11 rendering.
    explicit = render_completion_bar(3, 6, counter_width=0)
    assert explicit == render_completion_bar(3, 6)
    assert explicit == f"{GLYPH_FULL * 5}{GLYPH_EMPTY * 5}  3/6"


def test_render_completion_bar_counter_width_right_aligns_counter() -> None:
    # A 5-wide counter field right-aligns the 3-char ``6/6`` counter, so two
    # leading spaces pad it into the fixed field.
    bar = render_completion_bar(6, 6, counter_width=5)
    assert bar == f"{GLYPH_FULL * 10}    6/6"  # ``  6/6`` right-aligned in 5
    assert bar.endswith("  6/6")


def test_render_completion_bar_counter_width_constant_length_across_digit_widths() -> None:
    # Mixed-digit counters (6/6, 17/17, 21/21) all render to the same total
    # length when the same counter_width is passed — the alignment contract.
    widest = max(len(f"{n}/{n}") for n in (6, 17, 21))  # ``17/17`` -> 5
    bars = [render_completion_bar(n, n, counter_width=widest) for n in (6, 17, 21)]
    lengths = {len(b) for b in bars}
    assert len(lengths) == 1  # every bar is the same length
    # The glyph run width is constant, so the counter starts in the same
    # column on every row; the widest counter fills its field exactly.
    assert bars[1].endswith(" 17/17")  # 5-wide field, one leading pad
    for bar in bars:
        assert bar.endswith(("6/6", "17/17", "21/21"))


def test_render_completion_bar_counter_width_empty_state_unaffected() -> None:
    # A non-positive total still yields EMPTY_STATE regardless of the field.
    assert render_completion_bar(0, 0, counter_width=5) == EMPTY_STATE


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


# --------------------------------------------------------------------------
# render_bar_braille — Braille dot-matrix fill (U+2800-U+28FF, 2x sub-res)
# --------------------------------------------------------------------------


def test_render_bar_braille_constants_are_in_braille_block() -> None:
    # The full / half / empty cell glyphs all sit in the Braille block.
    for cell in (_BRAILLE_FULL, _BRAILLE_LEFT, _BRAILLE_EMPTY):
        assert 0x2800 <= ord(cell) <= 0x28FF
    # U+28FF (all dots), U+2847 (left column), U+2800 (no dots).
    assert ord(_BRAILLE_FULL) == 0x28FF
    assert ord(_BRAILLE_LEFT) == 0x2847
    assert ord(_BRAILLE_EMPTY) == 0x2800


def test_braille_subcell_fill() -> None:
    # 5 cells x 2 sub-columns = 10 sub-units. frac=0.5 -> 5 sub-columns ->
    # 2 full cells + 1 left-column half cell + 2 empty cells.
    bar = render_bar_braille(0.5)
    assert bar == f"{_BRAILLE_FULL * 2}{_BRAILLE_LEFT}{_BRAILLE_EMPTY * 2}"
    assert all(0x2800 <= ord(c) <= 0x28FF for c in bar)


def test_render_bar_braille_zero_all_empty() -> None:
    bar = render_bar_braille(0.0)
    assert bar == _BRAILLE_EMPTY * BAR_CELLS


def test_render_bar_braille_full_all_filled() -> None:
    bar = render_bar_braille(1.0)
    assert bar == _BRAILLE_FULL * BAR_CELLS


def test_render_bar_braille_over_budget_clamps_to_full() -> None:
    # Over-budget clamps the glyph run to a full bar; the colour band
    # carries the over-budget signal, not the fill.
    assert render_bar_braille(1.8) == _BRAILLE_FULL * BAR_CELLS


def test_render_bar_braille_negative_clamps_to_empty() -> None:
    assert render_bar_braille(-0.5) == _BRAILLE_EMPTY * BAR_CELLS


def test_render_bar_braille_lights_first_subcell_at_half_subcell() -> None:
    # 10 sub-units: half a sub-unit = 0.05; round-half-up lights the first
    # left-column sub-cell at >= 0.05, off below.
    just_below = render_bar_braille(0.04)
    assert just_below == _BRAILLE_EMPTY * BAR_CELLS
    at_boundary = render_bar_braille(0.05)
    assert at_boundary.startswith(_BRAILLE_LEFT)


def test_render_bar_braille_off_by_one_subcell_rounding() -> None:
    # 0.15 over 10 sub-units = 1.5 -> round-half-up -> 2 sub-units -> the
    # first cell is fully lit (both sub-columns), the rest empty.
    bar = render_bar_braille(0.15)
    assert bar == f"{_BRAILLE_FULL}{_BRAILLE_EMPTY * 4}"


def test_render_bar_braille_custom_width_doubles_resolution() -> None:
    # width=3 -> 6 sub-units; frac=1/6 -> 1 sub-unit -> one half cell.
    bar = render_bar_braille(1 / 6, width=3)
    assert bar == f"{_BRAILLE_LEFT}{_BRAILLE_EMPTY * 2}"
    assert len(bar) == 3
    # Resolution is 2x the cell count.
    assert BRAILLE_SUBCOLS == 2


# --------------------------------------------------------------------------
# render_mode flip — braille vs ascii across every bar renderer
# --------------------------------------------------------------------------


def test_render_bar_markup_braille_mode_status_tinted() -> None:
    # Braille glyphs wrapped in the status-tint band span (bar hue == the
    # row's status hue). 80% -> warn band, full braille fill front.
    markup = render_bar_markup(4.0, 5.0, mode="braille")
    assert markup.startswith("[$ok]")  # 80% is the $ok upper bound
    assert _BRAILLE_FULL in markup
    assert "80%" in markup


def test_render_bar_markup_braille_tint_tracks_band() -> None:
    # Over-budget -> $err tint on the braille run (status-tinted).
    markup = render_bar_markup(6.0, 5.0, mode="braille")
    assert markup.startswith("[$err]")
    assert "120%" in markup


def test_ascii_fallback_when_no_braille() -> None:
    # render_mode=ascii renders the #/- glyph set, never the braille block.
    markup = render_bar_markup(2.5, 5.0, mode="ascii")
    assert f"{GLYPH_FULL * 3}{GLYPH_EMPTY * 2}" in markup
    assert all(ord(c) < 0x2800 or ord(c) > 0x28FF for c in markup)


def test_ascii_mode_matches_legacy_default() -> None:
    # The ASCII fill is byte-for-byte the pre-braille #/- rendering
    # (50% -> $ok band, 3 filled + 2 empty cells, content-markup span).
    assert render_bar_markup(2.5, 5.0, mode="ascii") == "[$ok]###--[/]  50%"


def test_render_bar_plain_honours_mode() -> None:
    assert _BRAILLE_FULL in render_bar_plain(5.0, 5.0, mode="braille")
    assert render_bar_plain(5.0, 5.0, mode="ascii") == f"{GLYPH_FULL * 5}  100%"


def test_render_eu_bar_plain_honours_mode() -> None:
    assert _BRAILLE_FULL in render_eu_bar_plain(4.0, 4.0, mode="braille")
    assert GLYPH_FULL in render_eu_bar_plain(4.0, 4.0, mode="ascii")
    # Empty-state guard is mode-independent.
    assert render_eu_bar_plain(0.0, 0.0, mode="braille") == EMPTY_STATE


def test_render_completion_bar_honours_mode() -> None:
    braille = render_completion_bar(3, 6, mode="braille")
    assert braille.endswith("  3/6")
    assert _BRAILLE_FULL in braille
    ascii_bar = render_completion_bar(3, 6, mode="ascii")
    assert ascii_bar == f"{GLYPH_FULL * 5}{GLYPH_EMPTY * 5}  3/6"


def test_render_completion_bar_empty_state_mode_independent() -> None:
    assert render_completion_bar(0, 0, mode="braille") == EMPTY_STATE
    assert render_completion_bar(0, 0, mode="ascii") == EMPTY_STATE


def test_render_size_bar_honours_mode() -> None:
    braille = render_size_bar("M", mode="braille")
    assert braille.endswith("  M")
    assert _BRAILLE_FULL in braille
    ascii_bar = render_size_bar("M", mode="ascii")
    assert ascii_bar == f"{GLYPH_FULL * 3}{GLYPH_EMPTY * 2}  M"


def test_render_size_bar_unknown_bucket_empty_state_both_modes() -> None:
    assert render_size_bar("ZZ", mode="braille") == EMPTY_STATE
    assert render_size_bar("ZZ", mode="ascii") == EMPTY_STATE


# --------------------------------------------------------------------------
# EUBar.render_mode — reactive flip repaints the widget
# --------------------------------------------------------------------------


def test_eu_bar_render_mode_flip_repaints() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(40, 6)) as pilot:
            await pilot.pause()
            bar = app.query_one("#bar", EUBar)
            bar.set_eu(5.0, 5.0)
            await pilot.pause()
            assert _BRAILLE_FULL in app.export_screenshot()
            bar.render_mode = "ascii"
            await pilot.pause()
            rendered = app.export_screenshot()
            assert GLYPH_FULL in rendered
            assert _BRAILLE_FULL not in rendered

    asyncio.run(body())


# --------------------------------------------------------------------------
# EaApp.render_mode — glyph coverage probe + flip resolution
# --------------------------------------------------------------------------


def test_probe_braille_coverage_default_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FONT_NO_BRAILLE", raising=False)
    assert probe_braille_coverage() is True


def test_probe_braille_coverage_false_on_font_no_braille(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FONT_NO_BRAILLE", "1")
    assert probe_braille_coverage() is False


def test_resolve_render_mode_ascii_glyphs_forces_ascii() -> None:
    # ui.glyphs=ascii pins ascii even when coverage is fine.
    assert resolve_render_mode("ascii", braille_ok=True) == "ascii"


def test_resolve_render_mode_unicode_braille_when_covered() -> None:
    assert resolve_render_mode("unicode", braille_ok=True) == "braille"
    assert resolve_render_mode("unicode", braille_ok=False) == "ascii"


def test_resolve_render_mode_auto_tracks_coverage() -> None:
    assert resolve_render_mode("auto", braille_ok=True) == "braille"
    assert resolve_render_mode("auto", braille_ok=False) == "ascii"


def test_eaapp_render_mode_falls_back_to_ascii_on_font_no_braille(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # MOUNT probes coverage; FONT_NO_BRAILLE flips render_mode to ascii and
    # rerenders every mounted bar as #/-.
    monkeypatch.setenv("FONT_NO_BRAILLE", "1")
    monkeypatch.setattr(eaapp_mod, "_persisted_glyphs", lambda: "auto")

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_EMPTY_REPO)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.render_mode == "ascii"
            for bar in app.query(EUBar):
                assert bar.render_mode == "ascii"

    asyncio.run(body())


def test_eaapp_render_mode_braille_when_coverage_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FONT_NO_BRAILLE", raising=False)
    monkeypatch.setattr(eaapp_mod, "_persisted_glyphs", lambda: "auto")

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_EMPTY_REPO)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.render_mode == "braille"
            for bar in app.query(EUBar):
                assert bar.render_mode == "braille"

    asyncio.run(body())


def test_eaapp_render_mode_ascii_when_glyphs_ascii(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ui.glyphs=ascii pins ascii even when the coverage probe would pass.
    monkeypatch.delenv("FONT_NO_BRAILLE", raising=False)
    monkeypatch.setattr(eaapp_mod, "_persisted_glyphs", lambda: "ascii")

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_EMPTY_REPO)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.render_mode == "ascii"

    asyncio.run(body())
