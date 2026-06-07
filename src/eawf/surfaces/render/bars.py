"""Block-eighths bar renderable for the render + TUI surfaces.

Maps a ``0..1`` completion ratio onto the Unicode block-eighths glyph run
(U+2581 .. U+2588 -- ``a horizontal scale of eight partial-cell heights``)
so a bar reads its fill at one-eighth-cell sub-resolution. A width-``N``
bar resolves to ``N * 8`` fillable sub-units: every whole cell to the left
of the fill front is the full block (U+2588), the boundary cell carries the
partial-eighths glyph for the remainder, and the rest are blank cells.

The single-cell helper :func:`block_eighths_glyph` is the primitive: it
rounds a ``0..1`` ratio onto the nine fill levels (empty plus the eight
partial glyphs) with round-half-up at each eighth boundary, so an exact
eighth lands on its own glyph and a midpoint rounds up to the next. The
multi-cell :func:`render_block_bar` composes that primitive into a
fixed-width run.

This module is the pure render core; it carries no colour and no widget
state. The :mod:`~eawf.surfaces.tui.widgets.progress` widget and the
status-pane / roadmap-tree / workspace-table bars consume it, and
:mod:`~eawf.surfaces.render.mode` flips it to an ASCII fallback when the
render mode demands a Braille-free / block-free glyph set.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: The eight block-eighths glyphs, smallest-to-largest (U+2581 .. U+2588).
#: Index ``i`` (0-based) is the ``(i + 1)``-eighths-filled glyph: index 0 is
#: the one-eighth lower block, index 7 is the full block. A zero-fill cell
#: renders :data:`BLOCK_EMPTY` (a blank cell), not one of these.
BLOCK_EIGHTHS: tuple[str, ...] = ("▁", "▂", "▃", "▄", "▅", "▆", "▇", "█")

#: Number of fill levels one cell encodes -- the eight partial glyphs.
EIGHTHS: int = 8

#: Full-cell glyph (U+2588). Aliases the last block-eighths glyph; named so
#: the whole-cell composition in :func:`render_block_bar` reads clearly.
BLOCK_FULL: str = BLOCK_EIGHTHS[-1]

#: Empty-cell glyph -- a blank space. A zero-fill cell is blank rather than
#: a sub-glyph so the bar's left edge starts at the lowest visible fill.
BLOCK_EMPTY: str = " "

#: Default bar cell count when a caller threads no explicit width.
DEFAULT_WIDTH: int = 10


def block_eighths_glyph(ratio: float) -> str:
    """Return the single-cell block-eighths glyph for *ratio*.

    Rounds *ratio* onto the nine fill levels (empty plus the eight partial
    glyphs in :data:`BLOCK_EIGHTHS`) with round-half-up at each eighth
    boundary. An exact eighth lands on its own glyph; a midpoint between two
    eighths rounds up to the higher glyph. ``0.0`` yields :data:`BLOCK_EMPTY`
    and ``1.0`` yields :data:`BLOCK_FULL`.

    Args:
        ratio: Fill ratio in the closed interval ``[0, 1]``.

    Returns:
        One character: :data:`BLOCK_EMPTY` for a zero-rounded cell, else one
        of the eight glyphs in :data:`BLOCK_EIGHTHS`.

    Raises:
        ValueError: When *ratio* is outside ``[0, 1]``.
    """
    if ratio < 0.0 or ratio > 1.0:
        raise ValueError(f"ratio out of range, expected [0, 1]: {ratio!r}")
    level = int(ratio * EIGHTHS + 0.5)
    if level <= 0:
        return BLOCK_EMPTY
    return BLOCK_EIGHTHS[level - 1]


def render_block_bar(ratio: float, *, width: int = DEFAULT_WIDTH) -> str:
    """Render a block-eighths fill bar of *width* cells for *ratio*.

    Fills left-to-right at one-eighth-cell sub-resolution: a width-*width*
    bar resolves to ``width * EIGHTHS`` fillable sub-units. Every whole cell
    to the left of the fill front is :data:`BLOCK_FULL`, the single boundary
    cell carries the partial-eighths glyph for the remainder (via
    :func:`block_eighths_glyph`), and the rest are :data:`BLOCK_EMPTY`. The
    sub-unit count is round-half-up so the boundary cell's partial glyph
    matches the single-cell primitive.

    Args:
        ratio: Fill ratio in the closed interval ``[0, 1]``.
        width: Bar cell count (> 0). Defaults to :data:`DEFAULT_WIDTH`.

    Returns:
        A glyph run of exactly *width* characters.

    Raises:
        ValueError: When *ratio* is outside ``[0, 1]`` or *width* is not
            positive.
    """
    if ratio < 0.0 or ratio > 1.0:
        raise ValueError(f"ratio out of range, expected [0, 1]: {ratio!r}")
    if width <= 0:
        raise ValueError(f"width must be positive: {width!r}")
    subunits = int(ratio * width * EIGHTHS + 0.5)
    full_cells, remainder = divmod(subunits, EIGHTHS)
    cells = [BLOCK_FULL] * full_cells
    if remainder and full_cells < width:
        cells.append(BLOCK_EIGHTHS[remainder - 1])
    cells += [BLOCK_EMPTY] * (width - len(cells))
    return "".join(cells)


__all__ = [
    "BLOCK_EIGHTHS",
    "BLOCK_EMPTY",
    "BLOCK_FULL",
    "DEFAULT_WIDTH",
    "EIGHTHS",
    "block_eighths_glyph",
    "render_block_bar",
]
