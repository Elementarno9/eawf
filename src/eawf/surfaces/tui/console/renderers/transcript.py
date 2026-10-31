"""transcript: the Run's blocks as an operator reads them, windowed around the cursor.

A window that shows a slice of the blocks carries a thumb column. Under a held clock the
feed is frozen and the blocks are exactly the authored history.

Under a held read model the blocks are the Run's event lines in stream order rather than
the prototype feed. Three things the native frame draws are things the prototype cannot:
a range the daemon never received is a block of its own saying how many sequences are
missing, the state row labels the thinking state derived when eawf inferred it for a
provider with no start marker, and the sequence a contiguous read reaches is printed
beside the block count. With no read model held the route draws its epoch-1 frame, which
the golden contract replays.
"""

from __future__ import annotations

import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any

from eawf.kernel.projection.transcript import (
    PurgedRange,
    SubagentLine,
    TranscriptBlock,
    TranscriptReadModel,
)
from eawf.kernel.projection.truth import TruthField, TruthKind
from eawf.kernel.runtime.events import RunEventKind
from eawf.kernel.runtime.usage import UsageTotals
from eawf.kernel.state.epoch2.run import RunStatus
from eawf.runtime.runtimes.run_transcript import TranscriptEntry, TranscriptSource
from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.cells import value_cell
from eawf.surfaces.tui.console.derive import plural
from eawf.surfaces.tui.console.format import clock_time, group
from eawf.surfaces.tui.console.frame import (
    Breadth,
    Fixed,
    Receded,
    View,
    acting_pairs,
    bar,
    build,
    g_frame,
    g_pad,
    snap_caret,
    strip_chips,
    thin,
)
from eawf.surfaces.tui.console.keybar import CHROME_KEYS, KEY, KeyEntry, keybar, route_pairs
from eawf.surfaces.tui.console.keymap import native_keys
from eawf.surfaces.tui.console.live_reads import HeldRawTranscript, held_raw_transcript, held_usage
from eawf.surfaces.tui.console.navigation import Ctx, busy, copied
from eawf.surfaces.tui.console.renderers.budget_lines import cost_line
from eawf.surfaces.tui.console.renderers.read_model import (
    UNKNOWN_WORD,
    cell,
    crumb,
    native,
    native_header,
    route_crumb,
)
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.tokens import RAIL
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
# The native bar names Space beside f, since both hold and follow a live Run's tail.
_NATIVE_KEYS = tuple(
    entry.pair() for entry in native_keys("transcript", windowed=False) if entry not in CHROME_KEYS
)
_NEWEST = KeyEntry("newest", ("End",)).pair()
_PAGE = KEY["page"].pair()
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


def _pieces(word: str, room: int) -> list[str]:
    """Return *word* cut into runs of at most *room* cells.

    A reference or digest has no space to wrap at, and a word wider than the region
    would otherwise be clipped at its edge, which a block may never be.
    """
    if cell_len(word) <= room:
        return [word]
    pieces = [""]
    for char in word:
        if pieces[-1] and cell_len(pieces[-1] + char) > room:
            pieces.append("")
        pieces[-1] += char
    return pieces


def _wrap_to(text: str, room: int) -> list[str]:
    out: list[str] = []
    cur = ""
    for word in (piece for raw in text.split(" ") for piece in _pieces(raw, room)):
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


def _scrollbar(body: list[str], win: Window, total: int) -> list[str]:
    """Return ``body`` with the scrollbar column appended: a thumb on a thin track.

    The thumb's length is the share of the transcript on screen and its offset the share
    above it, both scaled to the track and kept inside it, so a reader sees where the
    window sits in a long run as well as that it was cut.

    Args:
        body: The block section's rows, one per track row.
        win: The window the rows draw.
        total: How many transcript lines the window was cut from; more than ``win`` shows.

    Returns:
        Each row followed by a space and ``█`` on a thumb row or ``│`` on a track row.
    """
    track = len(body)
    size = min(track, max(1, round(track * len(win.lines) / total)))
    start = max(0, min(track - size, round(track * win.top / total)))
    return [f"{row} {'█' if start <= i < start + size else RAIL}" for i, row in enumerate(body)]


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

#: What the state row prints beside a thinking cell eawf inferred, so the frame never
#: presents that inference as an observation.
DERIVED_LABEL = "derived"

#: What the state row says beside the Run's status when no reasoning turn is open.
NOT_THINKING = "no reasoning turn open"

#: What the state row ends on before the Run's first usage reading.
USAGE_NOT_READ = "usage not read yet"

#: What the line under the header says of a RUNNING Run with no block open: the Run
#: itself is still in flight, between the things it does.
RUN_IN_FLIGHT = "in flight"

#: What the in-progress row says of a running Run waiting on no tool: the model has the turn.
MODEL_TURN = "model turn"

#: What the in-progress row says of a queued Run, which the daemon has not admitted yet.
AWAITING_ADMISSION = "waiting on admission"

# The lanes whose block is a tool call a running Run can be waiting on.
_TOOL_LANES = frozenset({"tool", "file"})

#: What the block section says for a Run that has produced nothing.
NO_BLOCK = "∅ this Run has produced no event · nothing has been observed of it yet"

#: What a purged block shows in its fold slot: the store holds none of it.
PURGED_MARK = "✗ purged"

#: What a delegation whose child's lines were not read shows in its fold slot.
UNAVAILABLE_MARK = "∅ unavailable"

# The cells a block body's label column takes: ``WAITS ON`` and its gap.
_LABEL_W = 10

# The kind column's width: the longest kind word and its gap.
_KIND_W = 11

# The in-progress row's head: no clock time, since it is no recorded line, then the
# running glyph and its kind word in the columns a block head puts them in.
_LIVE_HEAD = f" {' ' * 8}  {NATIVE_GLYPH['running']} " + pad("now", _KIND_W)


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
    """Return what a block says: its purged range, or its own words.

    The kind column already names the kind as glyph and word, so the text is the block's
    first line alone.
    """
    if block.purged is not None:
        return _purged_text(block.purged)
    return value_cell(block.text).full


def _delegation_body(line: SubagentLine) -> list[tuple[str, str]]:
    """Return a delegation block's labelled lines: what the child does now or how it
    ended, what it found, and that it reports back into this Run.

    A child whose own lines were not read states that instead of its now and found, so
    an unreadable transcript is never drawn as a child that said nothing.
    """
    ended = line.outcome is not None
    first = ("ENDED", str(line.outcome)) if ended else ("NOW", line.now or "nothing yet")
    rows = (
        [first, ("FOUND", line.found or "nothing reported yet")]
        if line.readable
        else [("NOW", f"{UNAVAILABLE_MARK} · the child's transcript could not be read")]
    )
    verb = "reported back into" if ended else "reports back into"
    rows.append(("REPORTS", f"{verb} {line.reports_to}" + ("" if ended else " when it ends")))
    return rows


def _body_lines(block: TranscriptBlock, room: int) -> list[str]:
    """Return a block's body under its text, each labelled line wrapped as prose.

    A delegation's body is read from its child; every other kind's comes with the block.
    """
    rows = list(block.body) if block.delegation is None else _delegation_body(block.delegation)
    lines: list[str] = []
    for label, text in rows:
        wrapped = _wrap_to(text, max(4, room - _LABEL_W)) or [""]
        lines.append(pad(label, _LABEL_W) + wrapped[0])
        lines.extend(" " * _LABEL_W + rest for rest in wrapped[1:])
    return lines


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
    """Return a block head's right-hand note: its elapsed, its durations, its fold or its hole.

    A block in flight states how long it has run and, where width allows, how long its
    kind of work usually takes -- a derived comparison, never a countdown: the wide
    layout shows it and the widest labels it. A finished block states the input and
    output tokens and the seconds of the model turn that produced it and how long its
    call took, before its fold mark, so a slow Run shows whether its time went to the
    model or to its tools.
    """
    if block.purged is not None:
        return PURGED_MARK
    if block.in_flight:
        text = fmt_dur(_seconds(block, to))
        if block.lane == "question":
            text = f"waiting {text}"
        if block.typical_seconds is not None and breadth >= Breadth.WIDE:
            typical = f"~{fmt_dur(block.typical_seconds)}"
            text += f" · {typical}" + (" typical" if breadth is Breadth.XWIDE else "")
        # work in flight that folds lines away still says so, before its elapsed
        return (("▾ " if is_open else "▸ ") if hidden else "") + text
    spent = [
        f"+{group(tokens)} {word}"
        for tokens, word in ((block.turn_input_tokens, "in"), (block.turn_output_tokens, "out"))
        if tokens is not None
    ]
    if block.turn_seconds is not None:
        spent.append(f"turn {fmt_dur(block.turn_seconds)}")
    if block.duration_seconds is not None:
        spent.append(fmt_dur(block.duration_seconds))
    mark = ""
    if block.delegation is not None and not block.delegation.readable and not is_open:
        mark = UNAVAILABLE_MARK
    elif hidden:
        mark = f"▾ {plural(hidden, 'line')}" if is_open else f"▸ {plural(hidden, 'line')}"
    return " · ".join(part for part in (*spent, mark) if part)


# The most lines a fold note is sized for, so its widest form is ``▾ 999 lines``.
_MOST_HIDDEN = 999


def _wrap_two(text: str, first: int, rest: int) -> list[str]:
    """Return ``text`` wrapped at words: the first line to ``first`` cells, the rest to ``rest``."""
    wrapped = _wrap_to(text, first)
    if len(wrapped) <= 1:
        return wrapped
    words = text.split(" ")[wrapped[0].count(" ") + 1 :]
    return [wrapped[0], *_wrap_to(" ".join(words), rest)]


@dataclass(frozen=True, slots=True)
class _Laid:
    """One block laid out at the frame's width: its head, wrapped text, body and note."""

    head: str
    wrapped: list[str]
    body: list[str]
    note: str

    @property
    def hidden(self) -> int:
        """Return how many lines the closed fold hides: text past the preview, and the body."""
        return max(0, len(self.wrapped) - PREVIEW) + len(self.body)


def _layout(view: View, model: TranscriptReadModel, index: int) -> _Laid:
    """Return one block laid out at the frame's width.

    The preview shows the first two lines of the text and the fold opens the rest and
    the body. The first line leaves room for the note as wide as it can grow: its
    durations or elapsed beside the widest fold mark, so the count it states is the
    count of lines it really hides.
    """
    block, w = model.blocks[index], view.w
    word = native_word(block)
    head = f" {clock_time(block.at)}  {NATIVE_GLYPH.get(word, UNGLYPHED)} " + pad(word, _KIND_W)
    room = max(8, (w - 2) - cell_len(head) - 1)
    to = reference(view, model)
    widest = _note(block, hidden=_MOST_HIDDEN, is_open=False, to=to, breadth=view.breadth)
    reserve = cell_len(widest) + 2
    wrapped = _wrap_two(_block_text(block), max(4, room - reserve), room)
    body = _body_lines(block, room)
    hidden = max(0, len(wrapped) - PREVIEW) + len(body)
    is_open = bool(folds(view.session).get(index))
    note = _note(block, hidden=hidden, is_open=is_open, to=to, breadth=view.breadth)
    return _Laid(head=head, wrapped=wrapped, body=body, note=note)


def _native_block_lines(view: View, model: TranscriptReadModel, index: int) -> list[str]:
    """Return one block's lines: its head, then the lines its fold state shows.

    Blocks are prose: they wrap inside the region and fold beyond the preview, and are
    never cut with an ellipsis.
    """
    laid = _layout(view, model, index)
    room = max(8, (view.w - 2) - cell_len(laid.head) - 1)
    is_open = bool(folds(view.session).get(index))
    shown = [*laid.wrapped, *laid.body] if is_open else laid.wrapped[:PREVIEW]
    note = laid.note
    first = room - (cell_len(note) + 2 if note else 0)
    lines = [laid.head + pad(shown[0], first) + (f"  {note}" if note else "")]
    lines.extend(pad("", cell_len(laid.head)) + line for line in shown[1:])
    return lines


def native_hidden(view: View, model: TranscriptReadModel, index: int) -> int:
    """Return how many lines block ``index`` folds away at the frame's width."""
    return _layout(view, model, index).hidden


def _awaited_tool(model: TranscriptReadModel) -> TranscriptBlock | None:
    """Return the newest tool call the Run is held by, or ``None`` when it waits on none.

    A call is awaited while it is in flight, and also as the newest block while it is
    only requested: a gateway call is in flight from its acceptance, yet the Run already
    waits on it. Work detached into the background holds nothing.
    """
    last = model.blocks[-1] if model.blocks else None
    return next(
        (
            b
            for b in reversed(model.blocks)
            if b.lane in _TOOL_LANES
            and not b.background
            and (b.in_flight or (b is last and b.kind is RunEventKind.TOOL_REQUESTED))
        ),
        None,
    )


def _in_progress(view: View, model: TranscriptReadModel) -> str | None:
    """Return what the Run is doing now and for how long, or ``None`` once it is not live.

    A running Run waits on the tool call it is held by, timed from its request, or else
    on the model, timed from when the latest block's work ended; a queued Run waits on
    admission, timed from its latest block. A Run with no block yet has nothing to time
    from, so the row names the wait alone.

    Args:
        view: The render being built; its clock is the instant the elapsed runs to.
        model: The route's read model, whose stored Run status says whether it is live.

    Returns:
        The row's words, or ``None`` for a Run in any status but queued or running.
    """
    run = view.session.subj_id or (model.rows[0].key if model.rows else None)
    status = _run_status(model, run)
    stored = status.value if status is not None else None
    if stored not in (RunStatus.RUNNING, RunStatus.QUEUED):
        return None
    tool = _awaited_tool(model) if stored == RunStatus.RUNNING else None
    since: datetime | None = None
    if tool is not None:
        # the block's first words name the call: its tool id, tool name or command family
        words = f"waiting on tool {_block_text(tool).split(' · ')[0]}"
        since = tool.at
    else:
        words = MODEL_TURN if stored == RunStatus.RUNNING else AWAITING_ADMISSION
        if model.blocks:
            latest = model.blocks[-1]
            # a finished call's line is its request, so its work ended a duration later
            since = latest.at + timedelta(seconds=latest.duration_seconds or 0)
    to = reference(view, model)
    if since is None or to is None:
        return words
    return f"{words} · {fmt_dur(max(0, int((to - since).total_seconds())))}"


def native_context(view: View, model: TranscriptReadModel) -> str:
    """Return the line under the header: what is true of the Run right now.

    It names the Run, whether it is thinking, held by work or waiting on a question and
    for how long, how many pieces of work run in the background, whether the view follows
    the tail, and, where width allows, the block count -- every one derived from the
    blocks rather than stored beside them.
    """
    s = view.session
    run = s.subj_id or (model.rows[0].key if model.rows else "no run")
    to = reference(view, model)
    going = [b for b in model.blocks if b.in_flight]
    thinking = next((b for b in reversed(going) if b.lane == "thinking"), None)
    held = next((b for b in reversed(going) if b.lane == "tool" and not b.background), None)
    asked = next((b for b in reversed(going) if b.lane == "question"), None)
    state = ""
    if thinking is not None:
        state = f"THINKING for {fmt_dur(_seconds(thinking, to))}"
    elif held is not None:
        state = f"RUNNING for {fmt_dur(_seconds(held, to))}"
    elif asked is not None:
        state = f"WAITING for {fmt_dur(_seconds(asked, to))}"
    background = sum(1 for b in going if b.background or b.lane == "subagent")
    if not state:
        # nothing holding the Run says nothing about the Run itself, so its register
        # status is stated beside it rather than a claim that nothing runs; with work in
        # the background the count below says what is in flight, and a RUNNING Run is
        # itself in flight between its blocks
        status = _run_status(model, run)
        stored = value_cell(status).slot if status is not None else None
        quiet = "nothing in flight" if model.blocks else "no event recorded yet"
        if status is not None and status.value == RunStatus.RUNNING:
            quiet = f"{RUN_IN_FLIGHT} · " + ("no block open" if model.blocks else quiet)
        state = " · ".join(part for part in (stored, "" if background else quiet) if part)
    parts = [f"Run {run}", state]
    if background:
        parts.append(f"{background} running in the background")
    parts.append(follow_word(s, len(model.blocks)))
    if view.wide:
        parts.append(plural(len(model.blocks), "block"))
    return " · ".join(parts)


def follow_word(session: Session, total: int) -> str:
    """Return ``following``, or ``held · N new`` counting the blocks that arrived since.

    Args:
        session: The session whose follow flag and hold point are read.
        total: How many blocks the frame draws now.
    """
    if session.follow:
        return "following"
    since = total if session.tr_held_from is None else session.tr_held_from
    return f"held · {max(0, total - since)} new"


def _run_status(model: TranscriptReadModel, run: str | None) -> TruthField[str] | None:
    """Return the stored status of the Run the frame is about, or ``None`` when none is held."""
    found = model.index_of(run)
    return model.rows[found].field("status") if found is not None else None


def _usage_words(usage: UsageTotals | None) -> str:
    """Return the Run's totals as the state row ends on them.

    A counter no reading reported is left out rather than drawn as zero, and a Run whose
    readings priced nothing says so rather than stating a cost of nothing.
    """
    if usage is None:
        return USAGE_NOT_READ
    counts = [
        f"{word} {group(tokens)}"
        for word, tokens in (
            ("in", usage.input_tokens),
            ("out", usage.output_tokens),
            ("cache", usage.cache_tokens),
        )
        if tokens is not None
    ]
    cost = usage.cost_microusd
    priced = "cost unpriced" if cost is None else f"cost {cost / 1_000_000:.4f} USD"
    return " · ".join((*counts, priced))


def _state_rows(view: View, model: TranscriptReadModel, w: int) -> list[str]:
    """Return the state row, and the thinking cell's reason under it when the cell is marked.

    The state row carries the thinking cell as its token and word, its label and what the
    run reaches; the reason goes on its own rows, wrapped at the frame width rather than
    cut, and without the state word the row above already printed. With no reasoning turn
    open the row leads with the Run's stored status instead, since a Run that is not
    thinking is still in a state the register knows. The row ends on what the Run's
    usage readings add up to.
    """
    run = view.session.subj_id or (model.rows[0].key if model.rows else None)
    status = _run_status(model, run)
    thinking = value_cell(model.thinking)
    if not model.is_thinking() and status is not None:
        thinking = value_cell(status)
        state = f"{thinking.full} · {NOT_THINKING}"
    else:
        state = f"{thinking.slot} {thinking.word}".rstrip()
        # a turn a provider's own start marker opened is observed, and says nothing more
        if model.thinking.truth_kind is TruthKind.DERIVED:
            state += f" · {DERIVED_LABEL}"
    parts = [
        state,
        plural(len(model.blocks), "block"),
        plural(len(model.purged), "purged range"),
        f"contiguous through {group(model.last_contiguous_sequence)}",
    ]
    if model.quarantined:
        parts.append(f"{group(model.quarantined)} quarantined")
    parts.append(_usage_words(model.usage))
    rows = [" STATE     " + " · ".join(parts)]
    if thinking.reason:
        indent = " " * 11
        rows.extend(indent + line for line in textwrap.wrap(thinking.basis, width=max(8, w - 12)))
    return rows


def _crumb(view: View, model: TranscriptReadModel) -> str:
    """Return the crumb through the Run to the Transcript leaf, as the Run frame's climbs."""
    run = view.session.subj_id
    return route_crumb(view, model, run, "Transcript") if run else crumb(view, model)


def _outcome_rows(view: View, model: TranscriptReadModel) -> list[str]:
    """Return how the Run ended, as its stored status says, and what it cost so far.

    The cost is the Run's usage read: until it arrives the line says so rather than
    drawing a zero, and a Run whose readings priced nothing says it is unmetered. Both
    share one row, so the blocks keep every row they had at the narrowest frame.
    """
    run = view.session.subj_id or (model.rows[0].key if model.rows else None)
    found = model.index_of(run)
    if found is None:
        outcome = f"{UNKNOWN_WORD} · no Run is held"
    else:
        row = model.rows[found]
        failure = row.field("failure").value
        outcome = cell(row.field("outcome")) + (f" · {failure}" if failure else "")
    usage = held_usage(view.live, run) if run is not None else None
    cost = (
        cost_line(usage.cost_microusd, usage.cap_cost_microusd).removeprefix("cost ")
        if usage is not None
        else f"{UNKNOWN_WORD} · usage not read yet"
    )
    return [f" OUTCOME   {outcome} · cost {cost}"]


#: The cells an entry's text is indented by under its label.
_RAW_INDENT = 5

#: The key that swaps the blocks for the runtime's transcript files and back.
_RAW = KEY["raw"].pair()

#: The raw view's bar: the cursor walks lines, since one entry can outgrow the screen.
_RAW_KEYS = (
    KeyEntry("line", ("ArrowUp", "ArrowDown")).pair(),
    _PAGE,
    _NEWEST,
    *(pair for pair in _NATIVE_KEYS if pair[1] == "follow"),
    _RAW,
    *(pair for pair in _NATIVE_KEYS if pair[1] in ("copy", "back")),
)

#: What the raw view's body says when the Run has no transcript file to show.
NO_RAW_ENTRY = "∅ the transcript files hold no entry yet"


def _printable(text: str) -> str:
    """Return one line of *text* with tabs expanded and other control characters made visible.

    Tool output carries terminal escapes, which would move the cursor of the terminal
    the frame is drawn on.
    """
    return "".join(
        char if char.isprintable() else "\N{REPLACEMENT CHARACTER}" for char in text.expandtabs(4)
    )


def _raw_label(entry: TranscriptEntry) -> str:
    """Return what an entry's label line says: who spoke, or which tool was called."""
    if entry.kind == "tool_call":
        return f"tool call {entry.tool_name}"
    if entry.kind == "tool_result":
        failed = " error" if entry.is_error else ""
        return f"tool result{failed} {entry.tool_name or 'of an unnamed call'}"
    return entry.kind


def _raw_heading(source: TranscriptSource) -> str:
    """Return the line that opens one transcript file's entries."""
    whose = "subagent transcript" if source.subagent else "transcript"
    of = source.run_key or "no Run"
    return f" ── {source.runtime} {whose} of {of}"


def raw_lines(held: HeldRawTranscript, room: int) -> list[tuple[int, str]]:
    """Return every line the raw view draws, with the entry each belongs to.

    Each entry is a label line, then its whole text, line by line, wrapped at words to
    *room* cells and never cut. A heading opens the entries of each file whenever the
    file read from changes.

    Args:
        held: The entries read so far.
        room: The cells an entry's text may take after its indent.

    Returns:
        ``(entry index, line)`` pairs in drawing order; a heading belongs to the entry
        under it.
    """
    lines: list[tuple[int, str]] = []
    last: TranscriptSource | None = None
    for index, (source, entry) in enumerate(held.entries):
        if source != last:
            lines.append((index, _raw_heading(source)))
            last = source
        lines.append((index, f" {_raw_label(entry)}"))
        for paragraph in entry.text.splitlines() or [""]:
            pieces = _wrap_to(_printable(paragraph), room) or [""]
            lines.extend((index, " " * _RAW_INDENT + piece) for piece in pieces)
    return lines


def _raw_room(w: int) -> int:
    """Return the cells an entry's text takes at frame width *w*, its scrollbar paid for."""
    return max(8, w - 2 - _RAW_INDENT - 1)


def _raw_context(
    view: View, model: TranscriptReadModel, held: HeldRawTranscript, total: int
) -> str:
    """Return the line under the header while the raw view is on."""
    s = view.session
    run = s.subj_id or (model.rows[0].key if model.rows else "no run")
    files = len({source.path for source, _entry in held.entries})
    parts = [f"Run {run}", "runtime transcript"]
    if held.unindexed is None:
        entries = len(held.entries)
        parts += [
            f"{group(entries)} {'entry' if entries == 1 else 'entries'}",
            plural(files, "file"),
        ]
    parts.append(follow_word(s, total))
    return " · ".join(parts)


def raw_frame(view: View, model: TranscriptReadModel, held: HeldRawTranscript) -> list[str]:
    """Return the Transcript frame drawing the runtime's own transcript files in full.

    The header, state and outcome rows are the event view's, since they are about the
    Run. The body is every entry whole, read from the files indexed for the Run; a
    following view ends on the newest line, and a held one keeps its line and counts
    the lines that arrived since.

    Args:
        view: The render being built; its session's ``tr_sel`` is the line under the
            cursor.
        model: The route's read model, which the header and state rows are drawn from.
        held: The raw read's answer for the Run.

    Returns:
        The full frame, keybar last.
    """
    session, w, h = view.session, view.w, view.h
    lines = raw_lines(held, _raw_room(w))
    total = len(lines)
    sel = cursor(session, total)
    opening: list[str] = [
        native_header(view, _crumb(view, model), model.scope_id),
        Fixed(pad(" " + _raw_context(view, model, held, total), w)),
        bar(w),
        *_state_rows(view, model, w),
        thin(w),
    ]
    closing: list[str] = [thin(w), *_outcome_rows(view, model)]
    track = max(1, h - len(opening) - len(closing) - 1)
    body: list[str] = []
    if held.unindexed is not None:
        body.append(f" ENTRIES   ∅ {held.unindexed}")
    elif not lines:
        body.append(f" ENTRIES   {NO_RAW_ENTRY}")
    else:
        flat = [Line(n, True, raw) for n, (_index, raw) in enumerate(lines)]
        win = _window(flat, sel, track)
        session.tr_page = max(1, len(win.lines))
        sliced = bool(win.above or win.below)
        width = w - 2 if sliced else w
        if win.above:
            body.append(pad(f" … {win.above} above", width))
        body.extend(
            g_pad(("▸" if line.block == sel else " ") + line.raw[1:], width) for line in win.lines
        )
        if win.below:
            body.append(pad(f" … {win.below} below", width))
        if sliced:
            body = _scrollbar(body, win, total)
    body.extend([pad("", w)] * max(0, track - len(body)))
    drawn = [Fixed(pad(strip_chips(row), w)) for row in body]
    pairs = [pair for pair in _RAW_KEYS if lines or pair[0] in (_RAW[0], "Esc")]
    return build(view, [*opening, *drawn, *closing], keybar(acting_pairs(view, pairs), w))


def _native_pairs(view: View, model: TranscriptReadModel, sel: int) -> list[tuple[str, str]]:
    """Return the block view's bar pairs for the block under the caret, *sel*."""
    folded = native_hidden(view, model, sel) if model.block_at(sel) is not None else 0
    # Enter folds the block under the caret, so a block with nothing folded offers nothing,
    # and y copies it, so a Run with no block offers no copy
    held = model.block_at(sel) is not None
    pairs = [
        pair
        for pair in _NATIVE_KEYS
        if (folded or pair[0] != "Enter") and (held or pair[0] != "y") and pair != _RAW
    ]
    # the raw key is offered once the console has read the Run's transcript files, which
    # only a console launched in the tree that indexed them does
    if held_raw_transcript(view.live) is not None:
        pairs.insert(len(pairs) - 1, _RAW)
    # a live Run's blocks grow under a held view, so End, back to the newest block and
    # following it, sits beside follow; the prototype's golden bars stay the table's own
    after_follow = next(i for i, pair in enumerate(pairs) if pair[1] == "follow") + 1
    pairs.insert(after_follow, _NEWEST)
    # the page keys step as the arrows do, a window at a time, so they sit beside them
    after_block = next(i for i, pair in enumerate(pairs) if pair[1] == "block") + 1
    pairs.insert(after_block, _PAGE)
    return pairs


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
        native_header(view, _crumb(view, model), model.scope_id),
        Fixed(pad(" " + native_context(view, model), w)),
        bar(w),
        *_state_rows(view, model, w),
        thin(w),
    ]
    closing: list[str] = [thin(w), *_outcome_rows(view, model)]
    # the keybar takes the last row, and the two sections take their own
    track = max(1, h - len(opening) - len(closing) - 1)
    flat = [
        Line(index, n == 0, line)
        for index in range(len(model.blocks))
        for n, line in enumerate(_native_block_lines(view, model, index))
    ]
    live = _in_progress(view, model)
    live_shown = live is not None
    body: list[str] = []
    if flat:
        # the in-progress row sits under the blocks as a line of a block past the last,
        # so the caret, which marks a real block, never lands on it
        if live is not None:
            flat.append(Line(len(model.blocks), True, _LIVE_HEAD + live))
        win = _window(flat, sel, track)
        if live is not None and session.follow:
            tail = _window(flat, len(model.blocks), track)
            # a following window ends on the row, unless that scrolls the caret's own
            # head away, as opening a block longer than the section would
            if any(line.head and line.block == sel for line in tail.lines):
                win = tail
        session.tr_page = sum(
            1 for line in win.lines if line.head and line.block < len(model.blocks)
        )
        live_shown = live_shown and not win.below
        sliced = bool(win.above or win.below)
        # a cut window is what the page keys page through, so the bar offers them then
        session.windowed = session.windowed or sliced
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
            body = _scrollbar(body, win, len(flat))
    else:
        body.append(f" BLOCKS    {NO_BLOCK}")
        if live is not None:
            body.append(_LIVE_HEAD + live)
    drawn: list[str] = [Fixed(pad(strip_chips(row), w)) for row in body]
    if live_shown:
        # the in-progress row is always the last row drawn, and greyed as not yet a block
        drawn[-1] = Receded(drawn[-1])
    rows = [*opening, *drawn, *closing]
    session.nav_rows = len(model.blocks)
    pairs = _native_pairs(view, model, sel)
    return build(view, rows, keybar(acting_pairs(view, pairs), w))


def render(view: View) -> list[str]:
    """Return the Transcript frame, native when a read model is held, epoch-1 otherwise."""
    model = native(view)
    if isinstance(model, TranscriptReadModel):
        raw = held_raw_transcript(view.live)
        if view.session.tr_raw and raw is not None:
            return raw_frame(view, model, raw)
        return native_frame(view, model)
    return _proto_frame(view)


#: What Enter and ``y`` answer on a transcript that holds no block to fold or copy.
_EMPTY_ANSWERS: Mapping[str, str] = MappingProxyType(
    {
        "Enter": "no block to fold · this Run has produced no event",
        "y": "no block to copy · this Run has produced no event",
    }
)

#: What End answers when the cursor already follows the newest block.
_ALREADY_NEWEST = "already on the newest block · following the tail"


def _hold(s: Session, total: int) -> None:
    """Stop following, counting arrivals from the *total* blocks drawn now."""
    if s.follow or s.tr_held_from is None:
        s.tr_held_from = total
    s.follow = False


def _resume(s: Session, total: int) -> None:
    """Follow the tail again, the cursor on the newest of *total* blocks."""
    s.follow = True
    s.tr_held_from = None
    s.tr_sel = max(0, total - 1)


#: How many blocks one notch of the mouse wheel moves the cursor.
WHEEL_BLOCKS = 3

# The keys that move the cursor and the way each one moves it; a page key's step is
# scaled by the blocks the last window showed.
_MOVES: Mapping[str, int] = MappingProxyType(
    {
        "ArrowUp": -1,
        "ArrowDown": 1,
        "PageUp": -1,
        "PageDown": 1,
        "WheelUp": -WHEEL_BLOCKS,
        "WheelDown": WHEEL_BLOCKS,
    }
)


def _move(ctx: Ctx, key: str, model: TranscriptReadModel, sel: int) -> None:
    """Move the cursor from block ``sel`` as ``key`` does, holding or following.

    The move is clamped to the first and the newest block. Landing on the newest block
    follows the tail, as ``End`` does; landing anywhere above it holds the view.

    Args:
        ctx: The keystroke's context; its session's cursor and follow flag change.
        key: One of the moving keys, an arrow, a page key or a wheel notch.
        model: The read model whose blocks the cursor walks.
        sel: The block the cursor sat on before the key.
    """
    s = ctx.s
    total = len(model.blocks)
    newest = max(0, total - 1)
    page = max(1, s.tr_page) if key in ("PageUp", "PageDown") else 1
    to = max(0, min(newest, sel + _MOVES[key] * page))
    was = s.follow
    s.tr_sel = to
    if to == newest:
        _resume(s, total)
    else:
        _hold(s, total)
    if (to, s.follow) == (sel, was):
        # a move at the edge changes nothing, and a log line would toast every notch
        return
    block = model.block_at(to)
    note = f"{block.lane} · {clock_time(block.at)}" if block else "no block"
    ctx.log(key, note + (" · following the tail" if s.follow else ""))


def _native_seam(ctx: Ctx, key: str, model: TranscriptReadModel) -> bool:
    """Walk, fold, hold, follow and copy the blocks of a held read model.

    ``Space`` and ``f`` toggle between holding the cursor where it is and following the
    newest block, and ``End`` jumps to the newest block and follows it. An arrow, a page
    key or a wheel notch holds the view, unless it lands on the newest block, which
    follows the tail again as ``End`` does.
    """
    s = ctx.s
    total = len(model.blocks)
    sel = max(0, total - 1) if s.tr_sel is None else s.tr_sel
    s.tr_sel = sel
    if key in _MOVES:
        _move(ctx, key, model, sel)
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
    if key in ("f", " "):
        if s.follow:
            _hold(s, total)
        else:
            _resume(s, total)
        named = "Space" if key == " " else key
        ctx.log(named, "following the tail" if s.follow else "held where you are")
        return True
    if key == "End":
        if s.follow and sel == max(0, total - 1):
            ctx.notify(_ALREADY_NEWEST, "End")
            ctx.log("End", _ALREADY_NEWEST)
            return True
        _resume(s, total)
        ctx.log("End", "the newest block · following the tail")
        return True
    if key == "y" and block is not None:
        what = f"{block.lane} at {clock_time(block.at)}"
        written = ctx.copy(block.text.value or what, shown=what)
        ctx.log("y", f"{copied(written)} the block under the cursor")
        return True
    return False


#: What the raw key answers in a console that has read no transcript file for the Run.
NO_RAW_READ = "no transcript file read · the console reads them in the tree it was launched in"


def _toggle_raw(ctx: Ctx, raw: HeldRawTranscript | None) -> None:
    """Swap the blocks for the runtime's transcript files, or back, at the newest line."""
    s = ctx.s
    if raw is None:
        ctx.notify(NO_RAW_READ, _RAW[0])
        ctx.log(_RAW[0], NO_RAW_READ)
        return
    s.tr_raw = not s.tr_raw
    # the two views walk different things, so each opens following its own tail
    s.tr_sel = None
    s.follow = True
    s.tr_held_from = None
    ctx.log(_RAW[0], "the runtime's transcript files" if s.tr_raw else "the event blocks")


def _raw_seam(ctx: Ctx, key: str, raw: HeldRawTranscript) -> bool:
    """Walk, hold, follow and copy the lines of the runtime's transcript files.

    The cursor walks lines rather than entries, so an entry taller than the screen is
    read through line by line. Landing on the newest line follows the tail, as ``End``
    does; any other line holds the view.
    """
    s = ctx.s
    total = s.count
    newest = max(0, total - 1)
    sel = newest if s.tr_sel is None else s.tr_sel
    if key in _MOVES:
        page = max(1, s.tr_page) if key in ("PageUp", "PageDown") else 1
        s.tr_sel = max(0, min(newest, sel + _MOVES[key] * page))
        if s.tr_sel == newest:
            _resume(s, total)
        else:
            _hold(s, total)
        ctx.log(key, f"line {s.tr_sel + 1} of {total}")
        return True
    if key in ("f", " "):
        if s.follow:
            _hold(s, total)
        else:
            _resume(s, total)
        ctx.log("Space" if key == " " else key, "following the tail" if s.follow else "held")
        return True
    if key == "End":
        _resume(s, total)
        ctx.log("End", "the newest line · following the tail")
        return True
    if key == "y":
        lines = raw_lines(raw, _raw_room(ctx.w))
        if not lines:
            ctx.notify(NO_RAW_ENTRY, "y")
            return True
        source, entry = raw.entries[lines[min(sel, len(lines) - 1)][0]]
        what = f"{_raw_label(entry)} of {source.run_key or 'a subagent'}"
        written = ctx.copy(entry.text, shown=what)
        ctx.log("y", f"{copied(written)} the entry under the cursor")
        return True
    return False


def _linked_seam(ctx: Ctx, key: str, model: TranscriptReadModel) -> bool:
    """Act on a key over a held read model: in the raw view, or over the blocks."""
    raw = held_raw_transcript(ctx.live)
    if key == _RAW[0]:
        _toggle_raw(ctx, raw)
        return True
    if ctx.s.tr_raw and raw is not None:
        return _raw_seam(ctx, key, raw)
    return _native_seam(ctx, key, model)


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    """Walk the blocks, fold the selected one, toggle following and copy a block."""
    s = ctx.s
    if s.route != "transcript" or busy(s):
        return False
    if isinstance(ctx.projection, TranscriptReadModel):
        return _linked_seam(ctx, key, ctx.projection)
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
