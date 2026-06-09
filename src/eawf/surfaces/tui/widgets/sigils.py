"""Lifecycle + chrome sigil glyphs for the Eae TUI cosmic-terminal reskin.

This module is the SHAPE layer of the two-axis visual vocabulary the
reskin panes share. Its sibling
:mod:`~eawf.surfaces.tui.widgets.status_tint` is the COLOUR layer: it maps
a lifecycle-status string to a Wong deuteranopia-safe ``#rrggbb`` hex via
:data:`~eawf.surfaces.tui.widgets.status_tint.STATUS_COLOURS` /
:func:`~eawf.surfaces.tui.widgets.status_tint.status_colour`. Shape comes
from here; colour comes from there. No pane invents a glyph or a hex of
its own -- they call :func:`glyph` / :func:`chrome` for the mark and
:func:`tint` for the hue so a retune of either axis lands in one home.

The module is PURE: it imports no Textual primitive and holds no state,
so every consumer (the roadmap tree, the status pane, the dispatch /
gate / attention chrome) can resolve a glyph string without mounting a
widget, and the unit tests cover the whole surface lock-free.

Two glyph columns ship per mark -- a unicode column and an ASCII
fallback. The active column is chosen by the App's resolved render mode
string (see :func:`glyph` / :func:`chrome`): ``"ascii"`` selects the
ASCII column, any other label (``"unicode"`` and the legacy ``"braille"``
alias a sibling wave renames) selects the unicode column, so the helper
stays decoupled from the not-yet-landed render-mode rename.

The ASCII lifecycle alphabet is deliberately DECONFLICTED off the EU /
burn bar glyphs (the bar fills with ``#`` and pads with ``-``; see
:data:`~eawf.surfaces.tui.widgets.eu_bar.GLYPH_FULL` /
:data:`~eawf.surfaces.tui.widgets.eu_bar.GLYPH_EMPTY`). That is why the
closed sigil is ``@`` rather than the bar's ``#`` and the pending sigil
is ``o`` rather than the bar's ``-``: a row that renders a sigil beside
an inline bar would otherwise read ambiguously in ASCII mode. The
regression test pins the empty intersection of the two alphabets.
"""

from __future__ import annotations

from enum import Enum

from eawf.surfaces.tui.widgets.status_tint import status_colour

#: The render-mode label that selects the ASCII glyph column. Any other
#: label selects the unicode column (see :func:`glyph` / :func:`chrome`),
#: so the two not-yet-unified unicode labels (``"unicode"`` and the legacy
#: ``"braille"`` alias) both resolve to the unicode glyphs without this
#: module knowing which name is current.
ASCII_MODE: str = "ascii"


class Sigil(Enum):
    """Lifecycle-state marks the reskin panes render.

    The enum value of each member is the canonical lifecycle-status
    string the SHAPE and COLOUR layers share, EXCEPT :attr:`RUNNING`,
    whose value is the human ``"running"`` while its tint resolves
    against the ``"in_progress"`` status key (see :func:`tint`). Phase
    and iter rows draw the four-state subset (they never enter the
    CLAIMED state, which is wave-only); a consumer renders that subset
    simply by never passing :attr:`CLAIMED` -- no separate API exists for
    it.
    """

    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    CLOSED = "closed"
    FAILED = "failed"


#: Lifecycle shapes: :class:`Sigil` -> ``(unicode, ascii)``. The unicode
#: column is written with ``\uXXXX`` escapes so the source stays
#: ASCII-clean; the rendered marks are pending=hollow-dotted-circle,
#: claimed=half-filled-circle, running=filled-diamond,
#: closed=filled-circle, failed=multiplication-x. The ascii column is
#: deconflicted off the bar glyphs (see the module docstring): closed is
#: ``@`` not ``#`` and pending is ``o`` not ``-``.
_LIFECYCLE: dict[Sigil, tuple[str, str]] = {
    Sigil.PENDING: ("\u25cc", "o"),  # hollow dotted circle
    Sigil.CLAIMED: ("\u25d0", "("),  # half-filled circle (left)
    Sigil.RUNNING: ("\u25c6", "*"),  # filled diamond
    Sigil.CLOSED: ("\u25cf", "@"),  # filled circle
    Sigil.FAILED: ("\u2715", "x"),  # multiplication x
}

#: Chrome / action shapes: role string -> ``(unicode, ascii)``. The
#: unicode column uses ``\uXXXX`` escapes to keep the source ASCII-clean;
#: the rendered marks are dispatch=heavy-right-angle-quote,
#: gate=square-with-rounded-corners-lozenge, attention=up-triangle,
#: harmony=almost-equal, overview=identical-to (triple bar),
#: runtime=dollar, check_on=square-with-fill, check_off=hollow-square.
_CHROME: dict[str, tuple[str, str]] = {
    "dispatch": ("\u276f", ">"),  # heavy right-pointing angle quote
    "gate": ("\u2394", "[]"),  # software-function / lozenge
    "attention": ("\u25b3", "!"),  # white up-pointing triangle
    "harmony": ("\u2248", "~"),  # almost equal to
    "overview": ("\u2261", "="),  # identical to (triple bar)
    "runtime": ("$", "$"),  # dollar (same in both columns)
    "check_on": ("\u25a3", "[x]"),  # square with fill
    "check_off": ("\u25a2", "[ ]"),  # hollow square
}

#: :class:`Sigil` -> the :data:`~eawf.surfaces.tui.widgets.status_tint.STATUS_COLOURS`
#: key its tint resolves against. Every member maps to its own value
#: EXCEPT :attr:`Sigil.RUNNING`, whose lifecycle-status string is
#: ``"in_progress"`` while its human-facing enum value is ``"running"``.
#: Kept private here so the COLOUR layer's public API stays unchanged.
_TINT_KEY: dict[Sigil, str] = {
    Sigil.PENDING: "pending",
    Sigil.CLAIMED: "claimed",
    Sigil.RUNNING: "in_progress",
    Sigil.CLOSED: "closed",
    Sigil.FAILED: "failed",
}


def _column(unicode_glyph: str, ascii_glyph: str, *, mode: str) -> str:
    """Return the ASCII or unicode glyph from a ``(unicode, ascii)`` pair.

    Selection is binary: ``mode == "ascii"`` returns the ASCII column;
    any other label returns the unicode column. There is no third state,
    so the helper stays robust to both unicode labels (``"unicode"`` and
    the legacy ``"braille"`` alias) without coupling to whichever name is
    current.

    Args:
        unicode_glyph: The unicode column glyph.
        ascii_glyph: The ASCII column glyph.
        mode: The App's resolved render-mode label.

    Returns:
        The ASCII glyph when *mode* is ``"ascii"``, else the unicode glyph.
    """
    return ascii_glyph if mode == ASCII_MODE else unicode_glyph


def glyph(sigil: Sigil, *, mode: str) -> str:
    """Return the lifecycle *sigil*'s glyph in the active render *mode*.

    Args:
        sigil: The lifecycle-state mark to render.
        mode: The App's resolved render-mode label -- ``"ascii"`` selects
            the ASCII column; any other value (``"unicode"`` or the legacy
            ``"braille"`` alias) selects the unicode column.

    Returns:
        The single-cell glyph string for *sigil* in the resolved column.

    Raises:
        KeyError: If *sigil* is not a member of :class:`Sigil` (an enum
            that drifted past the :data:`_LIFECYCLE` table).
    """
    unicode_glyph, ascii_glyph = _LIFECYCLE[sigil]
    return _column(unicode_glyph, ascii_glyph, mode=mode)


def chrome(role: str, *, mode: str) -> str:
    """Return the chrome *role*'s glyph in the active render *mode*.

    Args:
        role: The chrome / action role -- one of ``"dispatch"`` /
            ``"gate"`` / ``"attention"`` / ``"harmony"`` / ``"overview"``
            / ``"runtime"`` / ``"check_on"`` / ``"check_off"``.
        mode: The App's resolved render-mode label -- ``"ascii"`` selects
            the ASCII column; any other value (``"unicode"`` or the legacy
            ``"braille"`` alias) selects the unicode column.

    Returns:
        The glyph string for *role* in the resolved column.

    Raises:
        KeyError: If *role* is not a known chrome role.
    """
    unicode_glyph, ascii_glyph = _CHROME[role]
    return _column(unicode_glyph, ascii_glyph, mode=mode)


def tint(sigil: Sigil) -> str | None:
    """Return the Wong status tint for *sigil*, or ``None`` when unmapped.

    Delegates to
    :func:`~eawf.surfaces.tui.widgets.status_tint.status_colour` so colour
    stays single-homed in the COLOUR layer. The :class:`Sigil` member is
    first mapped to its lifecycle-status string (via :data:`_TINT_KEY`,
    which resolves :attr:`Sigil.RUNNING` to the ``"in_progress"`` status
    key the COLOUR layer carries) and the tint is read off that string,
    so :func:`tint(Sigil.CLOSED) <tint>` returns the Wong closed green
    ``#009e73``.

    Args:
        sigil: The lifecycle-state mark whose tint to resolve.

    Returns:
        A concrete ``#rrggbb`` hex string, or ``None`` when the mapped
        status has no tint (the COLOUR layer drifted past its map).

    Raises:
        KeyError: If *sigil* is not a member of :class:`Sigil`.
    """
    status_key = _TINT_KEY[sigil]
    return status_colour(_StatusValue(status_key))


class _StatusValue:
    """A minimal ``.value``-bearing shim for :func:`status_colour`.

    :func:`~eawf.surfaces.tui.widgets.status_tint.status_colour` reads its
    argument's ``.value`` attribute (it expects a lifecycle-status enum
    member). Wrapping the bare status key in this shim lets :func:`tint`
    delegate to the COLOUR layer without changing its public API to accept
    a raw string.
    """

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value


__all__ = [
    "ASCII_MODE",
    "Sigil",
    "chrome",
    "glyph",
    "tint",
]
