"""Frame composition: the header and keybar rows, rules, column tables and ``build``.

A renderer returns ``build(view, rows, keys)``: exactly H rows of W cells with the keybar
last. ``build`` pads and clips the body rows, snaps a gap-separated cursor against the row
it marks, paints the ``--verbose`` trace row and the notification rack over the body, and
swaps the keybar of an absent or record frame. A row a renderer has already laid out is
marked :class:`Fixed` so ``build`` neither snaps nor clips it.

Some rows carry chip markers: control characters that colour a cell without taking a
column. The plain-text path strips them, and every helper here measures a row without
them, so a renderer can keep the markers in place.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from eawf.surfaces.tui.console.attention import open_count
from eawf.surfaces.tui.console.fixture import EntryState, Fixture
from eawf.surfaces.tui.console.header import ProcessValue, header_row
from eawf.surfaces.tui.console.keybar import KEY, KeyEntry, Pair, keybar
from eawf.surfaces.tui.console.session import Session, Toast
from eawf.surfaces.tui.console.tokens import RULE_HEAVY, RULE_THIN, Severity
from eawf.surfaces.tui.console.width import cell_len, clip_words, pad

CARET = "▸"
_CARET_GAP = re.compile(r"^(\s*)▸(\s{2,})(\S)")
_GUTTER = 13


@dataclass(frozen=True, slots=True, kw_only=True)
class View:
    """What one render reads.

    Attributes:
        session: The session the render reads and publishes into.
        fixture: The registers.
        w: The frame width in cells.
        h: The frame height in rows.
        verbose: Whether the ``--verbose`` trace row is painted.
        held: Whether the console clock is held, which freezes every live feed.
    """

    session: Session
    fixture: Fixture
    w: int
    h: int
    verbose: bool = False
    held: bool = False


class Fixed(str):
    """A row ``build`` neither snaps nor clips: it is already laid out."""

    __slots__ = ()


def bar(w: int) -> str:
    """Return the heavy rule under a frame's title rows."""
    return RULE_HEAVY * w


def thin(w: int) -> str:
    """Return the thin rule between a frame's sections."""
    return RULE_THIN * w


def snap_caret(row: str) -> str:
    """Move a gap-separated cursor against the row it marks, keeping the row's width."""
    found = _CARET_GAP.match(row)
    if not found:
        return row
    gap = found.group(2)
    return found.group(1) + " " * (len(gap) - 1) + f"{CARET} " + row[len(found.group(0)) - 1 :]


def header(view: View, crumb: str) -> str:
    """Return the frame's header row for ``crumb``.

    The pre-session layer shows its process value; every other route shows the open
    attention count, derived from the register now, and the connection value.
    """
    session, proto = view.session, view.fixture.proto
    if session.route == "entry":
        state = entry_state(view)
        process = ProcessValue(glyph=state.glyph, label=state.state)
        return header_row(
            session, crumb=crumb, scope=proto.scope, needs=0, w=view.w, process=process
        )
    needs = open_count(view.fixture) if proto.attention else 0
    return header_row(session, crumb=crumb, scope=proto.scope, needs=needs, w=view.w)


def entry_state(view: View) -> EntryState:
    """Return the entry layer's current pre-session state, the first past the end."""
    states = view.fixture.proto.entry
    sel = view.session.entry_sel
    return states[sel] if sel < len(states) else states[0]


class Table:
    """A table's columns declared once, composing both its head and its rows.

    Args:
        cols: Column widths; the last column is the remainder and is never padded.
        mark: The cursor gutter's width, which the head clears too.
    """

    __slots__ = ("cols", "mark")

    def __init__(self, cols: Sequence[int], mark: int = 2) -> None:
        self.cols = list(cols)
        self.mark = mark

    def head(self, names: Sequence[str]) -> str:
        """Return the head row."""
        out = " " + " " * self.mark
        for i, name in enumerate(names):
            out += pad(name, self.cols[i]) if i < len(self.cols) - 1 else name
        return out

    def row(self, cells: Sequence[str], cur: bool = False) -> str:
        """Return one row, the cursor in the gutter when ``cur``."""
        lead = (CARET + " " * (self.mark - 1) if cur else " " * self.mark) if self.mark else ""
        out = " " + lead
        for i, cell in enumerate(cells):
            out += pad(cell, self.cols[i]) if i < len(self.cols) - 1 else cell
        return out


TABLES: Mapping[str, Table] = MappingProxyType(
    {
        "RD": Table([25, 11, 0], 4),
        "RC": Table([11, 38, 9, 0], 4),
        "CR": Table([40, 0], 2),
        "MB": Table([34, 11, 0]),
        "RN": Table([34, 10, 12, 0]),
    }
)


def route_keys_bar(view: View, entries: Sequence[KeyEntry]) -> str:
    """Return a route keybar; a record frame with fewer than two rows drops ``↑↓ row``."""
    nav = view.session.record_nav
    one_row = nav is not None and len(nav) < 2
    return keybar([e.pair() for e in entries if not (one_row and e == KEY["up"])], view.w)


def _swapped_keys(session: Session, keys: str, w: int) -> str:
    """Return the keybar an absent or record frame shows in place of the route's."""
    if session.absent:
        return keybar([KEY["esc"].pair()], w)
    if session.record_facts is None:
        return keys
    nav = session.record_nav or []
    lead = [KEY["up"], KEY["enter"]] if len(nav) > 1 else ([KEY["enter"]] if nav else [])
    entries = [*lead, KEY["actions"], KEY["inspect"], KEY["esc"]]
    return keybar([entry.pair() for entry in entries], w)


def build(view: View, rows: Sequence[str], keys: str) -> list[str]:
    """Return the full frame: H rows of W cells, the keybar last.

    Args:
        view: The render being built.
        rows: The header, the subtitle and the body rows, top to bottom.
        keys: The route's keybar row.

    Raises:
        ValueError: a composed row is not exactly W cells.
    """
    session, w, h = view.session, view.w, view.h
    session.absent_frame = session.absent
    keys = _swapped_keys(session, keys, w)
    session.absent = False
    out: list[str] = [
        row + " " * max(0, w - cell_len(row)) if isinstance(row, Fixed) else pad(snap_caret(row), w)
        for row in rows[: h - 1]
    ]
    out.extend(" " * w for _ in range(h - 1 - len(out)))
    if view.verbose:
        paint_verbose(session, out, w)
    paint_rack(session, out, w, verbose=view.verbose)
    out.append(keys)
    for i, row in enumerate(out):
        if cell_len(row) != w:
            raise ValueError(f"row {i} is {cell_len(row)} cells, not {w}: {row!r}")
    return out


# ---------- chip markers and the table grid most routes share ----------

_CHIPS = re.compile("[\\x01-\\x07\\x10-\\x12]")
_CHIP_MARKS: Mapping[str, str] = MappingProxyType(
    {"ok": "\x01", "wn": "\x02", "er": "\x03", "info": "\x04", "dm": "\x05"}
)
CHIP_END = "\x06"
LABEL_MARK = "\x07"


def strip_chips(text: str) -> str:
    """Return ``text`` without its chip markers."""
    return _CHIPS.sub("", text)


def chip(kind: str, text: str) -> str:
    """Return ``text`` marked as a chip of ``kind``; the marker never takes a column.

    Raises:
        KeyError: ``kind`` names no chip.
    """
    return _CHIP_MARKS[kind] + text + CHIP_END


def g_pad(text: str, n: int) -> str:
    """Return ``text`` as ``n`` visible cells, measured without its chip markers."""
    vis = cell_len(strip_chips(text))
    if vis > n:
        return pad(strip_chips(text), n)
    return text if vis == n else text + " " * (n - vis)


def lab(name: str, text: str = "") -> str:
    """Return a labelled row: the label in the thirteen-cell gutter, then ``text``."""
    return g_pad(f" {LABEL_MARK}{name}{CHIP_END}", _GUTTER) + text


def rule_n(n: int) -> str:
    """Return a thin rule ``n`` cells wide."""
    return RULE_THIN * n


class Grid:
    """A table whose last cell takes the room left and whose cells ignore chip markers.

    An over-wide cell gives way to its width less one plus a space, so the next column
    never moves.

    Args:
        cols: Column widths; the last column is the remainder.
        mark: The cursor gutter's width.
    """

    __slots__ = ("cols", "mark")

    def __init__(self, cols: Sequence[int], mark: int = 2) -> None:
        self.cols = list(cols)
        self.mark = mark

    def head(self, names: Sequence[str]) -> str:
        """Return the head row."""
        out = " " + " " * self.mark
        for i, name in enumerate(names):
            out += g_pad(name, self.cols[i]) if i < len(self.cols) - 1 else name
        return out

    def row(self, cells: Sequence[str | None], cur: bool, w: int) -> str:
        """Return one row of a ``w``-cell frame, the cursor in the gutter when ``cur``."""
        lead = " "
        if self.mark:
            lead += CARET + " " * (self.mark - 1) if cur else " " * self.mark
        out = lead
        used = cell_len(strip_chips(lead))
        for i, raw in enumerate(cells):
            text = "" if raw is None else raw
            vis = cell_len(strip_chips(text))
            if i >= len(self.cols) - 1:
                room = w - used
                out += pad(strip_chips(text), max(1, room)) if vis > room else text
                break
            width = self.cols[i]
            if vis >= width:
                out += pad(strip_chips(text), max(1, width - 1)) + " "
            else:
                out += text + " " * (width - vis)
            used += width
        return out


def g_row(line: str, w: int) -> Fixed:
    """Return a laid-out row: cursor snapped, markers stripped, padded to the frame."""
    return Fixed(pad(strip_chips(snap_caret(line)), w))


def g_frame(
    view: View, *, crumb: str, ctx: str, body: Sequence[str], keys: Sequence[Pair]
) -> list[str]:
    """Return a frame of header, context row, heavy rule, ``body`` and keybar.

    Args:
        view: The render being built.
        crumb: The breadcrumb, without its leading gutter.
        ctx: The context row under the header.
        body: The body rows; a row not yet :class:`Fixed` is laid out here.
        keys: The keybar pairs.
    """
    w = view.w
    rows: list[str] = [header(view, " " + crumb), g_row(" " + ctx, w), bar(w)]
    rows.extend(row if isinstance(row, Fixed) else g_row(row, w) for row in body)
    return build(view, rows, keybar(keys, w))


def prose(name: str, sentences: Sequence[str], room: int) -> list[str]:
    """Return a labelled paragraph wrapped to ``room`` characters."""
    lines: list[str] = []
    cur = ""
    for word in " ".join(sentences).split(" "):
        if not cur:
            cur = word
        elif cell_len(f"{cur} {word}") <= room:
            cur += f" {word}"
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return [g_pad("", _GUTTER) + line if i else lab(name, line) for i, line in enumerate(lines)]


@dataclass(frozen=True, slots=True, kw_only=True)
class Scrollbar:
    """Where a card's window sits in its content.

    Attributes:
        total: The content's line count.
        take: The lines shown.
        start: The first line shown.
    """

    total: int
    take: int
    start: int


def boxed(
    view: View,
    *,
    crumb: str,
    ctx: str,
    pre: Sequence[str],
    title: str,
    lines: Sequence[str],
    foot: str | None,
    keys: Sequence[Pair],
    scrollbar: Scrollbar | None = None,
) -> list[str]:
    """Return a bordered card drawn over its own backdrop.

    Args:
        view: The render being built.
        crumb: The breadcrumb, without its leading gutter.
        ctx: The context row under the header.
        pre: Rows above the card.
        title: The card's title, set into its top border.
        lines: The card's content lines.
        foot: The row under the card, if any.
        keys: The keybar pairs.
        scrollbar: The window position; a card whose content is cut draws its thumb
            column inside the right border.
    """
    w = view.w
    body: list[str] = list(pre)
    sliced = scrollbar is not None and scrollbar.total > len(lines)
    top = f"┌─ {title} "
    body.append(top + RULE_THIN * max(0, w - cell_len(top) - 1) + "┐")
    for line in lines:
        body.append(f"│ {g_pad(line, w - 5)} █│" if sliced else f"│ {g_pad(line, w - 4)} │")
    body.append("└" + RULE_THIN * (w - 2) + "┘")
    if foot:
        body.append(" " + foot)
    return g_frame(view, crumb=crumb, ctx=ctx, body=body, keys=keys)


# ---------- the notification rack and the verbose row ----------


def toast_box(toast: Toast, w: int) -> list[str]:
    """Return a toast's three bordered rows, ``w`` cells wide."""
    glyph = "✗ " if toast.sev == Severity.ERR else ("! " if toast.sev == Severity.WARN else "")
    head = glyph + toast.title
    return [
        "┌─ " + head + " " + "─" * max(1, w - 5 - cell_len(head)) + "┐",
        "│ " + pad(clip_words(toast.text, w - 4), w - 4) + " │",
        "└" + "─" * (w - 2) + "┘",
    ]


def overlay_row(out: list[str], row: int, col: int, text: str) -> None:
    """Overwrite ``out[row]`` from column ``col``: the one place a row is painted over."""
    if row < 1 or row >= len(out):
        return
    base = out[row]
    out[row] = base[:col] + text + base[col + cell_len(text) :]


def paint_rack(session: Session, out: list[str], w: int, *, verbose: bool) -> None:
    """Paint the standing toasts bottom-right over the body, newest lowest, one shared width.

    Args:
        session: The session whose toasts are painted.
        out: The body rows, painted in place.
        w: The frame width in cells.
        verbose: Whether the verbose row takes the last body row.
    """
    if not session.toasts:
        return
    width = 0
    for toast in session.toasts:
        width = max(width, 28, cell_len(toast.title) + 9, cell_len(toast.text) + 4)
    width = min(w - 2, width)
    bottom = len(out) - 1 - (1 if verbose else 0)
    for toast in reversed(session.toasts):
        top = bottom - 2
        if top < 1:
            break
        for offset, text in enumerate(toast_box(toast, width)):
            overlay_row(out, top + offset, w - 1 - width, text)
        bottom = top - 1


def paint_verbose(session: Session, out: list[str], w: int) -> None:
    """Paint the ``--verbose`` trace over the last body row."""
    trace = session.trace or "no keystroke yet"
    overlay_row(out, len(out) - 1, 0, pad(f" VERBOSE   {trace}", w))
