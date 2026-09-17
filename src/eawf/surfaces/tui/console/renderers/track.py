"""track: three groups, milestones, campaigns and the shaped queue, that Tab cycles.

The focused group carries the cursor and the others recede, so which list the arrows walk
is visible rather than remembered. The fixture describes one track, so another track's id
says plainly that nothing is held for it.
"""

from __future__ import annotations

from dataclasses import dataclass

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.frame import (
    Fixed,
    Table,
    View,
    bar,
    build,
    header,
    route_keys_bar,
    thin,
)
from eawf.surfaces.tui.console.keybar import ROUTE_KEYS
from eawf.surfaces.tui.console.width import pad

OWN = "Runtime"


@dataclass(frozen=True, slots=True)
class Group:
    """One Tab-cycled group of the track and the routes its rows drill to."""

    id: str
    head: str
    rows: tuple[tuple[str, ...], ...]
    widths: tuple[int, ...]
    drills: tuple[tuple[str, str], ...]


GROUPS: tuple[Group, ...] = (
    Group(
        "MILESTONES",
        Table([34, 12, 9, 0], 2).head(["MILESTONES", "STATE", "BATCHES", "DUE"]),
        (
            ("MLS-0001 Replay-safe activity", "ACTIVE", "2", "W30"),
            ("MLS-0003 Escape ledger", "PLANNED", "0", "W32"),
            ("MLS-0000 Event normalizer", "COMPLETED", "3", "—"),
        ),
        (34, 12, 9),
        (("MLS-0001", "milestone"), ("MLS-0003", "milestone"), ("MLS-0000", "milestone")),
    ),
    Group(
        "CAMPAIGNS",
        Table([34, 12, 9, 0], 2).head(["CAMPAIGNS", "STATE", "FINDINGS", "CHECKPOINT"]),
        (("CAM-0001 Provider drift", "REVIEW", "4", "W30"),),
        (34, 12, 9),
        (("CAM-0001", "campaign"),),
    ),
    Group(
        "SHAPED QUEUE",
        Table([34, 19, 0], 2).head(["SHAPED QUEUE", "DEFINITION", "DUE"]),
        (
            ("EAWF-0091 Bound replay window", "criteria missing", "W30"),
            ("EAWF-0092 Retire wave shim", "criteria set", "W31"),
        ),
        (34, 19),
        (("EAWF-0091", "task.detail"), ("EAWF-0092", "task.detail")),
    ),
)
GROUP_IDS: tuple[str, ...] = tuple(g.id for g in GROUPS)


def group_of(group_id: str | None) -> Group:
    """Return the group named ``group_id``, the first group for an unknown one."""
    return next((g for g in GROUPS if g.id == group_id), GROUPS[0])


def _line(group: Group, row: tuple[str, ...], cur: bool) -> str:
    cells = "".join(
        pad(cell, group.widths[ci]) if ci < len(group.widths) else cell
        for ci, cell in enumerate(row)
    )
    return " " + ("▸ " if cur else "  ") + cells


def render(view: View) -> list[str]:
    """Return the Track frame."""
    s, fx, w = view.session, view.fixture, view.w
    focused = group_of(s.track_group)
    s.track_group = focused.id
    dv.sel_in(s, len(focused.rows))
    track = s.subj_id or OWN
    rows = [
        header(view, f" Eä ▸ {fx.scope} ▸ {track}"),
        f" Track {track}" + (" · ACTIVE · 2 of 3 batches in flight" if track == OWN else ""),
        bar(w),
        " POLICY    WIP 3 batches · 1 campaign · dispatch on your word only",
    ]
    for group in GROUPS:
        on = group is focused
        rows.append(thin(w))
        rows.append(group.head if on else Fixed(pad(group.head, w)))
        for i, row in enumerate(group.rows):
            line = _line(group, row, on and i == s.sel)
            rows.append(line if on else Fixed(pad(line, w)))
    rows.append(thin(w))
    rows.append(" RETIRED   nothing retired in this track")
    if track != OWN:
        what = "milestones, campaigns or a shaped queue"
        rows = dv.absent(s, fx, rows, entity_id=track, what=what, w=w)
    return build(view, rows, route_keys_bar(view, ROUTE_KEYS["track"]))
