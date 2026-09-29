"""The cursor ground: a neutral lift that shows, under which every text tone stays readable.

The light theme's raised panel sits 1.03:1 off its surface, too close to see, and every
light tone already sits near 4.5:1 on that surface, so a darker ground would drop one of
them below it; the light cursor ground is the white panel, lifting the row about 1.09:1.
The dark cursor ground is a text mix lifting the row about 1.10:1, the most it can lift
while the error tone keeps 4.5:1 on it.
"""

from __future__ import annotations

import pytest
from textual.theme import Theme

from eawf.surfaces.tui.chassis.theme import EA_CB, EA_DARK, EA_LIGHT, mix
from eawf.surfaces.tui.console.token_map import SURFACES, TOKEN_MAP, Channel, SurfaceToken
from tests.tui._contrast import contrast_ratio

# WCAG AA for body text.
AA = 4.5
# The grounds a console text run is drawn on inside the body: the canvas and the cursor row.
GROUNDS = ("surface", "cursor-ground")
TEXT_SURFACES = [row for row in TOKEN_MAP if row.channel is Channel.COLOR]


def test_the_cursor_surface_paints_the_cursor_ground() -> None:
    row = SURFACES["cursor"]
    assert (row.channel, row.token) == (Channel.BACKGROUND, "cursor-ground")


def test_the_light_cursor_ground_is_the_white_panel_and_lifts_off_the_surface() -> None:
    light = EA_LIGHT.variables
    assert light["cursor-ground"] == light["panel"] == "#ffffff"
    assert contrast_ratio(light["cursor-ground"], light["surface"]) == pytest.approx(1.09, abs=0.01)
    assert contrast_ratio(light["panel-2"], light["surface"]) < 1.05, "the raised panel is unseen"


@pytest.mark.parametrize("theme", [EA_DARK, EA_CB], ids=["dark", "cb"])
def test_a_dark_cursor_ground_is_a_text_mix_lifting_about_one_ten(theme: Theme) -> None:
    tones = theme.variables
    assert tones["cursor-ground"] == mix(tones["foreground"], tones["surface"], 0.06)
    assert contrast_ratio(tones["cursor-ground"], tones["surface"]) == pytest.approx(1.10, abs=0.01)


@pytest.mark.parametrize("ground", GROUNDS)
@pytest.mark.parametrize("row", TEXT_SURFACES, ids=[row.surface for row in TEXT_SURFACES])
@pytest.mark.parametrize("theme", [EA_DARK, EA_LIGHT], ids=["dark", "light"])
def test_every_text_surface_meets_aa_on_the_canvas_and_the_cursor_row(
    theme: Theme, row: SurfaceToken, ground: str
) -> None:
    tones = theme.variables
    ratio = contrast_ratio(tones[row.token], tones[ground])
    assert ratio >= AA, f"{row.surface} ({row.token}) is {ratio:.2f}:1 on {ground}"


def test_the_raised_dark_panel_would_drop_the_error_tone_below_aa() -> None:
    dark = EA_DARK.variables
    assert (
        contrast_ratio(dark["err"], dark["panel-2"])
        < AA
        <= contrast_ratio(dark["err"], dark["cursor-ground"])
    )


@pytest.mark.parametrize("share", [-0.01, 1.01])
def test_a_mix_share_outside_zero_to_one_is_refused(share: float) -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        mix("#000000", "#ffffff", share)
