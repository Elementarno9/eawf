"""The row painter: which console surface each run of a composed row is drawn as.

Renderers return rows of text, because every layout helper, the plain path and the golden
contract measure text. Colour is therefore read back off the finished words, the same way
the design packet's prototype highlights its rows: the brand and the crumb in the header,
the connection chip and the attention count beside it, the rules and the rail, the column
heads and pane labels, the cursor row, the lifecycle words, and each key against its
label in the keybar. A run is only ever painted as a surface named in
:data:`~eawf.surfaces.tui.console.token_map.TOKEN_MAP`, so which colour a surface takes is
decided in that one table and nowhere here; and because every rule reads the text, colour
never states a fact the words do not.

Truth tokens and quality markers keep the marks :func:`~eawf.surfaces.tui.console.cells.spans`
reads off the row, one run per mark, and a mark's surface wins over any other rule.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
from types import MappingProxyType

from eawf.surfaces.tui.console.action_menu import Disabled
from eawf.surfaces.tui.console.cells import Mark, spans
from eawf.surfaces.tui.console.frame import (
    LeadReceded,
    Lensed,
    RailReceded,
    Receded,
    Titled,
)
from eawf.surfaces.tui.console.header import CrumbPart, crumb_runs
from eawf.surfaces.tui.console.keybar import GAP
from eawf.surfaces.tui.console.lifecycle import WORD_CLASSES
from eawf.surfaces.tui.console.token_map import SURFACES
from eawf.surfaces.tui.console.tokens import CARET, RAIL


class Part(StrEnum):
    """Which band of the frame a row belongs to; each band reads its own grammar."""

    HEADER = "header"
    BODY = "body"
    KEYBAR = "keybar"


@dataclass(frozen=True, slots=True)
class Stroke:
    """One run of a row drawn in one style.

    Attributes:
        text: The run's text, exactly as the frame composed it.
        surface: The surface whose colour the glyphs take; ``None`` keeps the band's own.
        ground: The surface whose background the run sits on; ``None`` keeps the band's.
        bold: Whether the run is drawn bold.
        underline: Whether the run is underlined, as a typed id that is a link is.
        mark: The truth token or quality marker the run wears, if any.

    Raises:
        ValueError: ``surface`` or ``ground`` names no row of the token map, so the run
            would take a colour the map does not own.
    """

    text: str
    surface: str | None = None
    ground: str | None = None
    bold: bool = False
    underline: bool = False
    mark: Mark | None = None

    def __post_init__(self) -> None:
        """Refuse a surface the token map does not declare."""
        for name in (self.surface, self.ground):
            if name is not None and name not in SURFACES:
                raise ValueError(f"surface {name!r} is not in the token map")


# A truth token names an absence, so it is read at the severity the packet gives its word:
# the packet's canvases draw an unknown in the info tone, a denial as a warning and an
# invalidation as an error. A quality marker only qualifies a value, so it recedes to the
# hint tone the value stays legible in. A genuine zero is a value and keeps the text colour.
MARK_SURFACE: Mapping[Mark, str | None] = MappingProxyType(
    {
        Mark.UNKNOWN: "info",
        Mark.UNAVAILABLE: "dim",
        Mark.DENIED: "warn",
        Mark.PURGED: "dim",
        Mark.INVALIDATED: "err",
        Mark.ZERO: None,
        Mark.DERIVED: "hint",
        Mark.ESTIMATED: "hint",
    }
)

# The lifecycle and state words the console colours, each at the severity the packet's
# prototype gives it. Only these words are chips; prose around them stays plain.
_STATUS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:RUNNING|ACTIVE|SUCCEEDED|COMPLETED|READY_TO_INTEGRATE)\b"), "ok"),
    # ``passed`` is a verdict only as a cell of its own; in a sentence the word is prose, and
    # prose may negate it ("none reads passed") or mean an overrun ("budget passed").
    (re.compile(r"(?<=  )passed(?= {2,}|\s*$)"), "ok"),
    (re.compile(r"\b(?:LOST|FAILED|REJECTED)\b"), "err"),
    (re.compile(r"\b(?:WAIT-[A-Z]+|ACCEPTANCE_REVIEW|SUSPENDED|STALLED|STALE)\b|≠ denied"), "warn"),
    (re.compile(r"\b(?:QUEUED|PLANNED|STARTING|CHECKING)\b"), "info"),
    # The exception buckets name their severity in words beside a count; the count may be
    # unknown or estimated, and the bucket keeps its colour whether or not it is known.
    (re.compile(r"\b(?:needs operator|unknown control outcome)(?= +[\d?≈])"), "warn"),
    (re.compile(r"\b(?:lost or stale|failed)(?= +[\d?≈])"), "err"),
    (re.compile(r"\b(?:checking or integrating|terminal recent)(?= +[\d?≈])"), "info"),
    (re.compile(r"\brunning(?= +[\d?≈])"), "ok"),
)

# Every epoch-2 state word, in the severity class its family table gives it: colour carries
# the class and nothing else, so a word the table classes never takes a second colour.
_LIFECYCLE: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (
        re.compile(
            rf"\b(?:{'|'.join(sorted(w for w, c in WORD_CLASSES.items() if c is klass))})\b"
        ),
        klass.value,
    )
    for klass in sorted(set(WORD_CLASSES.values()))
)

# A row ending in a state word is a label and its value, never a row of column heads.
_CHIP_ROW = re.compile(
    r"\s(?:ACTIVE|PLANNED|COMPLETED|REVIEW|RUNNING|SUCCEEDED|STARTING|QUEUED|CHECKING|LOST"
    r"|FAILED|DENIED|DRAFTS|DEFERRED|PROMOTION|WAIT-[A-Z]+|ACCEPTANCE_REVIEW"
    r"|READY_TO_INTEGRATE)\s*$"
)
# Column heads: upper-case tokens separated by column gaps; a head may be two words, or two
# names joined by a middle dot (``REASON · LAST RESULT``).
_HEAD = r"[A-Z][A-Z0-9]*(?:(?: | · )[A-Z0-9]+)*"
_HEADS = re.compile(rf"^(\s*)({_HEAD}(?: {{2,}}{_HEAD})+)\s*$")
_ONE_HEAD = re.compile(r"^(\s*)([A-Z][A-Z0-9]*(?: [A-Z0-9]+)*)\s*$")
# A pane label is a position: an upper-case name at the start of the row, then a gap.
_PANE_LABEL = re.compile(rf"^[ {CARET}]{{0,4}}([A-Z][A-Z0-9]*(?: [A-Z0-9]+)*)(?= {{2,}}|\s*$)")
_LABEL_SURFACE: Mapping[str, str] = MappingProxyType(
    {
        "NEEDS YOU": "warn",
        "NEEDS OPERATOR": "warn",
        "STALLED": "warn",
        "OVER BUDGET": "warn",
        "UNKNOWN": "warn",
        "DENIED": "warn",
        "FAILED": "err",
        "LOST": "err",
        "REJECTED": "err",
        "ACTIVE": "ok",
        "QUEUED": "info",
        "RESOLVED": "info",
    }
)
# The classes the packet's stylesheet sets at weight 600 or 700: every run painted in one
# of them is drawn bold, whichever rule painted it, so a state word, a severity token and
# the accent carry the packet's weight as well as its colour.
_HEAVY = frozenset({"ok", "info", "warn", "err", "brand", "live", "caret"})
_RULES = re.compile(r"[═─┄]+")
# A junction or corner joins a rail to a rule: it takes the rail's tone, as the packet's
# join does, rather than the text colour no line around it is drawn in.
_JUNCTIONS = re.compile(r"[├┤┬┴┼╤╧╪┌┐└┘]")
# The caret a pane leads with: the pane's first glyph, or the first after its label. A caret
# anywhere else is a separator in prose (``LAYER ▸ repo``) and marks nothing.
_LEAD_CARET = rf"(?:[A-Z][A-Z0-9]*(?: [A-Z0-9]+)* {{2,}})?({CARET})"
_CURSOR_ROW = re.compile(rf"^\s*{_LEAD_CARET}")
_PANE_CARET = re.compile(rf"(?:^|[│├])\s*{_LEAD_CARET}")
# Where a rail meets a row: its own glyph, or the tee a rule crossing it is joined by.
_RAIL_EDGE = re.compile("[│├]")
# A boxed card's row: its two edges, and the pane between them read as a row of its own.
_BOX_ROW = re.compile(r"^(\s*│)(.*)│\s*$")
# A keybar's pairs are one gap apart; past a wider run the rest is a legend to the frame's
# glyphs, drawn as one faint run, as the packet's footer draws it.
_LEGEND = re.compile(rf"(?<=\S) {{{len(GAP) + 1},}}(\S.*?)\s*$")
# A typed entity id is a link: a Run's eight hex digits, a Track's code, or any capital
# prefix and dash before a key that starts with a digit (MLS-0100, EAWF-0042, EVT-2218).
_TYPED_ID = re.compile(
    r"(?<![\w-])(?:RUN-[0-9a-f]{8}|TRK-[A-Z0-9]+(?:-[A-Z0-9]+)*|[A-Z][A-Z0-9]*-\d[0-9A-Za-z]*)(?![\w-])"
)
_OUTSTANDING = re.compile(r"![1-9][\d,]*(?: NEEDS YOU)?")
# The header's state slot: one glyph, a space, then the upper-case value, at the row's end.
_STATE_SLOT = re.compile(r"(?<=\s)(\S) ([A-Z][A-Z /]*[A-Z])\s*$")
_LIVE = "LIVE"
# How each typed crumb run is drawn: its surface, bold, underline. A run not listed is plain.
_CRUMB_STYLE: Mapping[CrumbPart, tuple[str | None, bool, bool]] = MappingProxyType(
    {
        CrumbPart.BRAND: ("brand", True, False),
        CrumbPart.SEP: ("rail", False, False),
        CrumbPart.STEP: ("hint", False, False),
        CrumbPart.ID: ("hint", False, True),
        CrumbPart.FOLD: ("dim", False, False),
        CrumbPart.LEAF: (None, True, False),
    }
)
_GAP_GLYPH = "▲"


class _Canvas:
    """The per-character style of one row while the grammar rules write into it.

    Positions are string indices, not cells: a run is cut out of the row by slicing, so
    a wide glyph is still one position and the runs rejoin to the row exactly.
    """

    def __init__(self, row: str) -> None:
        self.row = row
        self.surface: list[str | None] = [None for _ch in row]
        self.ground: list[str | None] = [None for _ch in row]
        self.bold: list[bool] = [False for _ch in row]
        self.underline: list[bool] = [False for _ch in row]
        self.size = len(self.surface)

    def put(
        self,
        start: int,
        end: int,
        surface: str | None,
        *,
        bold: bool = False,
        underline: bool = False,
    ) -> None:
        """Paint positions ``start`` to ``end`` with ``surface``, adding bold or underline."""
        for i in range(start, end):
            if surface is not None:
                self.surface[i] = surface
            self.bold[i] = self.bold[i] or bold
            self.underline[i] = self.underline[i] or underline

    def strokes(self) -> tuple[Stroke, ...]:
        """Return the row as runs, a marked span always its own run.

        The heavy classes are bolded before the marks are laid, so a mark keeps its own
        weight: a truth token is never drawn heavier than its word.
        """
        for i, surface in enumerate(self.surface):
            self.bold[i] = self.bold[i] or surface in _HEAVY
        marks: list[tuple[Mark | None, int]] = [(None, -1)] * self.size
        at = 0
        for index, span in enumerate(spans(self.row)):
            end = at + len(span.text)
            if span.mark is not None:
                surface = MARK_SURFACE[span.mark]
                for i in range(at, end):
                    marks[i] = (span.mark, index)
                    self.surface[i] = surface
                    self.bold[i] = False
                    self.underline[i] = False
            at = end
        out: list[Stroke] = []
        start = 0
        for i in range(1, self.size + 1):
            if i < self.size and self._key(i) == self._key(start) and marks[i] == marks[start]:
                continue
            out.append(
                Stroke(
                    self.row[start:i],
                    surface=self.surface[start],
                    ground=self.ground[start],
                    bold=self.bold[start],
                    underline=self.underline[start],
                    mark=marks[start][0],
                )
            )
            start = i
        return tuple(out)

    def _key(self, i: int) -> tuple[str | None, str | None, bool, bool]:
        return (self.surface[i], self.ground[i], self.bold[i], self.underline[i])


def paint(row: str, part: Part) -> tuple[Stroke, ...]:
    """Return ``row`` cut into the runs it is drawn in, left to right.

    The runs concatenate back to ``row`` exactly, so painting never changes a frame's text.

    Args:
        row: One composed frame row.
        part: The band the row sits in.

    Returns:
        The runs; an empty row has none.
    """
    if not row:
        return ()
    canvas = _Canvas(row)
    if part is Part.HEADER:
        _header(canvas)
    elif part is Part.KEYBAR:
        _keybar(canvas)
    elif isinstance(row, Receded):
        _receded(canvas)
    elif isinstance(row, Disabled):
        canvas.put(0, canvas.size, "dim")
    elif isinstance(row, Titled):
        _pane_labels(canvas, row, 0)
    elif not _block(canvas):
        _body(canvas)
        _links(canvas)
        _cursor(canvas)
        if isinstance(row, RailReceded) and RAIL in row:
            _receded(canvas, start=row.rindex(RAIL) + 1)
        edge = _RAIL_EDGE.search(row) if isinstance(row, LeadReceded) else None
        if edge is not None:
            _receded(canvas, end=edge.start())
    return canvas.strokes()


def _links(canvas: _Canvas) -> None:
    """Underline every typed entity id, which names a record the console can open."""
    for found in _TYPED_ID.finditer(canvas.row):
        canvas.put(found.start(), found.end(), None, underline=True)


def _cursor(canvas: _Canvas) -> None:
    """Ground the pane the caret marks, and no other pane of the row.

    A row split by the rail carries two panes, and the caret of the narrow one marks a
    section, not the row the arrows walk: only the widest pane is grounded, and only when
    its own text starts at the caret.
    """
    row = canvas.row
    start, end = 0, canvas.size
    # a rail between two rule cells is a line crossing a lane, not a pane edge
    edges = [i for i, ch in enumerate(row) if ch == RAIL and row[i - 1 : i + 2] != f"─{RAIL}─"]
    if edges:
        widest = (0, 0)
        for left, right in pairwise([-1, *edges, canvas.size]):
            if right - left - 1 > widest[1] - widest[0]:
                widest = (left + 1, right)
        start, end = widest
    if _CURSOR_ROW.match(row[start:end]) is None:
        return
    for i in range(start, end):
        canvas.ground[i] = "cursor"


def _receded(canvas: _Canvas, start: int = 0, end: int | None = None) -> None:
    """Draw a row, from ``start`` to ``end``, in the receded tone, keeping what needs the operator.

    Recession never hides an outstanding count, so ``!N`` keeps its weight and colour,
    and the row's pane labels and column heads keep their weight: the pane loses its
    colours, never its structure.
    """
    stop = canvas.size if end is None else end
    for i in range(start, stop):
        canvas.bold[i] = False
        canvas.underline[i] = False
    if start == 0 and not _heads(canvas, canvas.row[:stop], 0):
        _pane_labels(canvas, canvas.row[:stop], 0)
    canvas.put(start, stop, "recede")
    for count in _OUTSTANDING.finditer(canvas.row, start, stop):
        canvas.put(count.start(), count.end(), "warn", bold=True)


def _header(canvas: _Canvas) -> None:
    """Paint the brand, the crumb, the attention count and the state slot."""
    row = canvas.row
    left_end = canvas.size
    slot = _STATE_SLOT.search(row)
    if slot is not None:
        glyph, label = slot.group(1), slot.group(2)
        surface = "live" if label == _LIVE else "warn" if glyph == _GAP_GLYPH else "dim"
        canvas.put(slot.start(1), slot.end(2), surface, bold=label == _LIVE)
        left_end = slot.start(1)
    for count in _OUTSTANDING.finditer(row, 0, left_end):
        canvas.put(count.start(), count.end(), "warn", bold=True)
        left_end = min(left_end, count.start())
    # The crumb is read through the header's own typed runs, so the painter and the pointer
    # agree on which step is which: every step above the leaf is a place Escape walks back
    # to, the leaf names the frame, and a typed id above the leaf is a link.
    at = 0
    for run in crumb_runs(row):
        end = at + len(run.text)
        if end <= left_end:
            step, bold, underline = _CRUMB_STYLE.get(run.part, (None, False, False))
            canvas.put(at, end, step, bold=bold, underline=underline)
        at = end


def _keybar(canvas: _Canvas) -> None:
    """Paint each key bold and its label in the hint tone.

    A pair is ``<key> <label>``: the key token is its first word plus any following word
    that names a key rather than a verb (``PageUp PageDown``, ``Home End``), and a label
    is always lower-case.
    """
    row = canvas.row
    legend = _LEGEND.search(row)
    if legend is not None:
        canvas.put(legend.start(1), legend.end(1), "dim")
        row = row[: legend.start()]
    at = 0
    for chunk in row.split(GAP):
        words = list(re.finditer(r"\S+", chunk))
        if words:
            key_end = words[0].end()
            label_from = len(words)
            for n, word in enumerate(words[1:], start=1):
                if word.group(0)[0].islower():
                    label_from = n
                    break
                key_end = word.end()
            canvas.put(at + words[0].start(), at + key_end, None, bold=True)
            if label_from < len(words):
                canvas.put(at + words[label_from].start(), at + words[-1].end(), "hint")
        at += len(chunk) + len(GAP)


# A transcript block's head: the clock, then the kind cell -- one glyph and its full word.
_BLOCK_KIND = re.compile(r"^[ \u25b8]\d\d:\d\d:\d\d  (\S [a-z]+)\b")
# The class each transcript kind is coloured as. Colour sits on the kind cell alone and
# repeats what the word says; the block's own text is never coloured.
_KIND_SURFACE: Mapping[str, str] = MappingProxyType(
    {
        "message": "text",
        "tool": "info",
        "file": "info",
        "background": "info",
        "subagent": "info",
        "running": "info",
        "question": "warn",
        "error": "err",
        "thinking": "hint",
        "heartbeat": "dim",
        "purged": "dim",
        "event": "dim",
    }
)


def _block(canvas: _Canvas) -> bool:
    """Colour a transcript block head's kind cell and nothing else; return whether it was one."""
    found = _BLOCK_KIND.match(canvas.row)
    surface = _KIND_SURFACE.get(found.group(1).split(" ")[1]) if found is not None else None
    if found is None or surface is None:
        return False
    canvas.put(found.start(1), found.end(1), surface)
    if canvas.row.startswith(CARET):
        canvas.put(0, 1, "caret", bold=True)
    return True


def _body(canvas: _Canvas) -> None:
    """Paint the rules, rail, heads, pane labels, caret, counts and state words.

    A boxed card's row is read between its edges, so its heads and labels are found as
    they are on an open row. A row of heads is bold and nothing else: its words name
    columns, never a state.
    """
    row = canvas.row
    box = _BOX_ROW.match(row)
    at, inner = (box.end(1), box.group(2)) if box is not None else (0, row)
    # a row ending in a state word is a label and its value, never a row of column heads
    if _CHIP_ROW.search(inner) is not None or not _heads(canvas, inner, at):
        _pane_labels(canvas, inner, at)
        for pattern, surface in (*_STATUS, *_LIFECYCLE):
            for found in pattern.finditer(row):
                canvas.put(found.start(), found.end(), surface)
    for count in _OUTSTANDING.finditer(row):
        canvas.put(count.start(), count.end(), "warn", bold=True)
    for rule in _RULES.finditer(row):
        canvas.put(rule.start(), rule.end(), "rule")
    for joint in _JUNCTIONS.finditer(row):
        canvas.put(joint.start(), joint.end(), "rail")
    for i, ch in enumerate(row):
        if ch == RAIL:
            canvas.put(i, i + 1, "rail")
    for caret in _PANE_CARET.finditer(row):
        canvas.put(caret.start(1), caret.end(1), "caret", bold=True)
    _settings(canvas)


def _pane_labels(canvas: _Canvas, row: str, at: int) -> None:
    """Bold the pane label that opens ``row``, and the one that opens the pane past a rail.

    Args:
        canvas: The row being painted.
        row: The part of the canvas row read, starting at position ``at``.
        at: Where ``row`` starts in the canvas row.
    """
    offset = row.rfind(RAIL) + 1 if row.count(RAIL) == 1 else 0
    for start in dict.fromkeys((0, offset)):
        label = _PANE_LABEL.match(row[start:])
        if label is not None:
            surface = _LABEL_SURFACE.get(label.group(1))
            first = at + start
            canvas.put(first + label.start(1), first + label.end(1), surface, bold=True)


def _heads(canvas: _Canvas, row: str, at: int) -> bool:
    """Bold a row of column heads, each side of a rail on its own; return whether it was one.

    Args:
        canvas: The row being painted.
        row: The part of the canvas row read, starting at position ``at``.
        at: Where ``row`` starts in the canvas row.
    """
    halves = row.split(RAIL)
    if len(halves) == 2:
        left, right = _HEADS.match(halves[0]), _HEADS.match(halves[1]) or _ONE_HEAD.match(halves[1])
        offset = at + len(halves[0]) + len(RAIL)
        if left is not None and right is not None:
            canvas.put(at + left.start(2), at + left.end(2), None, bold=True)
            canvas.put(offset + right.start(2), offset + right.end(2), None, bold=True)
            return True
        # the pane right of the rail carries its own heads whatever the left pane holds
        right = _HEADS.match(halves[1])
        if right is not None:
            canvas.put(offset + right.start(2), offset + right.end(2), None, bold=True)
            return True
    whole = _HEADS.match(row)
    if whole is None:
        return False
    canvas.put(at + whole.start(2), at + whole.end(2), None, bold=True)
    return True


# The settings rail: a category heading is an upper-case word at the row's start, the
# selected section is the caret row, and either is followed by the rail's own column.
_RAIL_CATEGORY = re.compile(r"^([A-Z][A-Z]+) +[│├]")
_RAIL_SECTION = re.compile(rf"^{CARET} ([a-z][a-z0-9_]*) +[│├]")
# A settings key row: after the rail, the cursor column, then the key's lens glyph.
_KEY_GLYPH = re.compile(rf"│ [{CARET} ] ([=≠·–]) ")  # noqa: RUF001
# The layer the lens writes to, bracketed in the writable chain.
_LENS = re.compile(r"\[(?:global|workspace|repo|branch|local)\]")
# What each lens glyph says: set here and winning, set here and shadowed, inherited, and
# stated by no layer but the defaults.
_GLYPH_SURFACE: Mapping[str, str] = MappingProxyType(
    {"=": "ok", "≠": "warn", "·": "dim", "–": "dim"}  # noqa: RUF001
)


def _settings(canvas: _Canvas) -> None:
    """Paint the settings rail headings and selection, the key glyphs and the lens."""
    row = canvas.row
    category = _RAIL_CATEGORY.match(row)
    if category is not None:
        canvas.put(category.start(1), category.end(1), "brand", bold=True)
    section = _RAIL_SECTION.match(row)
    if section is not None:
        canvas.put(section.start(1), section.end(1), "brand")
    for found in _KEY_GLYPH.finditer(row):
        canvas.put(found.start(1), found.end(1), _GLYPH_SURFACE[found.group(1)])
    for found in _LENS.finditer(row):
        canvas.put(found.start(), found.end(), None, bold=True)
    if isinstance(row, Lensed):
        _lens_strip(canvas, row)


# The lens strip: the layers a write may target, lowest precedence first.
_LENS_STRIP = re.compile(r"global › workspace › repo › branch › local")  # noqa: RUF001
_LAYER_WORD = re.compile(r"[a-z]+")


def _lens_strip(canvas: _Canvas, row: Lensed) -> None:
    """Draw the lens layer bold and each layer that sets the focused key in its surface."""
    strip = _LENS_STRIP.search(row)
    if strip is None:
        return
    for word in _LAYER_WORD.finditer(strip.group(0)):
        start, end = strip.start() + word.start(), strip.start() + word.end()
        canvas.put(start, end, row.layers.get(word.group(0)), bold=word.group(0) == row.lens)
