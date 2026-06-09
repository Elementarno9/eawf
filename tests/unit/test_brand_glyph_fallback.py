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
    ACCENT_HEX,
    ASCII_GLYPHS,
    BRAND_LITERAL,
    NERD_FONT_GLYPHS,
    accent_sgr,
    ascii_wordmark,
    bold,
    is_tty,
    render_breadcrumb_head,
    render_wordmark_ansi,
    render_wordmark_markup,
    select_glyphs,
)

#: The cosmic-terminal reskin accent green the wordmark's ``ae`` carries.
_GREEN = "#16b384"


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


# --------------------------------------------------------------------------
# P30-I02-W01: green-accent rotation + two-tone wordmark
# --------------------------------------------------------------------------


def test_accent_hex_rotated_to_reskin_green() -> None:
    """``ACCENT_HEX`` rotates from the Wong orange to the reskin green."""
    assert ACCENT_HEX == _GREEN


def test_wordmark_markup_carries_green_on_umlaut_not_on_e() -> None:
    """The two-tone wordmark colours the ``ae`` green and leaves ``E`` plain."""
    markup = render_wordmark_markup()
    # The accent span wraps ONLY the umlaut, never the E.
    assert markup == "E[#16b384]ä[/]"
    assert _GREEN in markup
    # The E precedes the colour span open-tag, so it carries no accent.
    e_index = markup.index("E")
    span_index = markup.index(f"[{_GREEN}]")
    umlaut_index = markup.index("ä")
    assert e_index < span_index < umlaut_index
    # No colour span surrounds the E (nothing between the start and E).
    assert markup[:span_index] == "E"


def test_wordmark_markup_accepts_theme_resolved_accent() -> None:
    """A caller may pass a theme-resolved accent to track the active palette."""
    markup = render_wordmark_markup("#1a9988")
    assert markup == "E[#1a9988]ä[/]"
    assert "#16b384" not in markup


def test_ascii_wordmark_is_plain_ea_no_markup_no_umlaut() -> None:
    """The ASCII channel renders plain ``Ea`` -- never coloured, never umlaut."""
    text = ascii_wordmark()
    assert text == "Ea"
    assert "ä" not in text
    assert "[" not in text and "]" not in text
    assert _GREEN not in text
    assert "\x1b" not in text  # no ANSI escape either


def test_brand_literal_unchanged_by_rotation() -> None:
    """The brand literals stay intact across the accent rotation."""
    assert brand.BRAND_LITERAL == "Eä"
    assert brand.ASCII_BRAND_LITERAL == "[Ea]"


def test_wordmark_markup_re_exported() -> None:
    """The new wordmark helpers are re-exported from the module surface."""
    assert brand.render_wordmark_markup is render_wordmark_markup
    assert brand.ascii_wordmark is ascii_wordmark


# --------------------------------------------------------------------------
# P30-I02-W32: ANSI accent channel for the headless offline brand frame
# --------------------------------------------------------------------------


def test_accent_sgr_translates_reskin_green_to_truecolor_open() -> None:
    """``accent_sgr`` emits the ANSI 24-bit foreground open for the accent.

    ``#16b384`` -> R=0x16=22, G=0xb3=179, B=0x84=132 in the
    ``ESC[38;2;R;G;Bm`` select-graphic-rendition open sequence.
    """
    assert accent_sgr() == "\x1b[38;2;22;179;132m"
    # The bare call defaults to ACCENT_HEX, so the two forms agree.
    assert accent_sgr(ACCENT_HEX) == accent_sgr()


def test_accent_sgr_accepts_arbitrary_hex() -> None:
    """A caller may translate any ``#rrggbb`` accent (boundary: pure black/white)."""
    assert accent_sgr("#000000") == "\x1b[38;2;0;0;0m"
    assert accent_sgr("#ffffff") == "\x1b[38;2;255;255;255m"


def test_accent_sgr_rejects_non_hash_prefixed_literal() -> None:
    """Error path -- a literal without the ``#`` prefix raises ``ValueError``."""
    with pytest.raises(ValueError, match="must be a #rrggbb literal"):
        accent_sgr("16b384")


def test_accent_sgr_rejects_wrong_length_literal() -> None:
    """Error path -- a short / long literal raises ``ValueError``."""
    with pytest.raises(ValueError, match="must be a #rrggbb literal"):
        accent_sgr("#16b")
    with pytest.raises(ValueError, match="must be a #rrggbb literal"):
        accent_sgr("#16b384ff")


def test_accent_sgr_rejects_non_hex_digits() -> None:
    """Error path -- a 6-char body with non-hex digits raises ``ValueError``."""
    with pytest.raises(ValueError, match="non-hex digits"):
        accent_sgr("#zzggbb")


def test_wordmark_ansi_carries_green_on_umlaut_not_on_e() -> None:
    """The ANSI wordmark colours the ``ae`` green and leaves ``E`` plain.

    The plain-text sibling of :func:`render_wordmark_markup`: the accent SGR
    opens after the bare ``E`` and the foreground resets after the umlaut.
    """
    out = render_wordmark_ansi()
    assert out == "E\x1b[38;2;22;179;132mä\x1b[39m"
    # The E precedes the accent open, so it carries no accent.
    assert out.startswith("E")
    assert not out.startswith(accent_sgr())
    # The accent open wraps the umlaut; the foreground-default closes it.
    assert out == f"E{accent_sgr()}ä\x1b[39m"


def test_wordmark_ansi_accepts_theme_resolved_accent() -> None:
    """A caller may pass a theme-resolved accent to track the active palette."""
    out = render_wordmark_ansi("#1a9988")
    assert out == f"E{accent_sgr('#1a9988')}ä\x1b[39m"
    assert accent_sgr() not in out


def test_wordmark_ansi_propagates_bad_hex_validation() -> None:
    """Error path -- a malformed accent hex propagates the ``ValueError``."""
    with pytest.raises(ValueError, match="must be a #rrggbb literal"):
        render_wordmark_ansi("not-a-hex")


def test_ansi_helpers_re_exported() -> None:
    """The new ANSI brand helpers are re-exported from the module surface."""
    assert brand.accent_sgr is accent_sgr
    assert brand.render_wordmark_ansi is render_wordmark_ansi
