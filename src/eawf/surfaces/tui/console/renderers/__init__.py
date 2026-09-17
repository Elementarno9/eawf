"""Route renderers: one module per registered route, bound here by route id.

A route module provides ``render(view) -> list[str]``, which returns the full frame
through :func:`~eawf.surfaces.tui.console.frame.build`, and may provide
``seam(ctx, key, shift) -> bool``, the route's own keys (tried before the dispatcher's, a
``True`` claims the key), and ``copy(session, fixture) -> str``, what ``y`` copies about the
route's subject. The app composes drawers, the rack and the verbose row around the frame.
A registered route with no module renders a marked placeholder, so the console never
fails on a route that has no renderer yet.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View, bar, build, header, thin
from eawf.surfaces.tui.console.keybar import keybar
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.renderers import (
    activity,
    attention,
    backlog,
    batch_detail,
    campaign,
    campaign_artifact,
    campaign_step,
    cost_ceiling,
    crash_recovery,
    entry,
    evidence,
    evidence_digest,
    export,
    git_pr,
    health,
    history,
    history_diff,
    merge_conflict,
    milestone,
    notifications,
    receipt,
    release,
    run_detail,
    sandbox_log,
    scope_home,
    search,
    settings,
    settings_stack,
    task_detail,
    timeline,
    track,
    transcript,
    trust,
    unattended,
)
from eawf.surfaces.tui.console.session import Session

Render = Callable[[View], list[str]]
Seam = Callable[[Ctx, str, bool], bool]
Copy = Callable[[Session, Fixture], str]
_DIGEST = re.compile(r"digest\s+(\S+)")
_CAMPAIGN_RECEIPTS: tuple[str, ...] = (
    "EVT-9101 EVT-9104 EVT-9108",
    "EVT-9120 EVT-9126",
    "EVT-9131",
)


@dataclass(frozen=True, slots=True, kw_only=True)
class RouteModule:
    """What one route binds: its renderer, and optionally its own keys and its copy."""

    render: Render
    seam: Seam | None = None
    copy: Copy | None = None


ROUTE_MODULES: Mapping[str, RouteModule] = MappingProxyType(
    {
        "entry": RouteModule(render=entry.render),
        "scope.home": RouteModule(render=scope_home.render, seam=scope_home.seam),
        "track": RouteModule(render=track.render),
        "batch.detail": RouteModule(render=batch_detail.render),
        "task.detail": RouteModule(render=task_detail.render),
        "git.pr": RouteModule(render=git_pr.render, seam=git_pr.seam),
        "activity": RouteModule(render=activity.render),
        "run.detail": RouteModule(render=run_detail.render),
        "attention": RouteModule(render=attention.render),
        "transcript": RouteModule(render=transcript.render, seam=transcript.seam),
        "cost.ceiling": RouteModule(render=cost_ceiling.render),
        "crash.recovery": RouteModule(render=crash_recovery.render),
        "milestone": RouteModule(render=milestone.render),
        "release": RouteModule(render=release.render),
        "timeline": RouteModule(render=timeline.render),
        "backlog": RouteModule(render=backlog.render, seam=backlog.seam),
        "campaign": RouteModule(render=campaign.render, seam=campaign.seam),
        "history": RouteModule(render=history.render),
        "settings": RouteModule(render=settings.render, seam=settings.seam),
        "search": RouteModule(render=search.render),
        "history.diff": RouteModule(render=history_diff.render, seam=history_diff.seam),
        "trust": RouteModule(render=trust.render),
        "evidence": RouteModule(render=evidence.render, seam=evidence.seam),
        "health": RouteModule(render=health.render),
        "sandbox.log": RouteModule(render=sandbox_log.render, seam=sandbox_log.seam),
        "unattended": RouteModule(render=unattended.render, seam=unattended.seam),
        "evidence.digest": RouteModule(render=evidence_digest.render, copy=evidence_digest.copy),
        "settings.stack": RouteModule(render=settings_stack.render),
        "notifications": RouteModule(render=notifications.render),
        "merge.conflict": RouteModule(render=merge_conflict.render),
        "export": RouteModule(render=export.render),
        "receipt": RouteModule(render=receipt.render),
        "campaign.step": RouteModule(
            render=campaign_step.render, seam=campaign_step.seam, copy=campaign_step.copy
        ),
        "campaign.artifact": RouteModule(
            render=campaign_artifact.render,
            seam=campaign_artifact.seam,
            copy=campaign_artifact.copy,
        ),
    }
)

_UNBOUND = sorted(set(ROUTE_MODULES) - set(REGISTRY.ids))
if _UNBOUND:
    raise ValueError(f"renderers for unregistered routes: {', '.join(_UNBOUND)}")


def seam_for(route: str) -> Seam | None:
    """Return the route's own key hook, if it has one."""
    module = ROUTE_MODULES.get(route)
    return module.seam if module else None


def placeholder(view: View) -> list[str]:
    """Return a clearly marked frame for a registered route with no renderer."""
    s, w = view.session, view.w
    rows = [
        header(view, f" Eä ▸ {view.fixture.scope} ▸ {s.route}"),
        f" NOT BOUND · {s.route} · no renderer is registered for this route",
        bar(w),
        " PLACEHOLDER  this frame stands in for a route the console has not built",
        thin(w),
    ]
    return build(view, rows, keybar([("Esc", "back")], w))


def render_route(view: View) -> list[str]:
    """Return the frame of the session's route."""
    module = ROUTE_MODULES.get(view.session.route)
    return module.render(view) if module else placeholder(view)


def copy_target(session: Session, fixture: Fixture) -> str:
    """Return what ``y`` copies about the frame's subject."""
    module = ROUTE_MODULES.get(session.route)
    if module is not None and module.copy is not None:
        return module.copy(session, fixture)
    proto = fixture.proto
    sel = session.sel
    if session.overlay == "evidence" and session.route == "campaign":
        return _CAMPAIGN_RECEIPTS[sel] if sel < len(_CAMPAIGN_RECEIPTS) else _CAMPAIGN_RECEIPTS[0]
    if session.route == "milestone":
        record = fixture.record(session.subj_id) or ()
        bundle = next((val for lab, val in record if lab.upper() == "BUNDLE"), None)
        found = _DIGEST.search(bundle) if bundle else None
        return f"digest {found.group(1)}" if found else "digest 7c1f…a94"
    if session.route == "activity":
        return proto.fleet[sel].run if sel < len(proto.fleet) else "nothing selected"
    if session.route == "run.detail":
        return run_detail.OWN
    if session.route == "scope.home":
        return proto.tracks[sel].id if sel < len(proto.tracks) else proto.scope
    return dv.urn(session, fixture)
