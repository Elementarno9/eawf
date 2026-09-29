"""The Tab table: what Tab and Shift-Tab cycle on each route.

A route with a register of its own (buckets, groups, sections, regions of its own
drawing) cycles it here; any other route cycles the focus regions its registry row
declares, and a frame with neither says nothing cycles.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType

from eawf.surfaces.tui.console import attention as att
from eawf.surfaces.tui.console import drill
from eawf.surfaces.tui.console.navigation import Ctx, busy, cycle, cycle_region, regions_of
from eawf.surfaces.tui.console.registry import SECTIONS
from eawf.surfaces.tui.console.renderers import timeline as tl
from eawf.surfaces.tui.console.renderers import track
from eawf.surfaces.tui.console.session import Session


def owns_region_cycle(s: Session) -> bool:
    """Return whether Tab walks this route's declared regions rather than a register."""
    return not busy(s) and s.route not in TAB and len(regions_of(s.route)) > 1


def tab(ctx: Ctx, k: str, pane: bool) -> None:
    """Cycle what Tab owns on this route: its own register, else its declared regions."""
    handler = TAB.get(ctx.s.route)
    if handler is not None:
        handler(ctx, back=False)
    elif not tab_region(ctx, back=False):
        drill.no_cycle(ctx, back=False)


def tab_region(ctx: Ctx, *, back: bool) -> bool:
    """Move the focus to the next declared region; ``False`` where the route has one."""
    region = cycle_region(ctx.s, back=back)
    if region is None:
        return False
    ctx.log("S-Tab" if back else "Tab", f"focus → {region}")
    return True


def _tab_release(ctx: Ctx, *, back: bool) -> None:
    s = ctx.s
    s.rel_reg = cycle(("MEMBERSHIP", "READINESS"), s.rel_reg or "MEMBERSHIP", back=back)
    s.rel_sel = 0
    ctx.log("S-Tab" if back else "Tab", f"region → {s.rel_reg}")


def _tab_timeline(ctx: Ctx, *, back: bool) -> None:
    s = ctx.s
    s.tl_reg = cycle(list(tl.REGIONS), s.tl_reg or tl.LANES, back=back)
    s.tl_sel = 0
    ctx.log("S-Tab" if back else "Tab", f"region → {s.tl_reg}")


def _tab_activity(ctx: Ctx, *, back: bool) -> None:
    s = ctx.s
    keys: list[str | None] = [None, *(b.key for b in ctx.fixture.proto.buckets)]
    s.bucket = cycle(keys, s.bucket if s.bucket in keys else None, back=back)
    s.sel = 0
    s.scroll = 0
    ctx.log("S-Tab" if back else "Tab", f"bucket → {s.bucket or 'all'}")


def _tab_attention(ctx: Ctx, *, back: bool) -> None:
    s, fx = ctx.s, ctx.fixture
    keys: list[str | None] = [None]
    for bucket in fx.proto.xbuckets:
        keys.append(bucket.key)
        keys.extend(sub.key for sub in bucket.sub or ())
    s.bucket = cycle(keys, s.bucket if s.bucket in keys else None, back=back)
    s.sel = 0
    s.scroll = 0
    label = att.bucket_label(fx, s.bucket) if s.bucket else "all"
    ctx.log("S-Tab" if back else "Tab", f"bucket → {label}")


def _tab_milestone(ctx: Ctx, *, back: bool) -> None:
    s = ctx.s
    s.section = SECTIONS.index(cycle(SECTIONS, SECTIONS[s.section], back=back))
    ctx.log("S-Tab" if back else "Tab", f"section → {SECTIONS[s.section]}")


def _tab_track(ctx: Ctx, *, back: bool) -> None:
    s = ctx.s
    groups = list(track.GROUP_IDS)
    s.track_group = cycle(groups, s.track_group or groups[0], back=back)
    s.sel = 0
    s.scroll = 0
    ctx.log("S-Tab" if back else "Tab", f"group → {s.track_group}")


TAB: Mapping[str, Callable[..., None]] = MappingProxyType(
    {
        "release": _tab_release,
        "timeline": _tab_timeline,
        "activity": _tab_activity,
        "attention": _tab_attention,
        "milestone": _tab_milestone,
        "track": _tab_track,
    }
)


__all__ = ["TAB", "owns_region_cycle", "tab", "tab_region"]
