"""The console token-to-surface map renders one parseable rule per surface."""

from __future__ import annotations

import pytest
from textual.css.stylesheet import Stylesheet

from eawf.surfaces.tui.console.token_map import TOKEN_MAP, Channel, SurfaceToken, render_css


def test_token_map_names_every_surface_once() -> None:
    surfaces = [row.surface for row in TOKEN_MAP]
    assert len(surfaces) == len(set(surfaces))


def test_token_map_covers_every_channel() -> None:
    assert {row.channel for row in TOKEN_MAP} == set(Channel)


def test_token_map_paints_the_focus_ring_and_the_frame_on_border_channels() -> None:
    by_surface = {row.surface: row for row in TOKEN_MAP}
    assert (by_surface["frame"].channel, by_surface["frame"].token) == (Channel.BORDER, "border")
    assert (by_surface["focus"].channel, by_surface["focus"].token) == (Channel.BORDER, "primary")


def test_surface_token_css_rule_per_channel() -> None:
    assert SurfaceToken("hint", Channel.COLOR, "muted", None).css_rule() == (
        ".console-hint { color: $muted; }"
    )
    assert SurfaceToken("band", Channel.BACKGROUND, "panel", None).css_rule() == (
        ".console-band { background: $panel; }"
    )
    assert SurfaceToken("frame", Channel.BORDER, "border", ".blk").css_rule() == (
        ".console-frame { border: round $border; }"
    )


@pytest.mark.parametrize("token", ["", "$accent"])
def test_surface_token_rejects_a_token_that_is_not_a_bare_name(token: str) -> None:
    with pytest.raises(ValueError, match="is not a bare name"):
        SurfaceToken("brand", Channel.COLOR, token, None)


def test_surface_token_rejects_an_empty_surface() -> None:
    with pytest.raises(ValueError, match="is empty"):
        SurfaceToken("", Channel.COLOR, "accent", None)


def test_render_css_empty_map_renders_nothing() -> None:
    assert render_css(()) == ""


def test_render_css_single_row_renders_its_rule() -> None:
    row = SurfaceToken("brand", Channel.COLOR, "accent", ".canvas .brand")
    assert render_css((row,)) == row.css_rule()


def test_render_css_keeps_row_order() -> None:
    lines = render_css(TOKEN_MAP).splitlines()
    assert lines == [row.css_rule() for row in TOKEN_MAP]


def test_render_css_rejects_a_surface_mapped_twice() -> None:
    row = SurfaceToken("brand", Channel.COLOR, "accent", None)
    with pytest.raises(ValueError, match="mapped twice"):
        render_css((row, SurfaceToken("brand", Channel.COLOR, "primary", None)))


def test_render_css_output_parses_as_textual_css() -> None:
    variables = {row.token: "#000000" for row in TOKEN_MAP}
    stylesheet = Stylesheet(variables=variables)
    stylesheet.add_source(render_css(TOKEN_MAP), read_from=("token_map", ""))
    stylesheet.parse()
    assert len(stylesheet.rules) == len(TOKEN_MAP)
