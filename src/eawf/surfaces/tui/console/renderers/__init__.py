"""Route renderers: one module per registered route, bound here by route id.

A route module provides ``render(view) -> list[str]``, which returns the full frame
through :func:`~eawf.surfaces.tui.console.frame.build`, and may provide
``seam(ctx, key, shift) -> bool``, the route's own keys (tried before the dispatcher's, a
``True`` claims the key), and ``copy(session, fixture) -> str``, what ``y`` copies about the
route's subject. The app composes drawers, the rack and the verbose row around the frame.
The binding is total over the route registry in both directions: a renderer for an
unregistered route and a registered route with no renderer each refuse the import, so
every frame the console draws comes from a registry row. A console holding no prototype
rows draws the unknown frame for any route whose read model it does not hold, whatever
module is bound; that frame states what the route answers and what it would need, so a
route opened with nothing read is honest about what is missing rather than blank.
"""

from __future__ import annotations

import re
import textwrap
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.chrome import EntryState
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View, bar, build, header, thin, unheld
from eawf.surfaces.tui.console.keybar import keybar
from eawf.surfaces.tui.console.keymap import ENTRY_ROUTE
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.overlays.bound_keys import unless_bound
from eawf.surfaces.tui.console.overlays.drawn import draw_card
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
from eawf.surfaces.tui.console.tokens import TRUTH

Render = Callable[[View], list[str]]
Seam = Callable[[Ctx, str, bool], bool]
Copy = Callable[[Session, Fixture], str]
_DIGEST = re.compile(r"digest\s+(\S+)")
# The entry state a linked console shows while its first read is outstanding.
RESOLVING_STATE = "resolving"
_LABEL_GUTTER = 11
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
        "timeline": RouteModule(render=timeline.render, seam=timeline.seam),
        "backlog": RouteModule(render=backlog.render, seam=backlog.seam),
        "campaign": RouteModule(render=campaign.render, seam=campaign.seam),
        "history": RouteModule(render=history.render),
        "settings": RouteModule(render=settings.render, seam=settings.seam),
        "search": RouteModule(render=search.render),
        "history.diff": RouteModule(render=history_diff.render, seam=history_diff.seam),
        "trust": RouteModule(render=trust.render, seam=trust.seam),
        "evidence": RouteModule(render=evidence.render, seam=evidence.seam),
        "health": RouteModule(render=health.render),
        "sandbox.log": RouteModule(render=sandbox_log.render, seam=sandbox_log.seam),
        "unattended": RouteModule(render=unattended.render, seam=unattended.seam),
        "evidence.digest": RouteModule(render=evidence_digest.render, copy=evidence_digest.copy),
        "settings.stack": RouteModule(render=settings_stack.render),
        "notifications": RouteModule(render=notifications.render),
        "merge.conflict": RouteModule(render=merge_conflict.render),
        "export": RouteModule(render=export.render, seam=export.seam),
        "receipt": RouteModule(render=receipt.render),
        "campaign.step": RouteModule(
            render=campaign_step.render,
            seam=unless_bound(campaign_step.seam),
            copy=campaign_step.copy,
        ),
        "campaign.artifact": RouteModule(
            render=campaign_artifact.render,
            seam=unless_bound(campaign_artifact.seam),
            copy=campaign_artifact.copy,
        ),
    }
)

_UNBOUND = sorted(set(ROUTE_MODULES) - set(REGISTRY.ids))
if _UNBOUND:
    raise ValueError(f"renderers for unregistered routes: {', '.join(_UNBOUND)}")
_UNDRAWN = sorted(set(REGISTRY.ids) - set(ROUTE_MODULES))
if _UNDRAWN:
    raise ValueError(f"registered routes with no renderer: {', '.join(_UNDRAWN)}")


def seam_for(route: str) -> Seam | None:
    """Return the route's own key hook, if it has one."""
    module = ROUTE_MODULES.get(route)
    return module.seam if module else None


def _labelled(label: str, text: str, w: int) -> list[str]:
    """Return ``text`` wrapped under ``label`` in the eleven-cell gutter, never clipped."""
    lines = textwrap.wrap(text, width=w - _LABEL_GUTTER - 1)
    lead = f" {label:<{_LABEL_GUTTER - 1}}"
    return [(lead if i == 0 else " " * _LABEL_GUTTER) + line for i, line in enumerate(lines)]


def unknown_frame(view: View) -> list[str]:
    """Return the frame of a route whose rows nothing has read: the unknown token, no row."""
    s, w = view.session, view.w
    spec = REGISTRY.by_id[s.route]
    rows = [
        header(view, f" Eä ▸ {view.fixture.scope} ▸ {s.route}"),
        f" NOT HELD · {s.route} · no read model is held for this route",
        bar(w),
        f" ROWS      {TRUTH['unknown'].unicode} unknown · nothing has been read, so no row"
        " is drawn",
        *_labelled("ANSWERS", spec.question, w),
        *_labelled("NEEDS", spec.needs, w),
        thin(w),
    ]
    return build(view, rows, keybar([("Esc", "back")], w))


def render_route(view: View) -> list[str]:
    """Return the frame of the session's route, the unknown frame when nothing is held.

    A linked console whose link has answered nothing yet draws the entry layer's
    resolving state instead: no projection exists, so no route is drawn.

    The entry layer is drawn before any session exists, so it never waits on a read
    model: it draws from the chrome's pre-session states alone. A card sub-surface bound
    to a record the console holds is drawn from that record.

    Raises:
        KeyError: the session's route is not registered, which the navigation seam makes
            unreachable: it opens only registered routes.
    """
    if awaiting_first_projection(view) and view.session.route != ENTRY_ROUTE:
        return entry.render_state(view, _resolving(view.fixture))
    card = draw_card(view)
    if card is not None:
        return card
    if unheld(view) and view.session.route != ENTRY_ROUTE:
        return unknown_frame(view)
    return ROUTE_MODULES[view.session.route].render(view)


def awaiting_first_projection(view: View) -> bool:
    """Return whether a linked console has read nothing yet, so no projection exists.

    Until one exists the process layer owns the whole frame: no route, no route verb
    and no header count is drawn from a link that has answered nothing.
    """
    return (
        view.linked
        and view.projection is None
        and view.register is None
        and view.attention is None
        and view.settings is None
    )


def _resolving(fixture: Fixture) -> EntryState:
    """Return the resolving state the chrome holds, which the attach path filled in."""
    return next(state for state in fixture.proto.entry if state.id == RESOLVING_STATE)


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
