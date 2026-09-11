"""transcript: the run's blocks as an operator reads them, windowed around the cursor,
with a thumb column when the window is a slice. Under the held clock the feed is frozen
and the blocks are exactly the authored tail (the goldens' case); a live clock would
deliver TR_FEED in wall time, which is left for the product build."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...chassis.frame import Fixed, g_frame, g_pad, snap_caret, strip_chips
from ...chassis.keys import Ctx, go
from ...chassis.width import pad

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

PREVIEW = 2
RUN_ID = "RUN-daa66d18"
TR_GLYPH = {
    "message": "¶",
    "tool": "›",
    "file": "±",
    "question": "?",
    "error": "!",
    "heartbeat": "~",
    "subagent": "»",
    "thinking": "°",
}


def fmt_dur(s: int) -> str:
    return f"{s}s" if s < 60 else f"{s // 60}m {s % 60:02d}s"


def tr_now(session: Session, fixture: Fixture) -> list[dict[str, Any]]:
    """The blocks as they stand: under a held clock, the authored history verbatim."""
    return list(fixture.g.TR_BLOCKS)


def tr_pending(session: Session) -> bool:
    """Whether a block is still streaming: never, because the clock is held."""
    return False


def _wrap_to(text: str, room: int) -> list[str]:
    out: list[str] = []
    cur = ""
    for t in str(text).split(" "):
        if not cur:
            cur = t
        elif len(cur + " " + t) <= room:
            cur += " " + t
        else:
            out.append(cur)
            cur = t
    if cur:
        out.append(cur)
    return out


def tr_hidden(bk: dict[str, Any], w: int) -> int:
    """How many lines a block holds beyond its preview at this width."""
    hw = len(" 00:00:00  ¶ " + pad("", 9))
    n = 1
    cur = ""
    for t in str(bk["text"]).split(" "):
        if not cur:
            cur = t
        elif len(cur + " " + t) <= w - 2 - hw - 1:
            cur += " " + t
        else:
            n += 1
            cur = t
    return max(0, n - 2) + (len(bk["body"]) if bk.get("body") else 0)


def tr_fold(session: Session) -> dict[int, bool]:
    if session.tr_fold is None:
        session.tr_fold = {}
    return session.tr_fold


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    wb = w - 2
    blocks = tr_now(s, fixture)
    n = len(blocks)
    if s.tr_sel is None:
        s.tr_sel = n - 1
    if s.follow is not False:
        s.tr_sel = n - 1
    s.tr_sel = max(0, min(n - 1, s.tr_sel))
    fold = tr_fold(s)
    flat: list[dict[str, Any]] = []
    for i, bk in enumerate(blocks):
        is_open = bool(fold.get(i))
        run = bk.get("run")
        run_s = bk.get("runS")
        flight = bool(run or run_s is not None) and bk["k"] == "tool"
        lead = ("»" if bk.get("bg") == "running" else "⋯") if flight else TR_GLYPH[bk["k"]]
        word = ("background" if bk.get("bg") == "running" else "running") if flight else bk["k"]
        going = bool(run or run_s is not None) and bk.get("bg") != "done"
        head = f" {bk['at']}  {lead} " + pad(word, 11)
        hw = len(head)
        hidden = tr_hidden(bk, w)
        dur = run or (fmt_dur(run_s) if run_s is not None else None)
        wait_txt = bk.get("wait") or (
            f"waiting {fmt_dur(bk['waitS'])}" if bk.get("waitS") is not None else None
        )
        if going:
            right = (("▾ " if is_open else "▸ ") if bk.get("body") else "") + (
                f"{dur} · {bk['est']}" if bk.get("est") and w >= 120 else str(dur)
            )
        elif wait_txt:
            right = wait_txt
        elif hidden:
            right = f"▾ {hidden} lines" if is_open else f"▸ {hidden} lines"
        elif bk.get("doneIn"):
            right = bk["doneIn"]
        else:
            right = ""
        room = wb - hw - (len(right) + 2 if right else 0) - 1
        lines = _wrap_to(bk["text"], room)
        show = lines if is_open else lines[:PREVIEW]
        flat.append(
            {
                "i": i,
                "head": True,
                "raw": head + pad(show[0], room) + (f"  {right}" if right else ""),
            }
        )
        for line in show[1:]:
            flat.append({"i": i, "head": False, "raw": pad("", hw) + line})
        if is_open and bk.get("body"):
            for line in bk["body"]:
                flat.append({"i": i, "head": False, "raw": pad("", 13) + "  " + strip_chips(line)})
    track_h0 = h - 4
    cur_ix = 0
    for fi, r in enumerate(flat):
        if r["i"] == s.tr_sel and r["head"]:
            cur_ix = fi
            break
    take = track_h0
    top = 0
    win: list[dict[str, Any]] = []
    above = below = 0
    for _pass in range(3):
        top = max(0, min(max(0, len(flat) - take), cur_ix - take // 2))
        win = flat[top : top + take]
        above = top
        below = len(flat) - (top + len(win))
        want = track_h0 - (1 if above else 0) - (1 if below else 0)
        if take == want:
            break
        take = want
    track_h = h - 4
    bar = len(flat) > take
    tw = wb if bar else w
    body: list[str] = []
    if above:
        body.append(pad(f" … {above} above", tw))
    for r in win:
        sel = r["head"] and r["i"] == s.tr_sel
        raw = ("▸" if sel else " ") + r["raw"][1:]
        body.append(g_pad(raw, tw))
    if below:
        body.append(pad(f" … {below} below", tw))
    while len(body) < track_h:
        body.insert(0, pad("", tw))
    b = body
    if bar:
        tsize = max(1, min(track_h, round(track_h * len(win) / len(flat))))
        thumb = max(0, min(track_h - tsize, round(track_h * top / len(flat))))
        b = [row + " █" for i, row in enumerate(body)]
        del thumb, tsize
    rows = [Fixed(pad(strip_chips(snap_caret(row)), w)) for row in b]
    live = next(
        (
            x
            for x in blocks
            if (x.get("run") or x.get("runS") is not None)
            and x.get("bg") != "running"
            and x["k"] not in ("subagent", "thinking")
        ),
        None,
    )
    think = next(
        (x for x in blocks if x["k"] == "thinking" and (x.get("run") or x.get("runS") is not None)),
        None,
    )
    bg = [x for x in blocks if x.get("bg") == "running"]
    if think:
        state = "THINKING for " + str(think.get("run") or fmt_dur(think["runS"]))
    elif live:
        state = "RUNNING for " + str(live.get("run") or fmt_dur(live["runS"]))
    elif tr_pending(s):
        state = "WORKING"
    elif s.rack_hold:
        state = "idle"
    else:
        state = "SUCCEEDED"
    ctx = (
        f"Run {RUN_ID} · {state}"
        + (
            f" · {len(bg)}" + (" running in the background" if w >= 120 else " in background")
            if bg
            else ""
        )
        + " · "
        + ("held" if s.follow is False else "following")
        + (f" · {len(blocks)} blocks" if w >= 120 else "")
    )
    return g_frame(
        s,
        fixture,
        f"Eä ▸ … ▸ {RUN_ID} ▸ Transcript",
        ctx,
        rows,
        [("↑↓", "block"), ("Enter", "fold"), ("f", "follow"), ("y", "copy"), ("Esc", "back")],
        w,
        h,
    )


def seam(ctx: Ctx, k: str, shift: bool) -> bool:
    s = ctx.s
    if s.route != "transcript" or s.overlay or s.edit or s.typing or s.prefix:
        return False
    blocks = tr_now(s, ctx.fixture)
    tn = len(blocks)
    if s.tr_sel is None:
        s.tr_sel = tn - 1
    if k in ("ArrowDown", "ArrowUp"):
        s.tr_sel = max(0, min(tn - 1, s.tr_sel + (1 if k == "ArrowDown" else -1)))
        s.follow = False
        ctx.log(k, f"{blocks[s.tr_sel]['k']} · {blocks[s.tr_sel]['at']}")
        return True
    if k == "Enter":
        bk = blocks[s.tr_sel]
        fd = tr_fold(s)
        n = tr_hidden(bk, ctx.w)
        if not n:
            ctx.log("Enter", "this block has nothing folded away")
            return True
        fd[s.tr_sel] = not fd.get(s.tr_sel)
        ctx.log("Enter", ("opened " if fd[s.tr_sel] else "folded ") + f"{n} lines")
        return True
    if k == "f":
        s.follow = s.follow is False
        if s.follow:
            s.tr_sel = tn - 1
        ctx.log("f", "following the tail" if s.follow else "held where you are")
        return True
    if k == "y":
        ctx.notify(f"{RUN_ID} · {blocks[s.tr_sel]['k']} at {blocks[s.tr_sel]['at']}", "copied")
        ctx.log("y", "copied the block under the cursor")
        return True
    return False


__all__ = ["go", "render", "seam"]
