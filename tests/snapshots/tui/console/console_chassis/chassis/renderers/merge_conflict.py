"""merge.conflict: one hunk at a time with both sides and their authorities; the console
never edits a file, so the card only displays."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...chassis import derive as dv
from ...chassis.frame import boxed

if TYPE_CHECKING:
    from ...chassis.fixture import Fixture
    from ...chassis.session import Session

_HUNKS: list[dict[str, list[str] | str]] = [
    {
        "file": "src/normalize.py",
        "ours": ["the agent · 13:58 · 8f14c2", "seq = acked_cursor(window)"],
        "theirs": ["you · 13:44 · 9a03e7", "seq = arrival_order(window)"],
    },
    {
        "file": "src/replay.py",
        "ours": ["the agent · 13:58 · 8f14c2", "window = until_acked(cursor)"],
        "theirs": ["you · 13:41 · 9a03e7", "window = until_last_seen(cursor)"],
    },
]
_KEYS: list[tuple[str, str]] = [("↑↓", "hunk"), ("y", "copy"), ("Esc", "close")]


def render(session: Session, fixture: Fixture, w: int, h: int) -> list[str]:
    s = session
    dv.sel_in(s, len(_HUNKS))
    hk = _HUNKS[s.sel]
    ours = hk["ours"]
    theirs = hk["theirs"]
    lines: list[str] = [
        f"\x07FILE\x06   {hk['file']} · hunk {s.sel + 1} of {len(_HUNKS)}",
        "",
        f"\x07OURS\x06   {ours[0]}",
        f"       {ours[1]}",
        f"\x07THEIRS\x06 {theirs[0]}",
        f"       {theirs[1]}",
        "",
        "Neither side is retracted and neither is chosen here.",
        "The console never edits a file — resolve it in your git tool.",
    ]
    return boxed(
        s,
        fixture,
        "Eä ▸ … ▸ Git ▸ Conflict",
        "branch eawf/bound-replay-window · ahead 4 · behind 1",
        [],
        "MERGE CONFLICT · 2 hunks",
        lines,
        "y copies this hunk with both sides and their authorities.",
        _KEYS,
        w,
        h,
    )
