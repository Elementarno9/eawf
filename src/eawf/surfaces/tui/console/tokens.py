"""Ratified glyph tables: connection, truth and quality tokens with ASCII twins, plus chrome.

Pure data plus one resolver. Nothing here knows about Textual, and the width oracle suite
checks every glyph so a Wide one cannot enter a fixed column.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from eawf.surfaces.tui.console.width import cell_len


class Severity(StrEnum):
    """How loudly a toast or a row speaks."""

    OK = "ok"
    INFO = "info"
    WARN = "warn"
    ERR = "err"


@dataclass(frozen=True, slots=True)
class Glyph:
    """One token in both render allocations.

    Attributes:
        unicode: What the Unicode allocation renders.
        ascii: The twin the ASCII allocation renders in the same number of cells.

    Raises:
        ValueError: ``ascii`` holds a non-ASCII character, or occupies a different
            number of cells than ``unicode``, so switching allocation would shift a column.
    """

    unicode: str
    ascii: str

    def __post_init__(self) -> None:
        """Reject a twin that is not ASCII or not cell-for-cell with its glyph."""
        if not self.ascii.isascii():
            raise ValueError(f"ASCII twin {self.ascii!r} of {self.unicode!r} is not ASCII")
        if cell_len(self.ascii) != cell_len(self.unicode):
            raise ValueError(
                f"ASCII twin {self.ascii!r} of {self.unicode!r} does not occupy the same cells"
            )


# The nine connection values in the packet's order, each with its header glyph.
CONNECTION: dict[str, Glyph] = {
    "LIVE": Glyph("●", "*"),
    "LIVE / PARTIAL": Glyph("◑", "("),
    "DEGRADED": Glyph("◐", ")"),
    "DISCONNECTED": Glyph("◌", "o"),
    "REPLAYING": Glyph("►", ">"),
    "GAP DETECTED": Glyph("▲", "^"),
    "SNAPSHOT LOADING": Glyph("◈", "#"),
    "SNAPSHOT REQUIRED": Glyph("◇", "<"),
    "OFFLINE SNAPSHOT": Glyph("▪", "="),
}

# A field the projection cannot vouch for says so with one glyph.
TRUTH: dict[str, Glyph] = {
    "unknown": Glyph("?", "?"),
    "unavailable": Glyph("∅", "-"),
    "denied": Glyph("⊘", "x"),
    "failed": Glyph("✗", "X"),
    "attention": Glyph("!", "!"),
    "zero": Glyph("0", "0"),
}

# Quality prefixes on a numeral: bare is measured, ~ derived, the approx sign estimated.
QUALITY: dict[str, Glyph] = {
    "measured": Glyph("", ""),
    "derived": Glyph("~", "~"),
    "estimated": Glyph("≈", "="),
}

# Chrome glyphs every frame shares.
CRUMB_SEP = " ▸ "
CARET = "▸"
BRAND = "Eä"
RULE_HEAVY = "═"
RULE_THIN = "─"
RULE_PALETTE = "┄"
RAIL = "│"

_CHROME: tuple[str, ...] = (CRUMB_SEP, CARET, BRAND, RULE_HEAVY, RULE_THIN, RULE_PALETTE, RAIL)


def truth_cell(field: str | None) -> str:
    """Render one truth cell: the token naming an absence, or nothing.

    Args:
        field: ``None`` for the never-acted case, or a truth token name.

    Returns:
        The cell text; empty for ``None``.

    Raises:
        KeyError: ``field`` names no truth token.
    """
    if field is None:
        return ""
    return TRUTH[field].unicode


def all_glyphs() -> frozenset[str]:
    """Return every non-ASCII character a console token or chrome string can emit."""
    texts = [glyph.unicode for table in (CONNECTION, TRUTH, QUALITY) for glyph in table.values()]
    texts.extend(_CHROME)
    return frozenset(ch for text in texts for ch in text if not ch.isascii())
