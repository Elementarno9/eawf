"""Theme-aware statusline rendering for ``eawf cc statusline`` (Phase 4 W06).

The orchestrator in :mod:`eawf.runtime.runtimes.claude.statusline` collects a list of
:class:`StatuslineSegment` records (one per module — see
:mod:`eawf.runtime.runtimes.claude.statusline_modules`). This module's job is the
last mile: take a list of segments and a theme, return a single line of
text ready for stdout.

Public surface:

- :class:`StatuslineSegment` — typed segment dataclass.
- :class:`StatuslineTheme` — typed theme record (``separator``,
  ``skip_failed``, ``colors``, ``glyph``).
- :func:`load_themes` — read ``templates/themes.yaml`` into a mapping.
- :func:`resolve_theme` — pick a theme by name with fallback to ``default``.
- :func:`render_segments` — apply the theme to a list of segments,
  return the joined line.

Theme schema is documented in ``src/eawf/platform/templates/themes.yaml``. The schema
is intentionally permissive — unknown segment/status/module keys silently
fall through to "no color" / "no glyph" so themes can omit modules they
don't want to decorate.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any, Literal

import yaml

logger = logging.getLogger(__name__)


GlyphMode = Literal["ascii", "unicode"]
"""Effective glyph set after resolving the configured policy.

The configured ``statusline.glyph_mode`` policy is one of ``auto`` /
``ascii`` / ``unicode``; :func:`resolve_glyph_mode` collapses ``auto`` to a
concrete :data:`GlyphMode` against the terminal capability probe, so the
render path only ever sees a resolved value.
"""

ColorMode = Literal["on", "off"]
"""Effective colour state after resolving the configured policy.

The configured ``statusline.color_mode`` policy is one of ``auto`` /
``always`` / ``never``; :func:`resolve_color_mode` collapses ``auto`` against
the terminal capability probe to a concrete :data:`ColorMode`.
"""

_GLYPH_MODE_AUTO: str = "auto"
_GLYPH_MODE_ASCII: GlyphMode = "ascii"
_GLYPH_MODE_UNICODE: GlyphMode = "unicode"

_COLOR_MODE_AUTO: str = "auto"
_COLOR_MODE_ALWAYS: str = "always"
_COLOR_MODE_NEVER: str = "never"
_COLOR_ON: ColorMode = "on"
_COLOR_OFF: ColorMode = "off"


SegmentStatus = Literal["ok", "warn", "missing", "degraded", "failed"]
"""Status carried on each :class:`StatuslineSegment`.

``failed`` is reserved for modules that raised during render — the
orchestrator wraps any module exception in a ``status="failed"`` segment so
the renderer can decide whether to skip or render ``<module>:!`` based on
``StatuslineTheme.skip_failed``.
"""


_THEMES_FILENAME: str = "themes.yaml"
_TEMPLATES_PACKAGE: str = "eawf.platform.templates"
_DEFAULT_THEME_NAME: str = "default"


@dataclass(frozen=True)
class StatuslineSegment:
    """One module's output — what the renderer joins into the final line.

    Attributes:
        module: Stable module identifier (``state``, ``git``,
            ``model_session_cwd``, ``context_tokens``, ``mcp_health``,
            ``hooks_plugins``, ``memory``, ``token_saving``). Used to look
            up the per-module glyph in the theme.
        text: User-facing body, e.g. ``"P04-W06"`` or ``"feature/x*"``.
            Already pre-formatted by the module — the renderer only adds
            colour and glyph wrappers.
        status: Segment status, drives the colour lookup.
    """

    module: str
    text: str
    status: SegmentStatus = "ok"


@dataclass(frozen=True)
class StatuslineTheme:
    """Resolved theme record used by :func:`render_segments`.

    Attributes:
        name: Theme name as found in ``themes.yaml`` (e.g. ``"default"``).
        separator: String inserted between segments. Defaults to ``" | "``.
        skip_failed: When ``True``, segments with ``status="failed"`` are
            dropped silently. When ``False``, they render as
            ``"<module>:!"`` so the operator sees the broken module.
        colors: Mapping ``status -> ANSI escape``. Empty string or missing
            entry disables colour for that status. ``reset`` is the
            terminator emitted after every coloured segment.
        glyph: Mapping ``module -> glyph string``. Missing entries render
            without a glyph prefix.
    """

    name: str
    separator: str = " | "
    skip_failed: bool = False
    colors: dict[str, str] = field(default_factory=dict)
    glyph: dict[str, str] = field(default_factory=dict)


_ANSI_ESC: str = "\x1b"
"""ESC byte prepended to YAML color values that start with ``[`` (CSI form).

YAML cannot store raw control bytes portably, so ``themes.yaml`` carries
``"[32m"`` (no ESC) and the loader prepends ``\\x1b`` here. Already-escaped
values (rare; tests inject them directly) round-trip unchanged.
"""


def _normalise_color(value: str) -> str:
    """Add the ESC byte to a CSI color value when it starts with ``[``.

    Empty strings stay empty (disables color for that status). Values that
    already begin with ``\\x1b`` are returned untouched so direct test
    inputs and future template forms keep working.
    """
    if not value:
        return ""
    if value.startswith(_ANSI_ESC):
        return value
    if value.startswith("["):
        return f"{_ANSI_ESC}{value}"
    return value


def _theme_from_payload(name: str, raw: dict[str, Any]) -> StatuslineTheme:
    """Build a :class:`StatuslineTheme` from a YAML mapping.

    Permissive on shape: unknown top-level keys are ignored, and missing keys
    fall back to the dataclass defaults. The renderer never raises on a
    badly-shaped theme; it falls through to plain text.
    """
    separator = raw.get("separator", " | ")
    if not isinstance(separator, str):
        separator = " | "
    skip_failed = bool(raw.get("skip_failed", False))
    colors_raw = raw.get("colors") or {}
    glyph_raw = raw.get("glyph") or {}
    colors: dict[str, str] = {
        str(k): _normalise_color(str(v) if v is not None else "") for k, v in colors_raw.items()
    }
    glyph: dict[str, str] = {str(k): str(v) if v is not None else "" for k, v in glyph_raw.items()}
    return StatuslineTheme(
        name=name,
        separator=separator,
        skip_failed=skip_failed,
        colors=colors,
        glyph=glyph,
    )


def load_themes() -> dict[str, StatuslineTheme]:
    """Read ``templates/themes.yaml`` into a name-keyed mapping.

    Returns a dict of every parseable theme. Themes that fail individual
    parsing are dropped with a warning so a single bad entry can't take down
    the renderer; callers should always check for the ``default`` key.

    The bundled file is loaded via :func:`importlib.resources.files` so the
    helper works from a wheel install, an editable install, and the source
    tree alike (mirrors :mod:`eawf.platform.profiles.loader`).
    """
    raw_text: str
    try:
        raw_text = (files(_TEMPLATES_PACKAGE) / _THEMES_FILENAME).read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning(f"load_themes themes-file-missing package={_TEMPLATES_PACKAGE!r}")
        return {_DEFAULT_THEME_NAME: StatuslineTheme(name=_DEFAULT_THEME_NAME)}
    parsed: Any
    try:
        parsed = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as exc:
        logger.warning(f"load_themes themes-yaml-parse-error error={exc}")
        return {_DEFAULT_THEME_NAME: StatuslineTheme(name=_DEFAULT_THEME_NAME)}
    if not isinstance(parsed, dict):
        logger.warning(f"load_themes themes-yaml-not-mapping got={type(parsed).__name__}")
        return {_DEFAULT_THEME_NAME: StatuslineTheme(name=_DEFAULT_THEME_NAME)}
    out: dict[str, StatuslineTheme] = {}
    for name, body in parsed.items():
        if not isinstance(name, str):
            continue
        if not isinstance(body, dict):
            logger.warning(f"load_themes theme-body-not-mapping theme={name!r}; skipping")
            continue
        out[name] = _theme_from_payload(name, body)
    if _DEFAULT_THEME_NAME not in out:
        out[_DEFAULT_THEME_NAME] = StatuslineTheme(name=_DEFAULT_THEME_NAME)
    return out


def resolve_theme(name: str | None, themes: dict[str, StatuslineTheme]) -> StatuslineTheme:
    """Return the theme matching *name* with fallback to ``default``.

    Args:
        name: Requested theme name (``--theme`` flag or env var). ``None`` /
            empty selects the default.
        themes: Mapping built by :func:`load_themes`.

    Returns:
        The matching :class:`StatuslineTheme`. The fallback chain is:
        requested name → ``default`` (always inserted by ``load_themes``) →
        a fresh defaulted :class:`StatuslineTheme` (only when ``themes`` is
        somehow empty — which shouldn't happen in production).
    """
    if name and name in themes:
        return themes[name]
    if _DEFAULT_THEME_NAME in themes:
        return themes[_DEFAULT_THEME_NAME]
    return StatuslineTheme(name=_DEFAULT_THEME_NAME)


def _decorate_segment(segment: StatuslineSegment, theme: StatuslineTheme) -> str:
    """Return the rendered string for one segment under *theme*.

    Wraps :attr:`StatuslineSegment.text` with the per-module glyph (prefix)
    and the per-status colour (with an explicit reset suffix). Missing glyph
    or colour entries are dropped silently.
    """
    glyph_prefix = theme.glyph.get(segment.module, "")
    body = f"{glyph_prefix} {segment.text}" if glyph_prefix else segment.text
    color = theme.colors.get(segment.status, "")
    if color:
        reset = theme.colors.get("reset", "")
        return f"{color}{body}{reset}"
    return body


def render_segments(segments: list[StatuslineSegment], theme: StatuslineTheme) -> str:
    """Apply *theme* to *segments* and return the joined statusline.

    Pipeline:

    1. Drop segments with ``status="failed"`` when ``theme.skip_failed`` is
       true.
    2. Decorate every remaining segment via :func:`_decorate_segment`.
    3. Join with ``theme.separator``.

    The output never ends in a trailing newline — the orchestrator decides
    how to write it to stdout.
    """
    visible: list[StatuslineSegment] = [
        s for s in segments if not (theme.skip_failed and s.status == "failed")
    ]
    decorated = [_decorate_segment(s, theme) for s in visible]
    return theme.separator.join(decorated)


def render_rows(
    rows_of_segments: list[list[StatuslineSegment]],
    theme: StatuslineTheme,
    *,
    rows: int,
) -> str:
    """Render exactly *rows* statusline lines, newline-joined.

    Each entry in *rows_of_segments* is one row's segment list, rendered
    through :func:`render_segments` under *theme*. The output always carries
    exactly *rows* lines: extra supplied rows beyond *rows* are dropped, and
    a shortfall is padded with empty lines so the row count is stable for a
    fixed-height statusline reader.

    Args:
        rows_of_segments: One segment list per row, in top-to-bottom order.
        theme: Theme applied to every row.
        rows: Number of lines to emit (> 0). Matches ``statusline.rows``.

    Returns:
        The rendered statusline of exactly *rows* newline-joined lines. No
        trailing newline -- the caller decides how to write it.

    Raises:
        ValueError: When *rows* is not positive.
    """
    if rows <= 0:
        raise ValueError(f"rows must be positive: {rows!r}")
    lines: list[str] = []
    for index in range(rows):
        segments = rows_of_segments[index] if index < len(rows_of_segments) else []
        lines.append(render_segments(segments, theme))
    return "\n".join(lines)


def terminal_supports_color() -> bool:
    """Return ``True`` when the active terminal can render ANSI colour.

    The probe is the colour-capability source of truth for the statusline
    auto modes. A terminal is treated as colour-capable unless any of the
    standard no-colour signals fires:

    - the ``NO_COLOR`` env var is set to any value (the cross-tool
      no-colour convention);
    - ``TERM`` is ``dumb`` or empty (a non-capable terminal);
    - stdout is not attached to a TTY (a pipe / file / CI capture).

    Returns:
        ``True`` when none of the no-colour signals fires, else ``False``.
    """
    if os.environ.get("NO_COLOR") is not None:
        return False
    term = os.environ.get("TERM", "")
    if term in ("", "dumb"):
        return False
    return sys.stdout.isatty()


def resolve_glyph_mode(configured: str, *, color_capable: bool) -> GlyphMode:
    """Resolve a configured glyph policy to a concrete :data:`GlyphMode`.

    The ``statusline.glyph_mode`` policy is one of ``auto`` / ``ascii`` /
    ``unicode``. ``auto`` downgrades to :data:`_GLYPH_MODE_ASCII` on a
    no-colour terminal (``color_capable`` is falsy) and selects
    :data:`_GLYPH_MODE_UNICODE` otherwise; the explicit ``ascii`` / ``unicode``
    policies pass through unchanged.

    Args:
        configured: The configured policy string.
        color_capable: Terminal colour capability, typically from
            :func:`terminal_supports_color`.

    Returns:
        The resolved :data:`GlyphMode`.

    Raises:
        ValueError: When *configured* is not one of the known policies.
    """
    if configured == _GLYPH_MODE_ASCII:
        return _GLYPH_MODE_ASCII
    if configured == _GLYPH_MODE_UNICODE:
        return _GLYPH_MODE_UNICODE
    if configured == _GLYPH_MODE_AUTO:
        return _GLYPH_MODE_UNICODE if color_capable else _GLYPH_MODE_ASCII
    raise ValueError(f"unknown statusline glyph mode: {configured!r}")


def resolve_color_mode(configured: str, *, color_capable: bool) -> ColorMode:
    """Resolve a configured colour policy to a concrete :data:`ColorMode`.

    The ``statusline.color_mode`` policy is one of ``auto`` / ``always`` /
    ``never``. ``always`` forces :data:`_COLOR_ON`, ``never`` forces
    :data:`_COLOR_OFF`, and ``auto`` defers to the terminal capability probe
    (``color_capable``), turning colour off on a no-colour terminal.

    Args:
        configured: The configured policy string.
        color_capable: Terminal colour capability, typically from
            :func:`terminal_supports_color`.

    Returns:
        The resolved :data:`ColorMode`.

    Raises:
        ValueError: When *configured* is not one of the known policies.
    """
    if configured == _COLOR_MODE_ALWAYS:
        return _COLOR_ON
    if configured == _COLOR_MODE_NEVER:
        return _COLOR_OFF
    if configured == _COLOR_MODE_AUTO:
        return _COLOR_ON if color_capable else _COLOR_OFF
    raise ValueError(f"unknown statusline color mode: {configured!r}")


__all__ = [
    "ColorMode",
    "GlyphMode",
    "SegmentStatus",
    "StatuslineSegment",
    "StatuslineTheme",
    "load_themes",
    "render_rows",
    "render_segments",
    "resolve_color_mode",
    "resolve_glyph_mode",
    "resolve_theme",
    "terminal_supports_color",
]
