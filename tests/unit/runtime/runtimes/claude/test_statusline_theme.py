"""The statusline theme: its colours, how it is picked, and how it keys the cache.

``ansi`` is the default and paints with the terminal's own 16 colours; the four
Catppuccin flavours reuse its roles with the upstream hex. The theme resolves
from ``--theme``, then ``NO_COLOR`` (``none``), then ``EAWF_STATUSLINE_THEME``,
then the ``statusline.theme`` config leaf, else ``ansi``; ``dark`` and ``light``
name Mocha and Latte. The flag and environment part keys the cached rows, so a
switch through either is never served from a line drawn another way.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal, get_args

import orjson
import pytest

from eawf.kernel.config import layered
from eawf.kernel.config.schema import StatuslineConfig, StatuslineGlyphs, StatuslineTheme
from eawf.runtime.runtimes.claude import statusline as orchestrator
from eawf.runtime.runtimes.claude.statusline_theme import (
    GLYPH_SETS,
    THEMES,
    render_key,
    statusline_config,
    theme_override,
)
from eawf.runtime.statusline_cache import StatuslineCache
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
_NERD: Final = ""


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``$HOME`` and the cache at this test's tmp dir; clear every theme source."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("EAWF_STATUSLINE_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("TERM", "xterm-256color")
    for variable in (
        "EAWF_STATUSLINE_THEME",
        "EAWF_STATUSLINE_GLYPHS",
        "NO_COLOR",
        "COLORFGBG",
        "EA_STATE",
        "COLUMNS",
        "LC_ALL",
        "LC_CTYPE",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(layered, "global_config_path", lambda: _global_path(home))
    return home


def _global_path(home: Path) -> Path:
    return home / ".config" / "eawf" / "config.yaml"


def _global_config(home: Path, body: str) -> None:
    path = _global_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _segment(module: str, status: Any = "ok") -> StatuslineSegment:
    value = "hooks!1" if module == "hooks_plugins" else module
    return sourced_segment(module, module, value, _SOURCE, status=status)


def _codes(rendered: str) -> list[tuple[str, str]]:
    """Return ``(params, text)`` for each escape of *rendered* and the text it leads."""
    matches = list(_SGR.finditer(rendered))
    return [
        (match.group(1), rendered[match.end() : nxt.start() if nxt else len(rendered)])
        for match, nxt in zip(matches, [*matches[1:], None], strict=True)
    ]


def _segment_sgr(segment: StatuslineSegment, theme: Theme) -> str:
    [line] = render_powerline([segment], [], LineStyle(theme=theme), width=None)
    return _codes(line)[0][0]


# ---- the ansi theme and the precedence ----------------------------------------


def test_ansi_theme_and_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated: Path
) -> None:
    ansi = load_theme("ansi")
    fills = {
        "model": "45",
        "project": "44",
        "active_time": "46",
        "cost": "42",
        "mcp_health": "47",
        "scope": "105",
        "budget": "106",
    }
    for module, fill in fills.items():
        assert _segment_sgr(_segment(module), ansi) == f"30;{fill}", module
    assert _segment_sgr(_segment("mcp_health", "warn"), ansi) == "30;43"
    assert _segment_sgr(_segment("hooks_plugins", "degraded"), ansi) == "30;41"
    assert _segment_sgr(_segment("model"), load_theme("ansi", ink="97")) == "97;45"
    # The ink knob is the ansi theme's alone.
    assert load_theme("catppuccin-mocha", ink="97").ink == "#11111b"

    # Gaps and meter-row text stay on 49: the last separator, label, percent, pace, detail.
    style = LineStyle(theme=ansi)
    [line] = render_powerline([_segment("model")], [], style, width=None)
    assert _codes(line)[-2:] == [("35;49", _NERD), ("0", "")]
    bar = PowerlineBar(label="5h", ratio=0.3, detail="2h05m", pace=4)
    [row] = render_powerline([], [bar], style, width=None)
    texts = {text.strip(): params for params, text in _codes(row) if text.strip()}
    assert texts["5h"] == "1;39;49"
    assert texts["30%"] == "1;39;49"
    assert texts["+4%"] == "33;49"
    assert texts["2h05m"] == "2;39;49"
    # Pace text: under 32, on pace dim 39, over 33, far over 31.
    for pace, drawn in (
        (-4, ("32;49", "  -4%")),
        (3, ("2;39;49", "    ~")),
        (11, ("31;49", " +11%")),
    ):
        [paced] = render_powerline(
            [], [PowerlineBar(label="7d", ratio=0.3, pace=pace)], style, width=None
        )
        assert drawn in _codes(paced), pace

    # Precedence: --theme, then NO_COLOR, then EAWF_STATUSLINE_THEME, then config, then ansi.
    cases: list[tuple[str | None, dict[str, str], str, ColourTheme | Literal["none"]]] = [
        (None, {}, "", "ansi"),
        (None, {}, "statusline:\n  theme: catppuccin-frappe\n", "catppuccin-frappe"),
        (
            None,
            {"EAWF_STATUSLINE_THEME": "catppuccin-macchiato"},
            "statusline:\n  theme: none\n",
            "catppuccin-macchiato",
        ),
        (None, {"NO_COLOR": "1", "EAWF_STATUSLINE_THEME": "ansi"}, "", "none"),
        ("catppuccin-latte", {"NO_COLOR": "1"}, "", "catppuccin-latte"),
        ("dark", {}, "", "catppuccin-mocha"),
        ("light", {}, "", "catppuccin-latte"),
        (None, {"EAWF_STATUSLINE_THEME": "dark"}, "", "catppuccin-mocha"),
        # a value naming no theme falls through to the next source
        ("powerline", {"EAWF_STATUSLINE_THEME": "light"}, "", "catppuccin-latte"),
        (None, {"EAWF_STATUSLINE_THEME": "sepia"}, "statusline:\n  theme: none\n", "none"),
        # an empty NO_COLOR is no NO_COLOR
        (None, {"NO_COLOR": ""}, "", "ansi"),
    ]
    for flag, env, config, expected in cases:
        for variable in ("NO_COLOR", "EAWF_STATUSLINE_THEME"):
            monkeypatch.delenv(variable, raising=False)
        for variable, value in env.items():
            monkeypatch.setenv(variable, value)
        _global_config(isolated, config)
        resolved = orchestrator._line_style(flag, None)
        expected_theme = None if expected == "none" else load_theme(expected)
        assert resolved.theme == expected_theme, (flag, env, config)
        assert (theme_override(flag, dict(env)) or statusline_config(None).theme) == expected

    # The light/dark guess is gone: Claude Code's theme setting and COLORFGBG change nothing.
    for variable in ("NO_COLOR", "EAWF_STATUSLINE_THEME"):
        monkeypatch.delenv(variable, raising=False)
    _global_config(isolated, "")
    (isolated / ".claude.json").write_text(json.dumps({"theme": "light"}), encoding="utf-8")
    monkeypatch.setenv("COLORFGBG", "0;15")
    assert orchestrator._line_style(None, None).theme == ansi


def test_the_config_section_sets_ink_glyphs_and_bar_width(isolated: Path) -> None:
    _global_config(isolated, "statusline:\n  ink: '97'\n  glyphs: unicode\n  bar_width: 30\n")
    style = orchestrator._line_style(None, None)
    assert style.theme is not None and style.theme.ink == "97"
    assert (style.glyphs, style.bar_width) == ("unicode", 30)


@pytest.mark.parametrize(
    "body",
    [
        "statusline:\n  bar_width: 9\n",
        "statusline:\n  bar_width: 41\n",
        "statusline:\n  theme: dark\n",
        "statusline:\n  ink: 31\n",
        "statusline:\n  colour: red\n",
        "statusline: [\n",
    ],
)
def test_a_malformed_config_draws_with_the_defaults(isolated: Path, body: str) -> None:
    # error-path: a redraw never fails over a config file.
    _global_config(isolated, body)
    assert statusline_config(None) == StatuslineConfig()
    assert orchestrator._line_style(None, None) == LineStyle(theme=load_theme("ansi"))


def test_the_config_reads_the_repository_layer(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    (repo / ".ea" / "config.yaml").write_text("statusline:\n  bar_width: 10\n", encoding="utf-8")
    assert statusline_config(repo).bar_width == 10
    assert orchestrator._line_style(None, repo / ".ea" / "state.json").bar_width == 10
    assert statusline_config(None).bar_width == 20


def test_the_hit_path_names_match_the_config_literals() -> None:
    assert frozenset(get_args(StatuslineTheme)) == THEMES
    assert frozenset(get_args(StatuslineGlyphs)) == GLYPH_SETS


# ---- the Catppuccin flavours ----------------------------------------------------

#: Upstream palette.json hex for the roles each flavour paints, in Theme field order:
#: ink, mauve, blue, teal, green, sky, lavender, yellow, peach, red, surface2,
#: pink, then the teal, yellow, red and green bar fills and the pink marker.
_UPSTREAM: Final = {
    "catppuccin-latte": ("#eff1f5", "#8839ef", "#1e66f5", "#179299", "#40a02b", "#04a5e5",
                         "#7287fd", "#df8e1d", "#fe640b", "#d20f39", "#acb0be", "#ea76cb"),
    "catppuccin-frappe": ("#232634", "#ca9ee6", "#8caaee", "#81c8be", "#a6d189", "#99d1db",
                          "#babbf1", "#e5c890", "#ef9f76", "#e78284", "#626880", "#f4b8e4"),
    "catppuccin-macchiato": ("#181926", "#c6a0f6", "#8aadf4", "#8bd5ca", "#a6da95", "#91d7e3",
                             "#b7bdf8", "#eed49f", "#f5a97f", "#ed8796", "#5b6078", "#f5bde6"),
    "catppuccin-mocha": ("#11111b", "#cba6f7", "#89b4fa", "#94e2d5", "#a6e3a1", "#89dceb",
                         "#b4befe", "#f9e2af", "#fab387", "#f38ba8", "#585b70", "#f5c2e7"),
}  # fmt: skip


@pytest.mark.parametrize("name", sorted(_UPSTREAM))
def test_catppuccin_flavours(name: Any) -> None:
    ink, mauve, blue, teal, green, sky, lavender, yellow, peach, red, surface2, pink = _UPSTREAM[
        name
    ]
    theme = load_theme(name)
    assert theme.model_dump() == {
        "ink": ink,
        "model": mauve,
        "project": blue,
        "active_time": teal,
        "cost": green,
        "mcp": sky,
        "scope": lavender,
        "budget": yellow,
        "warn": peach,
        "alert": red,
        "track": surface2,
        "calm": teal,
        "caution": yellow,
        "danger": red,
        "under": green,
        "marker": pink,
    }
    # The fills are 24-bit; the gaps and the meter-row text stay on 49.
    style = LineStyle(theme=theme)
    [line, row] = render_powerline(
        [_segment("model"), _segment("cost")],
        [PowerlineBar(label="ctx", ratio=0.6, detail="1K/2K")],
        style,
        width=None,
    )
    codes = _codes(line)

    def rgb(colour: str) -> str:
        return ";".join(str(int(colour[i : i + 2], 16)) for i in (1, 3, 5))

    assert codes[0][0] == f"38;2;{rgb(ink)};48;2;{rgb(mauve)}"
    assert codes[-2][0].endswith(";49") and codes[-2][1] == _NERD
    assert {params for params, text in _codes(row) if text.strip() and "█" not in text} <= {
        "1;39;49",
        "2;39;49",
        "0",
    }


def test_the_bundled_theme_file_is_closed() -> None:
    ansi = load_theme("ansi")
    with pytest.raises(ValueError, match="pattern"):
        Theme.model_validate({**ansi.model_dump(), "model": "48"})
    with pytest.raises(ValueError, match="pattern"):
        Theme.model_validate({**ansi.model_dump(), "ink": "45"})
    with pytest.raises(ValueError, match="pattern"):
        Theme.model_validate({**ansi.model_dump(), "marker": "105"})
    with pytest.raises(ValueError, match="Extra inputs"):
        Theme.model_validate({**ansi.model_dump(), "glow": "95"})
    with pytest.raises(ValueError, match="pattern"):
        load_theme("ansi", ink="#FFF")


# ---- the render key ------------------------------------------------------------


def test_the_render_key_carries_flag_and_environment() -> None:
    assert render_key(None, {}) == "config/config"
    assert render_key("dark", {}) == "catppuccin-mocha/config"
    assert render_key(None, {"NO_COLOR": "1"}) == "none/config"
    assert render_key(None, {"EAWF_STATUSLINE_GLYPHS": "ascii"}) == "config/ascii"
    assert render_key(None, {"TERM": "linux"}) == "config/ascii"
    assert render_key("bogus", {"EAWF_STATUSLINE_GLYPHS": "bogus"}) == "config/config"


def test_a_switched_theme_is_never_served_from_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = json.dumps({"session_id": "ses-theme", "model": "haiku"})
    monkeypatch.setenv("EAWF_STATUSLINE_THEME", "dark")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw.encode())))
    dark = orchestrator.run_with_cache(workspace=None, theme_name=None)
    assert dark == orchestrator.render_pipeline({"model": "haiku"}, workspace=None)
    path = orchestrator.cache_path_for("ses-theme")
    assert StatuslineCache(path, key=render_key(None, os.environ)).get() == dark

    monkeypatch.setenv("EAWF_STATUSLINE_THEME", "light")
    assert StatuslineCache(path, key=render_key(None, os.environ)).get() is None
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw.encode())))
    light = orchestrator.run_with_cache(workspace=None, theme_name=None)
    assert light != dark

    monkeypatch.setenv("EAWF_STATUSLINE_THEME", "dark")
    assert StatuslineCache(path, key=render_key(None, os.environ)).get() is None
    assert StatuslineCache(path, key=render_key("light", os.environ)).get() == light


def test_the_cache_key_separates_entries(tmp_path: Path) -> None:
    path = tmp_path / "entry.json"
    StatuslineCache(path, key="dark").write("dark rows", stamp=1.0)
    assert StatuslineCache(path, key="dark").get() == "dark rows"
    assert StatuslineCache(path, key="light").get() is None
    entry = StatuslineCache(path, key="light").read_entry()
    assert entry is not None and entry.key == "dark"


def test_an_entry_without_a_key_misses_a_keyed_cache(tmp_path: Path) -> None:
    # boundary: an entry written before the key existed is a miss, never a hit.
    path = tmp_path / "entry.json"
    path.write_bytes(orjson.dumps({"value": "old", "written_at": 4_000_000_000.0}))
    assert StatuslineCache(path, key="dark").get() is None
    assert StatuslineCache(path).get() == "old"


def test_an_entry_with_a_malformed_key_is_a_miss(tmp_path: Path) -> None:
    # error-path: a non-string key is a corrupt entry.
    path = tmp_path / "entry.json"
    path.write_bytes(orjson.dumps({"value": "x", "written_at": 1.0, "key": 3}))
    assert StatuslineCache(path, key="dark").read_entry() is None


# ---- ascii glyphs emit ASCII alone --------------------------------------------


def _full_payload() -> dict[str, Any]:
    """Return a payload filling every row: a non-ASCII model and project, the
    active share with its middle dot, the context and both limit windows."""
    now = datetime.now(UTC)
    return {
        "model": {"display_name": "Opus Eä 漢字"},
        "workspace": {"project_dir": "/work/café"},
        "cost": {
            "total_cost_usd": 0.52,
            "total_api_duration_ms": 1_482_000,
            "total_duration_ms": 3_900_000,
        },
        "context_window": {
            "context_window_size": 1_000_000,
            "current_usage": {
                "input_tokens": 1_000,
                "cache_creation_input_tokens": 4_440,
                "cache_read_input_tokens": 62_560,
            },
        },
        "rate_limits": {
            "five_hour": {
                "used_percentage": 31,
                "resets_at": (now + timedelta(hours=2)).isoformat(),
            },
            "seven_day": {
                "used_percentage": 62,
                "resets_at": (now + timedelta(days=2)).isoformat(),
            },
        },
    }


@pytest.mark.parametrize(
    ("variable", "value", "columns"),
    [
        ("LC_ALL", "C", None),
        ("EAWF_STATUSLINE_GLYPHS", "ascii", None),
        ("TERM", "linux", "40"),
    ],
)
def test_statusline_repro_ascii_render_emits_non_ascii(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str, columns: str | None
) -> None:
    monkeypatch.setenv(variable, value)
    if columns is not None:
        monkeypatch.setenv("COLUMNS", columns)
    for theme in ("ansi", "none"):
        rendered = orchestrator.render_pipeline(_full_payload(), workspace=None, theme_name=theme)
        assert len(rendered.splitlines()) == 4, rendered
        # At 40 columns the active segment is the first one dropped.
        assert (" 38% active" in rendered) is (columns is None), rendered
        assert [char for char in rendered if not char.isascii()] == [], rendered


def test_ascii_spells_the_active_share_and_folds_accents() -> None:
    style = LineStyle(theme=None, glyphs="ascii")
    segments = [
        sourced_segment("model", "model", "Eä 漢", _SOURCE),
        sourced_segment("active_time", "active", "1h05m · 38% active", _SOURCE),
    ]
    assert render_powerline(segments, [], style, width=None) == ["Ea ? | 1h05m 38% active"]


def test_unicode_glyphs_keep_the_middle_dot() -> None:
    # boundary: only the ascii set rewrites text.
    style = LineStyle(theme=None, glyphs="unicode")
    segment = sourced_segment("active_time", "active", "1h05m · 38% active", _SOURCE)
    assert render_powerline([segment], [], style, width=None) == ["1h05m · 38% active"]
