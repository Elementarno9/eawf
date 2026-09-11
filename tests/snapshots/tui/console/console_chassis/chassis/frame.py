"""Frame composition: header row, keybar row, rules, the TBL column composer, the caret
snap and ``build()``, which turns a renderer's row list into exactly H rows of W cells.

A renderer returns ``build(...)``: the full frame, keybar last, exactly as the prototype's
``R[route]`` does, so a port is a transcription rather than a re-composition. A row that
the prototype produced as an object (already padded, never re-snapped) is marked with
``Fixed`` so ``build`` leaves it alone.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ..chassis.attention import open_count
from ..chassis.registry import step_leaf
from ..chassis.tokens import BRAND, RULE_HEAVY, RULE_THIN
from ..chassis.width import cell_len, pad

if TYPE_CHECKING:
    from ..chassis.fixture import Fixture
    from ..chassis.session import Session

Pair = tuple[str, str]
_SEP = " ▸ "
_ELLIPSIS = "…"
_CARET_GAP = re.compile(r"^(\s*)▸(\s{2,})(\S)")


class Fixed(str):
    """A row build() neither snaps nor clips: the prototype's object rows."""

    __slots__ = ()


def bar(w: int) -> str:
    return RULE_HEAVY * w


def thin(w: int) -> str:
    return RULE_THIN * w


def snap_caret(row: str) -> str:
    """The cursor sits against the row it marks; the shift preserves the row's width."""
    m = _CARET_GAP.match(str(row))
    if not m:
        return row
    gap = m.group(2)
    return m.group(1) + " " * (len(gap) - 1) + "▸ " + str(row)[len(m.group(0)) - 1 :]


def keybar(pairs: list[Pair], w: int) -> str:
    """One line: bold key, dim label, three-space gaps; trailing pairs drop until it fits."""
    fit = list(pairs)
    while True:
        plain = " " + "   ".join(f"{k} {label}" for k, label in fit)
        if cell_len(plain) <= w or len(fit) <= 1:
            break
        fit.pop()
    return plain + " " * max(0, w - cell_len(plain))


def crumb_from_history(session: Session, fixture: Fixture, bc: str) -> str:
    """While there is history, the middle of the crumb is the history: the path Esc walks."""
    if not session.back:
        return bc
    seg = str(bc).split(_SEP)
    leaf = seg[-1]
    hist = session.back.items()
    if hist[0].route == "scope.home" and not hist[0].subj:
        hist = hist[1:]
    hist = [b for b in hist if b.route != session.route]
    chain = [s for s in (step_leaf(b.route, b.subj) for b in hist) if s]
    chain = [
        s for i, s in enumerate(chain) if (i + 1 >= len(chain) or s != chain[i + 1]) and s != leaf
    ]
    gutter = re.match(r"^\s*", str(bc)).group(0)
    return gutter + _SEP.join([BRAND, fixture.scope, *chain, leaf])


def header_row(session: Session, fixture: Fixture, bc: str, w: int) -> str:
    """The one-line header: crumb left, ``!N NEEDS YOU  <glyph> <CONN>`` right."""
    P = fixture.proto
    if session.route == "entry":
        e0 = P.entry[session.entry_sel] if session.entry_sel < len(P.entry) else P.entry[0]
        right = f"{e0.glyph} {e0.state}"
        return pad(bc, w - cell_len(right)) + right
    needs = open_count(fixture) if P.attention else 0
    glyph = P.states.glyph[session.conn]
    right = (f"!{needs} NEEDS YOU  " if needs else "") + f"{glyph} {session.conn}"
    room = w - cell_len(right)
    full = bc.replace(" … ", f" {P.scope} ")
    if cell_len(full) <= room:
        bc = full
    bc = crumb_from_history(session, fixture, bc)
    if cell_len(bc) > room:
        seg = bc.split(_SEP)
        while cell_len(_SEP.join(seg)) > room and len(seg) > 3:
            if seg[2] == _ELLIPSIS:
                del seg[3]
            else:
                seg[2:3] = [_ELLIPSIS]
        bc = _SEP.join(seg)
    return pad(bc, room) + right


class TBL:
    """A table's columns declared once, composing both its header and its rows.

    ``mark`` is the cursor gutter's width, so the header clears it too. The last column
    is the remainder and is never padded.
    """

    __slots__ = ("cols", "mark")

    def __init__(self, cols: list[int], mark: int = 2) -> None:
        self.cols = list(cols)
        self.mark = mark

    def head(self, names: list[str]) -> str:
        s = " " + " " * self.mark
        for i, n in enumerate(names):
            s += pad(n, self.cols[i]) if i < len(self.cols) - 1 else n
        return s

    def row(self, cells: list[str], cur: bool = False) -> str:
        if self.mark:
            lead = "▸" + " " * (self.mark - 1) if cur else " " * self.mark
        else:
            lead = ""
        s = " " + lead
        for i, c in enumerate(cells):
            s += pad(c, self.cols[i]) if i < len(self.cols) - 1 else str(c)
        return s


TSPEC: dict[str, TBL] = {
    "RD": TBL([25, 11, 0], 4),
    "RC": TBL([11, 38, 9, 0], 4),
    "CR": TBL([40, 0], 2),
    "MB": TBL([34, 11, 0]),
    "DR": TBL([34, 19, 0]),
    "EVD": TBL([34, 19, 0]),
    "RN": TBL([34, 10, 12, 0]),
    "ST": TBL([22, 14, 12, 0]),
}


def bar_(session: Session, pairs: list[tuple] | None, w: int) -> str:
    """The route keybar: ``↑↓ row`` is dropped when a record frame has fewer than two rows."""
    ks: list[Pair] = [
        p.pair() if hasattr(p, "pair") else (str(p[0]), str(p[1])) for p in (pairs or ())
    ]
    if session.record_nav is not None and len(session.record_nav) < 2:
        ks = [p for p in ks if not (p[0] == "↑↓" and p[1] == "row")]
    return keybar(ks, w)


def build(session: Session, rows: list[str], keys: str, w: int, h: int) -> list[str]:
    """H rows of W cells: body rows padded and clipped, the rack and the verbose row
    painted over, the keybar last. The keybar is overridden for an absent or record frame."""
    from ..chassis.keys import KEY
    from ..chassis.rack import paint_rack, paint_verbose

    rows = [r for r in rows if r is not None]
    session.absent_frame = session.absent
    if session.absent:
        keys = keybar([(KEY["esc"].token, KEY["esc"].label)], w)
    elif session.record_facts is not None:
        nav = session.record_nav or []
        lead = [KEY["up"], KEY["enter"]] if len(nav) > 1 else ([KEY["enter"]] if nav else [])
        keys = keybar(
            [(k.token, k.label) for k in lead + [KEY["actions"], KEY["inspect"], KEY["esc"]]], w
        )
    session.absent = False
    out: list[str] = []
    for r in rows[: h - 1]:
        if isinstance(r, Fixed):
            out.append(str(r) + " " * max(0, w - cell_len(r)))
        else:
            out.append(pad(snap_caret(r), w))
    while len(out) < h - 1:
        out.append(" " * w)
    paint_verbose(session, out, w)
    paint_rack(session, out, w)
    out.append(keys)
    for i, r in enumerate(out):
        if cell_len(r) != w:
            raise ValueError(f"row {i} is {cell_len(r)} cells, not {w}: {r!r}")
    return out


# ---------- the Phase G helpers (proto-g.js) ----------
#
# proto-g rows may carry chip markers (control characters U+0001..U+0007, U+0010..U+0012)
# that colour a cell without occupying a column. The text path strips them; every helper
# below measures and pads on the stripped text so a port can keep the markers verbatim.

_CHIPS = re.compile("[\\x01-\\x07\\x10-\\x12]")


def strip_chips(text: str) -> str:
    return _CHIPS.sub("", str(text))


def chip(kind: str, text: str) -> str:
    """A marked cell; the marker survives padding and is stripped on the text path."""
    marks = {"ok": "\x01", "wn": "\x02", "er": "\x03", "info": "\x04", "dm": "\x05"}
    return marks[kind] + text + "\x06"


def g_pad(text: str, n: int) -> str:
    """proto-g's pad: measured on the stripped text, clamped through the core pad."""
    text = str(text)
    vis = cell_len(strip_chips(text))
    if vis > n:
        return pad(strip_chips(text), n)
    return text if vis == n else text + " " * (n - vis)


def pad_vis(text: str, n: int) -> str:
    text = str(text)
    vis = cell_len(strip_chips(text))
    return text if vis >= n else text + " " * (n - vis)


def lab(name: str, text: str = "") -> str:
    return g_pad(" \x07" + name + "\x06", 13) + (text or "")


def rule_n(n: int) -> str:
    return RULE_THIN * n


class GTBL:
    """proto-g's TBL: the last cell is clamped to the room left, an over-wide cell gives
    way to ``width - 1`` plus a space, and every measure ignores chip markers."""

    __slots__ = ("cols", "mark")

    def __init__(self, cols: list[int], mark: int = 2) -> None:
        self.cols = list(cols)
        self.mark = mark

    def head(self, names: list[str]) -> str:
        s = " " + " " * self.mark
        for i, n in enumerate(names):
            s += g_pad(n, self.cols[i]) if i < len(self.cols) - 1 else n
        return s

    def row(self, cells: list[str], cur: bool, w: int) -> str:
        if self.mark:
            lead = " " + ("▸" + " " * (self.mark - 1) if cur else " " * self.mark)
        else:
            lead = " "
        s = lead
        used = cell_len(strip_chips(lead))
        for i, c in enumerate(cells):
            t = "" if c is None else str(c)
            vis = cell_len(strip_chips(t))
            if i >= len(self.cols) - 1:
                room = w - used
                s += pad(strip_chips(t), max(1, room)) if vis > room else t
                used += min(vis, room)
                break
            cw = self.cols[i]
            s += (pad(strip_chips(t), max(1, cw - 1)) + " ") if vis >= cw else t + " " * (cw - vis)
            used += cw
        return s


def g_row(line: str, w: int) -> Fixed:
    """proto-g's ROW: snap the caret, strip the markers, pad to the frame; never re-snapped."""
    line = snap_caret(str(line))
    return Fixed(pad(strip_chips(line), w))


def g_frame(
    session: Session,
    fixture: Fixture,
    crumb: str,
    ctx: str,
    body: list[str],
    klist: list[Pair],
    w: int,
    h: int,
) -> list[str]:
    """proto-g's frame(): header, context row, heavy rule, the body rows, the keybar."""
    rows: list[str] = [header_row(session, fixture, " " + crumb, w), g_row(" " + ctx, w), bar(w)]
    rows.extend(r if isinstance(r, Fixed) else g_row(r, w) for r in body)
    return build(session, rows, keybar(list(klist), w), w, h)


def prose(name: str, sentences: list[str], room: int) -> list[str]:
    """A labelled paragraph wrapped to the width it is read at (proto-g's prose)."""
    words = " ".join(sentences).split(" ")
    lines: list[str] = []
    cur = ""
    for t in words:
        if not cur:
            cur = t
        elif len(cur + " " + t) <= room:
            cur += " " + t
        else:
            lines.append(cur)
            cur = t
    if cur:
        lines.append(cur)
    return [(g_pad("", 13) + line) if i else lab(name, line) for i, line in enumerate(lines)]


def boxed(
    session: Session,
    fixture: Fixture,
    crumb: str,
    ctx: str,
    pre: list[str],
    title: str,
    lines: list[str],
    foot: str | None,
    klist: list[Pair],
    w: int,
    h: int,
    sb: dict[str, int] | None = None,
) -> list[str]:
    """proto-g's boxed(): a bordered card over its own backdrop, with an optional scrollbar
    (``sb = {"total", "take", "from"}``) drawn as a thumb column inside the right border."""
    b: list[str] = list(pre)
    track = len(lines)
    thumb = -1
    tsize = 0
    if sb and sb["total"] > track:
        tsize = max(1, round(track * sb["take"] / sb["total"]))
        thumb = max(0, min(track - tsize, round(track * sb["from"] / sb["total"])))
    head = f"┌─ {title} "
    b.append(head + RULE_THIN * max(0, w - cell_len(head) - 1) + "┐")
    for line in lines:
        if thumb < 0:
            b.append("│ " + g_pad(line, w - 4) + " │")
            continue
        b.append("│ " + g_pad(line, w - 5) + " █│")
    b.append("└" + RULE_THIN * (w - 2) + "┘")
    if foot:
        b.append(" " + foot)
    return g_frame(session, fixture, crumb, ctx, b, klist, w, h)
