"""Route registry: the closed route set of the console, and every index derived from it.

Adding a route is adding one ``RouteSpec`` row to ``ROUTES``. Every index the console
reads (the ``g`` map, the palette list, crumb leaves, Escape parents, Tab owners, rails,
route words) is derived from the rows by :class:`RouteRegistry`, so no header, keybar or
token module changes when a route arrives. The entry layer's pre-session states are the
registry's other table, :data:`ENTRY_STATES`; the overlays and drawers are render forms a
route carries and are never rows of either.

The rows are the design pack's route set with the pack-to-port normalisation map's two
classification corrections applied: the pack's ``timeline`` route carries the port key
``roadmap``, and ``notifications`` is a global diagnostics route rather than an entity
sub-surface. The pack id stays the row id because the golden contract addresses it.

Each row places its route in a route group, and an entity sub-surface -- a route whose
subject is one record of a parent entity -- names that parent and takes its group. Each
row names the typed Escape parent the breadcrumb climbs to on an empty back stack; only
the root lacks one, every other row reaches the root by climbing, and a sub-surface
climbs to the entity it is about.

A route composes as a stack of panes with at most one rail beside it, and a rail renders
only where its row declares one, from the width the row names. A rail is a display
region, never a focus region.

A route exists only with a way in. The ``g`` letter and palette doors come from a row's
own columns, a drill door from the entity-id prefix tables, and a light-verb or route-key
door is declared on the row; :func:`unreachable_routes` is the reachability audit, and a
registry holding a route with no door refuses to build. A route that needs a subject
opens only onto one, so it is never a ``g`` or palette destination.

Each row also names the read model it renders. The kernel's read-model declarations name
the routes each model serves, and a registry whose binding disagrees with them in either
direction refuses to build; a row with no read model is a specification hole the registry
lists in :attr:`RouteRegistry.read_model_holes`.

A row may also name the regions the focus moves between. The chassis bounds a route at
:data:`FOCUS_REGION_LIMIT` of them, because a fourth region is a frame an operator has to
remember rather than read, and a registry holding a row over the bound refuses to build.

Design coverage is declared per row: a route either has a golden frame at every size the
contract records or names its hole, the reason it has none.
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
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.tui.console import prototype as pt


class RouteGroup(StrEnum):
    """The route group a route belongs to: the unit the coverage grid headlines.

    ``family`` is reserved for the entity-state sets the galleries render, so the
    grouping of routes is never called one.
    """

    ENTRY = "entry"
    SPINE = "spine"
    LIVE = "live"
    ACCEPTANCE = "acceptance"
    PLANNING = "planning"
    DIAGNOSTICS = "diagnostics"
    VERIFICATION = "verification"
    OPERATIONS = "operations"


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

#: The route every Escape climb ends at, and the one route with no Escape parent.
ROOT_ROUTE: str = "scope.home"


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
class Escape:
    """Where Escape climbs from a route when the back stack is empty.

    Attributes:
        route: The parent route.
        subject: The one entity the parent opens onto; ``None`` opens it without one,
            unless ``via`` names where the subject is read from.
        via: The label of the field in the route subject's own record that names the
            parent's subject -- a Task's ``BATCH``, a Batch's ``MILESTONE``. A record
            that names no parent climbs to the root instead.

    Raises:
        ValueError: both a fixed ``subject`` and a ``via`` field are named.
    """

    route: str
    subject: str | None = None
    via: str | None = None

    def __post_init__(self) -> None:
        """Reject a parent whose subject is both fixed and read from a record."""
        if self.subject is not None and self.via is not None:
            raise ValueError(f"an Escape to {self.route!r} names a subject and a via field")


@dataclass(frozen=True, slots=True, kw_only=True)
class Rail:
    """The one display region a route draws beside its pane stack.

    Attributes:
        name: What the rail lists, as the diagnostics log and the coverage grid name it.
        min_width: The narrowest terminal the rail renders on; a narrower one folds it
            into the pane stack.

    Raises:
        ValueError: ``min_width`` is not a positive cell count.
    """

    name: str
    min_width: int

    def __post_init__(self) -> None:
        """Reject a rail with no width to start at."""
        if self.min_width <= 0:
            raise ValueError(f"rail {self.name!r} starts at {self.min_width} cells")


@dataclass(frozen=True, slots=True, kw_only=True)
class RouteSpec:
    """One route row.

    Attributes:
        id: The route id the golden contract addresses and renderers are named for.
        group: The route group; a sub-surface takes its parent's.
        question: The operator question the route answers.
        needs: The projection the route requires to answer it.
        escape: The typed Escape parent; ``None`` only for the root.
        key: The port's canonical route key; empty means ``id``. It differs from ``id``
            only where the normalisation map renames a route, and it is what new code
            keys on.
        sub_surface_of: The parent entity route of an entity sub-surface; ``None`` for a
            route of its own.
        subject_required: Whether the route opens only onto one entity; such a route is
            never a ``g`` or palette destination.
        palette_visible: Whether the palette's route list offers it.
        palette_overflow: Whether the row an overflowing palette list ends in opens it;
            such a route is reached from the palette but is never a row of its route list.
        go_letter: The ``g``-prefix letter that opens it.
        step_leaf: The crumb label with no subject. ``None`` means the route word, and
            ``""`` means the root itself.
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
        rail: The one rail beside the pane stack; ``None`` for a route drawing none.
        hole: Why the route has no golden frame at the recorded sizes; empty when it has.

    Raises:
        ValueError: ``doors`` declares a ``g``, palette or drill door, which the row's
            columns and the prefix tables own.
    """

    id: str
    group: RouteGroup
    question: str
    needs: str
    escape: Escape | None
    key: str = ""
    sub_surface_of: str | None = None
    subject_required: bool = False
    palette_visible: bool = False
    palette_overflow: bool = False
    go_letter: str | None = None
    step_leaf: str | None = None
    tab_owner: str | None = None
    via_leaf: str | None = None
    word: str | None = None
    fixed_subject: str | None = None
    overlay_backed: bool = False
    doors: tuple[Door, ...] = ()
    read_model: ReadModelKind | None = None
    focus_regions: tuple[str, ...] = ()
    rail: Rail | None = None
    hole: str = ""

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


@dataclass(frozen=True, slots=True, kw_only=True)
class EntryStateSpec:
    """One pre-session state of the entry layer.

    Attributes:
        id: The chrome's id for the state, which the entry goldens address.
        key: The registry key the packet names the state by.
        label: The crumb label the state shows.
        question: The operator question the state answers.
    """

    id: str
    key: str
    label: str
    question: str


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

#: The route each record collection opens on: a drill goes where the row's own kind lives.
#: The collection a read model states is the authority, because a key such as a Track's or
#: an imported Task's carries no prefix :data:`ROUTE_FOR_ID` knows.
COLLECTION_ROUTES: Mapping[Epoch2Collection, str] = MappingProxyType(
    {
        Epoch2Collection.TRACK: "track",
        Epoch2Collection.MILESTONE: "milestone",
        Epoch2Collection.BATCH: "batch.detail",
        Epoch2Collection.TASK: "task.detail",
        Epoch2Collection.RUN: "run.detail",
        Epoch2Collection.CAMPAIGN: "campaign",
    }
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


@dataclass(frozen=True, slots=True, kw_only=True)
class SurfaceSpec:
    """One overlay or drawer: the name a session opens it by and what it may do.

    Attributes:
        name: The name ``session.overlay`` holds while the surface is open.
        title: The word the surface's crumb names it by.
        cursor: Whether it draws a cursor, so it binds the arrows that move it; a
            cursorless surface swallows them instead.
        authority: Whether it offers a verb beyond open, cursor movement and copy. A
            surface without authority offers the action menu nothing and the connection
            gate nothing to refuse.
    """

    name: str
    title: str
    cursor: bool
    authority: bool


# The closed overlay set: full-frame, each with its own header crumb and keybar. The
# evidence viewer is over a Claim and the acceptance evidence over a sealed bundle, so they
# are two entries rather than one renderer branching on the route it was opened from.
OVERLAY_SPECS: tuple[SurfaceSpec, ...] = (
    SurfaceSpec(name="help", title="help", cursor=False, authority=False),
    SurfaceSpec(name="palette", title="palette", cursor=True, authority=False),
    SurfaceSpec(name="consequence", title="consequence", cursor=False, authority=True),
    SurfaceSpec(name="question", title="question", cursor=False, authority=True),
    SurfaceSpec(name="pause", title="pause", cursor=False, authority=True),
    SurfaceSpec(name="evidence", title="evidence", cursor=True, authority=False),
    SurfaceSpec(name="acceptance", title="acceptance evidence", cursor=True, authority=False),
    SurfaceSpec(name="readiness", title="readiness", cursor=True, authority=False),
    SurfaceSpec(name="resolution", title="resolution", cursor=False, authority=False),
    SurfaceSpec(name="draft", title="draft", cursor=True, authority=True),
    SurfaceSpec(name="marker", title="marker", cursor=False, authority=False),
)
OVERLAYS: tuple[str, ...] = tuple(spec.name for spec in OVERLAY_SPECS)
# Overlays that legitimately move a cursor; every other overlay swallows the arrows. The
# palette moves its own cursor before the arrows reach the shared handler.
OVERLAY_ARROWS: frozenset[str] = frozenset(
    spec.name for spec in OVERLAY_SPECS if spec.cursor and spec.name != "palette"
)
# Drawers keep the route body and replace its tail rows and keybar.
DRAWER_SPECS: tuple[SurfaceSpec, ...] = (
    SurfaceSpec(name="go", title="go", cursor=False, authority=False),
    SurfaceSpec(name="actions", title="actions", cursor=False, authority=True),
    SurfaceSpec(name="inspect", title="inspect", cursor=False, authority=False),
    SurfaceSpec(name="raw", title="raw", cursor=False, authority=False),
)
DRAWERS: tuple[str, ...] = tuple(spec.name for spec in DRAWER_SPECS)
SURFACES: Mapping[str, SurfaceSpec] = MappingProxyType(
    {spec.name: spec for spec in (*OVERLAY_SPECS, *DRAWER_SPECS)}
)


def _drill_prefixes() -> Mapping[str, tuple[str, ...]]:
    """Return, per route, the entity-id prefixes a drill resolves to it."""
    found: dict[str, list[str]] = {}
    for prefix, route in (*ROUTE_OF.items(), *ROUTE_FOR_ID):
        found.setdefault(route, []).append(prefix)
    return MappingProxyType(
        {route: tuple(dict.fromkeys(prefixes)) for route, prefixes in found.items()}
    )


#: Per route, the entity-id prefixes a drill resolves to it: the ids it takes as subject.
DRILL_PREFIXES: Mapping[str, tuple[str, ...]] = _drill_prefixes()


def doors_of(spec: RouteSpec) -> tuple[Door, ...]:
    """Return every door into ``spec``: its column doors, its drill doors, its declared doors."""
    doors: list[Door] = []
    if spec.go_letter:
        doors.append(Door(kind=DoorKind.GO, key=spec.go_letter))
    if spec.palette_visible or spec.palette_overflow:
        doors.append(Door(kind=DoorKind.PALETTE))
    doors.extend(Door(kind=DoorKind.DRILL, key=p) for p in DRILL_PREFIXES.get(spec.id, ()))
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


def escape_defects(routes: Sequence[RouteSpec]) -> list[str]:
    """Return every way the Escape parent table fails to be total, in row order.

    The table is total when exactly the root declares no parent, every parent is a
    registered route, a sub-surface climbs to the entity it is about, and a climb from
    every route reaches the root without passing a route twice.
    """
    by_id = {spec.id: spec for spec in routes}
    defects: list[str] = []
    rootless = [spec.id for spec in routes if spec.escape is None]
    if rootless != [ROOT_ROUTE]:
        defects.append(f"routes with no Escape parent must be {ROOT_ROUTE} alone: {rootless}")
    for spec in routes:
        if spec.sub_surface_of is not None and (
            spec.escape is None or spec.escape.route != spec.sub_surface_of
        ):
            defects.append(f"sub-surface {spec.id} escapes past its parent {spec.sub_surface_of}")
        seen = [spec.id]
        step = spec
        while step.escape is not None and step.escape.route in by_id:
            nxt = step.escape.route
            if nxt in seen:
                defects.append(f"Escape from {spec.id} loops: {' → '.join([*seen, nxt])}")
                break
            seen.append(nxt)
            step = by_id[nxt]
    return defects


def _validate_references(routes: tuple[RouteSpec, ...]) -> None:
    """Refuse rows that collide, say nothing, point nowhere or have no way in.

    Raises:
        ValueError: two rows share an id, key or ``g`` letter; a row states no operator
            question or no required projection; an Escape parent, sub-surface parent,
            door origin or drill target names an unregistered route; or a row has no door.
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
    unstated = [spec.id for spec in routes if not (spec.question.strip() and spec.needs.strip())]
    if unstated:
        raise ValueError(f"routes stating no question or projection: {', '.join(unstated)}")
    referenced = {spec.escape.route for spec in routes if spec.escape}
    referenced |= {spec.sub_surface_of for spec in routes if spec.sub_surface_of}
    referenced |= {door.origin for spec in routes for door in spec.doors if door.origin}
    referenced |= set(DRILL_PREFIXES)
    unknown = sorted(referenced - {spec.id for spec in routes})
    if unknown:
        raise ValueError(f"route table references unregistered routes: {', '.join(unknown)}")
    orphans = unreachable_routes(routes)
    if orphans:
        raise ValueError(f"routes with no door: {', '.join(orphans)}")


def _validate_subjects(routes: tuple[RouteSpec, ...]) -> None:
    """Refuse subject rules, sub-surface groups and Escape parents that do not hold.

    Raises:
        ValueError: a route needing a subject, or a sub-surface, is a ``g`` or palette
            destination; a sub-surface sits outside its parent's group; or the Escape
            table is not total.
    """
    listed = [
        spec.id
        for spec in routes
        if (spec.subject_required or spec.sub_surface_of)
        and (spec.go_letter or spec.palette_visible or spec.palette_overflow)
    ]
    if listed:
        raise ValueError(
            f"routes needing a subject offered by g or the palette: {', '.join(listed)}"
        )
    by_id = {spec.id: spec for spec in routes}
    strays = [
        f"{spec.id} is {spec.group}, its parent {spec.sub_surface_of} is "
        f"{by_id[spec.sub_surface_of].group}"
        for spec in routes
        if spec.sub_surface_of and spec.group is not by_id[spec.sub_surface_of].group
    ]
    if strays:
        raise ValueError(f"sub-surfaces outside their parent's group: {'; '.join(strays)}")
    defects = escape_defects(routes)
    if defects:
        raise ValueError(f"Escape parent table is not total: {'; '.join(defects)}")


def _validate_regions(routes: tuple[RouteSpec, ...]) -> None:
    """Refuse focus regions over the bound, repeated, or standing in for a rail.

    Raises:
        ValueError: a row declares more than :data:`FOCUS_REGION_LIMIT` focus regions,
            names one twice, or names its rail as one.
    """
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
    focused_rails = [
        spec.id for spec in routes if spec.rail is not None and spec.rail.name in spec.focus_regions
    ]
    if focused_rails:
        raise ValueError(f"routes naming their rail a focus region: {', '.join(focused_rails)}")


def _validate(routes: tuple[RouteSpec, ...]) -> None:
    """Refuse a route table the console could not navigate.

    Raises:
        ValueError: the rows fail a reference, subject or region check (see
            :func:`_validate_references`, :func:`_validate_subjects` and
            :func:`_validate_regions`), or their read-model binding disagrees with the
            kernel declarations.
    """
    _validate_references(routes)
    _validate_subjects(routes)
    _validate_regions(routes)
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
        overflow_route: The route an overflowing palette list's last row opens, if any.
        step_leaves: Crumb leaf per route that declares one.
        via_leaves: Drilled-from crumb leaf per list route that declares one.
        escapes: Escape parent per route; the root has none.
        groups: The route ids of each route group, in registry order.
        sub_surfaces: The parent entity route per entity sub-surface.
        tab_owners: Tab region per route that declares one.
        overlay_routes: The routes drawn over their own backdrop.
        read_models: Read model per route that declares one.
        read_model_holes: The routes with no read model, in registry order.
        focus_regions: Focus regions per route that declares any, in cycle order.
        rails: The rail per route that declares one.
        holes: The design-coverage hole per route that declares one.

    Raises:
        ValueError: the rows fail the registry checks (see :func:`_validate`).
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
        overflow = [s.id for s in self.routes if s.palette_overflow]
        if len(overflow) > 1:
            raise ValueError(f"more than one palette overflow route: {', '.join(overflow)}")
        self.overflow_route: str | None = overflow[0] if overflow else None
        self.step_leaves: Mapping[str, str] = MappingProxyType(
            {s.id: s.step_leaf for s in self.routes if s.step_leaf is not None}
        )
        self.via_leaves: Mapping[str, str] = MappingProxyType(
            {s.id: s.via_leaf for s in self.routes if s.via_leaf}
        )
        self.escapes: Mapping[str, Escape] = MappingProxyType(
            {s.id: s.escape for s in self.routes if s.escape is not None}
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
        self.rails: Mapping[str, Rail] = MappingProxyType(
            {s.id: s.rail for s in self.routes if s.rail is not None}
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

    def read_leaf(self, route: str, subj: str | None) -> str:
        """Return the crumb leaf a place shows when drawn from what a link read.

        A route with no subject whose declared leaf names a record names a prototype
        record the tree does not hold, so the route's own word stands in its place.
        """
        leaf = self.step_leaf(route, subj)
        return self.route_word(route) if subj is None and route_for_id(leaf) else leaf

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

    def rail_at(self, route: str, columns: int) -> Rail | None:
        """Return the rail ``route`` draws beside its pane stack on a terminal this wide.

        A route whose row declares no rail draws none at any width, and a declared rail
        folds into the stack below the width its row names. ``columns`` is the
        terminal's width, gutters included, since the pack's breakpoints are terminal
        sizes.
        """
        rail = self.rails.get(route)
        return rail if rail is not None and columns >= rail.min_width else None


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


_RM = ReadModelKind
_G = RouteGroup
_HOME = Escape(route=ROOT_ROUTE)
_RUN_PARENT = Escape(route="run.detail", subject=pt.RUN_PARENT)

ROUTES: tuple[RouteSpec, ...] = (
    RouteSpec(
        id="entry",
        group=_G.ENTRY,
        question="Which workspace is this session for, and what does it need first?",
        needs="the resolution steps, candidates, migration paths and session prerequisites",
        escape=_HOME,
        palette_visible=True,
        tab_owner="path",
        word="attach workspace",
        read_model=_RM.PROCESS_FRAME,
        focus_regions=("path",),
    ),
    RouteSpec(
        id="scope.home",
        group=_G.SPINE,
        question="Where should I look?",
        needs="scope summary, active outcomes, open attention items, freshness",
        escape=None,
        palette_visible=True,
        go_letter="h",
        step_leaf="",
        word="home",
        read_model=_RM.SCOPE_HOME_VIEW,
        focus_regions=("outcomes", "attention"),
    ),
    RouteSpec(
        id="track",
        group=_G.SPINE,
        question="Which outcome stream and WIP exist?",
        needs="Track policy, Milestones, Campaigns, shaped queue",
        escape=_HOME,
        subject_required=True,
        step_leaf="Runtime",
        read_model=_RM.ENTITY_DETAIL_VIEW,
        focus_regions=("milestones", "campaigns", "queue"),
    ),
    RouteSpec(
        id="batch.detail",
        group=_G.SPINE,
        question="What is integrated, checked, reviewed, and mergeable?",
        needs="exact-head ledger, Task frontier, attempts",
        escape=Escape(route="milestone", via="MILESTONE"),
        subject_required=True,
        read_model=_RM.ENTITY_DETAIL_VIEW,
        focus_regions=("tasks",),
    ),
    RouteSpec(
        id="task.detail",
        group=_G.SPINE,
        question="What result, criteria, dependencies, candidates, and Runs exist?",
        needs="Task detail, criteria and proof, lineage",
        escape=Escape(route="batch.detail", via="BATCH"),
        subject_required=True,
        word="task",
        read_model=_RM.ENTITY_DETAIL_VIEW,
        focus_regions=("criteria", "runs"),
    ),
    RouteSpec(
        id="git.pr",
        group=_G.SPINE,
        question="What did the agent do to the repository, and what does the review say?",
        needs="branch with ahead and behind, head, commits, review state, checks",
        escape=_RUN_PARENT,
        step_leaf="Git",
        doors=(Door(kind=DoorKind.LIGHT_VERB, key="b", origin="run.detail"),),
        read_model=_RM.GIT_PR_VIEW,
    ),
    RouteSpec(
        id="activity",
        group=_G.LIVE,
        question="Which Runs need orientation or recovery?",
        needs="aggregate buckets, paged fleet, selected detail",
        escape=_HOME,
        palette_visible=True,
        go_letter="a",
        step_leaf="Activity",
        tab_owner="buckets",
        via_leaf="Activity",
        read_model=_RM.FLEET_QUERY_PAGE,
        rail=Rail(name="buckets", min_width=120),
    ),
    RouteSpec(
        id="run.detail",
        group=_G.LIVE,
        question="What happened in this attempt?",
        needs="semantic timeline, controls, usage, lineage, diagnostics index",
        escape=Escape(route="task.detail", via="SCOPE"),
        subject_required=True,
        word="run",
        read_model=_RM.RUN_DETAIL_VIEW,
        focus_regions=("timeline",),
    ),
    RouteSpec(
        id="attention",
        group=_G.LIVE,
        question="Which exact decision or authority action blocks progress?",
        needs="durable pending actions and permission requests by exception bucket",
        escape=_HOME,
        palette_visible=True,
        go_letter="n",
        step_leaf="Needs you",
        via_leaf="Needs you",
        read_model=_RM.ATTENTION_PAGE,
        rail=Rail(name="buckets", min_width=120),
    ),
    RouteSpec(
        id="transcript",
        group=_G.LIVE,
        question="What did this Run say and do, in its own words and in order?",
        needs="time-ordered folding blocks with follow and hold",
        escape=_RUN_PARENT,
        step_leaf="Transcript",
        doors=(Door(kind=DoorKind.LIGHT_VERB, key="l", origin="run.detail"),),
        read_model=_RM.TRANSCRIPT_VIEW,
    ),
    RouteSpec(
        id="cost.ceiling",
        group=_G.LIVE,
        question="What was spent, what stopped, and where does the ceiling live?",
        needs="ceiling, stopped Runs, spend by provider",
        escape=_HOME,
        step_leaf="Cost ceiling",
        via_leaf="Cost ceiling",
        doors=(Door(kind=DoorKind.LIGHT_VERB, key="m", origin="run.detail"),),
        read_model=_RM.COST_CEILING_VIEW,
    ),
    RouteSpec(
        id="crash.recovery",
        group=_G.LIVE,
        question="The console lost its projection - which way back, and at what cost?",
        needs="the three doors of the reconnect protocol, each with its cost",
        escape=_HOME,
        palette_visible=True,
        step_leaf="Recovery",
        word="recovery",
        read_model=_RM.CRASH_RECOVERY_VIEW,
    ),
    RouteSpec(
        id="milestone",
        group=_G.ACCEPTANCE,
        question="Is the promised outcome ready for acceptance?",
        needs="exact acceptance bundle and Batch membership",
        escape=Escape(route="track", via="TRACK"),
        subject_required=True,
        tab_owner="section",
        read_model=_RM.ACCEPTANCE_BUNDLE_VIEW,
    ),
    RouteSpec(
        id="release",
        group=_G.ACCEPTANCE,
        question="Can exact accepted Milestones publish?",
        needs="membership, readiness, approval, publication facts",
        escape=_HOME,
        go_letter="r",
        step_leaf="REL-0001",
        tab_owner="section",
        read_model=_RM.RELEASE_READINESS_VIEW,
    ),
    RouteSpec(
        id="timeline",
        key="roadmap",
        group=_G.PLANNING,
        question="When do Milestones and Releases land?",
        needs="dated, forecast and undated markers and dependency explanation",
        escape=_HOME,
        palette_visible=True,
        go_letter="t",
        step_leaf="Timeline",
        via_leaf="Timeline",
        read_model=_RM.ROADMAP_VIEW,
    ),
    RouteSpec(
        id="backlog",
        group=_G.PLANNING,
        question="What is queued, and when must it be defined?",
        needs="DRAFT and DEFERRED Tasks by priority and due scope",
        escape=_HOME,
        palette_visible=True,
        go_letter="b",
        step_leaf="Backlog",
        tab_owner="groups",
        read_model=_RM.BACKLOG_VIEW,
    ),
    RouteSpec(
        id="campaign",
        group=_G.PLANNING,
        question=(
            "What question, bounds, evidence, conflicts, checkpoint, steps and artifacts exist?"
        ),
        needs="Campaign plan, question graph, claim and evidence ledger, steps, artifacts",
        escape=Escape(route="track", via="TRACK"),
        subject_required=True,
        step_leaf="CAM-0001",
        tab_owner="sections",
        read_model=_RM.CAMPAIGN_VIEW,
    ),
    RouteSpec(
        id="history",
        group=_G.DIAGNOSTICS,
        question="What fact changed, from what source, at which revision?",
        needs="cursor-paged canonical-state sequence with the resolution card",
        escape=_HOME,
        palette_visible=True,
        go_letter="y",
        step_leaf="History",
        via_leaf="History",
        read_model=_RM.HISTORY_PAGE,
    ),
    RouteSpec(
        id="settings",
        group=_G.DIAGNOSTICS,
        question="What was requested, what is effective, and why?",
        needs="layered config and certified capability projection, by catalog section",
        escape=_HOME,
        palette_visible=True,
        go_letter="s",
        step_leaf="Settings",
        tab_owner="rail",
        read_model=_RM.EFFECTIVE_SETTINGS_VIEW,
        rail=Rail(name="categories", min_width=1),
    ),
    RouteSpec(
        id="search",
        group=_G.DIAGNOSTICS,
        question="What are all the hits of this palette query over the entity registers?",
        needs="exact counts by kind and typed-id hits, cursor-paged",
        escape=_HOME,
        palette_overflow=True,
        step_leaf="Search",
        via_leaf="Search",
        read_model=_RM.SEARCH_PAGE,
    ),
    RouteSpec(
        id="history.diff",
        group=_G.DIAGNOSTICS,
        question="What changed on one entity between two of its revisions, and who caused it?",
        needs="both revisions in full, every changed field with its cause",
        escape=Escape(route="history"),
        step_leaf="Diff",
        doors=(Door(kind=DoorKind.LIGHT_VERB, key="d", origin="history"),),
        read_model=_RM.HISTORY_DIFF_VIEW,
    ),
    RouteSpec(
        id="trust",
        group=_G.VERIFICATION,
        question="Which verdicts and track records back a judgement?",
        needs="verdict rows, the calibration report, producer track record",
        escape=Escape(route="milestone", subject="MLS-0007"),
        step_leaf="Trust",
        fixed_subject="MLS-0007",
        doors=(Door(kind=DoorKind.LIGHT_VERB, key="v", origin="milestone"),),
        read_model=_RM.TRUST_VIEW,
    ),
    RouteSpec(
        id="evidence",
        group=_G.VERIFICATION,
        question="What claims exist, and what supports them?",
        needs="the claim with its prose, the four-rung ladder, graph edges, supports",
        escape=Escape(route="campaign", subject="CAM-0001"),
        subject_required=True,
        step_leaf="CLM-0004",
        fixed_subject="CLM-0004",
        read_model=_RM.EVIDENCE_VIEW,
    ),
    RouteSpec(
        id="health",
        group=_G.VERIFICATION,
        question="What is broken, and what repairs it?",
        needs="check results with reason, provenance and a docked repair readout",
        escape=_HOME,
        palette_visible=True,
        go_letter="d",
        step_leaf="Health",
        via_leaf="Health",
        read_model=_RM.HEALTH_VIEW,
    ),
    RouteSpec(
        id="sandbox.log",
        group=_G.OPERATIONS,
        question="What was authorised or denied, and under which policy?",
        needs="cursor-paged authorisation decisions with reason and policy revision",
        escape=_HOME,
        palette_visible=True,
        go_letter="l",
        step_leaf="Sandbox log",
        via_leaf="Sandbox log",
        word="sandbox log",
        read_model=_RM.SANDBOX_LOG_PAGE,
    ),
    RouteSpec(
        id="unattended",
        group=_G.OPERATIONS,
        question="What is queued, running, and forced sequential?",
        needs="dispatch queue, the derived concurrency plan, per-Run progress",
        escape=_HOME,
        palette_visible=True,
        go_letter="u",
        step_leaf="Unattended",
        via_leaf="Unattended",
        read_model=_RM.UNATTENDED_VIEW,
    ),
    RouteSpec(
        id="evidence.digest",
        group=_G.VERIFICATION,
        question="What did this rung check, over what, and what did it find?",
        needs="one rung record in full",
        escape=Escape(route="evidence"),
        sub_surface_of="evidence",
        step_leaf="Rung",
        overlay_backed=True,
        doors=(Door(kind=DoorKind.ROUTE_KEY, key="Enter", origin="evidence"),),
        read_model=_RM.EVIDENCE_RUNG_RECORD,
        hole="the design pack draws the rung record only inside the Evidence route",
    ),
    RouteSpec(
        id="settings.stack",
        group=_G.DIAGNOSTICS,
        question="Which layer set this key, and which one wins?",
        needs="the layer stack for one key, read only",
        escape=Escape(route="settings"),
        sub_surface_of="settings",
        step_leaf="Stack",
        overlay_backed=True,
        doors=(Door(kind=DoorKind.ROUTE_KEY, key="i", origin="settings"),),
        read_model=_RM.SETTINGS_LAYER_STACK,
    ),
    RouteSpec(
        id="notifications",
        group=_G.DIAGNOSTICS,
        question="Which notice class may interrupt me, and which contract decided so?",
        needs="the notification presentation matrix, read only",
        escape=_HOME,
        palette_visible=True,
        go_letter="i",
        step_leaf="Notifications",
        via_leaf="Notifications",
        overlay_backed=True,
        read_model=_RM.NOTIFICATION_MATRIX_VIEW,
    ),
    RouteSpec(
        id="merge.conflict",
        group=_G.SPINE,
        question="Which hunks conflict, whose are they, and where does resolution land?",
        needs="hunks with both sides and their authorities, display only",
        escape=Escape(route="git.pr"),
        sub_surface_of="git.pr",
        step_leaf="Conflict",
        overlay_backed=True,
        doors=(Door(kind=DoorKind.ROUTE_KEY, key="m", origin="git.pr"),),
        read_model=_RM.MERGE_CONFLICT_VIEW,
    ),
    RouteSpec(
        id="export",
        group=_G.LIVE,
        question="What will the report of this Run contain, at what size, and what is redacted?",
        needs="the report plan: parts, inclusion, size, redaction, destination",
        escape=_RUN_PARENT,
        sub_surface_of="run.detail",
        step_leaf="Export",
        overlay_backed=True,
        # the Run's action menu offers the report, but the design binds it no letter yet
        doors=(Door(kind=DoorKind.LIGHT_VERB, origin="run.detail"),),
        read_model=_RM.RUN_REPORT_PLAN,
    ),
    RouteSpec(
        id="receipt",
        group=_G.SPINE,
        question="What does this receipt record, and does the criterion hold at this digest?",
        needs="one immutable criterion receipt",
        # a receipt opened from an acceptance bundle takes the group of the Task it is
        # listed under first; the back stack still returns it to wherever it was opened
        escape=Escape(route="task.detail", via="KEPT"),
        sub_surface_of="task.detail",
        subject_required=True,
        read_model=_RM.CRITERION_RECEIPT_VIEW,
    ),
    RouteSpec(
        id="campaign.step",
        group=_G.PLANNING,
        question="What does this step wait on, who runs it, what is spent, what did it produce?",
        needs="one step of the Campaign plan",
        escape=Escape(route="campaign"),
        sub_surface_of="campaign",
        step_leaf="Step",
        overlay_backed=True,
        doors=(Door(kind=DoorKind.ROUTE_KEY, key="Enter", origin="campaign"),),
        read_model=_RM.CAMPAIGN_PLAN_STEP,
        hole="the design pack draws the step card only inside a journey, never at a size",
    ),
    RouteSpec(
        id="campaign.artifact",
        group=_G.PLANNING,
        question="What did this step write, and what does it say?",
        needs="one artifact rendered as text with its digest",
        escape=Escape(route="campaign"),
        sub_surface_of="campaign",
        step_leaf="Artifact",
        overlay_backed=True,
        doors=(
            Door(kind=DoorKind.ROUTE_KEY, key="Enter", origin="campaign"),
            Door(kind=DoorKind.ROUTE_KEY, key="Enter", origin="campaign.step"),
        ),
        read_model=_RM.ARTIFACT_CARD_VIEW,
        hole="the design pack draws no artifact card frame",
    ),
)

REGISTRY = RouteRegistry(ROUTES)

#: The entry layer's pre-session states, in the packet's order. Each exits by its own
#: key rather than climbing an Escape parent, and each is drawn by the ``entry`` route.
ENTRY_STATES: tuple[EntryStateSpec, ...] = (
    EntryStateSpec(
        id="resolving",
        key="resolving",
        label="Resolving",
        question="Which workspace is this directory, and what has been read so far?",
    ),
    EntryStateSpec(
        id="ambiguous",
        key="resolution.ambiguous",
        label="Not attached",
        question="Which registered candidate is this session for?",
    ),
    EntryStateSpec(
        id="failed",
        key="resolution.failed",
        label="Not attached",
        question="Why can nothing attach here, and which command changes that?",
    ),
    EntryStateSpec(
        id="migration",
        key="migration.required",
        label="Migration required",
        question="Which migration path runs first, and what never happens by itself?",
    ),
    EntryStateSpec(
        id="interrupted",
        key="migration.interrupted",
        label="Migration required",
        question="Which generation is authoritative now, and is the journal resumed or discarded?",
    ),
    EntryStateSpec(
        id="schema",
        key="schema.unsupported",
        label="Schema unsupported",
        question="Which console version wrote this workspace, and what read-only export exists?",
    ),
    EntryStateSpec(
        id="offline",
        key="offline.snapshot",
        label="Offline snapshot",
        question="What did the last snapshot say, and how old is it?",
    ),
    EntryStateSpec(
        id="onboarding",
        key="onboarding",
        label="First run",
        question="What does a session need before one can exist?",
    ),
)

#: The chrome id of every entry state, in order: what a chrome's entry table must hold.
ENTRY_STATE_IDS: tuple[str, ...] = tuple(state.id for state in ENTRY_STATES)
