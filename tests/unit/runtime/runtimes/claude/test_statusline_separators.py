"""Line 1's separators chain fill to fill, and the glyph set and NO_COLOR govern them.

Each separator glyph is drawn in the colour of the fill it closes on the fill it
opens, the last on the terminal's own background (SGR 49), and the next segment's
escape follows the glyph at once, so no gap cell is ever painted. The rows are
rendered from a recorded payload on stdin through ``run_with_cache``, the path
``eawf cc statusline`` prints, with ``$HOME``, the cache, the global config layer
and the working directory under the test's tmp dir, so no repository state is
read. The module imports nothing from eawf at load time and renders through a
function an older release also has, so the chaining test runs against it too.
"""

from __future__ import annotations

import io
import json
import re
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pytest

#: A recorded Claude Code status line payload: four line-1 segments and three rows.
PAYLOAD: Final[dict[str, Any]] = {
    "session_id": "ses-separators",
    "model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"},
    "workspace": {"project_dir": "/work/sample-project"},
    "cwd": "/work/sample-project",
    "cost": {
        "total_cost_usd": 1.2216,
        "total_api_duration_ms": 754_000,
        "total_duration_ms": 1_216_000,
    },
    "context_window": {
        "context_window_size": 1_000_000,
        "current_usage": {
            "input_tokens": 4_000,
            "cache_creation_input_tokens": 10_000,
            "cache_read_input_tokens": 50_000,
        },
    },
    "rate_limits": {
        "five_hour": {"used_percentage": 23},
        "seven_day": {"used_percentage": 41},
    },
}

_ESC: Final = "\x1b"
_SGR: Final = re.compile(r"\x1b\[([0-9;]*)m")
_NERD: Final = ""
_UNICODE: Final = "▌"
_SEPARATORS: Final = (_NERD, _UNICODE)

#: Variables of the developer's shell that would change what the line draws.
_SCRUBBED: Final = (
    "NO_COLOR",
    "EAWF_STATUSLINE_THEME",
    "EAWF_STATUSLINE_GLYPHS",
    "COLUMNS",
    "EA_STATE",
    "LC_ALL",
    "LC_CTYPE",
)


type Render = Callable[..., str]


@pytest.fixture
def render(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Render:
    """Return a renderer of the payload's rows under *env* over a clean environment."""
    from eawf.kernel.config import layered
    from eawf.runtime.runtimes.claude import statusline as orchestrator

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        layered, "global_config_path", lambda: home / ".config" / "eawf" / "config.yaml"
    )

    def _render(**env: str) -> str:
        for variable in _SCRUBBED:
            monkeypatch.delenv(variable, raising=False)
        for variable, value in {
            "HOME": str(home),
            "EAWF_STATUSLINE_CACHE": str(tmp_path / "cache"),
            "TERM": "xterm-256color",
            "LANG": "C.UTF-8",
            **env,
        }.items():
            monkeypatch.setenv(variable, value)
        # a fresh session per render, so no render is served another's cached line
        payload = {**PAYLOAD, "session_id": f"ses-{uuid.uuid4().hex}"}
        monkeypatch.setattr(
            sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))
        )
        return str(orchestrator.run_with_cache(workspace=None, theme_name=None))

    return _render


def _apply(params: str, state: dict[str, str]) -> None:
    """Fold one SGR parameter string into the *state* of foreground and background."""
    parts = params.split(";") if params else ["0"]
    index = 0
    while index < len(parts):
        code = parts[index]
        if code in {"38", "48"} and parts[index + 1 : index + 2] == ["2"]:
            state["fg" if code == "38" else "bg"] = ";".join(parts[index : index + 5])
            index += 5
            continue
        number = int(code)
        if number == 0:
            state.clear()
        elif 30 <= number <= 37 or 90 <= number <= 97 or number == 39:
            state["fg"] = code
        elif 40 <= number <= 47 or 100 <= number <= 107 or number == 49:
            state["bg"] = code
        index += 1


def _as_fg(background: str) -> str:
    """Return the foreground naming the colour of *background*."""
    if background.startswith("48;2;"):
        return f"38{background[2:]}"
    return str(int(background) - 10)


def _separators(line: str) -> list[tuple[dict[str, str], dict[str, str], str]]:
    """Return, per separator glyph of *line*: the SGR state before it, after it, what follows.

    The state before is the one the glyph is drawn in; the state after is the one
    the segment it opens is drawn in; what follows is the text between the glyph
    and the next escape, which must be empty.
    """
    found: list[tuple[dict[str, str], dict[str, str], str]] = []
    state: dict[str, str] = {}
    position = 0
    pending: tuple[dict[str, str], str] | None = None
    for match in [*_SGR.finditer(line), None]:
        end = match.start() if match is not None else len(line)
        text = line[position:end]
        if pending is not None:
            found.append((pending[0], dict(state), pending[1]))
            pending = None
        glyph_at = max(text.rfind(glyph) for glyph in _SEPARATORS)
        if glyph_at >= 0:
            pending = (dict(state), text[glyph_at + 1 :])
        if match is None:
            break
        _apply(match.group(1), state)
        position = match.end()
    if pending is not None:
        found.append((pending[0], dict(state), pending[1]))
    return found


def _fills(line: str) -> list[str]:
    """Return the background each segment of *line* is drawn on, left to right."""
    fills: list[str] = []
    state: dict[str, str] = {}
    position = 0
    for match in [*_SGR.finditer(line), None]:
        end = match.start() if match is not None else len(line)
        text = line[position:end]
        if text.startswith(" ") and text.strip() and "bg" in state:
            fills.append(state["bg"])
        if match is None:
            break
        _apply(match.group(1), state)
        position = match.end()
    return fills


def test_separator_chaining(render: Render) -> None:
    line = render().split("\n")[0]
    fills = _fills(line)
    separators = _separators(line)
    assert len(fills) >= 4
    assert len(separators) == len(fills)
    for index, (drawn_in, _after, following) in enumerate(separators):
        closing = fills[index]
        opening = fills[index + 1] if index + 1 < len(fills) else "49"
        assert drawn_in.get("fg") == _as_fg(closing), (index, drawn_in, closing)
        assert drawn_in.get("bg") == opening, (index, drawn_in, opening)
        assert following == "", (index, following)
    assert separators[-1][0]["bg"] == "49"


# ---- the parser itself --------------------------------------------------------


def test_the_parser_reads_a_chained_line() -> None:
    line = f"\x1b[30;45m a \x1b[35;44m{_NERD}\x1b[30;44m b \x1b[34;49m{_NERD}\x1b[0m"
    assert _fills(line) == ["45", "44"]
    assert [(before["fg"], before["bg"], after) for before, _, after in _separators(line)] == [
        ("35", "44", ""),
        ("34", "49", ""),
    ]


def test_the_parser_reads_truecolour_and_a_gap() -> None:
    # error-path: a glyph followed by a gap cell reports that cell.
    line = f"\x1b[48;2;1;2;3m a \x1b[48;2;9;9;9m\x1b[38;2;1;2;3m{_NERD} \x1b[0m"
    assert _fills(line) == ["48;2;1;2;3"]
    [(before, _after, following)] = _separators(line)
    assert (before["fg"], before["bg"], following) == ("38;2;1;2;3", "48;2;9;9;9", " ")


@pytest.mark.parametrize(
    ("background", "foreground"),
    [("40", "30"), ("47", "37"), ("100", "90"), ("107", "97"), ("48;2;0;0;0", "38;2;0;0;0")],
)
def test_as_fg_maps_each_background_family(background: str, foreground: str) -> None:
    assert _as_fg(background) == foreground


def test_as_fg_refuses_a_non_colour() -> None:
    with pytest.raises(ValueError, match="invalid literal"):
        _as_fg("bold")


# ---- glyph sets and NO_COLOR --------------------------------------------------


def _line_one(rows: str) -> str:
    return rows.split("\n")[0]


def test_glyphs_and_no_color(
    render: Render, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from eawf.runtime.runtimes.claude.statusline_theme import glyphs_override

    # The default set is nerd: U+E0B0 after every segment.
    nerd = _line_one(render())
    assert nerd.count(_NERD) >= 4 and _UNICODE not in nerd

    # The config picks unicode; the variable wins over the config.
    config = tmp_path / "home" / ".config" / "eawf" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("statusline:\n  glyphs: unicode\n", encoding="utf-8")
    unicode = _line_one(render())
    assert unicode.count(_UNICODE) == nerd.count(_NERD) and _NERD not in unicode
    assert _NERD in _line_one(render(EAWF_STATUSLINE_GLYPHS="nerd"))

    # ascii draws no glyph: the fills abut, and the text stays, spelt in ASCII.
    ascii_line = _line_one(render(EAWF_STATUSLINE_GLYPHS="ascii"))
    assert _NERD not in ascii_line and _UNICODE not in ascii_line
    assert _SGR.sub("", ascii_line) == _SGR.sub("", nerd).replace(_NERD, "").replace(" · ", " ")

    # TERM=linux and a non-UTF-8 locale force ascii over the variable.
    for forced in ({"TERM": "linux"}, {"LANG": "C"}, {"LC_ALL": "en_US.ISO-8859-1"}):
        line = _line_one(render(EAWF_STATUSLINE_GLYPHS="nerd", **forced))
        assert _NERD not in line and _UNICODE not in line, forced
    assert glyphs_override({"LANG": "en_US.utf8", "TERM": "xterm"}) is None
    assert glyphs_override({}) is None
    assert glyphs_override({"EAWF_STATUSLINE_GLYPHS": "braille"}) is None
    assert glyphs_override({"LC_ALL": "C", "LANG": "en_US.UTF-8"}) == "ascii"
    # Python's own C-locale coercion value is passed over for the session's LANG.
    assert glyphs_override({"LC_CTYPE": "C.UTF-8", "LANG": "C"}) == "ascii"
    assert glyphs_override({"LC_CTYPE": "C.UTF-8"}) is None

    # NO_COLOR emits no escape at all, on any row, and joins segments with " | ".
    capsys.readouterr()
    plain = render(NO_COLOR="1")
    assert _ESC not in plain
    assert capsys.readouterr() == ("", "")
    rows = plain.split("\n")
    assert rows[0].startswith("Opus 5.5 | sample-project | ")
    assert rows[1].startswith("ctx ")
    # an empty NO_COLOR is unset.
    assert _ESC in render(NO_COLOR="")
