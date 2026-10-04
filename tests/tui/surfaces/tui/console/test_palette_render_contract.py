"""The console paints its chrome from the packet palette, through one render contract.

Each test names the packet row it proves. AUTH-035: the render contract is owned by exactly
one file, so the console's stylesheet is the rendered token map, no other console module
names a colour, and the row painter can only ask for a surface the map declares. AUTH-058:
the design pack's stylesheet variables are the colour oracle, bound into the themes the
shipped console registers, so every coloured cell of a live frame resolves to its theme
token, the text the painter draws is the composed frame, and a colour-free terminal keeps
every fact.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
from rich.color import Color as RichColor
from rich.segment import Segment
from textual.color import Color
from textual.filter import Monochrome
from textual.theme import Theme

from eawf.surfaces.tui.chassis.theme import EA_DARK, EA_THEMES, LOGICAL_THEMES
from eawf.surfaces.tui.console.app import (
    Body,
    ConsoleApp,
    KeybarRow,
    ProjectionHeader,
)
from eawf.surfaces.tui.console.cells import Mark
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.paint import MARK_SURFACE, Part, Stroke, paint
from eawf.surfaces.tui.console.rows import RowsWidget
from eawf.surfaces.tui.console.session import SIZES, SessionSetup
from eawf.surfaces.tui.console.token_map import SURFACES, TOKEN_MAP, render_css
from eawf.surfaces.tui.console.tokens import QUALITY, TRUTH

TESTS_ROOT = Path(__file__).resolve().parents[4]
GOLDEN_ROOT = TESTS_ROOT / "fixtures" / "console" / "golden"
CONSOLE_SRC = Path(__file__).resolve().parents[5] / "src" / "eawf" / "surfaces" / "tui" / "console"
TOKEN_MAP_FILE = "token_map.py"

#: A theme variable, a hex colour or a Rich colour argument, as console source would spell one.
COLOUR_SPELLING = re.compile(r"\$[a-z][a-z0-9-]*|#[0-9a-fA-F]{6}\b|\b(?:bg)?color=")

THEMES: dict[str, Theme] = {
    logical: {theme.name: theme for theme in EA_THEMES}[name]
    for logical, name in LOGICAL_THEMES.items()
}
HEADER = " Eä ▸ eawf-core ▸ Activity" + " " * 20 + "!4 NEEDS YOU  ● LIVE"


def _golden_rows() -> list[tuple[str, Part]]:
    """Return every row of every tracked golden frame, with the band it sits in."""
    rows: list[tuple[str, Part]] = []
    for path in sorted((GOLDEN_ROOT / "sequences").glob("frames-*.json")):
        for state in json.loads(path.read_text(encoding="utf-8"))["states"]:
            frame = state["frame"].split("\n")
            rows.append((frame[0], Part.HEADER))
            rows.extend((row, Part.BODY) for row in frame[1:-1])
            rows.append((frame[-1], Part.KEYBAR))
    return rows


GOLDEN_ROWS = _golden_rows()


def _by_text(strokes: tuple[Stroke, ...]) -> dict[str, Stroke]:
    return {stroke.text.strip(): stroke for stroke in strokes if stroke.text.strip()}


# ---------------------------------------------------------------- AUTH-035 · one owner


def test_auth_035_the_console_stylesheet_is_the_rendered_token_map() -> None:
    assert ConsoleApp.CSS.strip() == render_css(TOKEN_MAP)


def test_auth_035_no_console_module_but_the_token_map_spells_a_colour() -> None:
    offenders = [
        f"{path.relative_to(CONSOLE_SRC)}:{n}"
        for path in sorted(CONSOLE_SRC.rglob("*.py"))
        if path.name != TOKEN_MAP_FILE
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if COLOUR_SPELLING.search(line)
    ]
    assert offenders == []


def test_auth_035_the_colour_scan_reds_on_a_spelled_colour() -> None:
    for line in ('CSS = "Screen { background: $background; }"', 'x = "#16b384"', "Style(color=c)"):
        assert COLOUR_SPELLING.search(line), line
    assert not COLOUR_SPELLING.search("price = f'${value}'")


def test_auth_035_the_row_widgets_style_components_from_the_map_alone() -> None:
    classes = {row.css_class for row in TOKEN_MAP}
    for widget in (RowsWidget, ProjectionHeader, Body, KeybarRow):
        assert widget._get_component_classes() == classes


def test_auth_035_every_painted_surface_is_a_mapped_row() -> None:
    painted: set[str] = set()
    for row, part in GOLDEN_ROWS:
        for stroke in paint(row, part):
            painted.update(name for name in (stroke.surface, stroke.ground) if name)
    assert painted <= set(SURFACES)
    # the painter reaches every chrome and severity surface the packet colours
    assert {"brand", "live", "rule", "rail", "caret", "cursor", "hint", "ok", "err", "warn"} <= (
        painted
    )
    assert {surface for surface in MARK_SURFACE.values() if surface} <= set(SURFACES)


def test_auth_035_a_stroke_refuses_a_surface_the_map_does_not_own() -> None:
    with pytest.raises(ValueError, match="not in the token map"):
        Stroke("x", surface="magenta")
    with pytest.raises(ValueError, match="not in the token map"):
        Stroke("x", ground="")
    assert Stroke("x", surface="ok", ground="cursor").surface == "ok"


def test_auth_035_the_surface_index_is_the_map() -> None:
    assert list(SURFACES) == [row.surface for row in TOKEN_MAP]
    with pytest.raises(TypeError):
        SURFACES["new"] = TOKEN_MAP[0]  # type: ignore[index]


# ---------------------------------------------------------------- painter grammar


def test_auth_058_painting_never_changes_a_row() -> None:
    for row, part in GOLDEN_ROWS:
        assert "".join(stroke.text for stroke in paint(row, part)) == row


@pytest.mark.parametrize("part", list(Part))
def test_auth_058_an_empty_row_paints_nothing_and_one_cell_paints_one_run(part: Part) -> None:
    assert paint("", part) == ()
    assert paint(" ", part) == (Stroke(" "),)
    assert [s.text for s in paint("x", part)] == ["x"]


def test_auth_058_the_header_paints_brand_crumb_count_and_chip() -> None:
    strokes = _by_text(paint(HEADER, Part.HEADER))
    assert strokes["Eä"] == Stroke("Eä", surface="brand", bold=True)
    assert strokes["▸"].surface == "rail"
    assert strokes["eawf-core"].surface == "hint"
    assert strokes["Activity"].surface is None
    assert strokes["!4 NEEDS YOU"] == Stroke("!4 NEEDS YOU", surface="warn", bold=True)
    assert strokes["● LIVE"] == Stroke("● LIVE", surface="live", bold=True)


@pytest.mark.parametrize(
    ("slot", "surface", "bold"),
    [
        ("▲ GAP DETECTED", "warn", True),
        ("◌ DISCONNECTED", "dim", False),
        ("◑ LIVE / PARTIAL", "dim", False),
    ],
)
def test_auth_058_a_chip_that_is_not_live_is_never_painted_live(
    slot: str, surface: str, bold: bool
) -> None:
    """A warn chip takes the packet's warn weight; a dim chip stays at the body weight."""
    strokes = _by_text(paint(f" Eä ▸ Home   {slot}", Part.HEADER))
    assert (strokes[slot].surface, strokes[slot].bold) == (surface, bold)


def test_auth_058_a_zero_count_is_never_painted() -> None:
    strokes = paint(" Eä   !0 NEEDS YOU", Part.HEADER)
    assert [s.surface for s in strokes if "!0" in s.text] == [None]


def test_auth_058_the_keybar_paints_keys_bold_and_labels_in_the_hint_tone() -> None:
    row = " ↑↓ row   PageUp PageDown page   y copy digest   Esc back   "
    strokes = [s for s in paint(row, Part.KEYBAR) if s.text.strip()]
    keys = [s.text.strip() for s in strokes if s.bold]
    labels = [s.text.strip() for s in strokes if s.surface == "hint"]
    assert keys == ["↑↓", "PageUp PageDown", "y", "Esc"]
    assert labels == ["row", "page", "copy digest", "back"]


def test_auth_058_the_body_paints_rules_heads_cursor_and_state_words() -> None:
    head = paint("   RUN        STATE     AS OF │ BUCKETS   ", Part.BODY)
    assert [s.text for s in head if s.bold] == ["RUN        STATE     AS OF", "BUCKETS"]
    assert _by_text(head)["│"].surface == "rail"
    cursor = paint(" ▸ RUN-538453eb  WAIT-PERM  needs operator 3 │ FAILED  ", Part.BODY)
    # the caret grounds its own pane; the rail beside it is another pane and stays plain
    rail = cursor.index(_by_text(cursor)["│"])
    assert {s.ground for s in cursor[:rail]} == {"cursor"}
    assert {s.ground for s in cursor[rail:]} == {None}
    found = _by_text(cursor)
    assert found["▸"] == Stroke("▸", surface="caret", ground="cursor", bold=True)
    assert found["WAIT-PERM"].surface == "warn"
    assert found["needs operator"].surface == "warn"
    assert found["FAILED"].surface == "err"
    plain = paint("   RUN-1  RUNNING  QUEUED  LOST", Part.BODY)
    assert {s.ground for s in plain} == {None}
    assert [(s.text, s.surface) for s in plain if s.surface] == [
        ("RUNNING", "ok"),
        ("QUEUED", "info"),
        ("LOST", "err"),
    ]
    assert {s.surface for s in paint("═══─── ┄┄", Part.BODY) if s.text.strip()} == {"rule"}


def test_auth_058_a_pane_label_is_bold_and_takes_its_severity() -> None:
    assert paint(" WINDOW    1-23 of 26", Part.BODY)[1] == Stroke("WINDOW", bold=True)
    assert paint(" FAILED    2 runs", Part.BODY)[1] == Stroke("FAILED", surface="err", bold=True)


@pytest.mark.parametrize("mark", list(Mark))
def test_auth_058_a_mark_is_one_run_in_its_own_surface(mark: Mark) -> None:
    if mark in (Mark.DERIVED, Mark.ESTIMATED):
        text = f"{QUALITY[mark.value].unicode}4.62"
    elif mark is Mark.ZERO:
        text = "0 measured"
    else:
        text = f"{TRUTH[mark.value].unicode} {mark.value}"
    strokes = paint(f" COST  {text} · rate card", Part.BODY)
    marked = [s for s in strokes if s.mark is not None]
    assert marked == [Stroke(text, surface=MARK_SURFACE[mark], mark=mark)]


# ---------------------------------------------------------------- AUTH-058 · live frame


def _fixture() -> Fixture:
    return load_fixture(GOLDEN_ROOT / "fixture")


def _hex(color: RichColor | None) -> str | None:
    if color is None:
        return None
    triplet = color.get_truecolor()
    return f"#{triplet.red:02x}{triplet.green:02x}{triplet.blue:02x}"


def _resolved(theme: Theme) -> dict[str, str]:
    return {
        k: v.lower() for k, v in {**theme.to_color_system().generate(), **theme.variables}.items()
    }


async def _capture(theme_name: str | None, route: str) -> dict[str, Any]:
    app = ConsoleApp(_fixture(), FakeClock())
    w, h = SIZES[1]
    async with app.run_test(size=(w, h)) as pilot:
        if theme_name is not None:
            app.theme = theme_name
        app.reset(SessionSetup(route=route, size=1))
        app.render_frame()
        await pilot.pause()
        lines: list[list[Segment]] = []
        for widget_id, cls in (
            ("#header", ProjectionHeader),
            ("#body", Body),
            ("#keybar", KeybarRow),
        ):
            widget = app.query_one(widget_id, cls)
            lines.extend(list(widget.render_line(y)) for y in range(len(widget.rows)))
        return {
            "theme": app.theme,
            "frame": list(app.frame_rows),
            "lines": lines,
            "filters": list(app._filters),
        }


def _colour_of(lines: list[list[Segment]], text: str) -> tuple[str | None, str | None, bool]:
    for line in lines:
        for seg in line:
            if seg.text.strip() == text and seg.style is not None:
                return _hex(seg.style.color), _hex(seg.style.bgcolor), bool(seg.style.bold)
    raise AssertionError(f"no painted run reads {text!r}")


def test_auth_058_the_shipped_console_opens_on_the_oracle_dark_theme() -> None:
    captured = asyncio.run(_capture(None, "activity"))
    assert captured["theme"] == EA_DARK.name
    app = ConsoleApp(_fixture(), FakeClock())
    assert {theme.name for theme in EA_THEMES} <= set(app.available_themes)


@pytest.mark.parametrize("logical", sorted(THEMES))
def test_auth_058_a_live_frame_paints_each_surface_its_theme_token(logical: str) -> None:
    theme = THEMES[logical]
    tokens = _resolved(theme)
    lines = asyncio.run(_capture(theme.name, "activity"))["lines"]
    expect = {
        "Eä": ("accent", True),
        "● LIVE": ("accent", True),
        "!4 NEEDS YOU": ("warn", True),
        "FAILED": ("err", True),
        "RUNNING": ("ok", True),
        "QUEUED": ("status-claimed", True),
        "WAIT-USER": ("warn", True),
        "drill": ("muted", False),
    }
    for text, (token, bold) in expect.items():
        fg, _bg, is_bold = _colour_of(lines, text)
        assert fg == tokens[token], text
        assert is_bold is bold, text
    assert _colour_of(lines, "Enter")[0] == tokens["foreground"]
    assert _colour_of(lines, "Enter")[2]
    assert _colour_of(lines[:1], "▸")[0] == tokens["dim"]
    caret_fg, caret_bg, _ = _colour_of(lines[1:-1], "▸")
    assert (caret_fg, caret_bg) == (tokens["accent"], tokens["cursor-ground"])
    body_bg = {_hex(seg.style.bgcolor) for line in lines[1:-1] for seg in line if seg.style}
    assert tokens["cursor-ground"] in body_bg and tokens["surface"] in body_bg
    rule = next(seg for line in lines for seg in line if seg.text.startswith("═"))
    assert rule.style is not None and _hex(rule.style.color) == tokens["muted"]


def test_auth_058_the_painted_text_is_the_composed_frame_and_is_colourful() -> None:
    captured = asyncio.run(_capture(None, "activity"))
    assert ["".join(seg.text for seg in line) for line in captured["lines"]] == captured["frame"]
    colours = {_hex(seg.style.color) for line in captured["lines"] for seg in line if seg.style}
    palette = set(_resolved(EA_DARK).values())
    assert len(colours) >= 7
    assert colours <= palette


def test_auth_058_no_color_keeps_the_console_monochrome(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    captured = asyncio.run(_capture(None, "activity"))
    (mono,) = [f for f in captured["filters"] if isinstance(f, Monochrome)]
    for line in captured["lines"]:
        filtered = mono.apply(line, Color(0, 0, 0))
        assert "".join(seg.text for seg in filtered) == "".join(seg.text for seg in line)
        for seg in filtered:
            if seg.style is not None and seg.style.color is not None:
                r, g, b = seg.style.color.get_truecolor()
                assert r == g == b, seg.text
