"""Unit + Pilot tests for the C06 ``EUBar`` widget (P26-W17).

Covers the pure render helpers (cell-fill maths, colour band selection,
markup string shape) and a Pilot-driven mount that confirms the bar
paints under a real app loading the W16 ``theme.tcss`` palette.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from rich.text import Text
from textual.app import ComposeResult

import eawf.surfaces.tui.app as eaapp_mod
import eawf.surfaces.tui.widgets.eu_bar as eu_bar_mod
from eawf.surfaces.render.bars import BLOCK_FULL
from eawf.surfaces.tui.app import EaApp, probe_braille_coverage, resolve_render_mode
from eawf.surfaces.tui.widgets.eu_bar import (
    _BUCKET_FIELD_WIDTH,
    BAR_CELLS,
    CANONICAL_BAR_CELLS,
    COMPLETION_FULL,
    COMPLETION_REMAINDER,
    DEFAULT_BAND_PALETTE,
    EMPTY_STATE,
    GLYPH_EMPTY,
    GLYPH_FULL,
    EUBar,
    band_var,
    render_bar_markup,
    render_bar_plain,
    render_bar_rich,
    render_completion_bar,
    render_eu_bar_plain,
    render_size_bar,
)

from ._palette_harness import PaletteHarnessApp

_THEME = Path(__file__).resolve().parents[2] / "src" / "eawf" / "surfaces" / "tui" / "theme.tcss"
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
    # Each cell covers 1/CANONICAL_BAR_CELLS of the range; round-half-up lights
    # cell 1 at >= half a cell (0.5 / CANONICAL_BAR_CELLS of the range).
    half_cell = 0.5 / CANONICAL_BAR_CELLS
    just_below = render_bar_markup(half_cell - 0.01, 1.0)  # below half a cell -> 0
    assert just_below.count(GLYPH_FULL) == 0
    at_boundary = render_bar_markup(half_cell, 1.0)  # exactly half a cell -> 1
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
            # The widget defaults to the unicode block-eighths fill; 80%
            # lights full cells.
            assert BLOCK_FULL in rendered

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
    assert bar == f"{GLYPH_EMPTY * CANONICAL_BAR_CELLS}      0/6"


def test_render_completion_bar_full_when_done_equals_total() -> None:
    bar = render_completion_bar(6, 6)
    assert bar == f"{GLYPH_FULL * CANONICAL_BAR_CELLS}      6/6"


def test_render_completion_bar_clamps_done_over_total() -> None:
    # done > total clamps both the fill and the count suffix to total.
    bar = render_completion_bar(9, 6)
    assert bar == f"{GLYPH_FULL * CANONICAL_BAR_CELLS}      6/6"


def test_render_completion_bar_clamps_negative_done_to_zero() -> None:
    bar = render_completion_bar(-3, 6)
    assert bar == f"{GLYPH_EMPTY * CANONICAL_BAR_CELLS}      0/6"


def test_render_completion_bar_zero_total_empty_state() -> None:
    assert render_completion_bar(0, 0) == EMPTY_STATE


def test_render_completion_bar_negative_total_empty_state() -> None:
    assert render_completion_bar(2, -1) == EMPTY_STATE


def test_render_completion_bar_half_ratio_fills_half() -> None:
    half = CANONICAL_BAR_CELLS // 2
    bar = render_completion_bar(3, 6)
    assert bar == f"{GLYPH_FULL * half}{GLYPH_EMPTY * half}      3/6"
    # 3/6 == 0.5 over the canonical-width bar -> exactly half the cells filled.
    fraction = 3 / 6
    assert fraction == pytest.approx(0.5)


def test_render_completion_bar_custom_width() -> None:
    bar = render_completion_bar(1, 4, width=4)
    assert bar == f"{GLYPH_FULL * 1}{GLYPH_EMPTY * 3}      1/4"


def test_render_completion_bar_counter_right_aligned_fixed_field() -> None:
    # The whole ``done/total`` counter is right-aligned into a fixed 7-cell
    # field (``###/###``), so a 3-char counter carries four leading pads.
    bar = render_completion_bar(6, 6)
    assert bar == f"{GLYPH_FULL * CANONICAL_BAR_CELLS}      6/6"
    assert bar.endswith("      6/6")
    counter = bar.removeprefix(GLYPH_FULL * CANONICAL_BAR_CELLS).lstrip(" ")
    assert bar.endswith(counter.rjust(7))


def test_render_completion_bar_counter_constant_length_across_digit_widths() -> None:
    # Mixed-digit counters (6/6, 12/12, 17/17) right-align into the same
    # fixed field, so every bar is the same total length — the alignment
    # contract that keeps the counters column-aligned.
    bars = [render_completion_bar(n, n) for n in (6, 12, 17)]
    lengths = {len(b) for b in bars}
    assert len(lengths) == 1  # every bar is the same length
    assert bars[0].endswith("      6/6")  # 3-char counter, four leading pads
    assert bars[1].endswith("  12/12")  # 5-char counter, two leading pads
    assert bars[2].endswith("  17/17")


def test_render_completion_bar_counter_overflows_field_gracefully() -> None:
    # A 4-digit total exceeds the 7-cell field; the width is a minimum so the
    # counter overflows rather than truncating.
    bar = render_completion_bar(1000, 1000)
    assert bar.endswith("1000/1000")


def test_render_completion_bar_empty_state_no_counter() -> None:
    # A non-positive total still yields EMPTY_STATE — no counter field.
    assert render_completion_bar(0, 0) == EMPTY_STATE


# --------------------------------------------------------------------------
# render_size_bar — wave effort-bucket bar (W05)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bucket", "filled"),
    [("XS", 1), ("S", 2), ("M", 3), ("L", 4), ("XL", 5)],
)
def test_render_size_bar_each_bucket(bucket: str, filled: int) -> None:
    bar = render_size_bar(bucket)
    # The bucket label is right-justified in a fixed field so every bucket's
    # bar string is the same length (so right-pinned glyph runs align).
    label = f"{bucket:>{_BUCKET_FIELD_WIDTH}}"
    assert bar == f"{GLYPH_FULL * filled}{GLYPH_EMPTY * (5 - filled)}  {label}"


def test_render_size_bar_fixed_width_constant_length() -> None:
    """Every bucket renders to the same total length (the alignment invariant)."""
    lengths = {len(render_size_bar(b)) for b in ("XS", "S", "M", "L", "XL")}
    assert len(lengths) == 1  # one-char and two-char buckets share a width


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
    # 2/4 == 0.5 over the canonical-width EU bar -> half the cells filled.
    assert bar.count(GLYPH_FULL) == CANONICAL_BAR_CELLS // 2
    assert pytest.approx(0.5) == (2.0 / 4.0)


def test_empty_state_constant_is_stable() -> None:
    # W06 imports this sentinel; the exact glyph is part of the contract.
    assert EMPTY_STATE == "— no data"


# --------------------------------------------------------------------------
# Braille retirement — the dead path is gone (deletion proof)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "render_bar_braille",
        "_braille_subcells",
        "BRAILLE_BASE",
        "BRAILLE_LEFT_COL",
        "BRAILLE_RIGHT_COL",
        "BRAILLE_SUBCOLS",
    ],
)
def test_retired_braille_symbol_is_gone(name: str) -> None:
    # The dead Braille path was deleted: every removed symbol raises on a
    # ``getattr`` so a stray import / call fails loudly rather than resurrecting
    # the unreachable machinery.
    with pytest.raises(AttributeError):
        getattr(eu_bar_mod, name)


def test_retired_braille_symbol_not_importable() -> None:
    # The removed names are not re-exported either: an ``import`` of the dead
    # symbol raises ImportError.
    with pytest.raises(ImportError):
        from eawf.surfaces.tui.widgets.eu_bar import (  # noqa: F401
            render_bar_braille,
        )


# --------------------------------------------------------------------------
# render_mode flip — unicode vs ascii across every bar renderer
# --------------------------------------------------------------------------


def test_render_bar_markup_unicode_mode_status_tinted() -> None:
    # Block-eighths glyphs wrapped in the status-tint band span (bar hue ==
    # the row's status hue). 80% -> warn band, full block-eighths fill front.
    markup = render_bar_markup(4.0, 5.0, mode="unicode")
    assert markup.startswith("[$ok]")  # 80% is the $ok upper bound
    assert BLOCK_FULL in markup
    assert "80%" in markup


def test_render_bar_markup_unicode_tint_tracks_band() -> None:
    # Over-budget -> $err tint on the block run (status-tinted).
    markup = render_bar_markup(6.0, 5.0, mode="unicode")
    assert markup.startswith("[$err]")
    assert "120%" in markup


def test_ascii_fallback_when_no_unicode() -> None:
    # render_mode=ascii renders the #/- glyph set, never the block-eighths run.
    # 50% over the canonical-width bar -> exactly half the cells filled.
    half = CANONICAL_BAR_CELLS // 2
    markup = render_bar_markup(2.5, 5.0, mode="ascii")
    assert f"{GLYPH_FULL * half}{GLYPH_EMPTY * half}" in markup
    assert all(ord(c) < 0x2580 or ord(c) > 0x259F for c in markup)


def test_ascii_mode_matches_legacy_default() -> None:
    # The ASCII fill is the #/- rendering at the canonical width (50% -> $ok
    # band, half filled + half empty cells, content-markup span).
    half = CANONICAL_BAR_CELLS // 2
    expected = f"[$ok]{GLYPH_FULL * half}{GLYPH_EMPTY * half}[/]  50%"
    assert render_bar_markup(2.5, 5.0, mode="ascii") == expected


# --------------------------------------------------------------------------
# render_bar_rich — hex-resolved tint for Rich-parsed contexts (W22)
# --------------------------------------------------------------------------


def test_render_bar_rich_emits_hex_not_palette_var() -> None:
    # The Rich-context variant bakes the band to a concrete hex (no $var),
    # so a Rich-parsed DataTable cell renders it without MarkupError.
    half = CANONICAL_BAR_CELLS // 2
    bar = render_bar_rich(2.5, 5.0, mode="ascii")
    assert bar == f"[{DEFAULT_BAND_PALETTE['ok']}]{GLYPH_FULL * half}{GLYPH_EMPTY * half}[/]  50%"
    assert "$" not in bar


def test_render_bar_rich_is_rich_parseable_across_bands() -> None:
    # Regression: [$ok|$warn|$err] vars crash Text.from_markup; resolved hex
    # must parse for the ok, warn, and over-budget err bands alike.
    for consumed, total in ((1.0, 5.0), (4.5, 5.0), (12.0, 5.0)):
        Text.from_markup(render_bar_rich(consumed, total, mode="unicode"))


def test_render_bar_rich_honours_custom_palette() -> None:
    full = GLYPH_FULL * CANONICAL_BAR_CELLS
    bar = render_bar_rich(6.0, 5.0, mode="ascii", palette={"err": "#123456"})
    assert bar == f"[#123456]{full}[/]  120%"


def test_render_bar_rich_falls_back_on_missing_band_key() -> None:
    # A palette missing the active band key falls back to the default hex.
    full = GLYPH_FULL * CANONICAL_BAR_CELLS
    bar = render_bar_rich(6.0, 5.0, mode="ascii", palette={"ok": "#000000"})
    assert bar == f"[{DEFAULT_BAND_PALETTE['err']}]{full}[/]  120%"


def test_render_bar_plain_honours_mode() -> None:
    assert BLOCK_FULL in render_bar_plain(5.0, 5.0, mode="unicode")
    assert render_bar_plain(5.0, 5.0, mode="ascii") == f"{GLYPH_FULL * CANONICAL_BAR_CELLS}  100%"


def test_render_eu_bar_plain_honours_mode() -> None:
    assert BLOCK_FULL in render_eu_bar_plain(4.0, 4.0, mode="unicode")
    assert GLYPH_FULL in render_eu_bar_plain(4.0, 4.0, mode="ascii")
    # Empty-state guard is mode-independent.
    assert render_eu_bar_plain(0.0, 0.0, mode="unicode") == EMPTY_STATE


def test_render_completion_bar_honours_mode() -> None:
    half = CANONICAL_BAR_CELLS // 2
    block = render_completion_bar(3, 6, mode="unicode")
    assert block.endswith("      3/6")
    assert BLOCK_FULL in block
    ascii_bar = render_completion_bar(3, 6, mode="ascii")
    assert ascii_bar == f"{GLYPH_FULL * half}{GLYPH_EMPTY * half}      3/6"


def test_render_completion_bar_unicode_full_block_cells_with_ratio() -> None:
    # The success-criterion shape: a fully-closed iter paints all FULL BLOCK
    # cells (no remainder) with the right-aligned n/n counter, and the ascii
    # fallback paints the #/- ratio with the same counter.
    block = render_completion_bar(6, 6, width=10, mode="unicode")
    assert block == f"{COMPLETION_FULL * 10}      6/6"
    ascii_bar = render_completion_bar(6, 6, width=10, mode="ascii")
    assert ascii_bar == f"{GLYPH_FULL * 10}      6/6"


def test_render_completion_bar_unicode_partial_uses_shade_remainder() -> None:
    # A partial ratio paints a FULL BLOCK fill run over a MEDIUM SHADE track
    # so the unfilled tail reads as track, not blank space.
    block = render_completion_bar(3, 6, width=10, mode="unicode")
    assert block == f"{COMPLETION_FULL * 5}{COMPLETION_REMAINDER * 5}      3/6"
    assert COMPLETION_REMAINDER == "▒"


def test_render_completion_bar_empty_state_mode_independent() -> None:
    assert render_completion_bar(0, 0, mode="unicode") == EMPTY_STATE
    assert render_completion_bar(0, 0, mode="ascii") == EMPTY_STATE


def test_render_size_bar_honours_mode() -> None:
    block = render_size_bar("M", mode="unicode")
    assert block.endswith(f"  {'M':>{_BUCKET_FIELD_WIDTH}}")
    assert BLOCK_FULL in block
    ascii_bar = render_size_bar("M", mode="ascii")
    assert ascii_bar == f"{GLYPH_FULL * 3}{GLYPH_EMPTY * 2}  {'M':>{_BUCKET_FIELD_WIDTH}}"


def test_render_size_bar_unknown_bucket_empty_state_both_modes() -> None:
    assert render_size_bar("ZZ", mode="unicode") == EMPTY_STATE
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
            assert BLOCK_FULL in app.export_screenshot()
            bar.render_mode = "ascii"
            await pilot.pause()
            rendered = app.export_screenshot()
            assert GLYPH_FULL in rendered
            assert BLOCK_FULL not in rendered

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


def test_resolve_render_mode_unicode_when_covered() -> None:
    assert resolve_render_mode("unicode", braille_ok=True) == "unicode"
    assert resolve_render_mode("unicode", braille_ok=False) == "ascii"


def test_resolve_render_mode_auto_tracks_coverage() -> None:
    assert resolve_render_mode("auto", braille_ok=True) == "unicode"
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


def test_eaapp_render_mode_unicode_when_coverage_ok(
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
            assert app.render_mode == "unicode"
            for bar in app.query(EUBar):
                assert bar.render_mode == "unicode"

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
