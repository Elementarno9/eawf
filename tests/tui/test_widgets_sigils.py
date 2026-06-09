"""Unit tests for the SHAPE-layer ``widgets/sigils.py`` helper.

Covers the two glyph columns (unicode vs ascii) across all five
lifecycle sigils and all eight chrome roles, the COLOUR-layer delegation
(``tint`` resolves the Wong status hex via ``status_colour``, including
the ``RUNNING -> in_progress`` key remap), the binary mode selection (any
non-``"ascii"`` label resolves the unicode column, decoupling the helper
from the not-yet-landed render-mode rename), and the deconfliction
regression invariant (the ascii sigil alphabet shares no character with
the EU / burn bar glyphs).

The module is PURE -- no Textual primitive, no daemon -- so these tests
mount nothing and need no lock.
"""

from __future__ import annotations

import pytest

from eawf.surfaces.tui.app import resolve_render_mode
from eawf.surfaces.tui.theme import WONG_VARIABLES
from eawf.surfaces.tui.widgets.eu_bar import GLYPH_EMPTY, GLYPH_FULL
from eawf.surfaces.tui.widgets.sigils import (
    Sigil,
    chrome,
    glyph,
    tint,
)

# The expected rendered glyphs, written as the actual code points so the
# test pins the real marks (the source uses \uXXXX escapes to stay ASCII).
_LIFECYCLE_UNICODE: dict[Sigil, str] = {
    Sigil.PENDING: "\u25cc",  # hollow dotted circle
    Sigil.CLAIMED: "\u25d0",  # half-filled circle
    Sigil.RUNNING: "\u25c6",  # filled diamond
    Sigil.CLOSED: "\u25cf",  # filled circle
    Sigil.FAILED: "\u2715",  # multiplication x
}
_LIFECYCLE_ASCII: dict[Sigil, str] = {
    Sigil.PENDING: "o",
    Sigil.CLAIMED: "(",
    Sigil.RUNNING: "*",
    Sigil.CLOSED: "@",
    Sigil.FAILED: "x",
}

_CHROME_UNICODE: dict[str, str] = {
    "dispatch": "\u276f",
    "gate": "\u2394",
    "attention": "\u25b3",
    "harmony": "\u2248",
    "overview": "\u2261",
    "runtime": "$",
    "check_on": "\u25a3",
    "check_off": "\u25a2",
}
_CHROME_ASCII: dict[str, str] = {
    "dispatch": ">",
    "gate": "[]",
    "attention": "!",
    "harmony": "~",
    "overview": "=",
    "runtime": "$",
    "check_on": "[x]",
    "check_off": "[ ]",
}


# --------------------------------------------------------------------------
# Criterion 1 -- lifecycle glyph correctness across all five states + modes
# --------------------------------------------------------------------------


def test_glyph_closed_unicode_is_filled_circle() -> None:
    assert glyph(Sigil.CLOSED, mode="unicode") == "\u25cf"


def test_glyph_closed_ascii_is_at_sign() -> None:
    # Deconflicted off the bar full glyph '#': closed sigil is '@'.
    assert glyph(Sigil.CLOSED, mode="ascii") == "@"


@pytest.mark.parametrize("sigil", list(Sigil))
def test_glyph_unicode_column_for_every_state(sigil: Sigil) -> None:
    assert glyph(sigil, mode="unicode") == _LIFECYCLE_UNICODE[sigil]


@pytest.mark.parametrize("sigil", list(Sigil))
def test_glyph_ascii_column_for_every_state(sigil: Sigil) -> None:
    assert glyph(sigil, mode="ascii") == _LIFECYCLE_ASCII[sigil]


def test_glyph_pending_ascii_is_o_not_dash() -> None:
    # Deconflicted off the bar empty glyph '-': pending sigil is 'o'.
    assert glyph(Sigil.PENDING, mode="ascii") == "o"
    assert glyph(Sigil.PENDING, mode="ascii") != GLYPH_EMPTY


# --------------------------------------------------------------------------
# Criterion 2 -- chrome glyph correctness for all eight roles + modes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role", list(_CHROME_UNICODE))
def test_chrome_unicode_column_for_every_role(role: str) -> None:
    assert chrome(role, mode="unicode") == _CHROME_UNICODE[role]


@pytest.mark.parametrize("role", list(_CHROME_ASCII))
def test_chrome_ascii_column_for_every_role(role: str) -> None:
    assert chrome(role, mode="ascii") == _CHROME_ASCII[role]


def test_chrome_covers_exactly_eight_roles() -> None:
    # The contract names eight chrome roles; pin the count so a future edit
    # that drops or adds one without a test update is caught.
    assert len(_CHROME_ASCII) == 8


def test_chrome_unknown_role_raises_key_error() -> None:
    with pytest.raises(KeyError):
        chrome("no-such-role", mode="unicode")


# --------------------------------------------------------------------------
# Criterion 3 -- tint delegates to the COLOUR layer (single-homed hue)
# --------------------------------------------------------------------------


def test_tint_closed_is_wong_closed_green() -> None:
    assert tint(Sigil.CLOSED) == "#009e73"


def test_tint_running_resolves_in_progress_key() -> None:
    # Sigil.RUNNING.value is "running" but the lifecycle status string is
    # "in_progress"; tint must remap so it does not fall through to None.
    assert tint(Sigil.RUNNING) == WONG_VARIABLES["status-in-progress"]


@pytest.mark.parametrize(
    ("sigil", "expected"),
    [
        (Sigil.PENDING, WONG_VARIABLES["status-pending"]),
        (Sigil.CLAIMED, WONG_VARIABLES["status-claimed"]),
        (Sigil.RUNNING, WONG_VARIABLES["status-in-progress"]),
        (Sigil.CLOSED, WONG_VARIABLES["status-closed"]),
        (Sigil.FAILED, WONG_VARIABLES["status-failed"]),
    ],
)
def test_tint_resolves_for_every_state(sigil: Sigil, expected: str) -> None:
    assert tint(sigil) == expected


def test_tint_never_none_for_any_sigil() -> None:
    # Every lifecycle sigil resolves a concrete tint (no row goes untinted).
    for sigil in Sigil:
        assert tint(sigil) is not None


# --------------------------------------------------------------------------
# Criterion 4 -- mode binding: any non-ascii label selects the unicode
# column; a non-TTY harness resolves the ascii column.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("unicode_label", ["unicode", "braille"])
def test_glyph_any_non_ascii_label_selects_unicode(unicode_label: str) -> None:
    # The legacy "braille" alias and the canonical "unicode" both resolve
    # the unicode column -- the helper is decoupled from the rename.
    assert glyph(Sigil.CLOSED, mode=unicode_label) == "\u25cf"
    assert chrome("dispatch", mode=unicode_label) == "\u276f"


def test_glyph_unrecognised_label_falls_to_unicode() -> None:
    # There is no third state: any label that is not exactly "ascii" maps to
    # the unicode column (binary selection).
    assert glyph(Sigil.CLOSED, mode="garbage") == "\u25cf"


def test_non_tty_harness_resolves_ascii_column() -> None:
    # When the app resolves "ascii" (the non-TTY / CI / Braille-less path:
    # resolve_render_mode("ascii", ...) or a failed coverage probe), the
    # helper gives the ascii column.
    mode = resolve_render_mode("ascii", braille_ok=False)
    assert mode == "ascii"
    assert glyph(Sigil.CLOSED, mode=mode) == "@"
    assert chrome("gate", mode=mode) == "[]"

    # The auto policy with a failed coverage probe (a non-TTY / Braille-less
    # terminal) also resolves ascii, and the helper honours it.
    auto_mode = resolve_render_mode("auto", braille_ok=False)
    assert auto_mode == "ascii"
    assert glyph(Sigil.PENDING, mode=auto_mode) == "o"


# --------------------------------------------------------------------------
# Criterion 5 -- deconfliction regression: ascii sigil alphabet shares no
# character with the EU / burn bar glyphs.
# --------------------------------------------------------------------------


def test_ascii_sigil_alphabet_disjoint_from_bar_glyphs() -> None:
    # The whole ascii sigil alphabet -- lifecycle 'o ( * @ x' plus chrome
    # '> [] ! ~ = $ [x] [ ]' -- must not contain the bar's '#' or '-' so a
    # row that renders a sigil beside an inline bar reads unambiguously in
    # ascii mode.
    ascii_chars: set[str] = set()
    for sigil in Sigil:
        ascii_chars.update(glyph(sigil, mode="ascii"))
    for role in _CHROME_ASCII:
        ascii_chars.update(chrome(role, mode="ascii"))

    bar_chars = set(GLYPH_FULL) | set(GLYPH_EMPTY)
    assert ascii_chars & bar_chars == set()


def test_lifecycle_ascii_chars_exclude_bar_full_and_empty() -> None:
    # Pin the two specific deconflictions the contract calls out: closed is
    # not the bar full glyph, pending is not the bar empty glyph.
    lifecycle_ascii = {glyph(sigil, mode="ascii") for sigil in Sigil}
    assert GLYPH_FULL not in lifecycle_ascii
    assert GLYPH_EMPTY not in lifecycle_ascii
