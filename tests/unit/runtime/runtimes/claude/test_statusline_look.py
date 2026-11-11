"""How the status line reads on a terminal: row spacing, segment ink and the pace marker.

One blank row separates the powerline row from the meter rows; in colour it
holds a lone reset, so a host that drops empty rows keeps it. Under the
``auto`` ink the ``ansi`` theme writes white on its dark fills and black on its
light ones, and the pace marker always contrasts with the cell it sits on.
"""

from __future__ import annotations

import re
from typing import Final, Literal, get_args

import pytest
from pydantic import ValidationError

from eawf.kernel.config.defaults import BUILT_IN_DEFAULTS
from eawf.kernel.config.registry.leaf_catalog import LEAF_KEY_REGISTRY
from eawf.kernel.config.schema import StatuslineConfig, StatuslineInk
from eawf.surfaces.render.statusline import (
    ColourTheme,
    LineStyle,
    PowerlineBar,
    SegmentSource,
    StatuslineSegment,
    Theme,
    load_theme,
    render_powerline,
    sourced_segment,
)

_SOURCE: Final = SegmentSource(producer="test-producer", provenance="test#field")
_SGR: Final = re.compile(r"\x1b\[([0-9;]*)m")
_MARKER: Final = "│"

#: The fill of each line-1 module in the ``ansi`` theme, and the warn and alert fills.
_ANSI_FILLS: Final[dict[tuple[str, Literal["ok", "warn", "degraded"]], str]] = {
    ("model", "ok"): "45",
    ("project", "ok"): "44",
    ("active_time", "ok"): "46",
    ("cost", "ok"): "42",
    ("mcp_health", "ok"): "47",
    ("scope", "ok"): "105",
    ("budget", "ok"): "106",
    ("mcp_health", "warn"): "43",
    ("hooks_plugins", "degraded"): "41",
}
_WHITE_ON: Final = frozenset({"41", "44", "45"})
_BLACK_ON: Final = frozenset({"42", "43", "46", "47", "105", "106"})

#: A bar per fill role: its pace selects the role whatever the used share.
_ROLE_PACE: Final = {"calm": 0, "caution": 5, "danger": 20, "under": -10}


def _segment(module: str, status: Literal["ok", "warn", "degraded"] = "ok") -> StatuslineSegment:
    value = "hooks!1" if module == "hooks_plugins" else module
    return sourced_segment(module, module, value, _SOURCE, status=status)


def _codes(rendered: str) -> list[tuple[str, str]]:
    """Return ``(params, text)`` for each escape of *rendered* and the text it leads."""
    matches = list(_SGR.finditer(rendered))
    return [
        (match.group(1), rendered[match.end() : nxt.start() if nxt else len(rendered)])
        for match, nxt in zip(matches, [*matches[1:], None], strict=True)
    ]


def _visible(row: str) -> str:
    return _SGR.sub("", row)


def _bars(count: int) -> list[PowerlineBar]:
    return [
        PowerlineBar(label=label, ratio=0.3, detail="2h05m", pace=4, expected=20.0)
        for label in ("ctx", "5h", "7d")[:count]
    ]


# ---- the blank row under the powerline row -------------------------------------


@pytest.mark.parametrize("theme", [None, "ansi", "catppuccin-mocha"])
@pytest.mark.parametrize("width", [None, 120, 40, 12])
@pytest.mark.parametrize("count", [0, 1, 3])
def test_spacer_row(count: int, width: int | None, theme: ColourTheme | None) -> None:
    style = LineStyle(theme=None if theme is None else load_theme(theme))
    rows = render_powerline([_segment("model"), _segment("cost")], _bars(count), style, width=width)
    meter_rows = [row for row in rows if _visible(row).lstrip().startswith(("ctx", "5h", "7d"))]
    blank = [index for index, row in enumerate(rows) if not _visible(row).strip()]
    if meter_rows:
        assert blank == [1]
        # A host drops an empty row; a lone reset keeps it and draws nothing.
        assert rows[1] == ("" if theme is None else "\x1b[0m")
        assert rows.index(meter_rows[0]) == 2
        assert len(rows) == 2 + len(meter_rows)
    else:
        assert blank == []
        assert len(rows) == 1
    if width is not None:
        assert all(len(_visible(row)) <= width for row in rows)


def test_spacer_row_needs_a_powerline_row() -> None:
    rows = render_powerline([], _bars(1), LineStyle(theme=load_theme("ansi")), width=None)
    assert len(rows) == 1
    assert _visible(rows[0]).startswith("ctx")


def test_spacer_row_absent_when_the_meter_rows_do_not_fit() -> None:
    rows = render_powerline([_segment("model")], _bars(3), LineStyle(theme=None), width=8)
    assert len(rows) == 1


# ---- segment ink per fill -------------------------------------------------------


def _ink_by_fill(theme: Theme) -> dict[str, str]:
    ink: dict[str, str] = {}
    for (module, status), fill in _ANSI_FILLS.items():
        [line] = render_powerline(
            [_segment(module, status)], [], LineStyle(theme=theme), width=None
        )
        params = _codes(line)[0][0]
        foreground, background = params.split(";")
        assert background == fill, module
        ink[fill] = foreground
    return ink


@pytest.mark.parametrize("setting", ["auto", "30", "97"])
def test_ink_per_fill(setting: Literal["auto", "30", "97"]) -> None:
    ink = _ink_by_fill(load_theme("ansi", ink=setting))
    assert set(ink) == _WHITE_ON | _BLACK_ON
    for fill, code in ink.items():
        if setting == "auto":
            assert code == ("97" if fill in _WHITE_ON else "30"), fill
        else:
            assert code == setting, fill


def test_ink_defaults_to_auto() -> None:
    assert get_args(StatuslineInk) == ("auto", "30", "97")
    assert StatuslineConfig().ink == "auto"
    assert BUILT_IN_DEFAULTS["statusline"]["ink"] == "auto"
    leaf = LEAF_KEY_REGISTRY["statusline.ink"]
    assert leaf.default == "auto"
    assert leaf.choices == ("auto", "30", "97")
    assert load_theme("ansi").ink == "auto"
    # Without the knob the ansi theme draws its own auto ink.
    assert _ink_by_fill(load_theme("ansi")) == _ink_by_fill(load_theme("ansi", ink="auto"))


def test_ink_knob_leaves_catppuccin_ink() -> None:
    assert load_theme("catppuccin-mocha", ink="auto").ink == "#11111b"
    assert load_theme("catppuccin-latte", ink="97").ink == "#eff1f5"


@pytest.mark.parametrize("value", ["31", "Auto", "", 97, None])
def test_ink_refuses_other_values(value: object) -> None:
    with pytest.raises(ValidationError):
        StatuslineConfig.model_validate({"ink": value})


@pytest.mark.parametrize("value", ["AUTO", "auto ", "45", "#FFF"])
def test_theme_ink_refuses_a_non_foreground(value: str) -> None:
    with pytest.raises(ValidationError):
        Theme.model_validate({**load_theme("ansi").model_dump(), "ink": value})


def test_theme_has_no_marker_role() -> None:
    with pytest.raises(ValidationError):
        Theme.model_validate({**load_theme("ansi").model_dump(), "marker": "95"})


# ---- the pace marker against its cell ------------------------------------------


def _marker_params(bar: PowerlineBar, theme: Theme) -> tuple[str, str]:
    """Return the marker's foreground and the background of its cell."""
    [row] = render_powerline([], [bar], LineStyle(theme=theme), width=None)
    [params] = [params for params, text in _codes(row) if text.startswith(_MARKER)]
    if params.startswith("38;2;"):
        parts = params.split(";")
        return ";".join(parts[2:5]), ";".join(parts[7:10])
    foreground, background = params.split(";")
    return foreground, background


def _palette_index(code: str) -> int:
    """Return the 16-colour palette slot an SGR foreground or background names."""
    value = int(code)
    return value % 10 + (8 if value >= 90 else 0)


def _cases() -> list[tuple[str, PowerlineBar]]:
    # Marker at cell 4 sits inside a bar 12 cells full; at cell 16 it sits on the track.
    on_fill = [
        (role, PowerlineBar(label="5h", ratio=0.6, pace=pace, expected=20.0))
        for role, pace in _ROLE_PACE.items()
    ]
    on_track = [
        (f"track-{role}", PowerlineBar(label="5h", ratio=0.6, pace=pace, expected=80.0))
        for role, pace in _ROLE_PACE.items()
    ]
    return on_fill + on_track


def test_marker_contrast() -> None:
    ansi = load_theme("ansi")
    expected = {
        "calm": ("30", "46"),
        "caution": ("30", "43"),
        "danger": ("97", "41"),
        "under": ("30", "42"),
    }
    for role, bar in _cases():
        want = ("97", "100") if role.startswith("track-") else expected[role]
        assert _marker_params(bar, ansi) == want, role
    for theme_name in get_args(ColourTheme):
        theme = load_theme(theme_name)
        for role, bar in _cases():
            foreground, background = _marker_params(bar, theme)
            if theme_name == "ansi":
                assert _palette_index(foreground) != _palette_index(background), role
            else:
                assert foreground != background, (theme_name, role)
                cell = theme.track if role.startswith("track-") else str(getattr(theme, role))
                assert background == ";".join(str(int(cell[i : i + 2], 16)) for i in (1, 3, 5)), (
                    theme_name,
                    role,
                )


def test_marker_absent_without_expected_share() -> None:
    bar = PowerlineBar(label="5h", ratio=0.6, pace=0)
    [row] = render_powerline([], [bar], LineStyle(theme=load_theme("ansi")), width=None)
    assert _MARKER not in row


@pytest.mark.parametrize(("expected", "on_track"), [(0.0, False), (99.9, True)])
def test_marker_at_the_bar_ends(expected: float, on_track: bool) -> None:
    bar = PowerlineBar(label="5h", ratio=0.5, pace=0, expected=expected)
    foreground, background = _marker_params(bar, load_theme("ansi"))
    assert (foreground, background) == (("97", "100") if on_track else ("30", "46"))
