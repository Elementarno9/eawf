"""Tests for statusline glyph-mode / color-mode resolution.

The render layer resolves the configured ``statusline.glyph_mode`` and
``statusline.color_mode`` policies against a terminal capability probe,
downgrading to ascii / no-colour on a no-color terminal. These tests pin the
auto-downgrade boundary (NO_COLOR env, dumb terminal, non-TTY -> ascii / off;
a colour terminal -> unicode / on), the explicit-policy pass-through, and the
unknown-policy error path.
"""

from __future__ import annotations

import pytest

from eawf.surfaces.render.statusline import (
    resolve_color_mode,
    resolve_glyph_mode,
    terminal_supports_color,
)


def _force_tty(monkeypatch: pytest.MonkeyPatch, *, value: bool) -> None:
    """Pin ``sys.stdout.isatty`` so the probe is deterministic in tests."""
    monkeypatch.setattr("sys.stdout.isatty", lambda: value)


# --- terminal_supports_color -------------------------------------------------


def test_color_capable_on_tty_without_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    # boundary: a real colour terminal -> capable.
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    _force_tty(monkeypatch, value=True)
    assert terminal_supports_color() is True


def test_no_color_env_disables_color(monkeypatch: pytest.MonkeyPatch) -> None:
    # boundary: NO_COLOR set to any value (even empty) -> not capable.
    monkeypatch.setenv("NO_COLOR", "")
    monkeypatch.setenv("TERM", "xterm-256color")
    _force_tty(monkeypatch, value=True)
    assert terminal_supports_color() is False


def test_dumb_terminal_disables_color(monkeypatch: pytest.MonkeyPatch) -> None:
    # boundary: a dumb terminal cannot render colour.
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "dumb")
    _force_tty(monkeypatch, value=True)
    assert terminal_supports_color() is False


def test_empty_term_disables_color(monkeypatch: pytest.MonkeyPatch) -> None:
    # boundary: an empty TERM is treated as non-capable.
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "")
    _force_tty(monkeypatch, value=True)
    assert terminal_supports_color() is False


def test_non_tty_disables_color(monkeypatch: pytest.MonkeyPatch) -> None:
    # boundary: a pipe / file / CI capture is not colour-capable.
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    _force_tty(monkeypatch, value=False)
    assert terminal_supports_color() is False


# --- resolve_glyph_mode ------------------------------------------------------


def test_glyph_auto_downgrades_to_ascii_on_no_color_terminal() -> None:
    # boundary: auto + no-colour terminal -> ascii.
    assert resolve_glyph_mode("auto", color_capable=False) == "ascii"


def test_glyph_auto_selects_unicode_on_color_terminal() -> None:
    # boundary: auto + colour terminal -> unicode.
    assert resolve_glyph_mode("auto", color_capable=True) == "unicode"


def test_glyph_explicit_ascii_passes_through_regardless_of_terminal() -> None:
    assert resolve_glyph_mode("ascii", color_capable=True) == "ascii"
    assert resolve_glyph_mode("ascii", color_capable=False) == "ascii"


def test_glyph_explicit_unicode_passes_through_regardless_of_terminal() -> None:
    assert resolve_glyph_mode("unicode", color_capable=True) == "unicode"
    assert resolve_glyph_mode("unicode", color_capable=False) == "unicode"


def test_glyph_unknown_policy_raises() -> None:
    # error-path: an unknown policy is rejected at the boundary.
    with pytest.raises(ValueError, match="unknown statusline glyph mode"):
        resolve_glyph_mode("braille", color_capable=True)


# --- resolve_color_mode ------------------------------------------------------


def test_color_auto_turns_off_on_no_color_terminal() -> None:
    # boundary: auto + no-colour terminal -> off.
    assert resolve_color_mode("auto", color_capable=False) == "off"


def test_color_auto_turns_on_on_color_terminal() -> None:
    # boundary: auto + colour terminal -> on.
    assert resolve_color_mode("auto", color_capable=True) == "on"


def test_color_always_forces_on_even_on_no_color_terminal() -> None:
    assert resolve_color_mode("always", color_capable=False) == "on"


def test_color_never_forces_off_even_on_color_terminal() -> None:
    assert resolve_color_mode("never", color_capable=True) == "off"


def test_color_unknown_policy_raises() -> None:
    # error-path: an unknown policy is rejected at the boundary.
    with pytest.raises(ValueError, match="unknown statusline color mode"):
        resolve_color_mode("rainbow", color_capable=True)
