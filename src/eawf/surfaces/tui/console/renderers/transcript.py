"""transcript: the Run's blocks as an operator reads them, windowed around the cursor.

A window that shows a slice of the blocks carries a thumb column. Under a held clock the
feed is frozen and the blocks are exactly the authored history.

Under a held read model the blocks are the Run's event lines in stream order rather than
the prototype feed. Three things the native frame draws are things the prototype cannot:
a range the daemon never received is a block of its own saying how many sequences are
missing, the state row names the thinking state as derived because no event states it,
and the sequence a contiguous read reaches is printed beside the block count. With no
read model held the route draws its epoch-1 frame, which the golden contract replays.
"""

from __future__ import annotations

import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any

from eawf.kernel.projection.transcript import (
    PurgedRange,
    TranscriptBlock,
    TranscriptReadModel,
)
from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.cells import value_cell
from eawf.surfaces.tui.console.derive import plural
from eawf.surfaces.tui.console.format import clock_time, group
from eawf.surfaces.tui.console.frame import (
    Breadth,
    Fixed,
    View,
    acting_pairs,
    bar,
    build,
    g_frame,
    g_pad,
    needs_count,
    scope_label,
    snap_caret,
    strip_chips,
    thin,
)
from eawf.surfaces.tui.console.header import header_row
from eawf.surfaces.tui.console.keybar import keybar, route_pairs
from eawf.surfaces.tui.console.navigation import Ctx, busy, copied
from eawf.surfaces.tui.console.renderers.read_model import (
    crumb,
    native,
    route_crumb,
    unstated_rows,
)
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.width import cell_len, pad

PREVIEW = 2
RUN_ID = pt.TRANSCRIPT_RUN
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
_KEYS = route_pairs("transcript")
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


def _right(block: Block, *, is_open: bool, hidden: int, wide: bool) -> str:
    """Return a block head's right-hand note: its duration, wait, fold or finish."""
    run_s = block.get("runS")
    dur = block.get("run") or (fmt_dur(run_s) if run_s is not None else None)
    wait_s = block.get("waitS")
    wait = block.get("wait") or (f"waiting {fmt_dur(wait_s)}" if wait_s is not None else None)
    if _running(block) and block.get("bg") != "done":
        fold = ("▾ " if is_open else "▸ ") if block.get("body") else ""
        est = f"{dur} · {block['est']}" if block.get("est") and wide else str(dur)
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
        right = _right(block, is_open=is_open, hidden=hidden_lines(block, w), wide=view.wide)
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
    s = view.session
    background = sum(1 for b in blocks if b.get("bg") == "running")
    where = " running in the background" if view.wide else " in background"
    return (
        f"Run {RUN_ID} · {_state(view, blocks)}"
        + (f" · {background}{where}" if background else "")
        + " · "
        + ("following" if s.follow else "held")
        + (f" · {len(blocks)} blocks" if view.wide else "")
    )


def cursor(session: Session, total: int) -> int:
    """Return the block the cursor sits on, clamped into ``total``, and publish the count.

    Both frames walk the same cursor, so the clamp lives here rather than twice: a
    native run shorter than the prototype feed must not leave the cursor past its end,
    and a run longer than it must let the arrows reach the tail.

    Args:
        session: The session whose ``tr_sel`` is the cursor and whose ``count`` is the
            block run the frame drew, which the key seam bounds itself by.
        total: How many blocks the frame is about to draw.

    Returns:
        The block offset the caret goes on; ``0`` for an empty run.
    """
    session.count = total
    last = total - 1
    kept = last if session.tr_sel is None or session.follow else session.tr_sel
    here = max(0, min(last, kept))
    session.tr_sel = here
    return here


def _proto_frame(view: View) -> list[str]:
    """Return the epoch-1 Transcript frame the prototype registers drive."""
    s, w, h = view.session, view.w, view.h
    blocks = blocks_now(view.fixture.registers.tr_blocks)
    sel = cursor(s, len(blocks))
    flat = _lines(view, blocks)
    track_h = h - 4
    win = _window(flat, sel, track_h)
    sliced = len(flat) > win.take
    width = w - 2 if sliced else w
    body: list[str] = []
    if win.above:
        body.append(pad(f" … {win.above} above", width))
    for line in win.lines:
        mark = "▸" if line.head and line.block == sel else " "
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


#: The glyph each native lane carries in the kind column, beside its full word.
NATIVE_GLYPH: Mapping[str, str] = MappingProxyType(
    {
        **TR_GLYPH,
        "background": "»",
        "running": "⋯",
        "purged": "✗",
        "event": "·",
    }
)

#: The lead glyph of a lane the console has no glyph for. A lane it cannot name is still
#: drawn, with a mark saying the console does not recognise it.
UNGLYPHED = "·"

#: What the state row prints beside the thinking cell. No event states that an agent is
#: thinking, so the frame never presents that inference as an observation.
DERIVED_LABEL = "derived"

#: What the block section says for a Run that has produced nothing.
NO_BLOCK = "∅ this Run has produced no event · nothing has been observed of it yet"

#: What a purged block shows in its fold slot: the store holds none of it.
PURGED_MARK = "✗ purged"

# The kind column's width: the longest kind word and its gap.
_KIND_W = 11


def _purged_text(purged: PurgedRange) -> str:
    """Return what a purged block says: the range, and whether a replay was asked for."""
    sequences = f"{group(purged.first)}-{group(purged.last)}"
    asked = "replay requested" if purged.replay_requested else "no replay requested"
    if not purged.replay_available:
        asked = "replay unavailable"
    return f"{group(purged.count)} sequences purged · {sequences} · {asked}"


def native_word(block: TranscriptBlock) -> str:
    """Return the kind word a block's second column states.

    A command still going is ``background`` when it was detached and ``running`` when
    the Run is held by it; every other block states its lane.
    """
    if block.in_flight and block.lane == "tool":
        return "background" if block.background else "running"
    return block.lane


def _block_text(block: TranscriptBlock) -> str:
    """Return what a block says: its purged range, or its text with its kind named."""
    if block.purged is not None:
        return _purged_text(block.purged)
    return f"{block.kind.value} · {value_cell(block.text).full}"


def reference(view: View, model: TranscriptReadModel) -> datetime | None:
    """Return the instant elapsed times are measured to: now, else the last block's.

    Under a held console clock no wall time is read, so the feed stays the authored tail.
    """
    if view.now is not None and not view.held:
        return view.now
    return model.blocks[-1].at if model.blocks else None


def _seconds(block: TranscriptBlock, to: datetime | None) -> int:
    return max(0, int((to - block.at).total_seconds())) if to is not None else 0


def _note(
    block: TranscriptBlock, *, hidden: int, is_open: bool, to: datetime | None, breadth: Breadth
) -> str:
    """Return a block head's right-hand note: its elapsed, its fold or its hole.

    A block in flight states how long it has run and, where width allows, how long its
    kind of work usually takes -- a derived comparison, never a countdown: the wide
    layout shows it and the widest labels it.
    """
    if block.purged is not None:
        return PURGED_MARK
    if block.in_flight:
        text = fmt_dur(_seconds(block, to))
        if block.typical_seconds is not None and breadth >= Breadth.WIDE:
            typical = f"~{fmt_dur(block.typical_seconds)}"
            text += f" · {typical}" + (" typical" if breadth is Breadth.XWIDE else "")
        return text
    if hidden:
        return f"▾ {hidden} lines" if is_open else f"▸ {hidden} lines"
    return ""


# The cells a fold note takes with its gap, at most: ``▾ 999 lines``.
_FOLD_NOTE_W = 13


def _wrap_two(text: str, first: int, rest: int) -> list[str]:
    """Return ``text`` wrapped at words: the first line to ``first`` cells, the rest to ``rest``."""
    wrapped = _wrap_to(text, first)
    if len(wrapped) <= 1:
        return wrapped
    words = text.split(" ")[wrapped[0].count(" ") + 1 :]
    return [wrapped[0], *_wrap_to(" ".join(words), rest)]


def _layout(view: View, model: TranscriptReadModel, index: int) -> tuple[str, list[str], str]:
    """Return one block's head cells, its wrapped text and its note, at the frame's width.

    The first line leaves room for the note; a block whose note is a fold reserves the
    widest fold note, so the count it states is the count of lines it really hides.
    """
    block, w = model.blocks[index], view.w
    word = native_word(block)
    head = f" {clock_time(block.at)}  {NATIVE_GLYPH.get(word, UNGLYPHED)} " + pad(word, _KIND_W)
    room = max(8, (w - 2) - cell_len(head) - 1)
    to = reference(view, model)
    fixed = _note(block, hidden=0, is_open=False, to=to, breadth=view.breadth)
    reserve = cell_len(fixed) + 2 if fixed else _FOLD_NOTE_W
    wrapped = _wrap_two(_block_text(block), max(4, room - reserve), room)
    hidden = max(0, len(wrapped) - PREVIEW)
    is_open = bool(folds(view.session).get(index))
    return head, wrapped, _note(block, hidden=hidden, is_open=is_open, to=to, breadth=view.breadth)


def _native_block_lines(view: View, model: TranscriptReadModel, index: int) -> list[str]:
    """Return one block's lines: its head, then the lines its fold state shows.

    Blocks are prose: they wrap inside the region and fold beyond the preview, and are
    never cut with an ellipsis.
    """
    head, wrapped, note = _layout(view, model, index)
    room = max(8, (view.w - 2) - cell_len(head) - 1)
    is_open = bool(folds(view.session).get(index))
    shown = wrapped if is_open else wrapped[:PREVIEW]
    first = room - (cell_len(note) + 2 if note else 0)
    lines = [head + pad(shown[0], first) + (f"  {note}" if note else "")]
    lines.extend(pad("", cell_len(head)) + line for line in shown[1:])
    return lines


def native_hidden(view: View, model: TranscriptReadModel, index: int) -> int:
    """Return how many lines block ``index`` folds away at the frame's width."""
    _head, wrapped, _note_text = _layout(view, model, index)
    return max(0, len(wrapped) - PREVIEW)


def native_context(view: View, model: TranscriptReadModel) -> str:
    """Return the line under the header: what is true of the Run right now.

    It names the Run, whether it is thinking or held by work and for how long, how many
    pieces of work run in the background, whether the view follows the tail, and the
    block count -- every one derived from the blocks rather than stored beside them.
    """
    s = view.session
    run = s.subj_id or (model.rows[0].key if model.rows else "no run")
    to = reference(view, model)
    going = [b for b in model.blocks if b.in_flight]
    thinking = next((b for b in reversed(going) if b.lane == "thinking"), None)
    held = next((b for b in reversed(going) if b.lane == "tool" and not b.background), None)
    if thinking is not None:
        state = f"THINKING for {fmt_dur(_seconds(thinking, to))}"
    elif held is not None:
        state = f"RUNNING for {fmt_dur(_seconds(held, to))}"
    else:
        # nothing in flight says nothing about the Run itself, so its register status is
        # stated beside it rather than a claim that nothing runs
        found = model.index_of(run)
        stored = value_cell(model.rows[found].field("status")).slot if found is not None else None
        quiet = "nothing in flight" if model.blocks else "no event recorded yet"
        state = f"{stored} · {quiet}" if stored else quiet
    background = sum(1 for b in going if b.background or b.lane == "subagent")
    parts = [f"Run {run}", state]
    if background:
        parts.append(f"{background} running in the background")
    parts += ["following" if s.follow else "held", plural(len(model.blocks), "block")]
    return " · ".join(parts)


def _state_rows(model: TranscriptReadModel, w: int) -> list[str]:
    """Return the state row, and the thinking cell's reason under it when the cell is marked.

    The state row carries the thinking cell as its token and word, its label and what the
    run reaches; the reason goes on its own rows, wrapped at the frame width rather than
    cut, and without the state word the row above already printed.
    """
    thinking = value_cell(model.thinking)
    parts = [
        f"{thinking.slot} {thinking.word}".rstrip() + f" · {DERIVED_LABEL}",
        plural(len(model.blocks), "block"),
        plural(len(model.purged), "purged range"),
        f"contiguous through {group(model.last_contiguous_sequence)}",
    ]
    if model.quarantined:
        parts.append(f"{group(model.quarantined)} quarantined")
    rows = [" STATE     " + " · ".join(parts)]
    if thinking.reason:
        indent = " " * 11
        rows.extend(indent + line for line in textwrap.wrap(thinking.basis, width=max(8, w - 12)))
    return rows


def _crumb(view: View, model: TranscriptReadModel) -> str:
    """Return the crumb through the Run to the Transcript leaf, as the Run frame's climbs."""
    run = view.session.subj_id
    return route_crumb(view, model, run, "Transcript") if run else crumb(view, model)


def native_frame(view: View, model: TranscriptReadModel) -> list[str]:
    """Return the Transcript frame drawn from the read model the daemon served.

    The frame opens on the latest block and fills from the bottom; a held view keeps its
    row and counts what is above and below it; the scrollbar column is drawn only when
    something is off screen.

    Args:
        view: The render being built; its session carries the block the cursor sits on.
        model: The route's read model at the committed cursor.

    Returns:
        The full frame, keybar last. A purged range occupies a block of its own, so the
        run is never renumbered around a hole the console cannot fill.
    """
    session, w, h = view.session, view.w, view.h
    sel = cursor(session, len(model.blocks))
    opening: list[str] = [
        header_row(
            session,
            crumb=_crumb(view, model),
            scope=scope_label(view, model.scope_id),
            needs=needs_count(view),
            w=w,
        ),
        Fixed(pad(" " + native_context(view, model), w)),
        bar(w),
        *_state_rows(model, w),
        thin(w),
    ]
    closing: list[str] = [thin(w), *unstated_rows(model)]
    # the keybar takes the last row, and the two sections take their own
    track = max(1, h - len(opening) - len(closing) - 1)
    flat = [
        Line(index, n == 0, line)
        for index in range(len(model.blocks))
        for n, line in enumerate(_native_block_lines(view, model, index))
    ]
    body: list[str] = []
    if flat:
        win = _window(flat, sel, track)
        sliced = bool(win.above or win.below)
        width = w - 2 if sliced else w
        if win.above:
            body.append(pad(f" … {win.above} above", width))
        body.extend(
            g_pad(("▸" if line.head and line.block == sel else " ") + line.raw[1:], width)
            for line in win.lines
        )
        if win.below:
            body.append(pad(f" … {win.below} below", width))
        # the feed fills from the bottom, so a short run sits against the section's foot
        body[:0] = [pad("", width)] * max(0, track - len(body))
        if sliced:
            body = [f"{row} █" for row in body]
    else:
        body.append(f" BLOCKS    {NO_BLOCK}")
    rows = [*opening, *(Fixed(pad(strip_chips(row), w)) for row in body), *closing]
    session.nav_rows = len(model.blocks)
    folded = native_hidden(view, model, sel) if model.block_at(sel) is not None else 0
    # Enter folds the block under the caret, so a block with nothing folded offers nothing,
    # and y copies it, so a Run with no block offers no copy
    held = model.block_at(sel) is not None
    pairs = [pair for pair in _KEYS if (folded or pair[0] != "Enter") and (held or pair[0] != "y")]
    return build(view, rows, keybar(acting_pairs(view, pairs), w))


def render(view: View) -> list[str]:
    """Return the Transcript frame, native when a read model is held, epoch-1 otherwise."""
    model = native(view)
    if isinstance(model, TranscriptReadModel):
        return native_frame(view, model)
    return _proto_frame(view)


#: What Enter and ``y`` answer on a transcript that holds no block to fold or copy.
_EMPTY_ANSWERS: Mapping[str, str] = MappingProxyType(
    {
        "Enter": "no block to fold · this Run has produced no event",
        "y": "no block to copy · this Run has produced no event",
    }
)


def _native_seam(ctx: Ctx, key: str, model: TranscriptReadModel) -> bool:
    """Walk, fold, follow and copy the blocks of a held read model."""
    s = ctx.s
    total = len(model.blocks)
    sel = max(0, total - 1) if s.tr_sel is None else s.tr_sel
    s.tr_sel = sel
    if key in ("ArrowDown", "ArrowUp"):
        s.tr_sel = max(0, min(total - 1, sel + (1 if key == "ArrowDown" else -1)))
        s.follow = False
        block = model.block_at(s.tr_sel)
        ctx.log(key, f"{block.lane} · {clock_time(block.at)}" if block else "no block")
        return True
    block = model.block_at(sel)
    if key in _EMPTY_ANSWERS and block is None:
        ctx.notify(_EMPTY_ANSWERS[key], key)
        ctx.log(key, _EMPTY_ANSWERS[key])
        return True
    if key == "Enter":
        view = View(session=s, fixture=ctx.fixture, w=ctx.w, h=ctx.h, gutter=ctx.gutter)
        n = native_hidden(view, model, sel) if model.block_at(sel) is not None else 0
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
            s.tr_sel = max(0, total - 1)
        ctx.log("f", "following the tail" if s.follow else "held where you are")
        return True
    if key == "y" and block is not None:
        what = f"{block.lane} at {clock_time(block.at)}"
        written = ctx.copy(block.text.value or what, shown=what)
        ctx.log("y", f"{copied(written)} the block under the cursor")
        return True
    return False


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    """Walk the blocks, fold the selected one, toggle following and copy a block."""
    s = ctx.s
    if s.route != "transcript" or busy(s):
        return False
    if isinstance(ctx.projection, TranscriptReadModel):
        return _native_seam(ctx, key, ctx.projection)
    blocks = blocks_now(ctx.fixture.registers.tr_blocks)
    # the frame publishes the run it drew, so the arrows reach the tail of a native run
    # longer than the prototype feed and stop at the end of a shorter one
    total = s.count or len(blocks)
    sel = total - 1 if s.tr_sel is None else s.tr_sel
    s.tr_sel = sel
    if key in ("ArrowDown", "ArrowUp"):
        s.tr_sel = max(0, min(total - 1, sel + (1 if key == "ArrowDown" else -1)))
        s.follow = False
        walked = blocks[s.tr_sel] if s.tr_sel < len(blocks) else None
        ctx.log(key, f"{walked['k']} · {walked['at']}" if walked else f"block {s.tr_sel + 1}")
        return True
    if key == "Enter":
        n = hidden_lines(blocks[sel], ctx.w) if sel < len(blocks) else 0
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
            s.tr_sel = total - 1
        ctx.log("f", "following the tail" if s.follow else "held where you are")
        return True
    if key == "y":
        picked = blocks[sel] if sel < len(blocks) else None
        what = f"{picked['k']} at {picked['at']}" if picked else f"block {sel + 1}"
        written = ctx.copy(f"{RUN_ID} · {what}")
        ctx.log("y", f"{copied(written)} the block under the cursor")
        return True
    return False
