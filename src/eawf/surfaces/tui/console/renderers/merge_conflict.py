"""merge.conflict: one hunk at a time, both sides with their authorities.

The card draws exactly one hunk however many the conflict holds. Two sides of one region
are already two authorities, two commits and two bodies of text to hold in mind; a card
that stacked the next hunk under them would be a diff viewer, and the console is not one.
``↑↓`` walks the run, and the title counts the position across the whole conflict rather
than across whichever file it started in.

The card only displays. Neither side is chosen here and neither is retracted, so nothing
this module reaches writes a file; the typed exit the daemon opened is printed instead,
because a conflict frame without a way out is what invites someone to edit the canonical
workspace by hand.
"""

from __future__ import annotations

from eawf.kernel.projection.integration import ConflictSideView, HunkView, MergeConflictReadModel
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.format import clock_time, group
from eawf.surfaces.tui.console.frame import CHIP_END, LABEL_MARK, View, boxed
from eawf.surfaces.tui.console.renderers.read_model import counts, native

#: The lines of one side the card prints before it stops. A hunk side may carry hundreds
#: and a card that drew them all would push its own footer off the frame.
SIDE_LINES = 3

#: What the card says for a Batch that is not blocked.
NO_CONFLICT = "no conflict frame is held · nothing is blocking this Batch"

#: The sentence the card prints where an operator looks for the verb it does not have.
READ_ONLY = "The console never edits a file — resolve it in your git tool."

_KEYS: tuple[tuple[str, str], ...] = (("↑↓", "hunk"), ("y", "copy"), ("Esc", "close"))
_PROTO_HUNKS: tuple[tuple[str, tuple[str, str], tuple[str, str]], ...] = (
    (
        "src/normalize.py",
        ("the agent · 13:58 · 8f14c2", "seq = acked_cursor(window)"),
        ("you · 13:44 · 9a03e7", "seq = arrival_order(window)"),
    ),
    (
        "src/replay.py",
        ("the agent · 13:58 · 8f14c2", "window = until_acked(cursor)"),
        ("you · 13:41 · 9a03e7", "window = until_last_seen(cursor)"),
    ),
)


def _label(name: str) -> str:
    return f"{LABEL_MARK}{name}{CHIP_END}"


def _side_lines(name: str, side: ConflictSideView) -> list[str]:
    """Return one side of a hunk: its authority row, then the lines it states."""
    rows = [f"{_label(name)} {side.authority} · {clock_time(side.at)} · {side.sha[:6]}"]
    shown = side.lines[:SIDE_LINES]
    rows.extend(f"       {line}" for line in shown)
    hidden = len(side.lines) - len(shown)
    if hidden:
        rows.append(f"       … {dv.plural(hidden, 'more line')}")
    return rows


def _hunk_lines(model: MergeConflictReadModel, hunk: HunkView) -> list[str]:
    """Return the card's content for one hunk, both sides and the exit under them."""
    total = len(model.hunks)
    lines = [
        f"{_label('FILE')}   {hunk.path} · hunk {group(hunk.position)} of {group(total)}",
        "",
    ]
    lines.extend(_side_lines("OURS", hunk.ours))
    lines.extend(_side_lines("THEIRS", hunk.theirs))
    lines.append("")
    frame = model.conflict_of(hunk)
    if frame is not None:
        lines.append(f"{_label('EXIT')}   {frame.exit_kind.value} · {frame.exit_ref}")
    lines.append("Neither side is retracted and neither is chosen here.")
    lines.append(READ_ONLY)
    return lines


def _context(model: MergeConflictReadModel, hunk: HunkView | None) -> str:
    """Return the context row: the branch the conflict was seen on, or the cursor alone."""
    frame = model.conflict_of(hunk) if hunk is not None else None
    if frame is None:
        return counts(model)
    return (
        f"branch {frame.branch} · ahead {group(frame.ahead)} · behind {group(frame.behind)}"
        f" · {counts(model)}"
    )


def native_frame(view: View, model: MergeConflictReadModel) -> list[str]:
    """Return the conflict card drawn from the read model the daemon served.

    Args:
        view: The render being built; its session carries the hunk the cursor sits on.
        model: The route's read model at the committed cursor.

    Returns:
        The full frame, keybar last. A Batch with no standing conflict draws a card that
        says so rather than an empty box.
    """
    cursor = dv.sel_in(view.session, len(model.hunks))
    hunk = model.hunk_at(cursor)
    total = len(model.hunks)
    title = f"MERGE CONFLICT · {dv.plural(total, 'hunk')}"
    lines = _hunk_lines(model, hunk) if hunk is not None else [NO_CONFLICT]
    return boxed(
        view,
        crumb="Eä ▸ … ▸ Git ▸ Conflict",
        ctx=_context(model, hunk),
        pre=[],
        title=title if hunk is not None else "MERGE CONFLICT · none held",
        lines=lines,
        foot="y copies this hunk with both sides and their authorities.",
        keys=_KEYS,
    )


def _proto_frame(view: View) -> list[str]:
    """Return the epoch-1 conflict card the prototype registers drive."""
    s = view.session
    dv.sel_in(s, len(_PROTO_HUNKS))
    path, ours, theirs = _PROTO_HUNKS[s.sel]
    lines = [
        f"{_label('FILE')}   {path} · hunk {s.sel + 1} of {len(_PROTO_HUNKS)}",
        "",
        f"{_label('OURS')}   {ours[0]}",
        f"       {ours[1]}",
        f"{_label('THEIRS')} {theirs[0]}",
        f"       {theirs[1]}",
        "",
        "Neither side is retracted and neither is chosen here.",
        READ_ONLY,
    ]
    return boxed(
        view,
        crumb="Eä ▸ … ▸ Git ▸ Conflict",
        ctx="branch eawf/bound-replay-window · ahead 4 · behind 1",
        pre=[],
        title=f"MERGE CONFLICT · {len(_PROTO_HUNKS)} hunks",
        lines=lines,
        foot="y copies this hunk with both sides and their authorities.",
        keys=_KEYS,
    )


def render(view: View) -> list[str]:
    """Return the conflict card, native when a read model is held and epoch-1 otherwise."""
    model = native(view)
    if isinstance(model, MergeConflictReadModel):
        return native_frame(view, model)
    return _proto_frame(view)
