"""Width oracle: the one place console frame text is measured, padded and clipped.

Textual lays out with ``rich.cells.cell_len``, and every measurement here uses the same
function, so a row that is W cells for the console is W cells for the compositor. The
East-Asian-Width class is exposed separately because both libraries resolve Ambiguous
glyphs to one cell silently, and the console needs that resolution to be a declared
policy rather than a library default. A terminal that draws Ambiguous glyphs two cells
wide is served by the ASCII allocation, not by a second layout here.
"""

from __future__ import annotations

import unicodedata
from enum import StrEnum

from rich.cells import cell_len as _rich_cell_len
from rich.cells import get_character_cell_size

_ELLIPSIS = "…"


class EastAsianWidth(StrEnum):
    """The Unicode East-Asian-Width classes ``unicodedata`` reports."""

    NARROW = "Na"
    NEUTRAL = "N"
    HALFWIDTH = "H"
    AMBIGUOUS = "A"
    WIDE = "W"
    FULLWIDTH = "F"


class AmbiguousWidth(StrEnum):
    """How many cells a layout assumes an Ambiguous glyph occupies."""

    NARROW = "narrow"
    WIDE = "wide"


# The console's declared resolution: one cell, the width Rich and Textual measure.
POLICY = AmbiguousWidth.NARROW

_TWO_CELLS = frozenset({EastAsianWidth.WIDE, EastAsianWidth.FULLWIDTH})


def cell_len(text: str) -> int:
    """Return the terminal cells ``text`` occupies under the narrow policy."""
    return _rich_cell_len(text)


def eaw_class(ch: str) -> EastAsianWidth:
    """Return the East-Asian-Width class of one character.

    Raises:
        TypeError: ``ch`` is not exactly one character.
    """
    return EastAsianWidth(unicodedata.east_asian_width(ch))


def assert_known_width(ch: str, *, policy: AmbiguousWidth = POLICY) -> None:
    """Reject a glyph whose width the fixed-column grid cannot rely on.

    Args:
        ch: One character.
        policy: The Ambiguous-width resolution the frame is laid out under.

    Raises:
        ValueError: ``ch`` is Wide or Fullwidth, so it takes two cells and shifts every
            column to its right; or it is Ambiguous and ``policy`` is not the narrow one.
        TypeError: ``ch`` is not exactly one character.
    """
    cls = eaw_class(ch)
    if cls in _TWO_CELLS:
        raise ValueError(f"glyph {ch!r} (U+{ord(ch):04X}) is East-Asian-Width {cls}: two cells")
    if cls == EastAsianWidth.AMBIGUOUS and policy != AmbiguousWidth.NARROW:
        raise ValueError(f"glyph {ch!r} (U+{ord(ch):04X}) is Ambiguous under the {policy} policy")


def _head(text: str, cells: int) -> str:
    """Return the longest prefix of ``text`` that fits in ``cells`` cells."""
    used = 0
    for index, ch in enumerate(text):
        used += get_character_cell_size(ch)
        if used > cells:
            return text[:index]
    return text


def clip_words(text: str, n: int) -> str:
    """Shorten a value at a word and mark the cut, so a clipped reason still reads as one.

    The cut keeps ``n - 1`` cells, backs up to the last space when that space lies past
    45 percent of the column, and appends an ellipsis. On narrow text this is the
    prototype's code-point cut exactly; measuring in cells keeps a Wide character from
    pushing the result past the column.

    Args:
        text: The value to fit.
        n: The column width in cells.

    Returns:
        ``text`` itself when it fits, otherwise a clipped value of at most ``n`` cells.

    Raises:
        ValueError: ``n`` is negative.
    """
    if n < 0:
        raise ValueError(f"column width must be non-negative, got {n}")
    if cell_len(text) <= n:
        return text
    if n == 0:
        return ""
    cut = _head(text, n - 1)
    space = cut.rfind(" ")
    if space >= 0 and cell_len(cut[:space]) > (n * 45) // 100:
        cut = cut[:space]
    return cut + _ELLIPSIS


def pad(text: str, w: int) -> str:
    """Return ``text`` as exactly ``w`` cells, word-clipped first when it is longer.

    Raises:
        ValueError: ``w`` is negative.
    """
    clipped = clip_words(text, w)
    return clipped + " " * (w - cell_len(clipped))


def rule(glyph: str, w: int) -> str:
    """Return a horizontal rule of one single-cell glyph across ``w`` cells.

    Raises:
        ValueError: ``glyph`` does not occupy exactly one cell, or ``w`` is negative.
    """
    if cell_len(glyph) != 1:
        raise ValueError(f"rule glyph {glyph!r} must occupy exactly one cell")
    if w < 0:
        raise ValueError(f"rule width must be non-negative, got {w}")
    return glyph * w
