"""Pick the light or the dark statusline palette for one redraw.

The palette is resolved on every redraw, a cache hit included, because the
resolved name keys the cached line: a theme switch must show on the next
redraw rather than after the cached line expires. So this module imports only
``orjson`` and the standard library, and reads at most four small settings
files.

Resolution order:

1. An explicit ``light`` or ``dark``: the ``--theme`` flag, then the
   ``EAWF_STATUSLINE_THEME`` environment variable.
2. Claude Code's ``theme`` setting, from the first layer that sets it: the
   project's ``.claude/settings.local.json``, its ``.claude/settings.json``,
   the user's ``~/.claude/settings.json``, then the user's global config
   ``~/.claude.json``, which is where Claude Code itself keeps the theme the
   ``/config`` menu writes. A value starting ``light`` or ``dark`` picks that
   palette, which covers the daltonized and ANSI variants.
3. When the setting is ``auto`` or absent, ``COLORFGBG``: a last field of 7
   or 15 names a light terminal background, any other integer a dark one.
4. Otherwise dark.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Final, Literal

import orjson

type PaletteName = Literal["light", "dark"]

#: The environment variable an operator pins the palette with.
THEME_ENV: Final = "EAWF_STATUSLINE_THEME"

_COLORFGBG_ENV: Final = "COLORFGBG"
_THEME_KEY: Final = "theme"
_AUTO: Final = "auto"

#: The ANSI colour indices terminals report for a light background.
_LIGHT_BACKGROUNDS: Final = frozenset({7, 15})


def _explicit(value: str | None) -> PaletteName | None:
    """Return *value* when it names a palette, else ``None``."""
    if value == "light":
        return "light"
    if value == "dark":
        return "dark"
    return None


def _project_dir(claude_payload: dict[str, Any]) -> Path | None:
    """Return the session's project directory, or ``None`` when the payload names none."""
    workspace = claude_payload.get("workspace")
    project = workspace.get("project_dir") if isinstance(workspace, dict) else None
    if not isinstance(project, str) or not project:
        project = claude_payload.get("cwd")
    return Path(project) if isinstance(project, str) and project else None


def _settings_layers(project: Path | None) -> list[Path]:
    """Return the files that may set ``theme``, the one that wins first."""
    home = Path.home()
    layers = [home / ".claude" / "settings.json", home / ".claude.json"]
    if project is not None:
        claude_dir = project / ".claude"
        layers[:0] = [claude_dir / "settings.local.json", claude_dir / "settings.json"]
    return layers


def _read_theme(path: Path) -> str | None:
    """Return the ``theme`` string *path* sets, or ``None`` when it sets none.

    An absent, unreadable or malformed file sets nothing, so the next layer
    decides: a redraw never fails over a settings file.
    """
    try:
        document = orjson.loads(path.read_bytes())
    except OSError, orjson.JSONDecodeError:
        return None
    value = document.get(_THEME_KEY) if isinstance(document, dict) else None
    return value if isinstance(value, str) and value else None


def _from_colorfgbg(raw: str | None) -> PaletteName:
    """Return the palette the terminal's ``COLORFGBG`` background implies, else dark."""
    background = (raw or "").rsplit(";", 1)[-1].strip()
    if background.isdigit() and int(background) in _LIGHT_BACKGROUNDS:
        return "light"
    return "dark"


def resolve_palette(claude_payload: dict[str, Any], *, override: str | None) -> PaletteName:
    """Return the palette this redraw renders with.

    Args:
        claude_payload: Decoded Claude stdin JSON, read for the project
            directory whose settings layers are consulted.
        override: The ``--theme`` flag value; only ``light`` and ``dark``
            are honoured, anything else falls through to the next source.

    Returns:
        ``light`` or ``dark``, by the order the module docstring states.
    """
    for candidate in (override, os.environ.get(THEME_ENV)):
        explicit = _explicit(candidate)
        if explicit is not None:
            return explicit
    for layer in _settings_layers(_project_dir(claude_payload)):
        setting = _read_theme(layer)
        if setting is None:
            continue
        if setting.startswith("light"):
            return "light"
        if setting.startswith("dark") or setting != _AUTO:
            return "dark"
        break
    return _from_colorfgbg(os.environ.get(_COLORFGBG_ENV))


__all__ = ["THEME_ENV", "PaletteName", "resolve_palette"]
