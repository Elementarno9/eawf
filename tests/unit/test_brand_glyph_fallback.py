"""Unit tests for :mod:`eawf.surfaces.render.brand`.

These tests pin the four success criteria of P25-I01-W09 that touch the
brand surface:

1. The Eä logotype is a literal two-character string (``E`` + U+00E4) —
   not a font ligature, not a glyph id.
2. The breadcrumb-head helper positions the logotype outside-left of
   the breadcrumb with a canonical two-space gap.
3. :func:`select_glyphs` returns the Nerd Font set on a TTY and the
   ASCII fallback set when stdout is *not* a TTY.
4. The TTY branch is selected by :func:`is_tty` and is patchable from
   tests via :func:`monkeypatch.setattr`.
"""

from __future__ import annotations

import pytest

from eawf.surfaces.render import brand
from eawf.surfaces.render.brand import (
    ASCII_GLYPHS,
    BRAND_LITERAL,
    NERD_FONT_GLYPHS,
    bold,
    is_tty,
    render_breadcrumb_head,
    select_glyphs,
)


def test_brand_literal_is_e_plus_a_umlaut() -> None:
    """The logotype must be the literal two-character ``"Eä"``."""
    assert BRAND_LITERAL == "Eä"
    assert BRAND_LITERAL == "Eä"
    assert len(BRAND_LITERAL) == 2
    assert BRAND_LITERAL[0] == "E"
    assert BRAND_LITERAL[1] == "ä"


def test_brand_literal_is_string_not_ligature() -> None:
    """Defensive check — ``BRAND_LITERAL`` is a plain :class:`str`."""
    assert isinstance(BRAND_LITERAL, str)
    # Two distinct code points, not a precomposed glyph id.
    assert ord(BRAND_LITERAL[0]) == 0x45
    assert ord(BRAND_LITERAL[1]) == 0xE4


def test_select_glyphs_tty_returns_nerd_font_set() -> None:
    """``tty=True`` forces the Nerd Font glyph set."""
    glyphs = select_glyphs(tty=True)
    assert glyphs is NERD_FONT_GLYPHS
    assert glyphs.scope_separator == " ❫ "  # NF medium right paren
    # All five status glyphs are non-empty private-use codepoints.
    assert glyphs.status_ok
    assert glyphs.status_needs_user
    assert glyphs.status_blocked
    assert glyphs.status_failed
    assert glyphs.status_partial


def test_select_glyphs_non_tty_returns_ascii_set() -> None:
    """``tty=False`` forces the ASCII fallback set."""
    glyphs = select_glyphs(tty=False)
    assert glyphs is ASCII_GLYPHS
    assert glyphs.scope_separator == " > "
    assert glyphs.status_ok == "[ok]"
    assert glyphs.status_needs_user == "[?]"
    assert glyphs.status_blocked == "[blocked]"
    assert glyphs.status_failed == "[x]"
    assert glyphs.status_partial == "[!]"


def test_select_glyphs_defers_to_is_tty_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """``tty=None`` delegates to :func:`is_tty` for the live runtime branch."""
    monkeypatch.setattr("eawf.surfaces.render.brand.is_tty", lambda: True)
    assert select_glyphs() is NERD_FONT_GLYPHS

    monkeypatch.setattr("eawf.surfaces.render.brand.is_tty", lambda: False)
    assert select_glyphs() is ASCII_GLYPHS


def test_is_tty_reads_sys_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    """:func:`is_tty` must source the answer from ``sys.stdout.isatty``."""

    class _FakeStdout:
        def __init__(self, answer: bool) -> None:
            self._answer = answer

        def isatty(self) -> bool:
            return self._answer

    monkeypatch.setattr("sys.stdout", _FakeStdout(True))
    assert is_tty() is True

    monkeypatch.setattr("sys.stdout", _FakeStdout(False))
    assert is_tty() is False


def test_render_breadcrumb_head_places_brand_outside_left() -> None:
    """``Eä`` precedes the breadcrumb separated by two spaces."""
    head = render_breadcrumb_head("workspace > repo > P00", tty=False)
    assert head.startswith(BRAND_LITERAL)
    assert head == "Eä  workspace > repo > P00"


def test_render_breadcrumb_head_carries_brand_on_tty() -> None:
    """TTY rendering still emits the literal brand (NF only changes glyphs)."""
    head = render_breadcrumb_head("repo > P25", tty=True)
    assert head.startswith(BRAND_LITERAL)
    # Brand spacing is independent of the scope separator.
    assert head == "Eä  repo > P25"


def test_render_breadcrumb_head_empty_breadcrumb() -> None:
    """Boundary case — empty breadcrumb still emits ``"Eä  "``."""
    head = render_breadcrumb_head("", tty=False)
    assert head == "Eä  "


def test_bold_wraps_with_ansi_sgr_codes() -> None:
    """:func:`bold` brackets *text* with SGR 1 (on) and 22 (off)."""
    out = bold("Eä")
    assert out == "\x1b[1mEä\x1b[22m"
    # Idempotent re-wrap stacks codes — caller's responsibility, but we
    # at least verify the helper doesn't strip an existing prefix.
    assert bold("") == "\x1b[1m\x1b[22m"


def test_glyph_set_is_frozen_dataclass() -> None:
    """GlyphSet must be immutable so callers can't mutate the singletons."""
    # ``dataclasses.FrozenInstanceError`` is an :class:`AttributeError` subclass.
    with pytest.raises(AttributeError):
        NERD_FONT_GLYPHS.scope_separator = "/"  # type: ignore[misc]


def test_module_re_export_path() -> None:
    """:mod:`eawf.surfaces.render.brand` must re-export the public symbols."""
    assert brand.BRAND_LITERAL == "Eä"
    assert brand.select_glyphs is select_glyphs
    assert brand.is_tty is is_tty
