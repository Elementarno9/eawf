"""Render-mode flip for the block-eighths bar (unicode vs ascii fallback).

The bar surfaces render in one of two glyph sets: the unicode block-eighths
fill (:mod:`~eawf.surfaces.render.bars`) when the terminal font covers the
block glyphs, and an ASCII fallback (``#``/``-``) when it does not (the
``ui.glyphs=ascii`` policy or a failed coverage probe). This module owns the
single flip between the two so a one-line mode change rerenders every bar
through one code path.

The flip is total: :func:`render_bar` paints the block-eighths bar in
:data:`UNICODE` mode and the ASCII fallback in :data:`ASCII` mode, and
:func:`to_ascii_fallback` maps an already-rendered block-eighths run to its
ASCII fallback glyph-for-glyph (every block-eighths glyph -> the full ASCII
fill, the blank cell -> the empty ASCII fill), so no block glyph survives a
flip to ASCII. The render layer is the canonical home for this map; the TUI
widgets thread the active mode through it.
"""

from __future__ import annotations

import logging
from typing import Literal

from eawf.surfaces.render.bars import BLOCK_EIGHTHS, BLOCK_EMPTY, render_block_bar

logger = logging.getLogger(__name__)

#: The two bar render modes. ``"unicode"`` paints the block-eighths fill;
#: ``"ascii"`` paints the ``#``/``-`` fallback for fonts lacking block-glyph
#: coverage.
RenderMode = Literal["unicode", "ascii"]

#: The unicode (block-eighths) render mode.
UNICODE: RenderMode = "unicode"

#: The ASCII-fallback render mode.
ASCII: RenderMode = "ascii"

#: ASCII fill glyph -- the fallback for every (partial or full) block-eighths
#: cell when the mode flips to :data:`ASCII`.
ASCII_FULL: str = "#"

#: ASCII empty glyph -- the fallback for a blank block-eighths cell.
ASCII_EMPTY: str = "-"


def to_ascii_fallback(block_bar: str) -> str:
    """Map a rendered block-eighths bar to its ASCII fallback, glyph-for-glyph.

    Every block-eighths glyph (partial or full, in :data:`BLOCK_EIGHTHS`)
    flips to :data:`ASCII_FULL`; the blank cell (:data:`BLOCK_EMPTY`) flips
    to :data:`ASCII_EMPTY`. The result carries no block glyph, so a flip to
    ASCII is total -- the swap leaves nothing in the block-glyph set.

    Args:
        block_bar: A bar string rendered in the unicode block-eighths set
            (e.g. from :func:`~eawf.surfaces.render.bars.render_block_bar`).

    Returns:
        The same-width bar with every block cell flipped to its ASCII
        fallback glyph.

    Raises:
        ValueError: When *block_bar* carries a character that is neither a
            block-eighths glyph nor the blank cell (a non-bar string).
    """
    out: list[str] = []
    for ch in block_bar:
        if ch in BLOCK_EIGHTHS:
            out.append(ASCII_FULL)
        elif ch == BLOCK_EMPTY:
            out.append(ASCII_EMPTY)
        else:
            raise ValueError(f"not a block-eighths bar glyph: {ch!r}")
    return "".join(out)


def render_bar(ratio: float, *, mode: RenderMode, width: int) -> str:
    """Render a fill bar for *ratio* in the active render *mode*.

    The single flip between the unicode block-eighths fill and the ASCII
    fallback: :data:`UNICODE` paints the block-eighths bar
    (:func:`~eawf.surfaces.render.bars.render_block_bar`); :data:`ASCII`
    paints the same fill flipped to the ``#``/``-`` glyph set
    (:func:`to_ascii_fallback`), so a one-line mode flip swaps every bar.

    Args:
        ratio: Fill ratio in the closed interval ``[0, 1]``.
        mode: The active render mode (:data:`UNICODE` or :data:`ASCII`).
        width: Bar cell count (> 0).

    Returns:
        The bar in the active mode's glyph set, *width* cells wide.

    Raises:
        ValueError: When *mode* is unknown, *ratio* is outside ``[0, 1]``,
            or *width* is not positive (the latter two via
            :func:`~eawf.surfaces.render.bars.render_block_bar`).
    """
    if mode not in (UNICODE, ASCII):
        raise ValueError(f"unknown render mode: {mode!r}")
    block_bar = render_block_bar(ratio, width=width)
    if mode == UNICODE:
        return block_bar
    return to_ascii_fallback(block_bar)


__all__ = [
    "ASCII",
    "ASCII_EMPTY",
    "ASCII_FULL",
    "UNICODE",
    "RenderMode",
    "render_bar",
    "to_ascii_fallback",
]
