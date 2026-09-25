"""Styled-capture golden tier: the cell colours of every console surface, per theme.

The frame and journey goldens beside this tier compare text only, so a palette drift passes
them untouched. This tier mounts one probe widget per row of the console token-to-surface
map under each registered theme, reads the foreground and background the compositor
resolved for the probe's first row, and compares them to a per-theme JSON golden under
``tests/fixtures/console/golden/styled``. The capture is also held to the palette itself:
each surface resolves its own theme token, the frame border is neutral, the dark hint is
the packet muted and the focus ring differs from the accent, so regenerating a golden
cannot quietly absorb a drift.

Regenerate the goldens after an intentional palette change with::

    EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/tui/console/test_styled_goldens.py -q
"""

from __future__ import annotations

import asyncio
import json
import os
from functools import cache
from typing import Any

import pytest
from rich.color import Color
from textual.app import App, ComposeResult
from textual.filter import Monochrome
from textual.geometry import Region
from textual.theme import Theme
from textual.widgets import Static

from eawf.surfaces.tui.chassis.pilot_harness import SNAPSHOT_REGEN_ENV
from eawf.surfaces.tui.chassis.theme import EA_THEMES, LOGICAL_THEMES
from eawf.surfaces.tui.console.token_map import TOKEN_MAP, Channel, render_css

from .goldens import GOLDEN_ROOT

STYLED_ROOT = GOLDEN_ROOT / "styled"
ORACLE: dict[str, Any] = json.loads(
    (GOLDEN_ROOT.parent / "colour-oracle.json").read_text(encoding="utf-8")
)
PROBE_SIZE = (40, 30)

#: Logical theme name -> registered theme, one golden file per entry.
THEMES: dict[str, Theme] = {
    logical: {theme.name: theme for theme in EA_THEMES}[name]
    for logical, name in LOGICAL_THEMES.items()
}

#: Widest channel spread a border may have and still read as grey rather than as a hue.
MAX_NEUTRAL_CHROMA = 0x18

#: One captured surface: its map row plus the colours its cells resolved.
Cell = dict[str, str | None]


class SurfaceProbe(App[None]):
    """One single-row probe per surface, styled only by the rendered token map."""

    CSS = "\n".join(
        (
            "Screen { background: $surface; color: $foreground; }",
            "Static { height: 1; }",
            ".border-probe { height: 3; }",
            render_css(TOKEN_MAP),
        )
    )

    def __init__(self, theme_name: str) -> None:
        super().__init__()
        # The capture must keep true colours even when NO_COLOR is set.
        self._filters = [item for item in self._filters if not isinstance(item, Monochrome)]
        for theme in EA_THEMES:
            self.register_theme(theme)
        self.theme = theme_name

    def compose(self) -> ComposeResult:
        for row in TOKEN_MAP:
            classes = row.css_class
            if row.channel is Channel.BORDER:
                classes = f"{classes} border-probe"
            yield Static(row.surface, id=f"probe-{row.surface}", classes=classes)


def _hex(color: Color | None) -> str | None:
    if color is None:
        return None
    triplet = color.get_truecolor()
    return f"#{triplet.red:02x}{triplet.green:02x}{triplet.blue:02x}"


def _only(values: set[str | None], what: str) -> str | None:
    assert len(values) == 1, f"{what} is not uniform: {values}"
    return next(iter(values))


def _first_row_colours(probe: Static) -> tuple[str | None, str | None]:
    """Return the one foreground of the row's glyphs and the one background of all its cells.

    Row 0 is the text line for a plain probe and the top border edge for a bordered one, so
    the glyph foreground is the channel colour either way.
    """
    (strip,) = probe.render_lines(Region(0, 0, probe.size.width, 1))
    foregrounds: set[str | None] = set()
    backgrounds: set[str | None] = set()
    for segment in strip._segments:
        style = segment.style
        backgrounds.add(_hex(style.bgcolor if style else None))
        if segment.text.strip():
            foregrounds.add(_hex(style.color if style else None))
    fg = _only(foregrounds, f"{probe.id} foreground")
    bg = _only(backgrounds, f"{probe.id} background")
    return fg, bg


async def _capture(theme_name: str) -> dict[str, Cell]:
    app = SurfaceProbe(theme_name)
    async with app.run_test(size=PROBE_SIZE) as pilot:
        await pilot.pause()
        cells: dict[str, Cell] = {}
        for row in TOKEN_MAP:
            fg, bg = _first_row_colours(app.query_one(f"#probe-{row.surface}", Static))
            cells[row.surface] = {
                "channel": row.channel.value,
                "token": row.token,
                "fg": fg,
                "bg": bg,
            }
        return cells


@cache
def captured(logical: str) -> dict[str, Cell]:
    """Capture every surface under one logical theme, once per test process."""
    return asyncio.run(_capture(THEMES[logical].name))


def _golden_record(logical: str) -> dict[str, Any]:
    return {
        "theme": THEMES[logical].name,
        "size": list(PROBE_SIZE),
        "surfaces": captured(logical),
    }


def _channel_colour(logical: str, surface: str) -> str | None:
    cell = captured(logical)[surface]
    return cell["bg"] if cell["channel"] == Channel.BACKGROUND.value else cell["fg"]


def _chroma(hex_colour: str) -> int:
    channels = [int(hex_colour[index : index + 2], 16) for index in (1, 3, 5)]
    return max(channels) - min(channels)


@pytest.mark.parametrize("logical", sorted(THEMES))
def test_styled_golden_matches_the_captured_cell_colours(logical: str) -> None:
    golden = STYLED_ROOT / f"{logical}.json"
    record = _golden_record(logical)
    if os.environ.get(SNAPSHOT_REGEN_ENV) == "1":
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        return
    assert record == json.loads(golden.read_text(encoding="utf-8"))


@pytest.mark.parametrize("logical", sorted(THEMES))
def test_styled_capture_paints_each_surface_with_its_theme_token(logical: str) -> None:
    theme = THEMES[logical]
    resolved = {**theme.to_color_system().generate(), **theme.variables}
    for row in TOKEN_MAP:
        assert _channel_colour(logical, row.surface) == resolved[row.token].lower(), row.surface


@pytest.mark.parametrize("logical", sorted(THEMES))
def test_styled_capture_frame_border_is_neutral(logical: str) -> None:
    frame = _channel_colour(logical, "frame")
    assert frame == THEMES[logical].variables["border"]
    assert frame is not None and _chroma(frame) <= MAX_NEUTRAL_CHROMA
    assert frame not in {_channel_colour(logical, "brand"), _channel_colour(logical, "focus")}


def test_styled_capture_neutral_check_rejects_the_derived_border() -> None:
    """Textual derives ``$border`` from ``primary``; that green must fail the neutral bound."""
    assert _chroma(THEMES["dark"].variables["primary"]) > MAX_NEUTRAL_CHROMA


def test_styled_capture_dark_hint_is_the_packet_muted() -> None:
    assert _channel_colour("dark", "hint") == "#828a94"
    assert ORACLE["oracle"]["dark"]["vars"]["muted"] == "#828a94"


@pytest.mark.parametrize("logical", sorted(THEMES))
def test_styled_capture_focus_ring_differs_from_the_accent(logical: str) -> None:
    variables = THEMES[logical].variables
    assert _channel_colour(logical, "focus") == variables["primary"]
    assert _channel_colour(logical, "brand") == variables["accent"]
    assert _channel_colour(logical, "focus") != _channel_colour(logical, "brand")


@pytest.mark.parametrize("logical", ["dark", "light"])
def test_styled_capture_matches_the_packet_oracle(logical: str) -> None:
    packet = dict(ORACLE["oracle"][logical]["vars"])
    for entry in ORACLE["divergences"]:
        if entry["theme"] == logical:
            packet[entry["oracle_var"]] = entry["shipped"]
    bindings = {**ORACLE["bindings"]["chrome"], **ORACLE["bindings"]["semantic"]}
    bound = [row for row in TOKEN_MAP if row.token in bindings]
    assert bound
    for row in bound:
        assert _channel_colour(logical, row.surface) == packet[bindings[row.token]], row.surface


def test_styled_capture_cb_chrome_matches_product_authored_rows() -> None:
    rows = ORACLE["product_authored"]["cb"]["chrome"]
    chrome = [row for row in TOKEN_MAP if row.token in rows]
    assert chrome
    for row in chrome:
        assert _channel_colour("cb", row.surface) == rows[row.token], row.surface
