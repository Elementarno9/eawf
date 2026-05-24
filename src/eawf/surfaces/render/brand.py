"""Eä logotype + Nerd Font / ASCII glyph set + breadcrumb composition.

The Eä logotype is the literal two-character string ``"Eä"`` (capital E +
lowercase a-umlaut, U+00E4) — *not* a font ligature. The TUI header places
it outside-left of the scope breadcrumb (``Eä  workspace > repo > P00``);
the CLI surface emits the same literal at the head of headlines.

Style. Bold + accent (default Wong-orange ``#E69F00``) per design brief
``.ea/artifacts/research/long-term/2026-05-16-c07b-vcs-worktree-events.md``
§5.6 (Branding). Rendering of the bold/colour layer is the caller's
responsibility — this module only exposes the literal + ANSI helpers.

Glyph set. Two parallel sets cover the interactive-TTY and piped/CI
paths:

- :data:`NERD_FONT_GLYPHS` — Nerd Font private-use codepoints. Emitted
  when stdout is a TTY (``sys.stdout.isatty()`` returns ``True``).
- :data:`ASCII_GLYPHS` — pure-ASCII fallback (``[ok]``, ``->``, …) used
  when stdout is *not* a TTY so log captures, CI scrapes, and dumb
  terminals remain readable.

:func:`select_glyphs` resolves the right set; the caller may pass an
explicit ``tty`` flag (e.g. to force one branch under test) or leave it
unset to defer to :func:`is_tty`. Callers that need a TTY-aware
breadcrumb head can use :func:`render_breadcrumb_head` which prepends
the literal + spacing in one step.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

# The Eä logotype. Two characters: ``E`` (U+0045) + ``ä`` (U+00E4). Treat
# this as a literal string in every test and renderer — it is *not* a
# font ligature, *not* a glyph id, *not* an emoji.
BRAND_LITERAL: str = "Eä"

# Pure-ASCII fallback for the logotype itself. Used when even Unicode is
# unsafe (rare; mostly defensive). The breadcrumb head still emits
# :data:`BRAND_LITERAL` under :data:`ASCII_GLYPHS` because ``ä`` is
# valid UTF-8 — the brand-literal fallback is here so callers can swap
# it on legacy terminals that mangle U+00E4.
ASCII_BRAND_LITERAL: str = "[Ea]"

# Spacing inserted between the logotype and the first breadcrumb segment.
# Two spaces give the brand visual breathing room without leaning on a
# Nerd Font separator (which would force the head into the NF branch).
_BRAND_GAP: str = "  "

# Default Wong 2011 deuteranopia-safe accent (orange). Matches §5.6
# ``palette.semantic_map.accent``. Callers that style the brand with
# ANSI 24-bit colour can pass this through their colour helper; the
# constant lives here so the canonical hex is grep-able from one
# place.
ACCENT_HEX: str = "#E69F00"

# ANSI SGR open/close pair for bold. Emitted by :func:`bold` so callers
# don't have to remember the escape codes.
_BOLD_ON: str = "\x1b[1m"
_BOLD_OFF: str = "\x1b[22m"


@dataclass(frozen=True)
class GlyphSet:
    """Frozen glyph mapping resolved by :func:`select_glyphs`.

    The same six fields are present on both the Nerd Font and the ASCII
    sets — callers can swap one for the other without branching.

    Attributes:
        brand: The Eä logotype literal as emitted by this set.
        scope_separator: String between adjacent breadcrumb segments
            (``" > "`` on ASCII, NF U+2771 ``" ❫ "`` on Nerd Font).
        status_ok: Glyph for ``EnvelopeStatus="ok"``.
        status_needs_user: Glyph for ``EnvelopeStatus="needs_user"``.
        status_blocked: Glyph for ``EnvelopeStatus="blocked"``.
        status_failed: Glyph for ``EnvelopeStatus="failed"``.
        status_partial: Glyph for ``EnvelopeStatus="partial"``.
    """

    brand: str
    scope_separator: str
    status_ok: str
    status_needs_user: str
    status_blocked: str
    status_failed: str
    status_partial: str


# Nerd Font private-use codepoints (see design brief §5.6 glyph table).
# These ride font fallback on a Nerd-Font-capable terminal; on a plain
# terminal they render as boxes — which is why :func:`select_glyphs`
# falls back to ASCII when stdout is not a TTY.
NERD_FONT_GLYPHS: GlyphSet = GlyphSet(
    brand=BRAND_LITERAL,
    scope_separator=" ❫ ",  # U+276B medium right-pointing parenthesis
    status_ok="",  # NF check
    status_needs_user="",  # NF question
    status_blocked="",  # NF hourglass
    status_failed="",  # NF x-mark
    status_partial="",  # NF warning triangle
)

# Pure-ASCII glyph set. Used when stdout is *not* a TTY (piped, CI, log
# capture) so consumers without a Nerd Font installation see readable
# text instead of placeholder boxes.
ASCII_GLYPHS: GlyphSet = GlyphSet(
    brand=BRAND_LITERAL,
    scope_separator=" > ",
    status_ok="[ok]",
    status_needs_user="[?]",
    status_blocked="[blocked]",
    status_failed="[x]",
    status_partial="[!]",
)


def is_tty() -> bool:
    """Return ``True`` when ``sys.stdout`` is attached to a terminal.

    Wraps :meth:`sys.stdout.isatty` so test code can patch the call site
    deterministically (``monkeypatch.setattr("eawf.surfaces.render.brand.is_tty",
    lambda: False)``). The function is the single source of truth for
    the TTY/non-TTY branch inside this module.
    """
    return sys.stdout.isatty()


def select_glyphs(tty: bool | None = None) -> GlyphSet:
    """Return the Nerd Font set on TTY, the ASCII set otherwise.

    Args:
        tty: Explicit override. ``None`` (default) defers to
            :func:`is_tty`; ``True`` forces the Nerd Font set; ``False``
            forces the ASCII fallback. Tests should pass an explicit
            flag rather than monkey-patching :func:`is_tty`.

    Returns:
        :data:`NERD_FONT_GLYPHS` when ``tty`` resolves truthy, else
        :data:`ASCII_GLYPHS`.
    """
    resolved = is_tty() if tty is None else tty
    return NERD_FONT_GLYPHS if resolved else ASCII_GLYPHS


def render_breadcrumb_head(breadcrumb: str, *, tty: bool | None = None) -> str:
    """Prepend the Eä logotype to *breadcrumb* with the canonical gap.

    The logotype lives outside-left of the breadcrumb per memory
    ``feedback_tui_branding`` and design brief §5.6 (e.g.
    ``"Eä  workspace > repo > P00 > I01 > W09"``).

    Args:
        breadcrumb: Pre-rendered breadcrumb string (segments already
            joined by the appropriate scope separator). Must not start
            with the logotype — this helper owns the head prefix.
        tty: Explicit TTY override forwarded to :func:`select_glyphs`.

    Returns:
        ``"Eä  <breadcrumb>"`` — two-space gap between the brand and
        the first segment. The output never carries ANSI escapes;
        callers that want bold + accent wrap the return through
        :func:`bold`.
    """
    glyphs = select_glyphs(tty=tty)
    return f"{glyphs.brand}{_BRAND_GAP}{breadcrumb}"


def bold(text: str) -> str:
    """Wrap *text* in ANSI bold SGR codes.

    Returns ``text`` sandwiched between ``ESC[1m`` and ``ESC[22m`` so
    the bold attribute is closed cleanly (vs. ``ESC[0m`` which would
    reset colour too). Callers that target a non-ANSI sink should skip
    this helper.
    """
    return f"{_BOLD_ON}{text}{_BOLD_OFF}"


__all__ = [
    "ACCENT_HEX",
    "ASCII_BRAND_LITERAL",
    "ASCII_GLYPHS",
    "BRAND_LITERAL",
    "NERD_FONT_GLYPHS",
    "GlyphSet",
    "bold",
    "is_tty",
    "render_breadcrumb_head",
    "select_glyphs",
]
