"""Route registry: the data table the console dispatcher, crumb and palette read.

Adding a route is adding one ``RouteSpec`` row to ``ROUTES``. Every index the console
reads (the ``g`` map, the palette list, crumb leaves, fixed parents, Tab owners, route
words) is derived from the rows by :class:`RouteRegistry`, so no header, keybar or token
module changes when a route arrives.

The rows are the design pack's route set with the pack-to-port normalisation map's two
classification corrections applied: the pack's ``timeline`` route carries the port key
``roadmap``, and ``notifications`` is a global diagnostics route rather than an entity
sub-surface. The pack id stays the row id because the golden contract addresses it.

A route exists only with a way in. The ``g`` letter and palette doors come from a row's
own columns, a drill door from the entity-id prefix tables, and a light-verb or route-key
door is declared on the row; :func:`unreachable_routes` is the reachability audit, and a
registry holding a route with no door refuses to build.

Each row also names the read model it renders. The kernel's read-model declarations name
the routes each model serves, and a registry whose binding disagrees with them in either
direction refuses to build; a row with no read model is a specification hole the registry
lists in :attr:`RouteRegistry.read_model_holes`.

A row may also name the regions the focus moves between. The chassis bounds a route at
:data:`FOCUS_REGION_LIMIT` of them, because a fourth region is a frame an operator has to
remember rather than read, and a registry holding a row over the bound refuses to build.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from eawf.kernel.projection.read_models import (
    READ_MODEL_BY_KIND,
    ReadModelKind,
    route_binding_mismatches,
)
from eawf.surfaces.tui.console import prototype as pt


class RouteFamily(StrEnum):
    """The registry group a route belongs to."""

    ENTRY_LAYER = "entry_layer"
    SPINE = "spine"
    LIVE = "live"
    ACCEPTANCE = "acceptance"
    PLANNING = "planning"
    DIAGNOSTICS = "diagnostics"
    VERIFICATION = "verification"
    OPERATIONS = "operations"
    ENTITY_SUB_SURFACES = "entity_sub_surfaces"


class DoorKind(StrEnum):
    """How an operator reaches a route."""

    GO = "go"
    PALETTE = "palette"
    DRILL = "drill"
    LIGHT_VERB = "light_verb"
    ROUTE_KEY = "route_key"


# Doors that live on another route; the rest are reachable from anywhere.
_LOCAL_DOORS = frozenset({DoorKind.LIGHT_VERB, DoorKind.ROUTE_KEY})

#: The most focus regions one route may declare. Three is the chassis bound: a frame with
#: a fourth place for the arrows to be is one an operator has to remember rather than read.
FOCUS_REGION_LIMIT: int = 3


@dataclass(frozen=True, slots=True, kw_only=True)
class Door:
    """One way into a route.

    Attributes:
        kind: How the door opens: the ``g`` prefix, the palette route list, a drill onto
            an entity id, an entry in another route's action menu, or a key another route
            binds (Enter on a row or a route letter).
        key: What the operator presses or matches: the ``g`` letter, the key bound on
            ``origin``, or the entity-id prefix a drill resolves. ``None`` for the palette,
            and for a menu entry the design has not bound to a letter.
        origin: The route a light verb or route key lives on; ``None`` for a global door.

    Raises:
        ValueError: a light-verb or route-key door names no origin, or a global door
            names one.
    """

    kind: DoorKind
    key: str | None = None
    origin: str | None = None

    def __post_init__(self) -> None:
        """Reject a door whose origin does not match its kind."""
        if (self.kind in _LOCAL_DOORS) != (self.origin is not None):
            raise ValueError(
                f"a {self.kind} door {'needs' if self.kind in _LOCAL_DOORS else 'takes no'} "
                "origin route"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class RouteSpec:
    """One route row.

    Attributes:
        id: The route id the golden contract addresses and renderers are named for.
        key: The port's canonical route key; empty means ``id``. It differs from ``id``
            only where the normalisation map renames a route, and it is what new code
            keys on.
        family: The registry group.
        subject_required: Whether the route opens only onto one entity.
        palette_visible: Whether the palette's route list offers it; only routes that
            open without a subject belong there.
        go_letter: The ``g``-prefix letter that opens it.
        step_leaf: The crumb leaf with no subject. ``None`` means the route word, and
            ``""`` means the root itself.
        parent: The fixed ``(route, subject)`` Escape climbs to on an empty back stack.
        tab_owner: The region Tab cycles on this route, named in the diagnostics log.
        via_leaf: The crumb leaf an entity drilled from this list surface shows.
        word: The palette and crumb word when it differs from ``id``.
        fixed_subject: The one entity a route renders when it opens without a subject.
        overlay_backed: Whether the route draws over its own backdrop, like an overlay,
            while staying a route.
        doors: The light-verb and route-key doors other routes bind to this one.
        read_model: The read model the route renders; ``None`` marks a specification hole.
        focus_regions: The places the focus moves between on this route, in cycle order;
            empty for a route whose frame has one place for the arrows to be.

    Raises:
        ValueError: ``doors`` declares a ``g``, palette or drill door, which the row's
            columns and the prefix tables own.
    """

    id: str
    family: RouteFamily
    key: str = ""
    subject_required: bool = False
    palette_visible: bool = False
    go_letter: str | None = None
    step_leaf: str | None = None
    parent: tuple[str, str | None] | None = None
    tab_owner: str | None = None
    via_leaf: str | None = None
    word: str | None = None
    fixed_subject: str | None = None
    overlay_backed: bool = False
    doors: tuple[Door, ...] = ()
    read_model: ReadModelKind | None = None
    focus_regions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Default the key to the id and keep derived doors in the columns that own them."""
        if not self.key:
            # a frozen dataclass admits the default only through object.__setattr__
            object.__setattr__(self, "key", self.id)
        derived = sorted({door.kind for door in self.doors if door.kind not in _LOCAL_DOORS})
        if derived:
            raise ValueError(
                f"route {self.id!r} declares {', '.join(derived)} doors; "
                "those come from its columns and the drill tables"
            )


# The id prefix -> route table the drill seam, the palette and the scope-home tree share.
ROUTE_OF: Mapping[str, str] = MappingProxyType(
    {
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
)

# The only correct destination for a drill onto an id, first matching prefix wins.
ROUTE_FOR_ID: tuple[tuple[str, str], ...] = (
    ("RUN-", "run.detail"),
    ("BAT-", "batch.detail"),
    ("MLS-", "milestone"),
    ("REL-", "release"),
    ("CAM-", "campaign"),
    ("EAWF-", "task.detail"),
    ("EVT-", "receipt"),
    ("CLM-", "evidence"),
    ("EVD-", "evidence"),
)

KIND: Mapping[str, str] = MappingProxyType(
    {
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
)

# Milestone sections, cycled by Tab.
SECTIONS: tuple[str, ...] = ("glance", "try", "changes", "evidence", "risks", "raw")

# The closed overlay set: full-frame, each with its own header crumb and keybar.
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
# Overlays that legitimately move a cursor; every other overlay swallows the arrows.
OVERLAY_ARROWS: frozenset[str] = frozenset({"evidence", "readiness", "marker", "draft"})
# Drawers keep the route body and replace its tail rows and keybar.
DRAWERS: tuple[str, ...] = ("go", "actions", "inspect", "raw")


def _drill_prefixes() -> Mapping[str, tuple[str, ...]]:
    """Return, per route, the entity-id prefixes a drill resolves to it."""
    found: dict[str, list[str]] = {}
    for prefix, route in (*ROUTE_OF.items(), *ROUTE_FOR_ID):
        found.setdefault(route, []).append(prefix)
    return MappingProxyType(
        {route: tuple(dict.fromkeys(prefixes)) for route, prefixes in found.items()}
    )


_DRILL_PREFIXES = _drill_prefixes()


def doors_of(spec: RouteSpec) -> tuple[Door, ...]:
    """Return every door into ``spec``: its column doors, its drill doors, its declared doors."""
    doors: list[Door] = []
    if spec.go_letter:
        doors.append(Door(kind=DoorKind.GO, key=spec.go_letter))
    if spec.palette_visible:
        doors.append(Door(kind=DoorKind.PALETTE))
    doors.extend(Door(kind=DoorKind.DRILL, key=p) for p in _DRILL_PREFIXES.get(spec.id, ()))
    doors.extend(spec.doors)
    return tuple(doors)


def unreachable_routes(routes: Iterable[RouteSpec]) -> tuple[str, ...]:
    """Return the ids of the routes that declare no door, in row order."""
    return tuple(spec.id for spec in routes if not doors_of(spec))


def _duplicates(values: Iterable[str]) -> list[str]:
    """Return the values that occur more than once, sorted."""
    seen: set[str] = set()
    repeated: set[str] = set()
    for value in values:
        (repeated if value in seen else seen).add(value)
    return sorted(repeated)


def _validate(routes: tuple[RouteSpec, ...]) -> None:
    """Refuse a route table the console could not navigate.

    Raises:
        ValueError: two rows share an id, key or ``g`` letter; a parent, door origin or
            drill target names an unregistered route; a row has no door; a row declares
            more than :data:`FOCUS_REGION_LIMIT` focus regions or names one twice; or the
            rows' read-model binding disagrees with the kernel declarations.
    """
    columns = (
        ("id", [spec.id for spec in routes]),
        ("key", [spec.key for spec in routes]),
        ("g letter", [spec.go_letter for spec in routes if spec.go_letter]),
    )
    for column, values in columns:
        repeated = _duplicates(values)
        if repeated:
            raise ValueError(f"route {column} registered twice: {', '.join(repeated)}")
    referenced = {spec.parent[0] for spec in routes if spec.parent}
    referenced |= {door.origin for spec in routes for door in spec.doors if door.origin}
    referenced |= set(_DRILL_PREFIXES)
    unknown = sorted(referenced - {spec.id for spec in routes})
    if unknown:
        raise ValueError(f"route table references unregistered routes: {', '.join(unknown)}")
    orphans = unreachable_routes(routes)
    if orphans:
        raise ValueError(f"routes with no door: {', '.join(orphans)}")
    crowded = [
        f"{spec.id} declares {len(spec.focus_regions)}"
        for spec in routes
        if len(spec.focus_regions) > FOCUS_REGION_LIMIT
    ]
    if crowded:
        raise ValueError(
            f"routes over the {FOCUS_REGION_LIMIT}-region focus limit: {', '.join(crowded)}"
        )
    repeated_regions = [
        f"{spec.id}: {', '.join(_duplicates(spec.focus_regions))}"
        for spec in routes
        if _duplicates(spec.focus_regions)
    ]
    if repeated_regions:
        raise ValueError(f"routes naming a focus region twice: {'; '.join(repeated_regions)}")
    binding = {spec.key: spec.read_model for spec in routes if spec.read_model is not None}
    mismatches = route_binding_mismatches(binding, READ_MODEL_BY_KIND)
    if mismatches:
        raise ValueError(f"route read-model binding disagrees: {'; '.join(mismatches)}")


class RouteRegistry:
    """The route rows and every index the console derives from them.

    Attributes:
        routes: The rows, in registry order.
        ids: Every route id, in registry order.
        by_id: Row per route id.
        by_key: Row per canonical port key.
        go_map: Route id per ``g`` letter.
        route_list: The palette's route list, alphabetical.
        step_leaves: Crumb leaf per route that declares one.
        via_leaves: Drilled-from crumb leaf per list route that declares one.
        parents: Fixed ``(route, subject)`` parent per route that declares one.
        tab_owners: Tab region per route that declares one.
        overlay_routes: The routes drawn over their own backdrop.
        read_models: Read model per route that declares one.
        read_model_holes: The routes with no read model, in registry order.
        focus_regions: Focus regions per route that declares any, in cycle order.

    Raises:
        ValueError: the rows fail the registry checks (duplicate id, key or ``g``
            letter; a reference to an unregistered route; a route with no door; a route
            over the focus-region limit or naming one region twice; a read-model binding
            the kernel declarations do not mirror).
    """

    def __init__(self, routes: Sequence[RouteSpec]) -> None:
        self.routes: tuple[RouteSpec, ...] = tuple(routes)
        _validate(self.routes)
        self.ids: tuple[str, ...] = tuple(spec.id for spec in self.routes)
        self.by_id: Mapping[str, RouteSpec] = MappingProxyType({s.id: s for s in self.routes})
        self.by_key: Mapping[str, RouteSpec] = MappingProxyType({s.key: s for s in self.routes})
        self.go_map: Mapping[str, str] = MappingProxyType(
            {s.go_letter: s.id for s in self.routes if s.go_letter}
        )
        self.route_list: tuple[str, ...] = tuple(
            sorted(s.id for s in self.routes if s.palette_visible)
        )
        self.step_leaves: Mapping[str, str] = MappingProxyType(
            {s.id: s.step_leaf for s in self.routes if s.step_leaf is not None}
        )
        self.via_leaves: Mapping[str, str] = MappingProxyType(
            {s.id: s.via_leaf for s in self.routes if s.via_leaf}
        )
        self.parents: Mapping[str, tuple[str, str | None]] = MappingProxyType(
            {s.id: s.parent for s in self.routes if s.parent}
        )
        self.tab_owners: Mapping[str, str] = MappingProxyType(
            {s.id: s.tab_owner for s in self.routes if s.tab_owner}
        )
        self.overlay_routes: frozenset[str] = frozenset(
            s.id for s in self.routes if s.overlay_backed
        )
        self.read_models: Mapping[str, ReadModelKind] = MappingProxyType(
            {s.id: s.read_model for s in self.routes if s.read_model is not None}
        )
        self.read_model_holes: tuple[str, ...] = tuple(
            s.id for s in self.routes if s.read_model is None
        )
        self.focus_regions: Mapping[str, tuple[str, ...]] = MappingProxyType(
            {s.id: s.focus_regions for s in self.routes if s.focus_regions}
        )

    def route_word(self, route: str) -> str:
        """Return the palette and crumb word for ``route``; an unknown id is its own word."""
        spec = self.by_id.get(route)
        return spec.word if spec is not None and spec.word else route

    def step_leaf(self, route: str, subj: str | None) -> str:
        """Return the crumb leaf for ``route``: the subject, its declared leaf, or its word."""
        if subj:
            return subj
        if route in self.step_leaves:
            return self.step_leaves[route]
        return self.route_word(route)

    def subj_now(self, route: str, entity_id: str | None) -> str | None:
        """Return the subject ``route`` renders: the given id, else its fixed subject."""
        if entity_id:
            return entity_id
        spec = self.by_id.get(route)
        return spec.fixed_subject if spec is not None else None

    def doors(self, route: str) -> tuple[Door, ...]:
        """Return every door into ``route``.

        Raises:
            KeyError: ``route`` is not registered.
        """
        return doors_of(self.by_id[route])


def route_for_id(entity_id: str | None) -> str | None:
    """Return the only correct destination for a drill onto ``entity_id``, if any."""
    if not entity_id:
        return None
    for prefix, route in ROUTE_FOR_ID:
        if entity_id.startswith(prefix):
            return route
    return None


def route_of(entity_id: str) -> str | None:
    """Return the route ``ROUTE_OF`` files ``entity_id`` under, by its first four characters."""
    return ROUTE_OF.get(entity_id[:4]) or ROUTE_OF.get(f"{entity_id[:3]}-")


def kind_of(entity_id: str | None) -> str:
    """Return the entity kind word for ``entity_id``; an unknown prefix is an ``entity``."""
    return KIND.get((entity_id or "").split("-")[0], "entity")


_RUN_PARENT = ("run.detail", pt.RUN_PARENT)
_RM = ReadModelKind

ROUTES: tuple[RouteSpec, ...] = (
    RouteSpec(
        id="entry",
        family=RouteFamily.ENTRY_LAYER,
        palette_visible=True,
        tab_owner="path",
        word="attach workspace",
        read_model=_RM.PROCESS_FRAME,
        focus_regions=("path",),
    ),
    RouteSpec(
        id="scope.home",
        family=RouteFamily.SPINE,
        palette_visible=True,
        go_letter="h",
        step_leaf="",
        word="home",
        read_model=_RM.SCOPE_HOME_VIEW,
        focus_regions=("outcomes", "attention"),
    ),
    RouteSpec(
        id="track",
        family=RouteFamily.SPINE,
        subject_required=True,
        step_leaf="Runtime",
        read_model=_RM.ENTITY_DETAIL_VIEW,
        focus_regions=("milestones", "campaigns", "queue"),
    ),
    RouteSpec(
        id="batch.detail",
        family=RouteFamily.SPINE,
        subject_required=True,
        read_model=_RM.ENTITY_DETAIL_VIEW,
        focus_regions=("tasks",),
    ),
    RouteSpec(
        id="task.detail",
        family=RouteFamily.SPINE,
        subject_required=True,
        word="task",
        read_model=_RM.ENTITY_DETAIL_VIEW,
        focus_regions=("criteria", "runs"),
    ),
    RouteSpec(
        id="git.pr",
        family=RouteFamily.SPINE,
        step_leaf="Git",
        parent=_RUN_PARENT,
        doors=(Door(kind=DoorKind.LIGHT_VERB, key="b", origin="run.detail"),),
        read_model=_RM.GIT_PR_VIEW,
    ),
    RouteSpec(
        id="activity",
        family=RouteFamily.LIVE,
        palette_visible=True,
        go_letter="a",
        step_leaf="Activity",
        tab_owner="buckets",
        via_leaf="Activity",
        read_model=_RM.FLEET_QUERY_PAGE,
    ),
    RouteSpec(
        id="run.detail",
        family=RouteFamily.LIVE,
        subject_required=True,
        word="run",
        read_model=_RM.RUN_DETAIL_VIEW,
        focus_regions=("timeline",),
    ),
    RouteSpec(
        id="attention",
        family=RouteFamily.LIVE,
        palette_visible=True,
        go_letter="n",
        step_leaf="Needs you",
        via_leaf="Needs you",
        read_model=_RM.ATTENTION_PAGE,
    ),
    RouteSpec(
        id="transcript",
        family=RouteFamily.LIVE,
        step_leaf="Transcript",
        parent=_RUN_PARENT,
        doors=(Door(kind=DoorKind.LIGHT_VERB, key="l", origin="run.detail"),),
        read_model=_RM.TRANSCRIPT_VIEW,
    ),
    RouteSpec(
        id="cost.ceiling",
        family=RouteFamily.LIVE,
        step_leaf="Cost ceiling",
        via_leaf="Cost ceiling",
        doors=(Door(kind=DoorKind.LIGHT_VERB, key="m", origin="run.detail"),),
        read_model=_RM.COST_CEILING_VIEW,
    ),
    RouteSpec(
        id="crash.recovery",
        family=RouteFamily.LIVE,
        palette_visible=True,
        step_leaf="Recovery",
        word="recovery",
        read_model=_RM.CRASH_RECOVERY_VIEW,
    ),
    RouteSpec(
        id="milestone",
        family=RouteFamily.ACCEPTANCE,
        subject_required=True,
        tab_owner="section",
        read_model=_RM.ACCEPTANCE_BUNDLE_VIEW,
    ),
    RouteSpec(
        id="release",
        family=RouteFamily.ACCEPTANCE,
        subject_required=True,
        go_letter="r",
        step_leaf="REL-0001",
        tab_owner="section",
        read_model=_RM.RELEASE_READINESS_VIEW,
    ),
    RouteSpec(
        id="timeline",
        key="roadmap",
        family=RouteFamily.PLANNING,
        palette_visible=True,
        go_letter="t",
        step_leaf="Timeline",
        via_leaf="Timeline",
        read_model=_RM.ROADMAP_VIEW,
    ),
    RouteSpec(
        id="backlog",
        family=RouteFamily.PLANNING,
        palette_visible=True,
        go_letter="b",
        step_leaf="Backlog",
        tab_owner="groups",
        read_model=_RM.BACKLOG_VIEW,
    ),
    RouteSpec(
        id="campaign",
        family=RouteFamily.PLANNING,
        subject_required=True,
        step_leaf="CAM-0001",
        tab_owner="sections",
        read_model=_RM.CAMPAIGN_VIEW,
    ),
    RouteSpec(
        id="history",
        family=RouteFamily.DIAGNOSTICS,
        palette_visible=True,
        go_letter="y",
        step_leaf="History",
        via_leaf="History",
        read_model=_RM.HISTORY_PAGE,
    ),
    RouteSpec(
        id="settings",
        family=RouteFamily.DIAGNOSTICS,
        palette_visible=True,
        go_letter="s",
        step_leaf="Settings",
        tab_owner="rail",
        read_model=_RM.EFFECTIVE_SETTINGS_VIEW,
    ),
    RouteSpec(
        id="search",
        family=RouteFamily.DIAGNOSTICS,
        palette_visible=True,
        step_leaf="Search",
        via_leaf="Search",
        read_model=_RM.SEARCH_PAGE,
    ),
    RouteSpec(
        id="history.diff",
        family=RouteFamily.DIAGNOSTICS,
        step_leaf="Diff",
        parent=("history", None),
        doors=(Door(kind=DoorKind.LIGHT_VERB, key="d", origin="history"),),
        read_model=_RM.HISTORY_DIFF_VIEW,
    ),
    RouteSpec(
        id="trust",
        family=RouteFamily.VERIFICATION,
        step_leaf="Trust",
        parent=("milestone", "MLS-0007"),
        fixed_subject="MLS-0007",
        doors=(Door(kind=DoorKind.LIGHT_VERB, key="v", origin="milestone"),),
        read_model=_RM.TRUST_VIEW,
    ),
    RouteSpec(
        id="evidence",
        family=RouteFamily.VERIFICATION,
        subject_required=True,
        step_leaf="CLM-0004",
        parent=("campaign", "CAM-0001"),
        fixed_subject="CLM-0004",
        read_model=_RM.EVIDENCE_VIEW,
    ),
    RouteSpec(
        id="health",
        family=RouteFamily.VERIFICATION,
        palette_visible=True,
        go_letter="d",
        step_leaf="Health",
        via_leaf="Health",
        read_model=_RM.HEALTH_VIEW,
    ),
    RouteSpec(
        id="sandbox.log",
        family=RouteFamily.OPERATIONS,
        palette_visible=True,
        go_letter="l",
        step_leaf="Sandbox log",
        via_leaf="Sandbox log",
        word="sandbox log",
        read_model=_RM.SANDBOX_LOG_PAGE,
    ),
    RouteSpec(
        id="unattended",
        family=RouteFamily.OPERATIONS,
        palette_visible=True,
        go_letter="u",
        step_leaf="Unattended",
        via_leaf="Unattended",
        read_model=_RM.UNATTENDED_VIEW,
    ),
    RouteSpec(
        id="evidence.digest",
        family=RouteFamily.ENTITY_SUB_SURFACES,
        overlay_backed=True,
        doors=(Door(kind=DoorKind.ROUTE_KEY, key="Enter", origin="evidence"),),
        read_model=_RM.EVIDENCE_RUNG_RECORD,
    ),
    RouteSpec(
        id="settings.stack",
        family=RouteFamily.ENTITY_SUB_SURFACES,
        step_leaf="Stack",
        parent=("settings", None),
        overlay_backed=True,
        doors=(Door(kind=DoorKind.ROUTE_KEY, key="i", origin="settings"),),
        read_model=_RM.SETTINGS_LAYER_STACK,
    ),
    RouteSpec(
        id="notifications",
        family=RouteFamily.DIAGNOSTICS,
        palette_visible=True,
        go_letter="i",
        step_leaf="Notifications",
        via_leaf="Notifications",
        overlay_backed=True,
        read_model=_RM.NOTIFICATION_MATRIX_VIEW,
    ),
    RouteSpec(
        id="merge.conflict",
        family=RouteFamily.ENTITY_SUB_SURFACES,
        step_leaf="Conflict",
        parent=("git.pr", None),
        overlay_backed=True,
        doors=(Door(kind=DoorKind.ROUTE_KEY, key="m", origin="git.pr"),),
        read_model=_RM.MERGE_CONFLICT_VIEW,
    ),
    RouteSpec(
        id="export",
        family=RouteFamily.ENTITY_SUB_SURFACES,
        step_leaf="Export",
        parent=_RUN_PARENT,
        overlay_backed=True,
        # the Run's action menu offers the report, but the design binds it no letter yet
        doors=(Door(kind=DoorKind.LIGHT_VERB, origin="run.detail"),),
        read_model=_RM.RUN_REPORT_PLAN,
    ),
    RouteSpec(
        id="receipt",
        family=RouteFamily.ENTITY_SUB_SURFACES,
        subject_required=True,
        read_model=_RM.CRITERION_RECEIPT_VIEW,
    ),
    RouteSpec(
        id="campaign.step",
        family=RouteFamily.ENTITY_SUB_SURFACES,
        overlay_backed=True,
        doors=(Door(kind=DoorKind.ROUTE_KEY, key="Enter", origin="campaign"),),
        read_model=_RM.CAMPAIGN_PLAN_STEP,
    ),
    RouteSpec(
        id="campaign.artifact",
        family=RouteFamily.ENTITY_SUB_SURFACES,
        overlay_backed=True,
        doors=(
            Door(kind=DoorKind.ROUTE_KEY, key="Enter", origin="campaign"),
            Door(kind=DoorKind.ROUTE_KEY, key="Enter", origin="campaign.step"),
        ),
        read_model=_RM.ARTIFACT_CARD_VIEW,
    ),
)

REGISTRY = RouteRegistry(ROUTES)
