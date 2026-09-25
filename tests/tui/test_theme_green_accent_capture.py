"""Per-theme colour capture of the green accent + closed-wave tint.

The wave's keystone is the teal -> green ``accent`` / ``primary`` rotation
that every downstream surface dogfoods. The existing
``tests/snapshots/tui`` golden harness captures the rendered screen as
**plain ASCII text** (``Strip.text``) and so discards colour entirely -- it
cannot prove a cell carries the green accent. This suite is the
colour-aware complement: it mounts the colour-bearing widgets under a
Textual ``run_test`` Pilot, reads the per-cell foreground hex off the
compositor's rendered strips, and asserts the green resolves per theme.

What it pins, per theme (dark / cb / light):

1. The header brand (``Ea`` + umlaut) renders the theme's green ``$accent``
   -- the header wraps the whole brand in ``[$accent][b]...[/]``, so the
   accent rotation lands on the brand glyphs.
2. A CLOSED wave row's status glyph (``#``) renders the green
   ``status-closed`` tint. The roadmap tree's row labels are Rich-parsed and
   cannot resolve the ``$status-*`` palette vars, so their glyph tint comes
   from the shared :data:`~eawf.surfaces.tui.widgets.status_tint.STATUS_COLOURS`
   map, which is sourced from the canonical Wong (dark) palette regardless of
   the ACTIVE theme. The closed glyph is therefore the Wong green
   ``#009e73`` on every theme -- the assertion pins that the rotation left
   the closed tint green (it did not ride the teal -> green accent move).
3. A ``/theme`` swap is a pure var rebind: after swapping dark -> cb -> dark
   the brand's resolved accent tracks the active theme with NO stale prior
   accent left behind (no recolour gap).
4. Against the palette rebound to the packet colour oracle, the dark accent
   stays apart from the closed green the oracle records as its one admitted
   divergence, and the epoch-1 ``theme.tcss`` rules the console rebuild has
   not yet retired (the user-scope section rules, the honest-empty
   placeholder rules and the lifecycle-tint carve-out) are still in place.

Colour capture is deterministic here: the brand + glyph colours are a pure
function of the registered theme's ``variables`` map, with no clock / git /
daemon volatility (those live on the full scope screens, not these two
widgets), so no golden file is needed -- the assertion is the resolved hex.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from textual.app import ComposeResult
from textual.theme import Theme

import eawf.surfaces.tui as tui_package
from eawf.kernel.state.enums import WaveStatus
from eawf.kernel.state.models import State
from eawf.surfaces.tui.chassis.sigils import Sigil, glyph
from eawf.surfaces.tui.chassis.theme import EA_CB, EA_DARK, EA_LIGHT, EA_THEMES
from eawf.surfaces.tui.widgets.header import Header
from eawf.surfaces.tui.widgets.roadmap_tree import RoadmapTree
from eawf.surfaces.tui.widgets.status_tint import STATUS_COLOURS

from ._palette_harness import PaletteHarnessApp

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"
_COLOUR_ORACLE = _FIXTURES.parents[1] / "console" / "colour-oracle.json"
_THEME_TCSS = Path(tui_package.__file__).resolve().parent / "theme.tcss"

#: Epoch-1 ``theme.tcss`` selectors that match no mounted widget today but
#: stay until the console rebuild retires the screens they were written for.
_RETAINED_SELECTORS: tuple[str, ...] = (
    ".section {",
    "#attention {",
    "#effort {",
    "#portfolio {",
    ".section-title {",
    ".placeholder-notice {",
    ".feed-empty {",
)

#: Lifecycle vars every theme keeps declaring although only the Rich-context
#: tint map reads their dark values.
_RETAINED_LIFECYCLE_VARS: tuple[str, ...] = (
    "ok",
    "status-pending",
    "status-claimed",
    "status-in-progress",
    "status-closed",
    "status-failed",
)

#: A per-cell foreground map: char -> the set of foreground hexes (or None)
#: it carries across the widget's rendered strips.
_CellMap = dict[str, set[str | None]]

#: The reskin green ``$accent`` each theme resolves (teal -> green rotation).
_THEME_ACCENT: dict[str, str] = {
    "ea-dark": "#16b384",
    "ea-cb": "#1a9988",
    "ea-light": "#0a7a52",
}

#: The green ``status-closed`` glyph tint the roadmap tree renders. The
#: tree's Rich-parsed labels cannot resolve ``$status-*`` vars, so the tint
#: is the canonical Wong (dark) ``status-closed`` green on EVERY theme,
#: not the per-theme palette value. Pinned here as the one Wong green.
_WONG_STATUS_CLOSED: str = "#009e73"


def _closed_wave_state() -> State:
    """Load the active fixture and flip every wave to CLOSED.

    The committed fixtures carry no CLOSED wave, so the closed-row colour
    assertion synthesises one by flipping the fixture's waves to
    :data:`WaveStatus.CLOSED`. The phase / iter / wave structure (and thus
    the tree shape) is otherwise the real fixture.

    Returns:
        A bound state whose waves are all CLOSED.
    """
    state = State.model_validate_json(_PHASE_ITER_WAVE.read_text(encoding="utf-8"))
    for wave in state.waves.values():
        wave.status = WaveStatus.CLOSED
    return state


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


class _Harness(PaletteHarnessApp):
    """Minimal app mounting the two colour-bearing widgets for capture."""

    CSS = """
    Header { height: 1; }
    RoadmapTree { height: 30; }
    """

    def compose(self) -> ComposeResult:
        yield Header(id="hdr")
        yield RoadmapTree(id="rt")


async def _capture_for_theme(theme_name: str) -> tuple[_CellMap, _CellMap]:
    """Mount the harness on *theme_name* and return (header, tree) cell maps."""
    app = _Harness()
    async with app.run_test(size=(120, 40)) as pilot:
        app.register_theme(EA_DARK)
        app.register_theme(EA_CB)
        app.register_theme(EA_LIGHT)
        app.theme = theme_name
        await pilot.pause()
        state = _closed_wave_state()
        header = app.query_one("#hdr", Header)
        tree = app.query_one("#rt", RoadmapTree)
        header.state = state
        tree.state = state
        await pilot.pause()
        await pilot.pause()
        return _cell_fg_hexes(header), _cell_fg_hexes(tree)


@pytest.mark.parametrize("theme_name", ["ea-dark", "ea-cb", "ea-light"])
def test_header_brand_renders_green_accent(theme_name: str) -> None:
    """The header brand umlaut carries the theme's green ``$accent``."""

    async def body() -> None:
        header_map, _tree_map = await _capture_for_theme(theme_name)
        # The brand umlaut (U+00E4) is the load-bearing branded glyph.
        umlaut_fg = header_map.get("ä")
        assert umlaut_fg is not None, "brand umlaut not rendered"
        assert _THEME_ACCENT[theme_name] in umlaut_fg

    asyncio.run(body())


@pytest.mark.parametrize("theme_name", ["ea-dark", "ea-cb", "ea-light"])
def test_closed_wave_row_renders_green_status_closed(theme_name: str) -> None:
    """A CLOSED wave row's sigil carries the green Wong ``status-closed``.

    The roadmap tree's Rich-parsed labels resolve their tint from the
    canonical Wong palette, so the closed glyph is the Wong green
    ``#009e73`` on every theme (see the module docstring). The glyph is
    resolved through the sigils home (ASCII column -- the bare harness app
    carries no ``render_mode`` so the tree falls back to ASCII) so a future
    shape change cannot silently un-pin the colour assert.
    """

    async def body() -> None:
        _header_map, tree_map = await _capture_for_theme(theme_name)
        closed_glyph_fg = tree_map.get(glyph(Sigil.CLOSED, mode="ascii"))
        assert closed_glyph_fg is not None, "closed-wave glyph not rendered"
        assert _WONG_STATUS_CLOSED in closed_glyph_fg

    asyncio.run(body())


def test_dark_accent_distinct_from_closed_tint_but_both_green() -> None:
    """In the rebound dark theme the accent green and the closed green stay distinct.

    The packet oracle gives ``status-closed`` the accent green; the product
    records keeping the Wong green instead as the one admitted divergence, so
    the header brand does not look identical to a closed-row glyph. The
    assertion reads the live palette and the recorded divergence, so a
    rebind that collapses the two reds here.
    """
    oracle = json.loads(_COLOUR_ORACLE.read_text(encoding="utf-8"))
    (divergence,) = oracle["divergences"]
    variables = EA_DARK.variables
    assert variables["accent"] == _THEME_ACCENT["ea-dark"]
    assert variables["status-closed"] == _WONG_STATUS_CLOSED == divergence["shipped"]
    assert STATUS_COLOURS["closed"] == _WONG_STATUS_CLOSED
    assert variables["accent"] != variables["status-closed"]
    assert divergence["oracle"] == variables["accent"]


def test_epoch1_theme_tcss_keeps_its_unretired_rules() -> None:
    """The console rebind leaves the unretired epoch-1 rules in ``theme.tcss``."""
    stylesheet = _THEME_TCSS.read_text(encoding="utf-8")
    missing = [selector for selector in _RETAINED_SELECTORS if selector not in stylesheet]
    assert not missing, f"theme.tcss lost {missing}"
    assert "color: $text-muted;" in stylesheet
    assert "STATUS_COLOURS" in stylesheet


@pytest.mark.parametrize("theme", EA_THEMES, ids=lambda theme: theme.name)
def test_every_theme_keeps_declaring_the_lifecycle_vars(theme: Theme) -> None:
    """Every theme still declares the lifecycle vars the tint carve-out mirrors."""
    missing = [name for name in _RETAINED_LIFECYCLE_VARS if name not in theme.variables]
    assert not missing, f"{theme.name} dropped {missing}"


def test_theme_swap_is_pure_var_rebind_no_recolour_gap() -> None:
    """A ``/theme`` swap rebinds the brand accent with no stale prior accent.

    Swaps dark -> cb -> dark on one live app and asserts the brand umlaut's
    resolved foreground tracks the active theme each step, with the prior
    theme's accent never lingering on the brand (the no-recolour-gap
    invariant the per-theme ``variables`` migration guarantees).
    """

    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            app.register_theme(EA_DARK)
            app.register_theme(EA_CB)
            app.register_theme(EA_LIGHT)
            app.theme = "ea-dark"
            await pilot.pause()
            header = app.query_one("#hdr", Header)
            header.state = _closed_wave_state()
            await pilot.pause()
            dark_fg = _cell_fg_hexes(header).get("ä")
            assert dark_fg == {_THEME_ACCENT["ea-dark"]}

            app.theme = "ea-cb"
            await pilot.pause()
            await pilot.pause()
            cb_fg = _cell_fg_hexes(header).get("ä")
            # Pure rebind: only the cb accent, no stale dark accent.
            assert cb_fg == {_THEME_ACCENT["ea-cb"]}
            assert _THEME_ACCENT["ea-dark"] not in (cb_fg or set())

            app.theme = "ea-dark"
            await pilot.pause()
            await pilot.pause()
            back_fg = _cell_fg_hexes(header).get("ä")
            assert back_fg == {_THEME_ACCENT["ea-dark"]}
            assert _THEME_ACCENT["ea-cb"] not in (back_fg or set())

    asyncio.run(body())
