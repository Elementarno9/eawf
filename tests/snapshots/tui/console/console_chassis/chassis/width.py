"""Width oracle: the one place frame text is measured, padded and clipped.

Textual lays out with ``rich.cells.cell_len``; every measurement here uses the same
function so a row that is W cells for the chassis is W cells for the compositor. The
East-Asian-Width class is exposed separately because both libraries resolve Ambiguous
glyphs to one cell silently, and the contract needs that resolution to be a declared
policy rather than a library default.
"""

from __future__ import annotations

import unicodedata

from rich.cells import cell_len as _cell_len

POLICY = "narrow"
_ELLIPSIS = "…"


def cell_len(text: str) -> int:
    """Terminal cells the text occupies under the narrow ambiguous-width policy."""
    return _cell_len(text)


def eaw_class(ch: str) -> str:
    """The Unicode East-Asian-Width class of one character (Na, N, H, A, W, F)."""
    return unicodedata.east_asian_width(ch)


def assert_known_width(ch: str, policy: str = POLICY) -> None:
    """Reject a glyph whose width the fixed-column grid cannot rely on.

    Wide and Fullwidth glyphs occupy two cells and break every column to their right;
    Ambiguous glyphs are accepted only because the console declares the narrow policy.
    """
    cls = eaw_class(ch)
    if cls in ("W", "F"):
        raise ValueError(f"glyph {ch!r} (U+{ord(ch):04X}) is East-Asian-Width {cls}: two cells")
    if cls == "A" and policy != "narrow":
        raise ValueError(f"glyph {ch!r} (U+{ord(ch):04X}) is Ambiguous under the {policy} policy")


def clip_words(text: str, n: int) -> str:
    """Shorten a cell at a word, then mark the cut, so a clipped reason still reads as one.

    Mirrors the prototype: cut to n-1 code points, back up to the last space when that
    space lies past 45 percent of the column, and append an ellipsis.
    """
    text = str(text)
    if cell_len(text) <= n:
        return text
    cut = text[: max(0, n - 1)]
    sp = cut.rfind(" ")
    if sp > (n * 45) // 100:
        cut = cut[:sp]
    return cut + _ELLIPSIS


def pad(text: str, w: int) -> str:
    """Pad to exactly w cells; a longer value is word-clipped first."""
    text = str(text)
    n = cell_len(text)
    if n > w:
        clipped = clip_words(text, w)
        return clipped + " " * max(0, w - cell_len(clipped))
    if n == w:
        return text
    return text + " " * (w - n)


def fit(text: str, w: int) -> str:
    """Clip without padding: the value gives way, the column does not move."""
    text = str(text)
    return clip_words(text, w) if cell_len(text) > w else text


def rule(glyph: str, w: int) -> str:
    """A horizontal rule of one glyph across the frame width."""
    return glyph * w
