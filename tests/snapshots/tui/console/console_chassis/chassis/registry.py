"""Route, overlay, drawer and card registries: data tables the dispatcher and chrome read.

Adding a route is one ``RouteSpec`` row here plus one module under ``chassis/renderers``;
nothing else in the chassis changes. The tables are ported from the prototype's
``STEP_LEAF``, ``ROUTE_WORD``, ``VIA_LEAF``, ``PARENT_ROUTE``, ``ROUTE_OF``, ``ROUTE_LIST``,
the ``g`` map, ``OVERLAYS``, ``PANES`` (called drawers here) and ``OVERLAY_ROUTES``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from ..normalisation import load_map

if TYPE_CHECKING:
    from ..chassis.fixture import Fixture
    from ..chassis.session import Session


@dataclass(frozen=True, slots=True)
class RouteSpec:
    # the route id the design pack's goldens address and the renderers are named for
    id: str
    # the port's canonical route key; it differs from ``id`` only where the
    # normalisation map admits a renamed route, and is what new code should key on
    key: str
    title: str
    family: str
    subject_required: bool
    palette_visible: bool
    go_letter: str | None
    # STEP_LEAF: the word the crumb shows for this route when it has no subject;
    # None means "use the route word" (routeWord), "" means the root itself
    step_leaf: str | None
    # PARENT_ROUTE: a fixed (route, subject) Escape climbs to when the back stack is empty
    parent: tuple[str, str | None] | None
    # the region Tab cycles on this route (TAB_OWNER), for the diagnostics log only
    tab_owner: str | None
    # the leaf the crumb shows when an entity was drilled FROM this list surface
    via_leaf: str | None


def _r(
    id: str,
    family: str,
    *,
    subject: bool = False,
    palette: bool = False,
    go: str | None = None,
    leaf: str | None = None,
    parent: tuple[str, str | None] | None = None,
    tab: str | None = None,
    via: str | None = None,
) -> RouteSpec:
    return RouteSpec(id, id, id, family, subject, palette, go, leaf, parent, tab, via)


_PACK_ROUTES: tuple[RouteSpec, ...] = (
    _r("entry", "entry_layer", palette=True, tab="path"),
    _r("scope.home", "spine", palette=True, go="h", leaf=""),
    _r("track", "spine", subject=True, leaf="Runtime"),
    _r("batch.detail", "spine", subject=True),
    _r("task.detail", "spine", subject=True),
    _r("git.pr", "spine", leaf="Git", parent=("run.detail", "RUN-538453eb")),
    _r("activity", "live", palette=True, go="a", leaf="Activity", tab="buckets", via="Activity"),
    _r("run.detail", "live", subject=True),
    _r("attention", "live", palette=True, go="n", leaf="Needs you", via="Needs you"),
    _r("transcript", "live", leaf="Transcript", parent=("run.detail", "RUN-538453eb")),
    _r("cost.ceiling", "live", leaf="Cost ceiling", via="Cost ceiling"),
    _r("crash.recovery", "live", palette=True, leaf="Recovery"),
    _r("milestone", "acceptance", subject=True, tab="section"),
    _r("release", "acceptance", subject=True, go="r", leaf="REL-0001", tab="section"),
    _r("timeline", "planning", palette=True, go="t", leaf="Timeline", via="Timeline"),
    _r("backlog", "planning", palette=True, go="b", leaf="Backlog", tab="groups"),
    _r("campaign", "planning", subject=True, leaf="CAM-0001", tab="sections"),
    _r("history", "diagnostics", palette=True, go="y", leaf="History", via="History"),
    _r("settings", "diagnostics", palette=True, go="s", leaf="Settings", tab="rail"),
    _r("search", "diagnostics", palette=True, leaf="Search", via="Search"),
    _r("history.diff", "diagnostics", leaf="Diff", parent=("history", None)),
    _r("trust", "verification", leaf="Trust", parent=("milestone", "MLS-0007")),
    _r("evidence", "verification", subject=True, leaf="CLM-0004", parent=("campaign", "CAM-0001")),
    _r("health", "verification", palette=True, go="d", leaf="Health", via="Health"),
    _r("sandbox.log", "operations", palette=True, go="l", leaf="Sandbox log", via="Sandbox log"),
    _r("unattended", "operations", palette=True, go="u", leaf="Unattended", via="Unattended"),
    _r("evidence.digest", "entity_sub_surfaces"),
    _r("settings.stack", "entity_sub_surfaces", leaf="Stack", parent=("settings", None)),
    _r(
        "notifications",
        "entity_sub_surfaces",
        palette=True,
        go="i",
        leaf="Notifications",
        via="Notifications",
    ),
    _r("merge.conflict", "entity_sub_surfaces", leaf="Conflict", parent=("git.pr", None)),
    _r("export", "entity_sub_surfaces", leaf="Export", parent=("run.detail", "RUN-538453eb")),
    _r("receipt", "entity_sub_surfaces", subject=True),
    _r("campaign.step", "entity_sub_surfaces"),
    _r("campaign.artifact", "entity_sub_surfaces"),
)


def _corrected(specs: tuple[RouteSpec, ...]) -> tuple[RouteSpec, ...]:
    """Return ``specs`` with the normalisation map's classification corrections applied.

    The pack table above stays as the pack recorded it, so the goldens keep addressing
    the ids they were generated with; the map is the single place a corrected port key
    or family is written, and this is where it binds.

    Args:
        specs: The route table exactly as the design pack classifies it.

    Raises:
        KeyError: the map corrects a route id the pack table does not carry.
    """
    corrections = load_map().route_corrections()
    unknown = sorted(set(corrections) - {spec.id for spec in specs})
    if unknown:
        raise KeyError(f"normalisation map corrects unregistered routes: {', '.join(unknown)}")
    return tuple(
        spec
        if spec.id not in corrections
        else replace(spec, key=corrections[spec.id].key, family=corrections[spec.id].family)
        for spec in specs
    )


ROUTES: tuple[RouteSpec, ...] = _corrected(_PACK_ROUTES)

ROUTE_BY_ID: dict[str, RouteSpec] = {r.id: r for r in ROUTES}
ROUTE_BY_KEY: dict[str, RouteSpec] = {r.key: r for r in ROUTES}
ROUTE_IDS: tuple[str, ...] = tuple(r.id for r in ROUTES)

# the twelve g destinations, in the prototype's letter order
GO_MAP: dict[str, str] = {r.go_letter: r.id for r in ROUTES if r.go_letter}

# the palette's alphabetical route list (R83: routes that open without a subject)
ROUTE_LIST: tuple[str, ...] = tuple(sorted(r.id for r in ROUTES if r.palette_visible))

# the word the palette and the crumb use for a route that is not spelled by its id
ROUTE_WORD: dict[str, str] = {
    "task.detail": "task",
    "run.detail": "run",
    "scope.home": "home",
    "sandbox.log": "sandbox log",
    "crash.recovery": "recovery",
    "entry": "attach workspace",
}

STEP_LEAF: dict[str, str] = {r.id: r.step_leaf for r in ROUTES if r.step_leaf is not None}
VIA_LEAF: dict[str, str] = {r.id: r.via_leaf for r in ROUTES if r.via_leaf}
PARENT_ROUTE: dict[str, tuple[str, str | None]] = {r.id: r.parent for r in ROUTES if r.parent}
TAB_OWNER: dict[str, str] = {r.id: r.tab_owner for r in ROUTES if r.tab_owner}

# a route that renders exactly one entity has that entity as its subject
ROUTE_SUBJ: dict[str, str] = {"evidence": "CLM-0004", "trust": "MLS-0007"}

# the id prefix -> route table the drill seam, the palette and the scope-home tree share
ROUTE_OF: dict[str, str] = {
    "RUN-": "run.detail",
    "EAWF": "task.detail",
    "BAT-": "batch.detail",
    "MLS-": "milestone",
    "REL-": "release",
    "CAM-": "campaign",
    "CLM-": "evidence",
    "TRK-": "track",
    "QST-": "attention",
    "EVT-": "run.detail",
}

KIND: dict[str, str] = {
    "RUN": "run",
    "EAWF": "task",
    "BAT": "batch",
    "MLS": "milestone",
    "CAM": "campaign",
    "CLM": "claim",
    "REL": "release",
    "ACT": "action",
    "EVT": "event",
    "TRK": "track",
    "EVD": "evidence",
}

# milestone sections, cycled by Tab
SECTIONS: tuple[str, ...] = ("glance", "try", "changes", "evidence", "risks", "raw")

# the closed overlay set: full-frame, own header crumb, own keybar
OVERLAYS: tuple[str, ...] = (
    "help",
    "palette",
    "consequence",
    "question",
    "pause",
    "evidence",
    "readiness",
    "resolution",
    "draft",
    "marker",
)
# overlays that legitimately move a cursor; every other overlay swallows arrows
OVERLAY_ARROWS: frozenset[str] = frozenset({"evidence", "readiness", "marker", "draft"})
# drawers keep the route body and replace its tail rows and keybar (the prototype's PANES)
DRAWERS: tuple[str, ...] = ("go", "actions", "inspect", "raw")
# overlay-backed routes: a route drawn over its own backdrop, never a fourteenth overlay
OVERLAY_ROUTES: tuple[str, ...] = (
    "notifications",
    "merge.conflict",
    "export",
    "settings.stack",
    "evidence.digest",
    "campaign.artifact",
    "campaign.step",
)

_ROUTE_FOR_ID = (
    (re.compile(r"^RUN-"), "run.detail"),
    (re.compile(r"^BAT-"), "batch.detail"),
    (re.compile(r"^MLS-"), "milestone"),
    (re.compile(r"^REL-"), "release"),
    (re.compile(r"^CAM-"), "campaign"),
    (re.compile(r"^EAWF-"), "task.detail"),
    (re.compile(r"^EVT-"), "receipt"),
    (re.compile(r"^CLM-|^EVD-"), "evidence"),
)


def route_word(route: str) -> str:
    return ROUTE_WORD.get(route, route)


def step_leaf(route: str, subj: str | None) -> str:
    if subj:
        return subj
    if route in STEP_LEAF:
        return STEP_LEAF[route]
    return route_word(route)


def route_for_id(entity_id: str | None) -> str | None:
    """The prototype's routeForId: the only correct destination for a drill onto an id."""
    if not entity_id:
        return None
    for pattern, route in _ROUTE_FOR_ID:
        if pattern.search(entity_id):
            return route
    return None


def route_of(entity_id: str) -> str | None:
    """The Phase G ROUTE_OF table, keyed by the first four characters or the prefix."""
    return ROUTE_OF.get(entity_id[:4]) or ROUTE_OF.get(entity_id[:3] + "-")


def kind_of(entity_id: str | None) -> str:
    return KIND.get(str(entity_id or "").split("-")[0], "entity")


def subj_now(route: str, entity_id: str | None) -> str | None:
    return entity_id or ROUTE_SUBJ.get(route)


def palette_routes() -> tuple[str, ...]:
    return ROUTE_LIST


def parent_of(session: Session, fixture: Fixture) -> tuple[str, str | None] | None:
    """One step up the breadcrumb when the back stack is empty (the containment rule).

    Fixed parents first, then the subject's own record fields (BATCH, MILESTONE, TRACK,
    SCOPE); scope home has no parent, and everything else climbs to scope home.
    """
    from ..chassis.derive import field_of, track_id_of

    route, subj = session.route, session.subj_id
    if route in PARENT_ROUTE:
        return PARENT_ROUTE[route]
    if subj:
        if subj.startswith("EAWF-"):
            m = re.search(r"BAT-\d{4}", str(field_of(fixture, subj, "BATCH") or ""))
            if m:
                return ("batch.detail", m.group(0))
        if subj.startswith("BAT-"):
            m = re.search(r"MLS-\d{4}", str(field_of(fixture, subj, "MILESTONE") or ""))
            if m:
                return ("milestone", m.group(0))
        if subj.startswith("MLS-"):
            tid = track_id_of(fixture, field_of(fixture, subj, "TRACK") or "")
            if tid:
                return ("track", tid)
        if subj.startswith("RUN-"):
            m = re.search(r"EAWF-\d{4}", str(field_of(fixture, subj, "SCOPE") or ""))
            if m:
                return ("task.detail", m.group(0))
    if route == "scope.home":
        return None
    return ("scope.home", None)
