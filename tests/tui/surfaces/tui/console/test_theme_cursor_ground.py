"""The cursor ground: a neutral lift that shows on every theme.

The light theme's raised panel sits 1.03:1 off its surface, too close to see, so the light
cursor ground is a text mix lifting the row about 1.15:1; the dark themes keep the raised
panel, which already lifts 1.13:1.
"""

from __future__ import annotations

import pytest
from textual.theme import Theme

from eawf.surfaces.tui.chassis.theme import EA_CB, EA_DARK, EA_LIGHT, mix
from eawf.surfaces.tui.console.token_map import SURFACES, Channel


def _luminance(colour: str) -> float:
    channels = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a: str, b: str) -> float:
    low, high = sorted((_luminance(a), _luminance(b)))
    return (high + 0.05) / (low + 0.05)


# ---------- V-08: the light cursor ground has a visible lift ----------


def test_v08_the_cursor_surface_paints_the_cursor_ground() -> None:
    row = SURFACES["cursor"]
    assert (row.channel, row.token) == (Channel.BACKGROUND, "cursor-ground")


def test_v08_the_light_cursor_ground_lifts_about_one_fifteen_off_the_surface() -> None:
    light = EA_LIGHT.variables
    assert _contrast(light["cursor-ground"], light["surface"]) == pytest.approx(1.15, abs=0.02)
    assert _contrast(light["panel-2"], light["surface"]) < 1.05, "the panel alone did not show"


@pytest.mark.parametrize("theme", [EA_DARK, EA_CB], ids=["dark", "cb"])
def test_v08_a_dark_cursor_ground_stays_the_raised_panel(theme: Theme) -> None:
    assert theme.variables["cursor-ground"] == theme.variables["panel-2"]


@pytest.mark.parametrize("tone", ["foreground", "muted", "dim", "recede", "warn", "err"])
def test_v08_the_plain_and_severity_tones_stay_readable_on_the_light_cursor_ground(
    tone: str,
) -> None:
    light = EA_LIGHT.variables
    assert _contrast(light[tone], light["cursor-ground"]) >= 4.5


@pytest.mark.parametrize("share", [-0.01, 1.01])
def test_v08_a_mix_share_outside_zero_to_one_is_refused(share: float) -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        mix("#000000", "#ffffff", share)
