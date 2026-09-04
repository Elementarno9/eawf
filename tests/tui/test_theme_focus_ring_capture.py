"""Styled capture proving the pane focus ring actually renders as a ring.

``theme.tcss`` paints the unfocused ``.pane`` border ``$accent`` and the
focused ``.pane.-focused`` border ``$primary``. While every theme shipped
``primary`` equal to ``accent`` those two rules resolved to the SAME border,
so the ring was invisible by construction -- the ``-focused`` class toggle in
``RepoZoomMixin._repaint_zoom_focus`` was live and correct, and still changed
nothing on screen. A class-toggle unit test cannot see that defect: the class
was always applied. Only a colour-aware capture of the rendered border can.

So this suite mounts two real ``.pane`` containers under the shared
``theme.tcss``, marks one ``-focused``, and reads the truecolor foreground off
each pane's own rendered top border row (``Widget.render_lines`` applies the
border chrome, so the strip carries the resolved border colour). It asserts,
per registered theme:

1. The focused pane's border resolves EXACTLY the theme's ``$primary``.
2. The unfocused sibling's border resolves EXACTLY the theme's ``$accent``.
3. Those two hexes differ, and differ by enough luminance to be seen -- a
   one-bit difference would satisfy ``!=`` while still rendering as no ring.

The capture is per-widget rather than whole-screen on purpose: both panes
draw the same ``round`` border glyphs, so a screen-wide char -> colour map
would merge the focused and unfocused rings into one bucket and could not
tell them apart.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.app import ComposeResult
from textual.color import Color
from textual.containers import Container
from textual.geometry import Region
from textual.theme import Theme
from textual.widgets import Static

import eawf.surfaces.tui as tui_package
from eawf.surfaces.tui.theme import EA_CB, EA_DARK, EA_LIGHT, EA_THEMES

from ._contrast import contrast_ratio
from ._palette_harness import PaletteHarnessApp

#: The shared structural stylesheet the App loads via ``CSS_PATH``. The
#: harness points at the same file so the ``.pane`` / ``.pane.-focused``
#: rules under test are the SHIPPED ones, not a test-local restatement.
_THEME_TCSS: str = str(Path(tui_package.__file__).resolve().parent / "theme.tcss")

#: Registered themes keyed by name, so a parametrised case can look up the
#: expected ``$primary`` / ``$accent`` for the theme it activated.
_THEMES: dict[str, Theme] = {theme.name: theme for theme in (EA_DARK, EA_CB, EA_LIGHT)}

#: Minimum WCAG contrast ratio between ``primary`` and ``accent``. A ring
#: that merely differs in the last bit is still invisible; 1.5:1 is the
#: floor at which the lit border reads as a separate tone from the dim one.
_MIN_RING_SEPARATION: float = 1.5


class _PaneHarness(PaletteHarnessApp):
    """Two bordered ``.pane`` containers under the shipped structural CSS."""

    CSS_PATH = _THEME_TCSS

    def compose(self) -> ComposeResult:
        with Container(id="pane-focused", classes="pane"):
            yield Static("focused")
        with Container(id="pane-idle", classes="pane"):
            yield Static("idle")


def _border_hexes(pane: Container) -> set[str]:
    """Truecolor foregrounds on *pane*'s own rendered top border row.

    ``Widget.render_lines`` runs the widget through its styles cache, which
    is where the border chrome is drawn, so row 0 of the result is the
    ``round`` border's top edge painted in the resolved border colour.

    Args:
        pane: A mounted, laid-out pane container.

    Returns:
        The set of ``#rrggbb`` foregrounds present on the top border row.
    """
    strips = pane.render_lines(Region(0, 0, pane.size.width, 1))
    hexes: set[str] = set()
    for strip in strips:
        for segment in strip._segments:
            style = segment.style
            if style is None or style.color is None:
                continue
            triplet = style.color.get_truecolor()
            hexes.add(f"#{triplet.red:02x}{triplet.green:02x}{triplet.blue:02x}")
    return hexes


async def _capture_ring(theme_name: str) -> tuple[set[str], set[str]]:
    """Mount the harness on *theme_name*; return (focused, unfocused) border hexes."""
    app = _PaneHarness()
    async with app.run_test(size=(60, 16)) as pilot:
        app.theme = theme_name
        await pilot.pause()
        focused = app.query_one("#pane-focused", Container)
        idle = app.query_one("#pane-idle", Container)
        focused.add_class("-focused")
        await pilot.pause()
        await pilot.pause()
        return _border_hexes(focused), _border_hexes(idle)


@pytest.mark.parametrize("theme_name", ["ea-dark", "ea-cb", "ea-light"])
def test_focused_pane_border_renders_primary(theme_name: str) -> None:
    """The ``.pane.-focused`` border resolves the theme's ``$primary``."""

    async def body() -> None:
        focused_hexes, _idle = await _capture_ring(theme_name)
        expected = _THEMES[theme_name].variables["primary"]
        assert focused_hexes == {expected}, (
            f"{theme_name} focused pane border resolved {focused_hexes}, expected {expected}"
        )

    asyncio.run(body())


@pytest.mark.parametrize("theme_name", ["ea-dark", "ea-cb", "ea-light"])
def test_unfocused_pane_border_renders_accent(theme_name: str) -> None:
    """A sibling ``.pane`` without ``-focused`` keeps the dim ``$accent``."""

    async def body() -> None:
        _focused, idle_hexes = await _capture_ring(theme_name)
        expected = _THEMES[theme_name].variables["accent"]
        assert idle_hexes == {expected}, (
            f"{theme_name} unfocused pane border resolved {idle_hexes}, expected {expected}"
        )

    asyncio.run(body())


@pytest.mark.parametrize("theme_name", ["ea-dark", "ea-cb", "ea-light"])
def test_focused_and_unfocused_borders_render_different_colours(theme_name: str) -> None:
    """The ring is the difference: the two captured borders must not match.

    This is the assertion that reds on the shipped defect -- with
    ``primary == accent`` both captures returned the same single hex.
    """

    async def body() -> None:
        focused_hexes, idle_hexes = await _capture_ring(theme_name)
        assert focused_hexes != idle_hexes, (
            f"{theme_name} renders no focus ring: both panes drew {focused_hexes}"
        )

    asyncio.run(body())


@pytest.mark.parametrize("theme", EA_THEMES, ids=lambda theme: theme.name)
def test_every_registered_theme_holds_primary_apart_from_accent(theme: Theme) -> None:
    """``primary`` differs from ``accent`` in BOTH the var map and the ctor.

    Textual resolves ``$primary`` from the ``variables`` map but derives its
    own built-ins (block cursor, ``$text-primary``) from the ctor argument,
    so a palette that split only one of the two would leave the other half
    of the surface on the collapsed pair.
    """
    variables = theme.variables
    assert variables["primary"] != variables["accent"]
    assert theme.primary != theme.accent
    assert variables["primary"] == theme.primary
    assert variables["accent"] == theme.accent


@pytest.mark.parametrize("theme", EA_THEMES, ids=lambda theme: theme.name)
def test_ring_separation_is_perceptible_not_merely_unequal(theme: Theme) -> None:
    """``primary`` and ``accent`` differ by at least :data:`_MIN_RING_SEPARATION`."""
    variables = theme.variables
    ratio = contrast_ratio(variables["primary"], variables["accent"])
    assert ratio >= _MIN_RING_SEPARATION, (
        f"{theme.name} ring separation {ratio:.2f}:1 is below "
        f"{_MIN_RING_SEPARATION}:1 -- the focused border reads as the unfocused one"
    )


@pytest.mark.parametrize("theme", EA_THEMES, ids=lambda theme: theme.name)
def test_every_palette_var_is_a_parseable_lowercase_hex(theme: Theme) -> None:
    """Every var in the map is a parseable, canonically-formatted ``#rrggbb``.

    The captures compare against the raw var strings, so an uppercase or
    shorthand hex would silently fail an equality assert that the renderer
    itself would have accepted.
    """
    for name, value in theme.variables.items():
        assert len(value) == 7 and value.startswith("#"), f"{theme.name}.{name} = {value!r}"
        assert value == value.lower(), f"{theme.name}.{name} = {value!r}"
        Color.parse(value)
