"""Value cells: the typed form a rendered value takes, and the marked spans a painter reads.

A value reaches a frame as a :class:`~eawf.kernel.projection.truth.TruthField`.
:func:`value_cell` turns one into a :class:`ValueCell`, which knows the one truth token or
quality marker it wears, spells its value slot, and states the bounded reason phrase that
makes the token readable without a legend: ``? unknown · producer silent``,
``~4.62 derived · rate card``, ``0 measured · a real zero``. Every renderer that draws a
truth field draws it through here, so a purged value, an invalidated one, an unavailable
one and a genuine zero can never collapse into one glyph.

Renderers still return rows of text, because every layout helper, the plain path and the
golden contract measure text. The mark a cell wears is recovered from a finished row by
:func:`spans`, whose grammar is exactly what :class:`ValueCell` writes: a truth token is
always followed by its state word, and a quality marker always sits against the numeral
it qualifies. The painter therefore reads the mark off the words, which is what keeps
colour from ever stating a fact the text does not.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from eawf.kernel.projection.truth import TruthField, TruthState
from eawf.kernel.state.enums import MeasurementQuality
from eawf.surfaces.tui.console.tokens import QUALITY, TRUTH
from eawf.surfaces.tui.console.width import clip_words


class Mark(StrEnum):
    """The class of one marked span: which token or quality marker it wears."""

    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"
    DENIED = "denied"
    PURGED = "purged"
    INVALIDATED = "invalidated"
    ZERO = "zero"
    DERIVED = "derived"
    ESTIMATED = "estimated"


#: The declared no-value: a known field whose value does not exist by design. It is not a
#: failed measurement, so it wears no truth token and states no reason.
NO_VALUE = "–"  # noqa: RUF001

#: The widest a reason phrase grows before it is cut at a word.
REASON_CELLS = 72

#: The basis a genuine measured zero states, so it reads as a value rather than a gap.
ZERO_BASIS = "a real zero"

_SEP = " · "

# The rendering vocabulary is exactly the four measurement qualities: three prefix a
# numeral, and an unavailable quality is never a marker, because a missing measurement
# renders the unavailable truth token in place of the value instead.
_QUALITY_MARK: Mapping[MeasurementQuality, Mark | None] = MappingProxyType(
    {
        MeasurementQuality.EXACT: None,
        MeasurementQuality.RECONSTRUCTED: Mark.DERIVED,
        MeasurementQuality.ESTIMATED: Mark.ESTIMATED,
        MeasurementQuality.UNAVAILABLE: None,
    }
)

# The word each quality is read as in a reason phrase.
QUALITY_WORD: Mapping[MeasurementQuality, str] = MappingProxyType(
    {
        MeasurementQuality.EXACT: "measured",
        MeasurementQuality.RECONSTRUCTED: "derived",
        MeasurementQuality.ESTIMATED: "estimated",
        MeasurementQuality.UNAVAILABLE: "unavailable",
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ValueCell:
    """One value slot as a frame draws it.

    Attributes:
        value: The value as spelled for the frame; ``None`` when the field is absent or
            carries the declared no-value.
        state: Whether the value is known, or which of the five absences it is.
        quality: The measurement quality the field states. It is kept even where
            ``exempt`` suppresses the glyph, so the type still says what the value is.
        basis: What the reason phrase names after the state word: the missing reason of
            an absent field, the producer of a marked one.
        exempt: The value is an identifier, ordinal, position, timestamp or count rather
            than a measurement, so it wears no quality marker.
    """

    value: str | None
    state: TruthState
    quality: MeasurementQuality
    basis: str
    exempt: bool = False

    @property
    def mark(self) -> Mark | None:
        """Return the token or marker the slot wears; ``None`` for a bare value."""
        if self.state is not TruthState.KNOWN:
            return Mark(self.state.value)
        if self.value is None or self.exempt:
            return None
        quality = _QUALITY_MARK[self.quality]
        if quality is None and self.value == TRUTH[Mark.ZERO.value].unicode:
            return Mark.ZERO
        return quality

    @property
    def slot(self) -> str:
        """Return the value slot: one truth token, or the value behind its quality prefix."""
        if self.state is not TruthState.KNOWN:
            return TRUTH[self.state.value].unicode
        if self.value is None:
            return NO_VALUE
        mark = self.mark
        if mark is Mark.DERIVED or mark is Mark.ESTIMATED:
            return QUALITY[mark.value].unicode + self.value
        return self.value

    @property
    def word(self) -> str:
        """Return the state word the slot is read beside; empty for a bare value."""
        mark = self.mark
        if mark is None:
            return ""
        return QUALITY_WORD[self.quality] if mark is Mark.ZERO else mark.value

    @property
    def reason(self) -> str:
        """Return the reason phrase, cut at a word to :data:`REASON_CELLS`; empty when bare."""
        word = self.word
        if not word:
            return ""
        basis = ZERO_BASIS if self.mark is Mark.ZERO else self.basis
        return clip_words(f"{word}{_SEP}{basis}", REASON_CELLS)

    @property
    def full(self) -> str:
        """Return the slot with its reason phrase beside it, as a remainder column draws it."""
        reason = self.reason
        return f"{self.slot} {reason}" if reason else self.slot


def value_cell[T](
    field: TruthField[T], *, spell: Callable[[T], str] = str, exempt: bool = False
) -> ValueCell:
    """Return the cell one truth field renders as.

    Args:
        field: The validated field.
        spell: How a known value is written for the frame.
        exempt: The field is an identifier, ordinal, position, timestamp or count, which
            wears no quality marker.

    Returns:
        The cell; its slot is never empty, because never-acted is not a truth field.
    """
    if field.state is not TruthState.KNOWN:
        return ValueCell(
            value=None,
            state=field.state,
            quality=field.measurement_quality,
            basis=field.missing_reason or field.producer,
            exempt=exempt,
        )
    return ValueCell(
        value=None if field.value is None else spell(field.value),
        state=field.state,
        quality=field.measurement_quality,
        basis=field.producer,
        exempt=exempt,
    )


@dataclass(frozen=True, slots=True)
class Span:
    """One run of a row's text, and the mark it wears; ``None`` for unmarked text."""

    text: str
    mark: Mark | None


# A truth token counts as one only beside its own state word, so ``? help`` and ``! error``
# stay plain. The genuine zero is read beside ``measured``, the word its reason starts with.
_ABSENCES: tuple[Mark, ...] = (
    Mark.UNKNOWN,
    Mark.UNAVAILABLE,
    Mark.DENIED,
    Mark.PURGED,
    Mark.INVALIDATED,
)
_WORD_OF_TOKEN: Mapping[str, tuple[str, Mark]] = MappingProxyType(
    {
        **{TRUTH[mark.value].unicode: (mark.value, mark) for mark in _ABSENCES},
        TRUTH[Mark.ZERO.value].unicode: (QUALITY_WORD[MeasurementQuality.EXACT], Mark.ZERO),
    }
)
_MARKER_OF: Mapping[str, Mark] = MappingProxyType(
    {QUALITY[m.value].unicode: m for m in (Mark.DERIVED, Mark.ESTIMATED)}
)
_TOKENS = "".join(re.escape(token) for token in _WORD_OF_TOKEN)
_MARKERS = "".join(re.escape(marker) for marker in _MARKER_OF)
_GRAMMAR = re.compile(
    rf"(?<!\S)(?P<token>[{_TOKENS}]) +(?P<word>[a-z]+)\b"
    rf"|(?<![\w.{_MARKERS}])(?P<marker>[{_MARKERS}])\d[\d,.]*(?:%|[a-z]+\b)?"
)


def spans(row: str) -> tuple[Span, ...]:
    """Return ``row`` cut into its marked and unmarked runs, left to right.

    The runs concatenate back to ``row`` exactly, so a painter that draws every run draws
    the same text the frame composed.

    Args:
        row: One composed frame row.

    Returns:
        The runs; an empty row has none.
    """
    out: list[Span] = []
    at = 0
    for found in _GRAMMAR.finditer(row):
        mark = _mark_of(found)
        if mark is None:
            continue
        if found.start() > at:
            out.append(Span(row[at : found.start()], None))
        out.append(Span(found.group(0), mark))
        at = found.end()
    tail = row[at:]
    if tail:
        out.append(Span(tail, None))
    return tuple(out)


def _mark_of(found: re.Match[str]) -> Mark | None:
    """Return the mark one grammar match wears, or ``None`` for a token beside another word."""
    marker = found.group("marker")
    if marker is not None:
        return _MARKER_OF[marker]
    word, mark = _WORD_OF_TOKEN[found.group("token")]
    return mark if found.group("word") == word else None
