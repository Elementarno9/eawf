"""The light menu verbs that act on the frame in place: no route opens, no clipboard fills.

Each changes only what this console shows, or opens the lens's own editor, so none needs a
daemon verb and each acts at once. The Activity verbs order, follow, hold and mark the Run
register's window; scope home's pin leads the operator's tree with one Track; the Settings
verbs run the lens's Enter and ``x`` on the key under the cursor; and the Timeline's
proposed date opens a date field whose Enter previews the Milestone's target-date write.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Final

from eawf.kernel.projection.spine import SpineView
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.tui.console.mutation import NOTHING_TO_MARK, select_key
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.renderers.activity import FOLLOW, SORT_COLUMNS
from eawf.surfaces.tui.console.renderers.provenance import lens_verb
from eawf.surfaces.tui.console.renderers.timeline import propose


def _say(ctx: Ctx, key: str, verb: str, note: str) -> None:
    """Raise the toast that says what the display now does, and log it under ``key``."""
    ctx.notify(note, verb)
    ctx.log(key, f"{verb} · {note}")


def _follow(ctx: Ctx, key: str) -> None:
    s = ctx.s
    following = s.activity_order != FOLLOW
    s.activity_order = FOLLOW if following else None
    note = "newest change first · the cursor follows it" if following else "following stopped"
    _say(ctx, key, "follow", note)


def _sort(ctx: Ctx, key: str) -> None:
    s = ctx.s
    at = SORT_COLUMNS.index(s.activity_order) + 1 if s.activity_order in SORT_COLUMNS else 0
    s.activity_order = SORT_COLUMNS[at] if at < len(SORT_COLUMNS) else None
    _say(ctx, key, "sort", f"by {s.activity_order}" if s.activity_order else "register order")


def _pause(ctx: Ctx, key: str) -> None:
    s = ctx.s
    s.activity_paused = not s.activity_paused
    s.activity_held = None
    note = (
        "the window holds its rows · . p resumes"
        if s.activity_paused
        else "resumed · the window shows the register as it stands"
    )
    _say(ctx, key, "pause display", note)


def _mark_all(ctx: Ctx, key: str) -> None:
    if not select_key(ctx, "*"):
        ctx.log(key, NOTHING_TO_MARK)


def _pin(ctx: Ctx, key: str) -> None:
    s, spine = ctx.s, ctx.projection
    rows = spine.rows if isinstance(spine, SpineView) else ()
    tracks = {row.key for row in rows if row.collection is Epoch2Collection.TRACK}
    track = next(
        (row.parent_key for row in rows if row.key == s.sel_id and row.parent_key in tracks),
        None,
    )
    if track is None:
        ctx.log(key, "pin outcome · no Track is filed over the cursor")
        return
    pinned = s.pinned_track != track
    s.pinned_track = track if pinned else None
    _say(ctx, key, "pin outcome", f"{track} leads your tree" if pinned else f"{track} unpinned")


#: The handler of each light verb that acts in place, by route and verb name.
IN_PLACE: Final[Mapping[tuple[str, str], Callable[[Ctx, str], None]]] = MappingProxyType(
    {
        ("activity", "follow"): _follow,
        ("activity", "sort"): _sort,
        ("activity", "pause display"): _pause,
        ("activity", "select all shown"): _mark_all,
        ("scope.home", "pin outcome"): _pin,
        ("timeline", "propose date"): propose,
        ("settings", "edit"): lambda ctx, key: lens_verb(ctx, key, "edit"),
        ("settings", "unset"): lambda ctx, key: lens_verb(ctx, key, "unset"),
    }
)


__all__ = ["IN_PLACE"]
