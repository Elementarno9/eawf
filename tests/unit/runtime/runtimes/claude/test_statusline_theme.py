"""The statusline palette: how it is picked, how it keys the cache, and its contrast.

The palette resolves from the ``--theme`` flag and ``EAWF_STATUSLINE_THEME``,
then Claude Code's ``theme`` setting (local project, project, user settings,
then the user's global config), then ``COLORFGBG``, else dark. The resolved
name keys the cached rows, so a switch is never served from a line painted in
the other palette. Every foreground and background pair the renderer emits
keeps a WCAG contrast of at least 4.5:1.
"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path
from typing import Any, Final, Literal

import orjson
import pytest

from eawf.runtime.runtimes.claude import statusline as orchestrator
from eawf.runtime.runtimes.claude.statusline_theme import resolve_palette
from eawf.runtime.statusline_cache import StatuslineCache
from eawf.surfaces.render.statusline import (
    Palette,
    PowerlineBar,
    SegmentSource,
    StatuslineSegment,
    load_palette,
    render_powerline,
    sourced_segment,
    unavailable_segment,
)

_SOURCE: Final = SegmentSource(producer="test-producer", provenance="test#field")
_SGR: Final = re.compile(r"\x1b\[([0-9;]*)m")
_MIN_CONTRAST: Final = 4.5


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``$HOME`` and the cache at this test's tmp dir; clear every theme source."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("EAWF_STATUSLINE_CACHE", str(tmp_path / "cache"))
    for variable in ("EAWF_STATUSLINE_THEME", "COLORFGBG", "EA_STATE", "COLUMNS"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.chdir(tmp_path)
    return home


def _write(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document if isinstance(document, str) else json.dumps(document))


def _resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    flag: str | None = None,
    env: str | None = None,
    colorfgbg: str | None = None,
    layers: dict[str, object] | None = None,
    payload_key: str = "workspace",
) -> str:
    """Resolve the palette for one fresh case and return its name."""
    case = tmp_path / f"case-{len(list(tmp_path.glob('case-*')))}"
    home, project = case / "home", case / "project"
    home.mkdir(parents=True)
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for variable, value in (("EAWF_STATUSLINE_THEME", env), ("COLORFGBG", colorfgbg)):
        if value is None:
            monkeypatch.delenv(variable, raising=False)
        else:
            monkeypatch.setenv(variable, value)
    paths = {
        "local": project / ".claude" / "settings.local.json",
        "project": project / ".claude" / "settings.json",
        "user": home / ".claude" / "settings.json",
        "global": home / ".claude.json",
    }
    for layer, document in (layers or {}).items():
        _write(paths[layer], document)
    payload: dict[str, Any] = (
        {"workspace": {"project_dir": str(project)}}
        if payload_key == "workspace"
        else {"cwd": str(project)}
    )
    return resolve_palette(payload, override=flag)


#: (case, keyword arguments for :func:`_resolve`, expected palette).
_ORDER: Final[list[tuple[str, dict[str, Any], str]]] = [
    ("flag over env", {"flag": "dark", "env": "light"}, "dark"),
    ("unknown flag falls through", {"flag": "ascii-fallback", "env": "light"}, "light"),
    ("env over settings", {"env": "light", "layers": {"local": {"theme": "dark"}}}, "light"),
    ("env over colorfgbg", {"env": "dark", "colorfgbg": "0;15"}, "dark"),
    (
        "unknown env falls through",
        {"env": "sepia", "layers": {"user": {"theme": "light"}}},
        "light",
    ),
    (
        "local over project over user over global",
        {
            "layers": {
                "local": {"theme": "light"},
                "project": {"theme": "dark"},
                "user": {"theme": "dark"},
                "global": {"theme": "dark"},
            }
        },
        "light",
    ),
    (
        "project over user",
        {"layers": {"project": {"theme": "light-daltonized"}, "user": {"theme": "dark"}}},
        "light",
    ),
    (
        "user over global",
        {"layers": {"user": {"theme": "dark-ansi"}, "global": {"theme": "light"}}},
        "dark",
    ),
    ("global config alone", {"layers": {"global": {"theme": "light"}}}, "light"),
    (
        "cwd when no workspace",
        {"layers": {"local": {"theme": "light"}}, "payload_key": "cwd"},
        "light",
    ),
    (
        "auto defers to colorfgbg",
        {"layers": {"local": {"theme": "auto"}, "user": {"theme": "dark"}}, "colorfgbg": "0;15"},
        "light",
    ),
    ("absent: colorfgbg 15", {"colorfgbg": "0;15"}, "light"),
    ("absent: colorfgbg 7", {"colorfgbg": "0;7"}, "light"),
    ("absent: last field counts", {"colorfgbg": "0;default;15"}, "light"),
    ("absent: colorfgbg dark", {"colorfgbg": "15;0"}, "dark"),
    ("absent: colorfgbg 8", {"colorfgbg": "15;8"}, "dark"),
    ("absent: colorfgbg not integer", {"colorfgbg": "default;default"}, "dark"),
    ("absent: colorfgbg empty", {"colorfgbg": ""}, "dark"),
    ("nothing at all", {}, "dark"),
    (
        "unknown setting is dark",
        {"layers": {"user": {"theme": "monokai"}}, "colorfgbg": "0;15"},
        "dark",
    ),
    (
        "malformed layers are skipped",
        {
            "layers": {
                "local": "{not json",
                "project": ["theme", "dark"],
                "user": {"theme": 7},
                "global": {"theme": "light"},
            }
        },
        "light",
    ),
]


def _seed_rows(palette: Literal["light", "dark"]) -> str:
    return orchestrator.render_pipeline({"model": "haiku"}, workspace=None, palette=palette)


def test_resolution_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for case, kwargs, expected in _ORDER:
        assert _resolve(tmp_path, monkeypatch, **kwargs) == expected, case

    # A changed resolved palette is never served from the cached line.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("COLORFGBG", raising=False)
    raw = json.dumps({"session_id": "ses-theme", "model": "haiku"})
    monkeypatch.setenv("EAWF_STATUSLINE_THEME", "dark")
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    dark = orchestrator.run_with_cache(workspace=None, theme_name=None)
    assert dark == _seed_rows("dark")
    assert orchestrator.serve_cached_line(raw, theme_name=None) == dark

    monkeypatch.setenv("EAWF_STATUSLINE_THEME", "light")
    assert orchestrator.serve_cached_line(raw, theme_name=None) is None
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    light = orchestrator.run_with_cache(workspace=None, theme_name=None)
    assert light == _seed_rows("light") != dark

    monkeypatch.setenv("EAWF_STATUSLINE_THEME", "dark")
    assert orchestrator.serve_cached_line(raw, theme_name=None) is None
    assert orchestrator.serve_cached_line(raw, theme_name="light") == light


def test_the_cache_key_separates_entries(tmp_path: Path) -> None:
    path = tmp_path / "entry.json"
    StatuslineCache(path, key="dark").write("dark rows")
    assert StatuslineCache(path, key="dark").get().value == "dark rows"
    assert StatuslineCache(path, key="light").get().present is False
    entry = StatuslineCache(path, key="light").read_entry()
    assert entry is not None and entry.key == "dark"


def test_an_entry_without_a_key_misses_a_keyed_cache(tmp_path: Path) -> None:
    # boundary: an entry written before the key existed is a miss, never a hit.
    path = tmp_path / "entry.json"
    path.write_bytes(orjson.dumps({"value": "old", "written_at": 4_000_000_000.0}))
    assert StatuslineCache(path, key="dark").get().present is False
    assert StatuslineCache(path).get().value == "old"


def test_an_entry_with_a_malformed_key_is_a_miss(tmp_path: Path) -> None:
    # error-path: a non-string key is a corrupt entry.
    path = tmp_path / "entry.json"
    path.write_bytes(orjson.dumps({"value": "x", "written_at": 1.0, "key": 3}))
    assert StatuslineCache(path, key="dark").read_entry() is None


# ---- CR-735: contrast -------------------------------------------------------


def _luminance(colour: str) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    red, green, blue = (int(colour[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)


def _contrast(first: str, second: str) -> float:
    lighter, darker = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _emitted_pairs(rendered: str) -> set[tuple[str, str]]:
    """Return every (foreground, background) a visible cell of *rendered* is drawn in."""
    pairs: set[tuple[str, str]] = set()
    foreground: str | None = None
    background: str | None = None
    position = 0
    for match in [*_SGR.finditer(rendered), None]:
        end = match.start() if match is not None else len(rendered)
        for char in rendered[position:end]:
            if char == "\n":
                continue
            assert foreground is not None and background is not None, repr(char)
            pairs.add((foreground, background))
        if match is None:
            break
        position = match.end()
        params = match.group(1).split(";")
        if params == ["0"]:
            foreground = background = None
        else:
            kind, red, green, blue = params[0], *map(int, params[2:5])
            colour = f"#{red:02x}{green:02x}{blue:02x}"
            if kind == "38":
                foreground = colour
            else:
                background = colour
    return pairs


def _every_state() -> tuple[list[StatuslineSegment], list[PowerlineBar]]:
    """Return segments and bars that between them draw every palette role."""
    segments = [
        sourced_segment("model", "model", "Opus", _SOURCE),
        sourced_segment("project", "project", "alpha", _SOURCE),
        sourced_segment("active_time", "active", "3m · 40% active", _SOURCE),
        sourced_segment("cost", "cost", "$0.10", _SOURCE),
        sourced_segment("mcp_health", "mcp", "2/2", _SOURCE),
        sourced_segment("mcp_health", "mcp", "1/2", _SOURCE, status="warn"),
        sourced_segment(
            "hooks_plugins", "hooks", "1 plugins:0 hook-errors:1", _SOURCE, status="degraded"
        ),
        sourced_segment("scope", "scope", "EAWF-0001", _SOURCE),
        sourced_segment("budget", "budget", "1.0k/2.0k", _SOURCE, status="info"),
        sourced_segment("other", "other", "x", _SOURCE),
        unavailable_segment("git", "git", "render-failed", _SOURCE, status="failed"),
    ]
    bars = [
        PowerlineBar(label="ctx", ratio=0.1, detail="10K/100K", trend=2),
        PowerlineBar(label="5h", ratio=0.6, detail="1h00m", trend=-2),
        PowerlineBar(label="7d", ratio=0.95, detail="2d3h"),
    ]
    return segments, bars


@pytest.mark.parametrize("name", ["light", "dark"])
def test_contrast(name: Literal["light", "dark"]) -> None:
    palette = load_palette(name)
    segments, bars = _every_state()
    pairs: set[tuple[str, str]] = set()
    for width in (None, 60, 30):
        pairs |= _emitted_pairs("\n".join(render_powerline(segments, bars, palette, width=width)))

    ratios = {pair: _contrast(*pair) for pair in pairs}
    worst = min(ratios, key=lambda pair: ratios[pair])
    assert ratios[worst] >= _MIN_CONTRAST, (worst, ratios[worst])
    drawn = {colour for pair in pairs for colour in pair}
    assert set(palette.model_dump().values()) <= drawn


def test_the_bundled_palettes_are_closed_and_differ() -> None:
    light, dark = load_palette("light"), load_palette("dark")
    assert light != dark
    with pytest.raises(ValueError, match="pattern"):
        Palette.model_validate({**dark.model_dump(), "base": "#FFF"})
    with pytest.raises(ValueError, match="Extra inputs"):
        Palette.model_validate({**dark.model_dump(), "glyph": "#ffffff"})
