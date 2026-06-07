"""Unit tests for the block-eighths bar renderable (P29-I13-W20).

Pins :func:`~eawf.surfaces.render.bars.block_eighths_glyph` (the single-cell
primitive) and :func:`~eawf.surfaces.render.bars.render_block_bar` (the
multi-cell composition): the ratio-to-glyph map, the exact-eighth boundaries,
the round-half-up midpoints, and the out-of-range error path.
"""

from __future__ import annotations

import pytest

from eawf.surfaces.render.bars import (
    BLOCK_EIGHTHS,
    BLOCK_EMPTY,
    BLOCK_FULL,
    EIGHTHS,
    block_eighths_glyph,
    render_block_bar,
)

# --------------------------------------------------------------------------
# block_eighths_glyph -- the single-cell primitive
# --------------------------------------------------------------------------


def test_block_eighths_glyph_zero_is_empty() -> None:
    """A 0.0 ratio rounds to the blank empty cell."""
    assert block_eighths_glyph(0.0) == BLOCK_EMPTY


def test_block_eighths_glyph_one_is_full() -> None:
    """A 1.0 ratio rounds to the full block."""
    assert block_eighths_glyph(1.0) == BLOCK_FULL


@pytest.mark.parametrize(
    ("eighth", "expected"),
    [(i + 1, BLOCK_EIGHTHS[i]) for i in range(EIGHTHS)],
)
def test_block_eighths_glyph_exact_eighth_boundaries(eighth: int, expected: str) -> None:
    """Each exact eighth lands on its own block-eighths glyph."""
    assert block_eighths_glyph(eighth / EIGHTHS) == expected


def test_block_eighths_glyph_midpoint_rounds_up() -> None:
    """A midpoint between two eighths rounds up to the higher glyph.

    1.5/8 sits exactly between the one-eighth and two-eighths glyphs; the
    round-half-up rule lands it on the two-eighths glyph.
    """
    assert block_eighths_glyph(1.5 / EIGHTHS) == BLOCK_EIGHTHS[1]


def test_block_eighths_glyph_first_cell_lights_at_half_eighth() -> None:
    """The first sub-glyph lights once the ratio reaches half an eighth.

    Just below half an eighth rounds to empty; at exactly half it rounds up
    to the one-eighth glyph.
    """
    assert block_eighths_glyph(0.45 / EIGHTHS) == BLOCK_EMPTY
    assert block_eighths_glyph(0.5 / EIGHTHS) == BLOCK_EIGHTHS[0]


@pytest.mark.parametrize("bad", [-0.01, -1.0, 1.01, 2.0])
def test_block_eighths_glyph_out_of_range_raises(bad: float) -> None:
    """A ratio outside ``[0, 1]`` raises ``ValueError``."""
    with pytest.raises(ValueError, match="ratio out of range"):
        block_eighths_glyph(bad)


# --------------------------------------------------------------------------
# render_block_bar -- the multi-cell composition
# --------------------------------------------------------------------------


def test_render_block_bar_zero_is_all_empty() -> None:
    """A 0.0 ratio renders an all-blank bar of the requested width."""
    assert render_block_bar(0.0, width=10) == BLOCK_EMPTY * 10


def test_render_block_bar_one_is_all_full() -> None:
    """A 1.0 ratio renders an all-full bar of the requested width."""
    assert render_block_bar(1.0, width=10) == BLOCK_FULL * 10


def test_render_block_bar_half_fills_half_the_cells() -> None:
    """A 0.5 ratio fills exactly half the cells with full blocks."""
    assert render_block_bar(0.5, width=10) == BLOCK_FULL * 5 + BLOCK_EMPTY * 5


def test_render_block_bar_partial_boundary_cell() -> None:
    """A fractional fill renders a partial-eighths glyph at the boundary.

    At ratio 0.125 over a 4-cell bar the fill is half a cell -- four eighths
    -- so the boundary cell carries the four-eighths glyph and the rest stay
    blank.
    """
    bar = render_block_bar(0.125, width=4)
    assert bar == BLOCK_EIGHTHS[3] + BLOCK_EMPTY * 3


def test_render_block_bar_length_equals_width() -> None:
    """The rendered bar is always exactly *width* cells wide."""
    for ratio in (0.0, 0.13, 0.5, 0.77, 1.0):
        assert len(render_block_bar(ratio, width=7)) == 7


def test_render_block_bar_agrees_with_single_cell_primitive() -> None:
    """A 1-cell bar matches the single-cell glyph primitive exactly."""
    for ratio in (0.0, 0.0625, 0.125, 0.5, 0.9375, 1.0):
        assert render_block_bar(ratio, width=1) == block_eighths_glyph(ratio)


@pytest.mark.parametrize("bad", [-0.01, 1.01])
def test_render_block_bar_out_of_range_raises(bad: float) -> None:
    """A ratio outside ``[0, 1]`` raises ``ValueError``."""
    with pytest.raises(ValueError, match="ratio out of range"):
        render_block_bar(bad, width=10)


@pytest.mark.parametrize("bad_width", [0, -1])
def test_render_block_bar_non_positive_width_raises(bad_width: int) -> None:
    """A non-positive width raises ``ValueError``."""
    with pytest.raises(ValueError, match="width must be positive"):
        render_block_bar(0.5, width=bad_width)
