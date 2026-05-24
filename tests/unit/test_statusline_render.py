"""Tests for ``eawf.surfaces.render.statusline`` — theme loading + segment rendering."""

from __future__ import annotations

from eawf.surfaces.render.statusline import (
    StatuslineSegment,
    StatuslineTheme,
    load_themes,
    render_segments,
    resolve_theme,
)


def test_load_themes_returns_default_powerline_ascii() -> None:
    themes = load_themes()
    assert "default" in themes
    assert "powerline" in themes
    assert "ascii-fallback" in themes


def test_default_theme_has_ansi_color_codes_with_esc_byte() -> None:
    themes = load_themes()
    default = themes["default"]
    # ESC byte ("\x1b") is prepended at load time so YAML stays printable.
    assert default.colors["ok"].startswith("\x1b[")
    assert default.colors["reset"].startswith("\x1b[")


def test_resolve_theme_falls_back_to_default() -> None:
    themes = load_themes()
    chosen = resolve_theme("does-not-exist", themes)
    assert chosen.name == "default"

    chosen = resolve_theme(None, themes)
    assert chosen.name == "default"


def test_render_segments_joins_with_separator() -> None:
    theme = StatuslineTheme(name="t", separator=" || ")
    segs = [
        StatuslineSegment(module="state", text="state:P04", status="ok"),
        StatuslineSegment(module="git", text="git:main", status="ok"),
    ]
    line = render_segments(segs, theme)
    assert line == "state:P04 || git:main"


def test_render_segments_applies_color_and_reset_when_present() -> None:
    theme = StatuslineTheme(
        name="t",
        separator=" | ",
        colors={"ok": "\x1b[32m", "reset": "\x1b[0m"},
    )
    seg = StatuslineSegment(module="state", text="state:P04", status="ok")
    line = render_segments([seg], theme)
    assert line == "\x1b[32mstate:P04\x1b[0m"


def test_render_segments_skips_failed_when_theme_says_skip() -> None:
    theme_skip = StatuslineTheme(name="ascii", skip_failed=True)
    theme_keep = StatuslineTheme(name="default", skip_failed=False)
    segs = [
        StatuslineSegment(module="state", text="state:P04", status="ok"),
        StatuslineSegment(module="boom", text="boom:!", status="failed"),
    ]
    assert render_segments(segs, theme_skip) == "state:P04"
    assert render_segments(segs, theme_keep) == "state:P04 | boom:!"


def test_render_segments_applies_glyph_prefix_when_set() -> None:
    theme = StatuslineTheme(name="t", glyph={"state": "P"})
    seg = StatuslineSegment(module="state", text="state:P04", status="ok")
    line = render_segments([seg], theme)
    assert line == "P state:P04"
