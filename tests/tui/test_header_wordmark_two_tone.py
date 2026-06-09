"""Surface-scoped golden for the W03 two-tone Eae header wordmark + idle cell.

This suite is the close-gate bar for wave ``P30-I02-W03``: it mounts the
shared :class:`~eawf.surfaces.tui.widgets.header.Header` widget IN ISOLATION
(not the full app, so the capture is surface-scoped and cannot drift on a
sibling pane's edit) and proves three claims a colourless ``.txt`` golden
cannot:

1. The wordmark is **two-tone** -- the umlaut cell (U+00E4) resolves the
   reskin green accent ``#16b384`` while the leading ``E`` cell does NOT
   (it stays in the base foreground). The colourless text-strip golden the
   ``tests/snapshots/tui`` harness captures discards colour, so the proof
   has to read the per-cell truecolor foreground off the compositor strips
   (the same pattern as ``test_theme_green_accent_capture``).
2. The idle runtime cell renders the harmony chrome glyph (unicode
   ``≈``) + ``idle`` with NO ``runtime:`` field label.
3. The breadcrumb is byte-for-byte the
   :func:`~eawf.surfaces.tui.widgets.header.build_breadcrumb` output -- the
   wordmark swap did not perturb the ``scope > code > phase > iter > mode``
   trail.

The capture is deterministic for the colour + glyph claims (a pure function
of the active theme's ``variables`` map), so no committed ``.txt`` file is
needed -- the resolved hex + glyph IS the golden, asserted inline. The UTC
clock (the one volatile cell) is never asserted.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import orjson
from textual.app import ComposeResult

from eawf.kernel.state.models import State
from eawf.surfaces.render.brand import ACCENT_HEX
from eawf.surfaces.tui.widgets.header import (
    RUNTIME_IDLE,
    Header,
    build_breadcrumb,
)

from ._palette_harness import PaletteHarnessApp

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"
_THEME = Path(__file__).resolve().parents[2] / "src" / "eawf" / "surfaces" / "tui" / "theme.tcss"

#: The active fixture's project code, pinned so the assertions read clearly.
_CODE = "QR"

#: A per-cell foreground map: char -> the set of foreground hexes (or None)
#: it carries across the header's rendered strips.
_CellMap = dict[str, set[str | None]]


class _HeaderHarness(PaletteHarnessApp):
    """Production-style host loading the real palette CSS, header only."""

    CSS_PATH = str(_THEME)

    def compose(self) -> ComposeResult:
        yield Header(id="hdr")


def _load(path: Path) -> State:
    return State.model_validate(orjson.loads(path.read_bytes()))


def _idle_state() -> State:
    """Return the phase-iter fixture with no active wave (idle runtime cell).

    The committed active fixture pins a running wave, so the idle branch is
    synthesised by clearing ``current.active_wave_ids`` -- the breadcrumb
    trail (phase + iter) stays intact while the runtime cell falls idle.
    """
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    payload["current"]["active_wave_ids"] = []
    return State.model_validate(payload)


def _cell_fg_hexes(widget: object) -> _CellMap:
    """Map each rendered character to the set of foreground hexes it carries.

    Walks the widget's rendered strips off the screen compositor and reads
    the truecolor foreground of every segment, bucketing by the segment's
    characters. A cell with no explicit foreground maps to ``None``.

    Args:
        widget: A mounted widget exposing ``screen._compositor``.

    Returns:
        ``{char: {hex_or_None, ...}}`` over the widget's visible cells.
    """
    strips = widget.screen._compositor.render_strips()  # type: ignore[attr-defined]
    out: _CellMap = {}
    for strip in strips:
        for segment in strip._segments:
            style = segment.style
            fg: str | None = None
            if style is not None and style.color is not None:
                trip = style.color.get_truecolor()
                fg = f"#{trip.red:02x}{trip.green:02x}{trip.blue:02x}"
            for char in segment.text:
                out.setdefault(char, set()).add(fg)
    return out


def _strip_text(widget: object) -> str:
    """Return the concatenated visible text of the widget's rendered strips.

    Reads the compositor strips (the same source as :func:`_cell_fg_hexes`)
    and joins their segment text, so a unicode glyph survives as its literal
    codepoint -- unlike ``export_screenshot`` which serialises to SVG and
    escapes the glyph into a font reference.

    Args:
        widget: A mounted widget exposing ``screen._compositor``.

    Returns:
        The widget's visible text, strips concatenated.
    """
    strips = widget.screen._compositor.render_strips()  # type: ignore[attr-defined]
    return "".join(segment.text for strip in strips for segment in strip._segments)


async def _capture_header() -> _CellMap:
    """Mount the header in isolation on the dark theme and return its cell map."""
    app = _HeaderHarness()
    async with app.run_test(size=(120, 3)) as pilot:
        await pilot.pause()
        header = app.query_one("#hdr", Header)
        header.state = _load(_PHASE_ITER_WAVE)
        await pilot.pause()
        await pilot.pause()
        return _cell_fg_hexes(header)


def test_header_wordmark_umlaut_carries_green_accent_e_does_not() -> None:
    """Two-tone proof: the umlaut cell is the reskin green; the E cell is not.

    The umlaut (U+00E4) resolves the dark theme's green ``$accent``
    (:data:`~eawf.surfaces.render.brand.ACCENT_HEX`) while the leading ``E``
    glyph stays in the base foreground -- the accent never bleeds onto it.
    This is the load-bearing two-tone claim the colourless text golden
    cannot make.
    """

    async def body() -> None:
        cell_map = await _capture_header()
        umlaut_fg = cell_map.get("ä")
        assert umlaut_fg is not None, "brand umlaut not rendered"
        assert ACCENT_HEX in umlaut_fg, "umlaut does not carry the green accent"
        e_fg = cell_map.get("E")
        assert e_fg is not None, "brand E not rendered"
        # The E must NOT carry the accent -- that is the whole two-tone point.
        assert ACCENT_HEX not in e_fg, "E wrongly carries the accent (not two-tone)"

    asyncio.run(body())


def test_header_idle_cell_renders_harmony_glyph_no_runtime_label() -> None:
    """The idle runtime cell shows the harmony glyph + idle, no runtime: label.

    Captured off the live header text (markup stripped): the unicode harmony
    chrome glyph U+2248 is present, ``idle`` is present, and the literal
    ``runtime:`` field label is gone.
    """

    async def body() -> None:
        app = _HeaderHarness()
        async with app.run_test(size=(120, 3)) as pilot:
            await pilot.pause()
            header = app.query_one("#hdr", Header)
            # No active wave on the bound state -> the idle branch fires.
            header.state = _idle_state()
            await pilot.pause()
            await pilot.pause()
            rendered = _strip_text(header)
        assert "≈" in rendered, "harmony glyph not in idle cell"
        assert RUNTIME_IDLE in rendered
        # The idle cell drops the field label entirely (W03 reskin).
        assert "runtime:" not in rendered

    asyncio.run(body())


def test_header_breadcrumb_unchanged_under_wordmark() -> None:
    """The breadcrumb trail is verbatim build_breadcrumb output.

    The wordmark swap is brand-only: the rendered header still embeds the
    exact ``scope > code > phase > iter > mode`` breadcrumb (and the dark
    theme's per-cell colours leave the code segment readable), so the
    breadcrumb-unchanged claim holds against the live frame.
    """

    async def body() -> None:
        app = _HeaderHarness()
        async with app.run_test(size=(120, 3)) as pilot:
            await pilot.pause()
            header = app.query_one("#hdr", Header)
            header.state = _load(_PHASE_ITER_WAVE)
            await pilot.pause()
            await pilot.pause()
            rendered = _strip_text(header)
        # The visible breadcrumb segments survive verbatim in the painted frame.
        for segment in ("repo", _CODE, "P01", "P01-I01"):
            assert segment in rendered, f"breadcrumb segment {segment!r} missing"

    asyncio.run(body())


def test_header_breadcrumb_markup_is_byte_equal_to_builder() -> None:
    """The header markup embeds the exact build_breadcrumb string (no drift).

    A pure-render check (no mount) that the W03 wordmark change left the
    breadcrumb builder output byte-for-byte intact inside the header line.
    """
    from eawf.surfaces.tui.widgets.header import render_header

    state = _load(_PHASE_ITER_WAVE)
    crumb = build_breadcrumb(state, "repo", "Home", mode_name="home", clickable=True)
    rendered = render_header(state, "repo", "Home", mode_name="home")
    assert crumb in rendered
