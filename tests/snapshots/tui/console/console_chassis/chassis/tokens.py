"""Ratified glyph tables: connection, truth, quality and severity, with ASCII twins.

Pure data plus one resolver. Nothing here knows about Textual, and every glyph is
checked by the width oracle test so a Wide glyph cannot enter a fixed column.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Severity(StrEnum):
    OK = "ok"
    INFO = "info"
    WARN = "warn"
    ERR = "err"


@dataclass(frozen=True, slots=True)
class Glyph:
    unicode: str
    ascii: str


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

# Truth tokens: a field the projection cannot vouch for says so with one glyph.
TRUTH: dict[str, Glyph] = {
    "unknown": Glyph("?", "?"),
    "unavailable": Glyph("∅", "-"),
    "denied": Glyph("⊘", "x"),
    "failed": Glyph("✗", "X"),
    "attention": Glyph("!", "!"),
    "zero": Glyph("0", "0"),
}

# Quality prefixes on a numeral: bare is measured, ~ derived, ≈ estimated.
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


def truth_cell(field: str | int | None, quality: str = "measured", ascii: bool = False) -> str:
    """Render one truth cell: quality prefix plus numeral, one truth token, or empty.

    ``None`` is the never-acted case and renders empty; a string names a truth token;
    an int renders with its quality prefix.
    """
    if field is None:
        return ""
    if isinstance(field, str):
        glyph = TRUTH[field]
        return glyph.ascii if ascii else glyph.unicode
    prefix = QUALITY[quality]
    return (prefix.ascii if ascii else prefix.unicode) + str(field)


def all_glyphs() -> set[str]:
    """Every non-ASCII glyph a chassis token can emit, for the width oracle test."""
    out: set[str] = set()
    for table in (CONNECTION, TRUTH, QUALITY):
        for glyph in table.values():
            out.update(ch for ch in glyph.unicode if ord(ch) > 127)
    for chrome in (CRUMB_SEP, CARET, BRAND, RULE_HEAVY, RULE_THIN, RULE_PALETTE, RAIL):
        out.update(ch for ch in chrome if ord(ch) > 127)
    return out
