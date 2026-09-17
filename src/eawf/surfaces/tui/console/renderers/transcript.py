"""transcript: the Run's blocks as an operator reads them, windowed around the cursor.

A window that shows a slice of the blocks carries a thumb column. Under a held clock the
feed is frozen and the blocks are exactly the authored history.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from eawf.surfaces.tui.console.frame import Fixed, View, g_frame, g_pad, snap_caret, strip_chips
from eawf.surfaces.tui.console.navigation import Ctx, busy
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.width import cell_len, pad

PREVIEW = 2
RUN_ID = "RUN-daa66d18"
TR_GLYPH: Mapping[str, str] = MappingProxyType(
    {
        "message": "¶",
        "tool": "›",  # noqa: RUF001
        "file": "±",
        "question": "?",
        "error": "!",
        "heartbeat": "~",
        "subagent": "»",
        "thinking": "°",
    }
)
# Cells the time, glyph and kind columns of a block head take before its text.
_HEAD_W = len(" 00:00:00  ¶ " + " " * 9)
_KEYS: tuple[tuple[str, str], ...] = (
    ("↑↓", "block"),
    ("Enter", "fold"),
    ("f", "follow"),
    ("y", "copy"),
    ("Esc", "back"),
)
Block = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Line:
    """One transcript line: the block it belongs to, whether it is the block's head."""

    block: int
    head: bool
    raw: str


def fmt_dur(seconds: int) -> str:
    """Return a duration as ``12s`` or ``2m 08s``."""
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m {seconds % 60:02d}s"


def blocks_now(authored: Sequence[Block]) -> list[Block]:
    """Return the blocks as they stand: under a held clock, the authored history verbatim."""
    return list(authored)


def _wrap_to(text: str, room: int) -> list[str]:
    out: list[str] = []
    cur = ""
    for word in text.split(" "):
        if not cur:
            cur = word
        elif cell_len(f"{cur} {word}") <= room:
            cur += f" {word}"
        else:
            out.append(cur)
            cur = word
    if cur:
        out.append(cur)
    return out


def hidden_lines(block: Block, w: int) -> int:
    """Return how many lines a block holds beyond its preview at width ``w``."""
    n = 1
    cur = ""
    for word in str(block["text"]).split(" "):
        if not cur:
            cur = word
        elif cell_len(f"{cur} {word}") <= w - 2 - _HEAD_W - 1:
            cur += f" {word}"
        else:
            n += 1
            cur = word
    return max(0, n - 2) + (len(block["body"]) if block.get("body") else 0)


def folds(session: Session) -> dict[int, bool]:
    """Return the session's open-block table, creating it on first use."""
    if session.tr_fold is None:
        session.tr_fold = {}
    return session.tr_fold


def _running(block: Block) -> bool:
    return bool(block.get("run") or block.get("runS") is not None)


def _right(block: Block, *, is_open: bool, hidden: int, w: int) -> str:
    """Return a block head's right-hand note: its duration, wait, fold or finish."""
    run_s = block.get("runS")
    dur = block.get("run") or (fmt_dur(run_s) if run_s is not None else None)
    wait_s = block.get("waitS")
    wait = block.get("wait") or (f"waiting {fmt_dur(wait_s)}" if wait_s is not None else None)
    if _running(block) and block.get("bg") != "done":
        fold = ("▾ " if is_open else "▸ ") if block.get("body") else ""
        est = f"{dur} · {block['est']}" if block.get("est") and w >= 120 else str(dur)
        return fold + est
    if wait:
        return str(wait)
    if hidden:
        return f"▾ {hidden} lines" if is_open else f"▸ {hidden} lines"
    return str(block.get("doneIn") or "")


def _lines(view: View, blocks: Sequence[Block]) -> list[Line]:
    """Return every visible transcript line, block heads first in each block."""
    s, w = view.session, view.w
    fold = folds(s)
    flat: list[Line] = []
    for i, block in enumerate(blocks):
        is_open = bool(fold.get(i))
        flight = _running(block) and block["k"] == "tool"
        background = block.get("bg") == "running"
        lead = ("»" if background else "⋯") if flight else TR_GLYPH[block["k"]]
        word = ("background" if background else "running") if flight else block["k"]
        head = f" {block['at']}  {lead} " + pad(word, 11)
        right = _right(block, is_open=is_open, hidden=hidden_lines(block, w), w=w)
        room = (w - 2) - cell_len(head) - (cell_len(right) + 2 if right else 0) - 1
        wrapped = _wrap_to(block["text"], room)
        shown = wrapped if is_open else wrapped[:PREVIEW]
        tail = f"  {right}" if right else ""
        flat.append(Line(i, True, head + pad(shown[0], room) + tail))
        flat.extend(Line(i, False, pad("", cell_len(head)) + line) for line in shown[1:])
        if is_open and block.get("body"):
            body = block["body"]
            flat.extend(Line(i, False, pad("", 13) + "  " + strip_chips(x)) for x in body)
    return flat


@dataclass(frozen=True, slots=True)
class Window:
    """The slice of transcript lines on screen."""

    lines: list[Line]
    top: int
    take: int
    above: int
    below: int


def _window(flat: list[Line], sel: int, track_h: int) -> Window:
    """Return the window centred on the selected block head, its edge markers paid for."""
    cur = next((i for i, line in enumerate(flat) if line.block == sel and line.head), 0)
    take = track_h
    top = above = below = 0
    lines: list[Line] = []
    for _ in range(3):
        top = max(0, min(max(0, len(flat) - take), cur - take // 2))
        lines = flat[top : top + take]
        above = top
        below = len(flat) - (top + len(lines))
        want = track_h - (1 if above else 0) - (1 if below else 0)
        if take == want:
            break
        take = want
    return Window(lines, top, take, above, below)


def _state(view: View, blocks: Sequence[Block]) -> str:
    """Return the Run's state word: thinking, running, idle under a held clock, or done."""
    think = next((b for b in blocks if b["k"] == "thinking" and _running(b)), None)
    live = next(
        (
            b
            for b in blocks
            if _running(b) and b.get("bg") != "running" and b["k"] not in ("subagent", "thinking")
        ),
        None,
    )
    for word, block in (("THINKING", think), ("RUNNING", live)):
        if block is not None:
            return f"{word} for " + str(block.get("run") or fmt_dur(block["runS"]))
    return "idle" if view.held else "SUCCEEDED"


def _context(view: View, blocks: Sequence[Block]) -> str:
    s, w = view.session, view.w
    background = sum(1 for b in blocks if b.get("bg") == "running")
    where = " running in the background" if w >= 120 else " in background"
    return (
        f"Run {RUN_ID} · {_state(view, blocks)}"
        + (f" · {background}{where}" if background else "")
        + " · "
        + ("following" if s.follow else "held")
        + (f" · {len(blocks)} blocks" if w >= 120 else "")
    )


def render(view: View) -> list[str]:
    """Return the Transcript frame."""
    s, w, h = view.session, view.w, view.h
    blocks = blocks_now(view.fixture.registers.tr_blocks)
    last = len(blocks) - 1
    s.tr_sel = max(0, min(last, last if s.tr_sel is None or s.follow else s.tr_sel))
    flat = _lines(view, blocks)
    track_h = h - 4
    win = _window(flat, s.tr_sel, track_h)
    sliced = len(flat) > win.take
    width = w - 2 if sliced else w
    body: list[str] = []
    if win.above:
        body.append(pad(f" … {win.above} above", width))
    for line in win.lines:
        mark = "▸" if line.head and line.block == s.tr_sel else " "
        body.append(g_pad(mark + line.raw[1:], width))
    if win.below:
        body.append(pad(f" … {win.below} below", width))
    body[:0] = [pad("", width)] * max(0, track_h - len(body))
    if sliced:
        body = [f"{row} █" for row in body]
    rows = [Fixed(pad(strip_chips(snap_caret(row)), w)) for row in body]
    return g_frame(
        view,
        crumb=f"Eä ▸ … ▸ {RUN_ID} ▸ Transcript",
        ctx=_context(view, blocks),
        body=rows,
        keys=_KEYS,
    )


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    """Walk the blocks, fold the selected one, toggle following and copy a block."""
    s = ctx.s
    if s.route != "transcript" or busy(s):
        return False
    blocks = blocks_now(ctx.fixture.registers.tr_blocks)
    sel = len(blocks) - 1 if s.tr_sel is None else s.tr_sel
    s.tr_sel = sel
    if key in ("ArrowDown", "ArrowUp"):
        s.tr_sel = max(0, min(len(blocks) - 1, sel + (1 if key == "ArrowDown" else -1)))
        s.follow = False
        block = blocks[s.tr_sel]
        ctx.log(key, f"{block['k']} · {block['at']}")
        return True
    if key == "Enter":
        n = hidden_lines(blocks[sel], ctx.w)
        if not n:
            ctx.log("Enter", "this block has nothing folded away")
            return True
        fold = folds(s)
        fold[sel] = not fold.get(sel)
        ctx.log("Enter", ("opened " if fold[sel] else "folded ") + f"{n} lines")
        return True
    if key == "f":
        s.follow = not s.follow
        if s.follow:
            s.tr_sel = len(blocks) - 1
        ctx.log("f", "following the tail" if s.follow else "held where you are")
        return True
    if key == "y":
        block = blocks[sel]
        ctx.notify(f"{RUN_ID} · {block['k']} at {block['at']}", "copied")
        ctx.log("y", "copied the block under the cursor")
        return True
    return False
