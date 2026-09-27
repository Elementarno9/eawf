"""Ratified glyph tables: connection, truth and quality tokens with ASCII twins, plus chrome.

Pure data, one resolver and the glyph table every rendered glyph is a row of, audited per
column class at import so no twin carries two meanings in one class. Nothing here knows
about Textual, and the width oracle suite checks every glyph so a Wide one cannot enter a
fixed column.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

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


# The nine connection values in the packet's order, each with its header glyph. The twin
# carries the severity class and the label beside it carries the fact, so a twin never has
# to be unique across the table, only inside the state slot's column class.
CONNECTION: dict[str, Glyph] = {
    "LIVE": Glyph("●", "*"),
    "LIVE / PARTIAL": Glyph("◑", "+"),
    "DEGRADED": Glyph("◐", "%"),
    "DISCONNECTED": Glyph("◌", "o"),
    "REPLAYING": Glyph("►", ">"),
    "GAP DETECTED": Glyph("▲", "^"),
    "SNAPSHOT LOADING": Glyph("◈", "~"),
    "SNAPSHOT REQUIRED": Glyph("◇", "<"),
    "OFFLINE SNAPSHOT": Glyph("▪", "#"),
}

# The ratified truth tokens, keyed by the state word each one is always read beside: the
# five absences a value slot can hold instead of a value, and the genuine zero, which is a
# value and is listed here only because the legend distinguishes it from all five.
TRUTH: dict[str, Glyph] = {
    "unknown": Glyph("?", "?"),
    "unavailable": Glyph("∅", "-"),
    "denied": Glyph("⊘", "x"),
    "purged": Glyph("✗", "X"),
    "invalidated": Glyph("!", "!"),
    "zero": Glyph("0", "0"),
}

# Quality prefixes on a numeral, written against it with no space: bare is measured, the
# plain tilde derived, the approximately-equal sign estimated. The derived marker is the
# ASCII tilde in both allocations because the modifier small tilde reads as estimated in
# terminal fonts.
QUALITY: dict[str, Glyph] = {
    "measured": Glyph("", ""),
    "derived": Glyph("~", "~"),
    "estimated": Glyph("≈", "^"),
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


class ColumnClass(StrEnum):
    """The column classes a glyph's meaning is fixed per; one meaning per glyph per class."""

    STATE_SLOT = "state slot"
    KIND = "kind"
    VALUE = "value"
    PROVENANCE = "provenance"
    CURSOR = "cursor"
    STRUCTURE = "structure"


@dataclass(frozen=True, slots=True)
class GlyphRow:
    """One glyph-table row: the glyph and its one-cell ASCII twin in each class it occupies.

    A glyph that twins differently by class carries one twin per class, which is why the
    twin is keyed by class rather than stored once.
    """

    glyph: str
    twins: Mapping[ColumnClass, str]

    @property
    def twin(self) -> str:
        """Return the twin a glyph takes where its class is not known: the structure one."""
        return self.twins.get(ColumnClass.STRUCTURE) or next(iter(self.twins.values()))


_V, _S, _K = ColumnClass.VALUE, ColumnClass.STATE_SLOT, ColumnClass.KIND
_P, _C, _T = ColumnClass.PROVENANCE, ColumnClass.CURSOR, ColumnClass.STRUCTURE

# Twins a column class may repeat, each bound to what follows it so a reader still tells
# the two apart: the unavailable token is followed by its word and the diff minus by a
# numeral; the at-most bound is bound to a numeral and the backward arrow sits between two
# values; the thinking kind is followed by its word and the coalescing sign by a count.
BOUND_TWINS: Mapping[tuple[ColumnClass, str], frozenset[str]] = MappingProxyType(
    {
        (_V, "-"): frozenset("∅−"),  # noqa: RUF001
        (_V, "<"): frozenset("≤←"),
        (_K, "*"): frozenset("°×"),  # noqa: RUF001
    }
)


def audit_classes(rows: Sequence[GlyphRow]) -> tuple[GlyphRow, ...]:
    """Return ``rows`` once each glyph is listed once and each twin means one thing per class.

    The structure class is exempt from twin uniqueness: a rule, a border and a separator
    carry no meaning a reader decodes from the twin.

    Raises:
        ValueError: the table is empty, a glyph is listed twice or occupies no class, a
            twin is not one ASCII cell, or two glyphs share a twin inside one non-structure
            class outside :data:`BOUND_TWINS`.
    """
    if not rows:
        raise ValueError("the glyph table is empty")
    seen: set[str] = set()
    shared: dict[tuple[ColumnClass, str], set[str]] = {}
    for row in rows:
        if row.glyph in seen:
            raise ValueError(f"glyph {row.glyph!r} is listed twice")
        seen.add(row.glyph)
        if not row.twins:
            raise ValueError(f"glyph {row.glyph!r} occupies no column class")
        for cls, twin in row.twins.items():
            if not (twin.isascii() and cell_len(twin) == 1):
                raise ValueError(f"twin {twin!r} of {row.glyph!r} is not one ASCII cell")
            if cls is not ColumnClass.STRUCTURE:
                shared.setdefault((cls, twin), set()).add(row.glyph)
    for (cls, twin), glyphs in shared.items():
        if len(glyphs) > 1 and BOUND_TWINS.get((cls, twin)) != frozenset(glyphs):
            raise ValueError(f"twin {twin!r} means {''.join(sorted(glyphs))} in the {cls} class")
    return tuple(rows)


def _row(glyph: str, twin: str, *classes: ColumnClass) -> GlyphRow:
    return GlyphRow(glyph, MappingProxyType(dict.fromkeys(classes, twin)))


# Every glyph the console renders, in the packet's glyph-table order. The tokens above are
# rows here too, and the plain path twins from this table alone.
GLYPH_TABLE: tuple[GlyphRow, ...] = audit_classes(
    (
        _row("?", "?", _V),
        _row("∅", "-", _V),
        _row("⊘", "x", _V),
        _row("✗", "X", _V),
        _row("!", "!", _V, _S, _K),
        _row("0", "0", _V),
        _row("~", "~", _V, _K),
        _row("≈", "^", _V),
        _row("–", "_", _V, _P),  # noqa: RUF001
        _row("●", "*", _S, _C),
        _row("◑", "+", _S),
        _row("◐", "%", _S),
        _row("◌", "o", _S),
        _row("►", ">", _S),
        _row("▲", "^", _S),
        _row("◈", "~", _S),
        _row("◇", "<", _S),
        _row("▪", "#", _S),
        _row("¶", '"', _K),
        _row("›", ">", _K, _T),  # noqa: RUF001
        _row("‹", "<", _T),  # noqa: RUF001
        _row("±", "+", _K),
        _row("°", "*", _K),
        _row("»", "}", _K),
        _row("⋯", ".", _K, _C),
        _row("=", "=", _P),
        _row("≠", "#", _P),
        GlyphRow("·", MappingProxyType({_T: "-", _P: "."})),
        _row("…", ";", _V, _T),
        _row("▸", ">", _C, _T),
        _row("▾", "v", _C),
        _row("○", "o", _C),
        _row("▣", "[", _C),
        _row("┼", "+", _T),
        _row("┄", ".", _T),
        _row("┊", ":", _T),
        _row("─", "-", _T),
        _row("═", "=", _T),
        _row("│", "|", _T),
        *(_row(corner, "+", _T) for corner in "┌┐└┘├┤┬┴"),
        _row("█", "#", _T),
        _row("▏", "|", _T),
        _row("↑", "^", _T),
        _row("↓", "v", _T),
        _row("←", "<", _T, _V),
        _row("→", ">", _T, _V),
        _row("↳", "L", _T),
        _row("×", "*", _K),  # noqa: RUF001
        _row("≤", "<", _V),
        _row("−", "-", _V),  # noqa: RUF001
        _row("•", "*", _T),
        _row("✓", "+", _C),
        _row("ä", "a", _T),
    )
)

# Prose punctuation outside the glyph table: it never occupies a value, kind, state,
# provenance or cursor cell, so it carries no meaning the table must keep apart.
PROSE_TWINS: Mapping[str, str] = MappingProxyType(
    {"—": "-", "’": "'", "“": '"', "”": '"'}  # noqa: RUF001
)


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
    texts.extend(row.glyph for row in GLYPH_TABLE)
    return frozenset(ch for text in texts for ch in text if not ch.isascii())
