"""The ``muted`` hint colour clears WCAG 4.5:1 on every theme's backgrounds.

``$muted`` is the header / footer hint colour, and those chassis rows are
painted ``background: $panel`` while the scope body is ``background:
$surface``. The shipped dark ``muted`` (``#6c6c6c``) was tuned against the
darker ``$surface`` and scored only ~2.5:1 against ``$panel``, so the header
hints read as unlabelled grey mush on the band -- legible in a screenshot of
the body, not on the surface they are actually drawn on.

This suite pins the fix as a computed budget rather than a hex snapshot:

1. The dark theme returns the blue-grey ``#828a94``.
2. On EVERY registered theme, ``muted`` clears 4.5:1 against both ``$panel``
   and ``$surface`` -- resolved from the theme's OWN generated colour system,
   so the assertion tracks whatever Textual derives rather than a hardcoded
   background that could silently drift out from under it.
3. The replaced ``#6c6c6c`` still fails that budget, which is the proof this
   suite reds on the real defect instead of merely restating the new palette.

``$panel`` is the tighter of the two backgrounds on the dark themes (it is
lifted off ``$surface``), so it is the constraint that actually binds; the
themes pin ``panel`` explicitly for exactly that reason -- see
:mod:`eawf.surfaces.tui.theme`.
"""

from __future__ import annotations

import pytest
from textual.theme import Theme

from eawf.surfaces.tui.theme import EA_DARK, EA_THEMES

from ._contrast import contrast_ratio, relative_luminance

#: WCAG 2.x AA minimum for normal-size body text.
_AA_NORMAL_TEXT: float = 4.5

#: The dark ``muted`` this wave replaced. Kept as the falsifier: it must
#: still FAIL the budget on the dark panel, or the budget proves nothing.
_SUPERSEDED_DARK_MUTED: str = "#6c6c6c"


def _backgrounds(theme: Theme) -> dict[str, str]:
    """Resolve a theme's generated ``panel`` / ``surface`` background hexes.

    Args:
        theme: A registered :class:`~textual.theme.Theme`.

    Returns:
        ``{"panel": "#rrggbb", "surface": "#rrggbb"}`` as Textual generates
        them for that theme.
    """
    generated = theme.to_color_system().generate()
    return {"panel": generated["panel"], "surface": generated["surface"]}


def test_dark_muted_is_the_blue_grey_replacement() -> None:
    """``ea-dark`` returns ``#828a94``, not the superseded flat grey."""
    assert EA_DARK.variables["muted"] == "#828a94"
    assert EA_DARK.variables["muted"] != _SUPERSEDED_DARK_MUTED


@pytest.mark.parametrize("theme", EA_THEMES, ids=lambda theme: theme.name)
@pytest.mark.parametrize("background", ["panel", "surface"])
def test_muted_clears_aa_contrast_on_every_theme_background(theme: Theme, background: str) -> None:
    """``muted`` scores at least 4.5:1 against the theme's panel and surface."""
    muted = theme.variables["muted"]
    behind = _backgrounds(theme)[background]
    ratio = contrast_ratio(muted, behind)
    assert ratio >= _AA_NORMAL_TEXT, (
        f"{theme.name} muted {muted} on ${background} {behind} "
        f"scores {ratio:.2f}:1, below {_AA_NORMAL_TEXT}:1"
    )


def test_superseded_dark_muted_still_fails_the_panel_budget() -> None:
    """The replaced ``#6c6c6c`` fails on the dark panel -- the gate can red.

    Without this the 4.5:1 assertions above could pass on a palette that
    never had a contrast problem, and the suite would be a tautology.
    """
    panel = _backgrounds(EA_DARK)["panel"]
    assert contrast_ratio(_SUPERSEDED_DARK_MUTED, panel) < _AA_NORMAL_TEXT


@pytest.mark.parametrize("theme", EA_THEMES, ids=lambda theme: theme.name)
def test_panel_is_pinned_not_derived_from_primary(theme: Theme) -> None:
    """Each theme pins ``panel``, so the ring colour cannot move the band.

    Textual derives an unpinned ``panel`` as ``surface.blend(primary, 0.1)``
    (plus a dark-theme boost), which would chain the band background to the
    focus-ring colour and re-open the contrast hole the moment ``primary``
    is retuned.
    """
    assert theme.panel is not None
    assert _backgrounds(theme)["panel"].lower() == theme.panel.lower()


@pytest.mark.parametrize("theme", EA_THEMES, ids=lambda theme: theme.name)
def test_panel_and_surface_stay_distinguishable_planes(theme: Theme) -> None:
    """The band never collapses onto the body: panel and surface differ.

    A panel darkened purely to buy contrast headroom would eventually meet
    ``surface`` and erase the chassis depth cue, so the two are pinned
    apart.
    """
    backgrounds = _backgrounds(theme)
    assert backgrounds["panel"].lower() != backgrounds["surface"].lower()
    assert relative_luminance(backgrounds["panel"]) != relative_luminance(backgrounds["surface"])


def test_contrast_ratio_boundaries_are_one_and_twentyone() -> None:
    """Identical colours score 1.0; black against white scores the 21.0 max."""
    assert contrast_ratio("#828a94", "#828a94") == pytest.approx(1.0)
    assert contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast_ratio("#ffffff", "#000000") == pytest.approx(21.0)


def test_relative_luminance_endpoints_are_zero_and_one() -> None:
    """Black is 0.0 and white is 1.0; the ``#`` prefix is optional."""
    assert relative_luminance("#000000") == pytest.approx(0.0)
    assert relative_luminance("#ffffff") == pytest.approx(1.0)
    assert relative_luminance("FFFFFF") == pytest.approx(1.0)


def test_relative_luminance_rejects_a_non_hex_channel() -> None:
    """A malformed channel raises ``ValueError`` rather than scoring silently."""
    with pytest.raises(ValueError):
        relative_luminance("#gggggg")


def test_relative_luminance_rejects_a_truncated_colour() -> None:
    """A short hex raises ``ValueError`` instead of reading a partial channel."""
    with pytest.raises(ValueError):
        relative_luminance("#82")
