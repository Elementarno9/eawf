"""Resolve the statusline theme and glyph set for one redraw.

The sources a process environment states are resolved on every redraw, a
cache hit included, because they key the cached line: a theme switch through
a flag or a variable must show on the next redraw. So this module imports only
the standard library at load time; the layered config is read only when a
render runs, through :func:`statusline_config`.

Theme, first source that names one:

1. The ``--theme`` flag.
2. ``NO_COLOR`` set to anything non-empty: ``none``.
3. The ``EAWF_STATUSLINE_THEME`` environment variable.
4. The ``statusline.theme`` config leaf, whose default is ``ansi``.

``dark`` and ``light`` name ``catppuccin-mocha`` and ``catppuccin-latte``; a
value naming no theme falls through to the next source.

Glyphs: ``TERM=linux`` or a non-UTF-8 locale forces ``ascii``, because such a
terminal draws neither the separators nor the bar eighths; otherwise
``EAWF_STATUSLINE_GLYPHS``, then the ``statusline.glyphs`` leaf.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

if TYPE_CHECKING:
    from eawf.kernel.config.schema import StatuslineConfig, StatuslineGlyphs, StatuslineTheme

#: The environment variable an operator pins the theme with.
THEME_ENV: Final = "EAWF_STATUSLINE_THEME"
#: The environment variable an operator pins the glyph set with.
GLYPHS_ENV: Final = "EAWF_STATUSLINE_GLYPHS"

_NO_COLOR_ENV: Final = "NO_COLOR"
_TERM_ENV: Final = "TERM"
#: The locale variables in the order the C library consults them.
_CTYPE_ENV: Final = "LC_CTYPE"
_LOCALE_ENVS: Final = ("LC_ALL", _CTYPE_ENV, "LANG")
#: The ``LC_CTYPE`` values Python's C-locale coercion sets.
_COERCED_CTYPES: Final = frozenset({"C.UTF-8", "C.utf8", "UTF-8"})

#: Every theme name, kept beside the config's ``StatuslineTheme`` so the hit
#: path needs no config import; a test holds the two equal.
THEMES: Final = frozenset(
    {
        "ansi",
        "catppuccin-latte",
        "catppuccin-frappe",
        "catppuccin-macchiato",
        "catppuccin-mocha",
        "none",
    }
)
#: Every glyph set name, held equal to the config's ``StatuslineGlyphs`` the same way.
GLYPH_SETS: Final = frozenset({"nerd", "unicode", "ascii"})

_ALIASES: Final = {"dark": "catppuccin-mocha", "light": "catppuccin-latte"}

#: The cache-key part that says the config, not the environment, decides.
_FROM_CONFIG: Final = "config"


def _theme_named(value: str | None) -> StatuslineTheme | None:
    """Return the theme *value* names, through the dark and light aliases, else ``None``."""
    if value is None:
        return None
    name = _ALIASES.get(value, value)
    return cast("StatuslineTheme", name) if name in THEMES else None


def theme_override(flag: str | None, env: Mapping[str, str]) -> StatuslineTheme | None:
    """Return the theme the flag or the environment names, or ``None`` to defer to config.

    Args:
        flag: The ``--theme`` value, or ``None`` when it is not given.
        env: The process environment.

    Returns:
        The flag's theme, ``none`` under ``NO_COLOR``, the
        ``EAWF_STATUSLINE_THEME`` theme, in that order; ``None`` when none names one.
    """
    named = _theme_named(flag)
    if named is not None:
        return named
    if env.get(_NO_COLOR_ENV):
        return "none"
    return _theme_named(env.get(THEME_ENV))


def _utf8_locale(env: Mapping[str, str]) -> bool:
    """Return whether the locale the environment names encodes UTF-8.

    An environment naming no locale at all states no encoding, so it is not
    read as a non-UTF-8 one. Python's C-locale coercion (PEP 538) writes
    ``LC_CTYPE`` into this very process's environment when the locale was
    ``C``, so a coercion value there is passed over for the variables the
    terminal's session set.
    """
    for variable in _LOCALE_ENVS:
        value = env.get(variable)
        if not value or (variable == _CTYPE_ENV and value in _COERCED_CTYPES):
            continue
        return "utf8" in value.lower().replace("-", "")
    return True


def glyphs_override(env: Mapping[str, str]) -> StatuslineGlyphs | None:
    """Return the glyph set the environment forces or names, or ``None`` to defer to config.

    Args:
        env: The process environment.

    Returns:
        ``ascii`` for ``TERM=linux`` or a non-UTF-8 locale, else the
        ``EAWF_STATUSLINE_GLYPHS`` set; ``None`` when neither applies.
    """
    if env.get(_TERM_ENV) == "linux" or not _utf8_locale(env):
        return "ascii"
    value = env.get(GLYPHS_ENV)
    return cast("StatuslineGlyphs", value) if value in GLYPH_SETS else None


def render_key(flag: str | None, env: Mapping[str, str]) -> str:
    """Return the key a cached line is stored under for this flag and environment.

    Args:
        flag: The ``--theme`` value, or ``None``.
        env: The process environment.

    Returns:
        ``<theme>/<glyphs>``, each part ``config`` where the config decides.
    """
    theme = theme_override(flag, env) or _FROM_CONFIG
    glyphs = glyphs_override(env) or _FROM_CONFIG
    return f"{theme}/{glyphs}"


def statusline_config(repo_root: Path | None) -> StatuslineConfig:
    """Return the ``statusline`` section of the layered config.

    A redraw never fails over a config file: a malformed layer or section is
    logged and the shipped defaults draw the line.

    Args:
        repo_root: The repository whose layers compose over the global one;
            ``None`` outside a repository, where the global layer still applies.

    Returns:
        The section, defaults filled in.
    """
    import logging

    from pydantic import ValidationError as SectionError

    from eawf.kernel.config.layered import merge_config
    from eawf.kernel.config.schema import StatuslineConfig
    from eawf.surfaces.cli.errors import ValidationError as LayerError

    try:
        merged, _sources = merge_config(workspace=repo_root, repo=repo_root)
        return StatuslineConfig.model_validate(merged.get("statusline") or {})
    except (LayerError, SectionError) as exc:
        logging.getLogger(__name__).warning(f"statusline_config config-unreadable error={exc}")
        return StatuslineConfig()


__all__ = [
    "GLYPHS_ENV",
    "GLYPH_SETS",
    "THEMES",
    "THEME_ENV",
    "glyphs_override",
    "render_key",
    "statusline_config",
    "theme_override",
]
