"""track: three groups, milestones, campaigns and the shaped queue, that Tab cycles.

The focused group carries the cursor and the others recede, so which list the arrows walk
is visible rather than remembered. The fixture describes one track, so another track's id
says plainly that nothing is held for it.

The native frame draws the same three groups for one Track from the read model: the
Milestones filed under it, by their parent key, with the Batches cut under each; then the
Campaigns and the shaped queue, which no record files under a Track yet, so each says so
rather than borrowing the fixture's rows.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from eawf.kernel.projection.spine import SpineRow, SpineView
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.frame import (
    Fixed,
    Table,
    View,
    bar,
    build,
    header,
    recede,
    route_keys_bar,
    thin,
    window_rows,
)
from eawf.surfaces.tui.console.keybar import ROUTE_KEYS
from eawf.surfaces.tui.console.renderers.children import (
    child_cursor,
    children,
    lrow,
    named,
    status,
)
from eawf.surfaces.tui.console.renderers.detail import subject_of
from eawf.surfaces.tui.console.renderers.read_model import UNKNOWN_WORD
from eawf.surfaces.tui.console.renderers.spine import (
    detail_head,
    detail_keys,
    held,
    native_frame,
)
from eawf.surfaces.tui.console.width import clip_words, pad

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


#: What the two groups no record files under a Track say in place of rows.
UNFILED: Mapping[str, str] = MappingProxyType(
    {
        GROUPS[1].id: "∅ a Campaign names no Track, so none is listed under this one",
        GROUPS[2].id: "∅ no record files a shaped Task under a Track yet",
    }
)


def _name_w(wide: bool) -> int:
    """Return the Milestone name column: the packet's width, widened in the wide layout."""
    return 48 if wide else 34


def _milestone_cells(spine: SpineView, milestone: SpineRow, width: int) -> list[str]:
    """Return one Milestone's cells: its name, status, Batches cut under it, and due date."""
    batches = children(spine.rows, milestone.key, Epoch2Collection.BATCH)
    # the name is clipped short of its column so the status never runs into it
    name = clip_words(named(milestone), width - 2)
    return [name, status(milestone), str(len(batches)), UNKNOWN_WORD]


def track_frame(view: View, spine: SpineView) -> list[str]:
    """Return one Track's frame: its facts, then the three groups Tab cycles.

    The focused group carries the caret and publishes what Enter drills into; the other
    two recede.

    Args:
        view: The render being built; its session names the Track and the focused group.
        spine: The Track register with the Milestones and Batches filed under it.

    Returns:
        The full frame, keybar last; the register frame when the session names no Track.
    """
    session, w = view.session, view.w
    subject = subject_of(view, spine)
    if subject is None:
        return native_frame(view, spine)
    focused = group_of(session.track_group)
    session.track_group = focused.id
    milestones = children(spine.rows, subject.key, Epoch2Collection.MILESTONE)
    runs = subject.facts.get("runs")
    rows = [
        *detail_head(view, spine, subject),
        lrow("RUNS", f"{runs} filed under it" if runs is not None else UNKNOWN_WORD),
    ]
    on_milestones = focused.id == GROUPS[0].id
    keys = [m.key for m in milestones] if on_milestones else []
    cursor = child_cursor(session, keys, subject=subject.key)
    table = Table([_name_w(view.wide), 12, 9, 0], 2)
    head = table.head(["MILESTONES", "STATE", "BATCHES", "DUE"])
    rows += [thin(w), head if on_milestones else recede(head, w)]
    chrome = len(rows) + 2 * len(UNFILED) + 2
    win = window_rows(view, total=len(milestones), cursor=cursor, chrome=chrome)
    for index in range(win.start, win.stop):
        on = on_milestones and index == cursor
        line = table.row(_milestone_cells(spine, milestones[index], _name_w(view.wide)), on)
        rows.append(line if on else Fixed(pad(line, w)) if on_milestones else recede(line, w))
    if not milestones:
        rows.append("   ∅ no Milestone is filed under this Track")
    if win.hides:
        rows.append(win.line(complete=spine.complete))
    # the Milestone table is windowed whichever group holds the focus; the arrows walk
    # only the focused group's rows
    session.nav_rows = len(keys)
    for group_id, empty in UNFILED.items():
        own = focused.id == group_id
        rows += [
            thin(w),
            *(r if own else recede(r, w) for r in (f"   {group_id}", f"   {empty}")),
        ]
    return build(view, rows, route_keys_bar(view, detail_keys(view, spine)))


def render(view: View) -> list[str]:
    """Return the Track frame, native when a read model is held."""
    spine = held(view)
    if spine is not None:
        return track_frame(view, spine)
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
        rows.append(group.head if on else recede(group.head, w))
        for i, row in enumerate(group.rows):
            line = _line(group, row, on and i == s.sel)
            rows.append(line if on else recede(line, w))
    rows.append(thin(w))
    rows.append(" RETIRED   nothing retired in this track")
    if track != OWN:
        what = "milestones, campaigns or a shaped queue"
        rows = dv.absent(s, fx, rows, entity_id=track, what=what, w=w)
    return build(view, rows, route_keys_bar(view, ROUTE_KEYS["track"]))
