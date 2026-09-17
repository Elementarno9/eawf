"""merge.conflict: one hunk at a time, both sides with their authorities.

The console never edits a file, so the card only displays.
"""

from __future__ import annotations

from dataclasses import dataclass

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.frame import CHIP_END, LABEL_MARK, View, boxed


@dataclass(frozen=True, slots=True)
class Hunk:
    """One conflicting hunk: the file and each side's authority and line."""

    file: str
    ours: tuple[str, str]
    theirs: tuple[str, str]


_HUNKS: tuple[Hunk, ...] = (
    Hunk(
        "src/normalize.py",
        ("the agent · 13:58 · 8f14c2", "seq = acked_cursor(window)"),
        ("you · 13:44 · 9a03e7", "seq = arrival_order(window)"),
    ),
    Hunk(
        "src/replay.py",
        ("the agent · 13:58 · 8f14c2", "window = until_acked(cursor)"),
        ("you · 13:41 · 9a03e7", "window = until_last_seen(cursor)"),
    ),
)
_KEYS: tuple[tuple[str, str], ...] = (("↑↓", "hunk"), ("y", "copy"), ("Esc", "close"))


def _label(name: str) -> str:
    return f"{LABEL_MARK}{name}{CHIP_END}"


def render(view: View) -> list[str]:
    """Return the conflict card."""
    s = view.session
    dv.sel_in(s, len(_HUNKS))
    hunk = _HUNKS[s.sel]
    lines = [
        f"{_label('FILE')}   {hunk.file} · hunk {s.sel + 1} of {len(_HUNKS)}",
        "",
        f"{_label('OURS')}   {hunk.ours[0]}",
        f"       {hunk.ours[1]}",
        f"{_label('THEIRS')} {hunk.theirs[0]}",
        f"       {hunk.theirs[1]}",
        "",
        "Neither side is retracted and neither is chosen here.",
        "The console never edits a file — resolve it in your git tool.",
    ]
    return boxed(
        view,
        crumb="Eä ▸ … ▸ Git ▸ Conflict",
        ctx="branch eawf/bound-replay-window · ahead 4 · behind 1",
        pre=[],
        title=f"MERGE CONFLICT · {len(_HUNKS)} hunks",
        lines=lines,
        foot="y copies this hunk with both sides and their authorities.",
        keys=_KEYS,
    )
