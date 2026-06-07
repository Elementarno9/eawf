"""Indeterminate braille spinner helper for the render + TUI surfaces.

Cycles a single braille dot through all eight dot positions of one
Braille-Patterns cell (U+2800 base OR-ed with each dot bit in turn), so an
indeterminate wait reads as a spinning dot. The frame set is the
deterministic eight-dot rotation :data:`BRAILLE_SPINNER_FRAMES`; each
:meth:`BrailleSpinner.advance` call returns the next frame and wraps back
to the first after the eighth, so the cycle repeats with period eight.

This is the indeterminate counterpart to the determinate block-eighths
:mod:`~eawf.surfaces.render.bars` bar: a known completion fraction reads as
a left-to-right fill, an unknown one as this cycling spinner. The helper is
pure (no widget, no colour); a caller advances it on its own tick and
paints the returned frame.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Braille Patterns block base code point (U+2800 -- the all-dots-off cell).
BRAILLE_BASE: int = 0x2800

#: The eight single-dot bit masks, in clockwise rotation order: dot 1
#: (top-left), 2, 3 (lower-left), 7 (bottom-left), 8 (bottom-right), 6, 5,
#: 4 (top-right). OR-ing one mask onto :data:`BRAILLE_BASE` lights exactly
#: that dot, so stepping through them spins a single dot around the cell.
_DOT_BITS: tuple[int, ...] = (0x01, 0x02, 0x04, 0x40, 0x80, 0x20, 0x10, 0x08)

#: The deterministic eight-frame braille spinner cycle, one single-dot
#: glyph per rotation step. Index ``i`` is the ``i``-th frame; the cycle
#: wraps after the eighth (period :data:`SPINNER_PERIOD`).
BRAILLE_SPINNER_FRAMES: tuple[str, ...] = tuple(chr(BRAILLE_BASE | bit) for bit in _DOT_BITS)

#: Number of frames in one full spinner cycle.
SPINNER_PERIOD: int = len(BRAILLE_SPINNER_FRAMES)


class BrailleSpinner:
    """A stateful indeterminate braille spinner over the eight-dot cycle.

    Holds the current frame index and steps it on each
    :meth:`advance`. The sequence is deterministic and wraps with period
    :data:`SPINNER_PERIOD`, so a caller that advances on every tick paints
    a steadily spinning dot.
    """

    def __init__(self) -> None:
        """Initialise the spinner before its first frame.

        The first :meth:`advance` returns the zeroth frame
        (:data:`BRAILLE_SPINNER_FRAMES`\\ ``[0]``).
        """
        self._index = -1

    @property
    def frame(self) -> str:
        """Return the current frame without advancing.

        Returns:
            The braille glyph for the current cycle position. Before the
            first :meth:`advance` this is the zeroth frame.
        """
        if self._index < 0:
            return BRAILLE_SPINNER_FRAMES[0]
        return BRAILLE_SPINNER_FRAMES[self._index % SPINNER_PERIOD]

    def advance(self) -> str:
        """Step to the next frame and return it.

        Each call moves one step along :data:`BRAILLE_SPINNER_FRAMES` and
        wraps back to the zeroth frame after the eighth, so the sequence
        repeats with period :data:`SPINNER_PERIOD`.

        Returns:
            The next braille glyph in the eight-dot cycle.
        """
        self._index = (self._index + 1) % SPINNER_PERIOD
        return BRAILLE_SPINNER_FRAMES[self._index]

    def reset(self) -> None:
        """Reset the spinner so the next :meth:`advance` returns frame zero."""
        self._index = -1


__all__ = [
    "BRAILLE_BASE",
    "BRAILLE_SPINNER_FRAMES",
    "SPINNER_PERIOD",
    "BrailleSpinner",
]
