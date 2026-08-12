"""CVD-distinctness gate over the EA_CB lifecycle band swatches.

The colourblind theme's ``status-*`` band hexes must not merely be
byte-stable: a future palette edit that collapses two bands into the same
perceived colour for a colourblind operator must RED a gate. This suite is
the cheap structural backstop behind the ``svg_pixel_diff`` legend golden:
it simulates each EA_CB band through the Vienot 1999 dichromat model
(deuteranopia + protanopia) and asserts every pair stays separated by more
than a collapse epsilon.

Note on the model + the lifecycle design. The IBM colourblind-safe palette
is NOT engineered for wide hue separation under dichromacy -- the lifecycle
layer always pairs colour with a distinct glyph (``- > ~ # x !``), so
colour is additive, not the sole signal. Under the Vienot model several
bands simulate to near-greys (e.g. ``closed`` / ``failed`` / ``pending``
land within ~10 RGB units of each other for a dichromat). The gate's job is
therefore to catch a genuine COLLAPSE (two bands becoming the same colour),
not to assert wide separation. The epsilon is set conservatively below the
current minimum pairwise distance so today's palette passes with margin and
any edit that pushes two bands closer reds the gate.
"""

from __future__ import annotations

from itertools import combinations

import pytest

from eawf.surfaces.tui.theme import _IBM_VARIABLES
from eawf.surfaces.tui.widgets.cvd import (
    CVD_TYPES,
    EA_CB_BANDS,
    colour_distance,
    render_status_legend_svg,
    simulate_cvd,
)

#: Collapse epsilon in 8-bit RGB Euclidean distance. Two simulated swatches
#: within this distance are treated as collapsed. Set well below the current
#: minimum pairwise distance (~9.9 under protanopia) so the present palette
#: passes with margin while a band-merging edit reds the gate.
_COLLAPSE_EPSILON: float = 4.0


def test_ea_cb_bands_sourced_from_ibm_palette() -> None:
    """The legend bands mirror the canonical EA_CB ``status-*`` hexes (DRY)."""
    by_label = dict(EA_CB_BANDS)
    assert by_label["pending"] == _IBM_VARIABLES["status-pending"]
    assert by_label["claimed"] == _IBM_VARIABLES["status-claimed"]
    assert by_label["in_progress"] == _IBM_VARIABLES["status-in-progress"]
    assert by_label["closed"] == _IBM_VARIABLES["status-closed"]
    assert by_label["failed"] == _IBM_VARIABLES["status-failed"]


@pytest.mark.parametrize("cvd_type", CVD_TYPES)
def test_ea_cb_bands_pairwise_distinct_under_cvd(cvd_type: str) -> None:
    """No two EA_CB bands collapse under deuteranopia / protanopia.

    Simulates all five lifecycle band hexes through *cvd_type* and asserts
    every pair stays separated by more than :data:`_COLLAPSE_EPSILON`. A
    future palette edit that merges two bands drops a pair below the epsilon
    and reds this gate.
    """
    simulated = {label: simulate_cvd(hex_colour, cvd_type) for label, hex_colour in EA_CB_BANDS}
    for (label_a, _hex_a), (label_b, _hex_b) in combinations(EA_CB_BANDS, 2):
        distance = colour_distance(simulated[label_a], simulated[label_b])
        assert distance > _COLLAPSE_EPSILON, (
            f"{cvd_type}: bands {label_a!r} and {label_b!r} collapse "
            f"(distance={distance:.2f} <= epsilon={_COLLAPSE_EPSILON})"
        )


@pytest.mark.parametrize("cvd_type", CVD_TYPES)
def test_simulate_cvd_is_idempotent_for_neutral_grey(cvd_type: str) -> None:
    """A neutral grey is invariant under the dichromat projection.

    Vienot 1999 maps the achromatic axis to itself, so a pure grey
    simulates to (within rounding of) the same grey -- a sanity anchor that
    the matrices were transcribed in the right space.
    """
    simulated = simulate_cvd("#808080", cvd_type)
    assert colour_distance(simulated, "#808080") <= 2.0


def test_simulate_cvd_rejects_unknown_type() -> None:
    """An unrecognised CVD type raises ValueError (never silently passes)."""
    with pytest.raises(ValueError, match="unknown cvd type"):
        simulate_cvd("#16b384", "tritanopia")


def test_simulate_cvd_rejects_malformed_hex() -> None:
    """A non-``#rrggbb`` colour raises ValueError at the boundary."""
    with pytest.raises(ValueError, match="#rrggbb"):
        simulate_cvd("16b384", "deuteranopia")
    with pytest.raises(ValueError, match="#rrggbb"):
        simulate_cvd("#abc", "deuteranopia")


def test_colour_distance_zero_for_identical() -> None:
    """Identical colours have zero distance; black-white is the diagonal max."""
    assert colour_distance("#16b384", "#16b384") == 0.0
    assert colour_distance("#000000", "#ffffff") == pytest.approx(441.6729, abs=1e-3)


def test_legend_svg_has_one_rect_per_band() -> None:
    """The legend SVG renders one ``<rect>`` swatch per band, in order."""
    svg = render_status_legend_svg()
    assert svg.count("<rect") == len(EA_CB_BANDS)
    # Each band hex appears as a fill in lifecycle order.
    last_index = -1
    for _label, hex_colour in EA_CB_BANDS:
        index = svg.index(f'fill="{hex_colour}"')
        assert index > last_index, f"band {hex_colour} out of order in legend SVG"
        last_index = index
    # Well-formed shell.
    assert svg.startswith("<?xml")
    assert svg.rstrip().endswith("</svg>")
