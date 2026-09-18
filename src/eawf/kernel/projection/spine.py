"""The spine read models: the rows and the derived counts each spine route renders.

:mod:`~eawf.kernel.projection.compute` answers a route at a cursor; it does not say what
any one route draws with the answer. This module does, for the five projection-backed
spine routes, and it is deliberately the only place a spine field is declared.

Two rules shape it. A field is rendered from the read model or it is not rendered at all:
a spine field whose producer has not shipped is declared with ``produced=False`` and comes
back as a :class:`~eawf.kernel.projection.truth.TruthField` in the ``unknown`` state naming
why, never as a blank a console would draw as an empty cell. And a count is derived here
from the rows the projection carries, per collection the route binds, so a count is either
taken from the read model or absent -- a count the read model has no register for is
missing from the map and the console renders it unavailable rather than as a zero.

Nothing here reads a document or a lock. The input is one already-validated
:class:`~eawf.kernel.projection.compute.RouteProjection`, so a view is a pure function of
the projection the daemon served, which is what lets the terminal, the plain-text parity
path and an export draw the same rows at one cursor.

The entry layer is absent on purpose. It renders
:attr:`~eawf.kernel.projection.read_models.ReadModelKind.PROCESS_FRAME`, the one read model
no projection carries, because there is no session to project before one exists.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from eawf.kernel.projection.compute import (
    PROJECTION_PRODUCER,
    ROUTE_COLLECTIONS,
    ROUTE_READ_MODELS,
    RouteProjection,
)
from eawf.kernel.projection.read_models import ReadModelKind
from eawf.kernel.projection.truth import (
    Completeness,
    Freshness,
    Precision,
    TruthField,
    TruthKind,
    TruthState,
)
from eawf.kernel.state.enums import MeasurementQuality
from eawf.kernel.store.tiers import Epoch2Collection

logger = logging.getLogger(__name__)


#: The console routes this module states a read model for. Every one is bound by
#: :data:`~eawf.kernel.projection.compute.ROUTE_COLLECTIONS`, so every one is served by
#: ``projection.<route>.read`` and by ``projection.<route>.reconnect``.
SPINE_ROUTES: Final[tuple[str, ...]] = (
    "scope.home",
    "track",
    "batch.detail",
    "task.detail",
    "run.detail",
)

#: The route the spine opens on before a session exists. It carries no projection, so it
#: is named here to be excluded rather than left to look like an oversight.
ENTRY_ROUTE: Final = "entry"

#: The field every spine row states from the document, and the only one that does.
STATUS_FIELD: Final = "status"

#: Why a declared field comes back unknown. The console prints the truth token beside it;
#: the reason is what an operator reads when asking why the cell is not a value.
UNPRODUCED_REASON: Final = "no epoch-2 producer states this field yet"


@dataclass(frozen=True, slots=True, kw_only=True)
class SpineFieldSpec:
    """One field a spine route renders per row.

    Attributes:
        name: The field's console name, which is also its key in a row's fields.
        produced: Whether an epoch-2 producer states the field today. A field declared
            ``False`` renders unknown, which is a statement, where leaving it out of the
            table would be silence.
    """

    name: str
    produced: bool = False


def _fields(*names: str) -> tuple[SpineFieldSpec, ...]:
    """Return the status field followed by one unproduced field per name."""
    return (
        SpineFieldSpec(name=STATUS_FIELD, produced=True),
        *(SpineFieldSpec(name=name) for name in names),
    )


#: What each spine route renders per row, in column order. The first field of every route
#: is the status the document states; the rest are the design's columns whose producers
#: are dev4 items, declared so the console draws the unknown token in their place.
SPINE_FIELDS: Final[Mapping[str, tuple[SpineFieldSpec, ...]]] = MappingProxyType(
    {
        "scope.home": _fields("runs", "attention", "progress"),
        "track": _fields("batches", "due"),
        "batch.detail": _fields("checks", "runs"),
        "task.detail": _fields("criteria", "candidates"),
        "run.detail": _fields("provider", "elapsed", "cost"),
    }
)


def _check_declarations() -> None:
    """Refuse a spine table the console could not render.

    Raises:
        ValueError: A spine route binds no collection, so no projection serves it; a
            spine route declares no fields; a field table does not lead with the stored
            status; or a table names one field twice. Raised at import, because a spine
            route that cannot be rendered is a startup failure rather than a frame that
            draws the wrong thing.
    """
    defects: list[str] = []
    for route in SPINE_ROUTES:
        if route not in ROUTE_COLLECTIONS:
            defects.append(f"route {route!r} binds no collection, so no projection serves it")
        specs = SPINE_FIELDS.get(route, ())
        if not specs:
            defects.append(f"route {route!r} declares no field to render")
            continue
        if specs[0].name != STATUS_FIELD or not specs[0].produced:
            defects.append(f"route {route!r} does not lead with the stored {STATUS_FIELD}")
        names = [spec.name for spec in specs]
        defects += [
            f"route {route!r} declares {name!r} twice"
            for name in sorted(set(names))
            if names.count(name) > 1
        ]
    extra = sorted(set(SPINE_FIELDS) - set(SPINE_ROUTES))
    defects += [f"route {route!r} declares fields but is not a spine route" for route in extra]
    if ENTRY_ROUTE in SPINE_FIELDS:
        defects.append(f"route {ENTRY_ROUTE!r} carries no projection, so it states no fields")
    if defects:
        raise ValueError(f"spine declarations are invalid: {'; '.join(defects)}")


_check_declarations()


@dataclass(frozen=True, slots=True, kw_only=True)
class SpineRow:
    """One record a spine route renders, with every declared field stated.

    Attributes:
        key: The record's public key, which is also the stable id a selection restores by.
        urn: The record's canonical address.
        collection: The document collection the record was read from.
        revision: The record's compare-and-swap token when the projection was built.
        fields: Every field the route declares, by name, each a truth field.
    """

    key: str
    urn: str
    collection: Epoch2Collection
    revision: int
    fields: Mapping[str, TruthField[str]]

    def field(self, name: str) -> TruthField[str]:
        """Return the named field.

        Raises:
            KeyError: The route does not declare ``name``, so the row never stated it.
        """
        return self.fields[name]


@dataclass(frozen=True, slots=True, kw_only=True)
class SpineView:
    """One spine route's read model, as the console draws it.

    Attributes:
        route: The console route key the rows were gathered for.
        read_model: The declared read model the route renders.
        scope_id: The scope the projection was built for.
        source_cursor: The committed ``canonical_sequence`` the rows were read through,
            carried verbatim from the projection header.
        digest: The projection's digest, which one cursor yields once.
        complete: Whether the projection claimed every row of its scope, and so whether a
            count taken from it may be called complete.
        rows: The records, in the projection's own order.
        counts: The rows per collection the route binds, keyed by collection name. A
            collection the route does not bind has no entry, which is how a count with no
            register is told apart from a register that holds nothing.
    """

    route: str
    read_model: ReadModelKind
    scope_id: str
    source_cursor: str
    digest: str
    complete: bool
    rows: tuple[SpineRow, ...]
    counts: Mapping[str, int]

    def count(self, name: str) -> int | None:
        """Return the derived count of ``name``, or ``None`` when no register holds it."""
        return self.counts.get(name)

    def index_of(self, selected_id: str | None) -> int | None:
        """Return the row position ``selected_id`` names now, or ``None`` when it is gone.

        This is what restores a selection after a keyed patch reorders the rows: the
        console persists the stable id, never the offset, so an insert above the selection
        moves the cursor with the row rather than onto its neighbour.
        """
        if selected_id is None:
            return None
        for index, row in enumerate(self.rows):
            if row.key == selected_id:
                return index
        return None

    def field_names(self) -> tuple[str, ...]:
        """Return the fields this route renders, in column order."""
        return tuple(spec.name for spec in SPINE_FIELDS[self.route])

    def unproduced(self) -> tuple[str, ...]:
        """Return the declared fields no epoch-2 producer states yet, in column order."""
        return tuple(spec.name for spec in SPINE_FIELDS[self.route] if not spec.produced)


def _unknown_field(*, urn: str, revision: int) -> TruthField[str]:
    """Return the truth field a declared but unproduced column renders as."""
    return TruthField[str](
        value=None,
        state=TruthState.UNKNOWN,
        truth_kind=TruthKind.DERIVED,
        producer=PROJECTION_PRODUCER,
        producer_revision=revision,
        precision=Precision.UNAVAILABLE,
        measurement_quality=MeasurementQuality.UNAVAILABLE,
        freshness=Freshness.LIVE,
        provenance_refs=(urn,),
        missing_reason=UNPRODUCED_REASON,
    )


def build_spine_view(projection: RouteProjection) -> SpineView:
    """Return the read model a spine route draws from one served projection.

    Args:
        projection: The route projection the daemon answered, already validated.

    Returns:
        The route's rows with every declared field stated, and the counts derived from
        those rows -- one per collection the route binds, including a bound collection
        that holds nothing, which counts zero because the count was taken.

    Raises:
        ValueError: The projection is for a route this module states no read model for.
            A spine frame drawn from another route's rows would be showing one route's
            records under another's columns.
    """
    route = projection.route
    if route not in SPINE_FIELDS:
        stated = ", ".join(SPINE_ROUTES)
        raise ValueError(
            f"route {route!r} has no spine read model, so its projection states no "
            f"spine rows; spine routes: {stated}"
        )
    specs = SPINE_FIELDS[route]
    rows = tuple(
        SpineRow(
            key=row.key,
            urn=row.urn,
            collection=row.collection,
            revision=row.revision,
            fields=MappingProxyType(
                {
                    spec.name: (
                        row.status
                        if spec.produced
                        else _unknown_field(urn=row.urn, revision=row.revision)
                    )
                    for spec in specs
                }
            ),
        )
        for row in projection.rows
    )
    counts = MappingProxyType(
        {
            collection.value: sum(1 for row in rows if row.collection is collection)
            for collection in ROUTE_COLLECTIONS[route]
        }
    )
    logger.debug(f"build_spine_view route={route} cursor={projection.header.source_cursor}")
    return SpineView(
        route=route,
        read_model=ROUTE_READ_MODELS[route],
        scope_id=projection.header.scope_id,
        source_cursor=projection.header.source_cursor,
        digest=projection.digest,
        complete=projection.header.completeness is Completeness.COMPLETE,
        rows=rows,
        counts=counts,
    )


__all__ = [
    "ENTRY_ROUTE",
    "SPINE_FIELDS",
    "SPINE_ROUTES",
    "STATUS_FIELD",
    "UNPRODUCED_REASON",
    "SpineFieldSpec",
    "SpineRow",
    "SpineView",
    "build_spine_view",
]
