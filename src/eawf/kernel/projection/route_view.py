"""The route read model every non-spine family draws: rows, counts and silent columns.

:mod:`~eawf.kernel.projection.spine` states what the five spine routes draw. The
verification and operations families need the same three things -- the rows a projection
carried, the counts derived from them, and the columns no producer states -- so the shape
is declared once here and each family module declares only its own field table.

Two rules carry over from the spine and one is added. A field is rendered from the read
model or it is not rendered at all: a column whose producer has not shipped is declared
with ``produced=False`` and comes back as a
:class:`~eawf.kernel.projection.truth.TruthField` in the ``unknown`` state naming why. A
count is derived from the rows the projection carries, per collection the route binds, so
a count is either taken or absent. The addition is that an unproduced column may name the
work item whose producer is missing, because "no producer yet" and "the sandbox-decision
producer has not shipped" are different answers to an operator asking why a cell is blank.

Nothing here reads a document or a lock. The input is one already-validated
:class:`~eawf.kernel.projection.compute.RouteProjection`, so a view is a pure function of
the projection the daemon served.
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
from eawf.kernel.projection.spine import STATUS_FIELD, UNPRODUCED_REASON
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

#: What a truth cell names when the reason a column is silent is a named work item rather
#: than the generic absence of an epoch-2 producer.
MISSING_PRODUCER_REASON: Final = "no producer states this field yet: {item}"


@dataclass(frozen=True, slots=True, kw_only=True)
class RouteFieldSpec:
    """One field a route renders per row.

    Attributes:
        name: The field's console name, which is also its key in a row's fields.
        produced: Whether a producer states the field today. A field declared ``False``
            renders unknown, which is a statement, where leaving it out of the table
            would be silence.
        missing_producer: The work item whose producer would state the field, when one is
            named. ``None`` falls back to the generic unproduced reason.
    """

    name: str
    produced: bool = False
    missing_producer: str | None = None

    def missing_reason(self) -> str:
        """Return why this column is silent, naming the missing producer when one is known."""
        if self.missing_producer is None:
            return UNPRODUCED_REASON
        return MISSING_PRODUCER_REASON.format(item=self.missing_producer)


def status_and(*specs: RouteFieldSpec) -> tuple[RouteFieldSpec, ...]:
    """Return the stored status field followed by ``specs``, in column order."""
    return (RouteFieldSpec(name=STATUS_FIELD, produced=True), *specs)


def unstated(name: str, *, missing_producer: str | None = None) -> RouteFieldSpec:
    """Return one declared column no producer states, optionally naming the missing one."""
    return RouteFieldSpec(name=name, missing_producer=missing_producer)


@dataclass(frozen=True, slots=True, kw_only=True)
class RouteRecord:
    """One record a route renders, with every declared field stated.

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
class RouteReadModel:
    """One route's read model at one cursor, as the console draws it.

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
        specs: What the route renders per row, in column order.
    """

    route: str
    read_model: ReadModelKind
    scope_id: str
    source_cursor: str
    digest: str
    complete: bool
    rows: tuple[RouteRecord, ...]
    counts: Mapping[str, int]
    specs: tuple[RouteFieldSpec, ...]

    def count(self, name: str) -> int | None:
        """Return the derived count of ``name``, or ``None`` when no register holds it."""
        return self.counts.get(name)

    def index_of(self, selected_id: str | None) -> int | None:
        """Return the row position ``selected_id`` names now, or ``None`` when it is gone."""
        if selected_id is None:
            return None
        for index, row in enumerate(self.rows):
            if row.key == selected_id:
                return index
        return None

    def field_names(self) -> tuple[str, ...]:
        """Return the fields this route renders, in column order."""
        return tuple(spec.name for spec in self.specs)

    def unproduced(self) -> tuple[RouteFieldSpec, ...]:
        """Return the declared columns no producer states yet, in column order."""
        return tuple(spec for spec in self.specs if not spec.produced)


def check_field_tables(
    *, family: str, routes: tuple[str, ...], fields: Mapping[str, tuple[RouteFieldSpec, ...]]
) -> None:
    """Refuse a family table the console could not render.

    Args:
        family: The family name the refusal names, so one message says which table failed.
        routes: The routes the family states a read model for.
        fields: What each of those routes renders, in column order.

    Raises:
        ValueError: A route binds no collection, so no projection serves it; a route
            declares no field; a table does not lead with the stored status; a table names
            one field twice; or a table names a route the family does not hold. Raised at
            import, because a route that cannot be rendered is a startup failure rather
            than a frame that draws the wrong thing.
    """
    defects: list[str] = []
    for route in routes:
        if route not in ROUTE_COLLECTIONS:
            defects.append(f"route {route!r} binds no collection, so no projection serves it")
        specs = fields.get(route, ())
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
    extra = sorted(set(fields) - set(routes))
    defects += [f"route {route!r} declares fields but is not a {family} route" for route in extra]
    if defects:
        raise ValueError(f"{family} declarations are invalid: {'; '.join(defects)}")


def unknown_field(*, urn: str, revision: int, reason: str) -> TruthField[str]:
    """Return the truth field a declared but unstated cell renders as.

    Args:
        urn: The record the cell would have described, carried as its provenance.
        revision: The record's revision when the projection was built.
        reason: Why the cell states no value; an operator reads this beside the token.

    Returns:
        A derived truth field in the ``unknown`` state.
    """
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
        missing_reason=reason,
    )


def known_field(*, value: str, urn: str, revision: int) -> TruthField[str]:
    """Return the truth field a stated cell renders as.

    Args:
        value: The stated value; a blank is refused by the field's own validation.
        urn: The record the value rests on.
        revision: The producer revision the value was read at.

    Returns:
        A derived truth field in the ``known`` state.
    """
    return TruthField[str](
        value=value,
        state=TruthState.KNOWN,
        truth_kind=TruthKind.DERIVED,
        producer=PROJECTION_PRODUCER,
        producer_revision=revision,
        precision=Precision.EXACT,
        measurement_quality=MeasurementQuality.EXACT,
        freshness=Freshness.LIVE,
        provenance_refs=(urn,),
    )


def build_route_read_model(
    projection: RouteProjection,
    *,
    family: str,
    fields: Mapping[str, tuple[RouteFieldSpec, ...]],
) -> RouteReadModel:
    """Return the read model one route draws from one served projection.

    Args:
        projection: The route projection the daemon answered, already validated.
        family: The family name a refusal names.
        fields: The family's field table, keyed by route.

    Returns:
        The route's rows with every declared field stated, and the counts derived from
        those rows -- one per collection the route binds, including a bound collection
        that holds nothing, which counts zero because the count was taken.

    Raises:
        ValueError: The projection is for a route this family states no read model for. A
            frame drawn from another route's rows would be showing one route's records
            under another's columns.
    """
    route = projection.route
    specs = fields.get(route)
    if specs is None:
        stated = ", ".join(sorted(fields))
        raise ValueError(
            f"route {route!r} has no {family} read model, so its projection states no "
            f"{family} rows; {family} routes: {stated}"
        )
    rows = tuple(
        RouteRecord(
            key=row.key,
            urn=row.urn,
            collection=row.collection,
            revision=row.revision,
            fields=MappingProxyType(
                {
                    spec.name: (
                        row.status
                        if spec.produced
                        else unknown_field(
                            urn=row.urn, revision=row.revision, reason=spec.missing_reason()
                        )
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
    logger.debug(f"build_route_read_model family={family} route={route} rows={len(rows)}")
    return RouteReadModel(
        route=route,
        read_model=ROUTE_READ_MODELS[route],
        scope_id=projection.header.scope_id,
        source_cursor=projection.header.source_cursor,
        digest=projection.digest,
        complete=projection.header.completeness is Completeness.COMPLETE,
        rows=rows,
        counts=counts,
        specs=specs,
    )


__all__ = [
    "MISSING_PRODUCER_REASON",
    "RouteFieldSpec",
    "RouteReadModel",
    "RouteRecord",
    "build_route_read_model",
    "check_field_tables",
    "known_field",
    "status_and",
    "unknown_field",
    "unstated",
]
