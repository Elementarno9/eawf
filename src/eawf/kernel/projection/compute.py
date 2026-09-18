"""Route read models and keyed patches, computed where the order is allocated.

The daemon is the only writer of the epoch-2 document and the only allocator of
``canonical_sequence``, so it is the only place a read model can be built at a cursor
that means anything. Building one here rather than once per console is what lets the
terminal, the plain-text parity path and an export render the same rows at the same
cursor, and compare on the one digest a projection carries.

Two shapes come out of this module. :func:`build_route_projection` answers a whole
route at one cursor, headed by a :class:`~eawf.kernel.projection.truth.ProjectionHeader`
whose ``source_cursor`` is that cursor. :func:`patches_for_event` turns one committed
transition into the keyed patches the routes it touches need, and does it without
reading the document again: the committed event already carries the row's new status,
its new revision and the ordinal the commit allocated, so a patch costs no lock and
cannot disagree with the cursor it names.

:data:`ROUTE_COLLECTIONS` is the only place that says which document collections a
console route renders. A route it does not name has no document binding, and reading
that route is refused rather than answered with an empty projection a console would
draw as zero rows.

The digest covers the route, the cursor and the rows, and deliberately not the
generation stamp: two reads of one route at one cursor must digest alike, or the
digest cannot be what two surfaces agree through.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Annotated, Any, Final, Literal

from pydantic import ConfigDict, Field

from eawf.kernel.identity import IdentityError, QualifiedUrn, parse_qualified_urn
from eawf.kernel.projection.read_models import READ_MODEL_BY_KIND, ReadModelKind
from eawf.kernel.projection.truth import (
    Completeness,
    ConnectionState,
    Freshness,
    Precision,
    ProjectionHeader,
    TruthField,
    TruthKind,
    TruthState,
)
from eawf.kernel.spec.release import Sha256DigestStr
from eawf.kernel.state.enums import MeasurementQuality
from eawf.kernel.state.epoch2.base import Epoch2Model, NonEmptyStr, StrictPositiveInt
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.tiers import ENTITY_COLLECTIONS, Epoch2Collection

logger = logging.getLogger(__name__)


#: The schema of the read and patch shapes this module emits.
PROJECTION_SCHEMA_VERSION: Final = "1.0"

#: What a projection names as the producer of its rows. It is the module, not the
#: process, because two daemons reading one document owe the same answer.
PROJECTION_PRODUCER: Final = "eawf.kernel.projection"

#: The policy revision a projection is built under. Nothing is filtered out of a
#: projection yet, so every one is built under the first revision; the header states
#: it anyway, because a console must be able to tell two filterings apart.
PROJECTION_POLICY_REVISION: Final = 1

#: Why a row's status reads as unknown. A console renders this instead of a blank,
#: which would be indistinguishable from a record that really has no status.
MISSING_STATUS_REASON: Final = "the stored row states no status"

#: The transition-event payload field carrying the ordinal the commit allocated.
#: Spelled the same as the document's high-water-mark key and separate from it: one
#: is what a single event was stamped with, the other is what the tree has reached.
CANONICAL_SEQUENCE_FIELD: Final = "canonical_sequence"


class _ProjectionViewModel(Epoch2Model):
    """Strict and immutable, like every other projection shape."""

    model_config = ConfigDict(extra="forbid", frozen=True)


#: The corpus the diagnostics routes read across: every register another console route
#: renders. A history entry is about one of these records and a search hits one of them,
#: so stating the set once keeps the diagnostics routes over the corpus the rest of the
#: console draws instead of each inventing a set of its own.
DIAGNOSTICS_CORPUS: Final[tuple[Epoch2Collection, ...]] = (
    Epoch2Collection.TRACK,
    Epoch2Collection.CAMPAIGN,
    Epoch2Collection.MILESTONE,
    Epoch2Collection.BATCH,
    Epoch2Collection.TASK,
    Epoch2Collection.RUN,
    Epoch2Collection.ARTIFACT,
)


#: Which epoch-2 collections each console route renders, in render order. A route
#: absent from this table has no document binding yet, so its read is refused; an
#: empty projection would be indistinguishable from a scope that holds nothing.
#:
#: A register whose producer has not shipped is still bound, because a register that
#: was read and held nothing counts zero honestly; what a route may not do is claim a
#: collection it does not render.
ROUTE_COLLECTIONS: Final[Mapping[str, tuple[Epoch2Collection, ...]]] = MappingProxyType(
    {
        "activity": (Epoch2Collection.RUN,),
        "attention": (Epoch2Collection.PENDING_ACTION,),
        "backlog": (Epoch2Collection.TASK,),
        "batch.detail": (Epoch2Collection.BATCH,),
        "campaign": (Epoch2Collection.CAMPAIGN,),
        # a step and an artifact card are sub-surfaces of the campaign: the record each
        # addresses is the campaign row and the artifact row the campaign produced
        "campaign.artifact": (Epoch2Collection.ARTIFACT,),
        "campaign.step": (Epoch2Collection.CAMPAIGN,),
        "cost.ceiling": (Epoch2Collection.RUN,),
        "crash.recovery": (Epoch2Collection.RUN,),
        "evidence": (Epoch2Collection.CLAIM, Epoch2Collection.EVIDENCE),
        "evidence.digest": (Epoch2Collection.EVIDENCE,),
        # a report of a Run is taken over that Run's own record, so the export card
        # renders the register the Run surface it was opened from renders
        "export": (Epoch2Collection.RUN,),
        # the delivery a Batch received is a Batch fact: a generation and the
        # conflict that blocked one are both filed against the Batch they moved
        "git.pr": (Epoch2Collection.BATCH,),
        "health": (Epoch2Collection.HEALTH_VIEW,),
        "history": DIAGNOSTICS_CORPUS,
        "history.diff": DIAGNOSTICS_CORPUS,
        "merge.conflict": (Epoch2Collection.BATCH,),
        # the Milestone frame draws the Milestone and the Batches cut under it, which
        # is the membership an acceptance bundle is sealed over
        "milestone": (Epoch2Collection.MILESTONE, Epoch2Collection.BATCH),
        "notifications": (Epoch2Collection.RUN,),
        "receipt": (Epoch2Collection.RECEIPT,),
        # a candidate's membership is the Milestones it carries, so the Release frame
        # renders the release register beside them
        "release": (Epoch2Collection.RELEASE, Epoch2Collection.MILESTONE),
        "roadmap": (Epoch2Collection.MILESTONE, Epoch2Collection.BATCH),
        "run.detail": (Epoch2Collection.RUN,),
        "sandbox.log": (Epoch2Collection.SANDBOX_POLICY,),
        "scope.home": (Epoch2Collection.TRACK, Epoch2Collection.MILESTONE),
        "search": DIAGNOSTICS_CORPUS,
        "task.detail": (Epoch2Collection.TASK,),
        "track": (Epoch2Collection.TRACK,),
        "transcript": (Epoch2Collection.RUN,),
        "trust": (Epoch2Collection.CLAIM,),
        "unattended": (Epoch2Collection.RUN,),
    }
)


def _compile_route_read_models(
    bindings: Mapping[str, tuple[Epoch2Collection, ...]],
) -> Mapping[str, ReadModelKind]:
    """Return the read model each bound route renders.

    Args:
        bindings: The route-to-collection table to check and index.

    Returns:
        A read-only mapping from each bound route to its declared read model.

    Raises:
        ValueError: A bound route is not declared by any read model, the read model
            that declares it carries no projection, or a route binds no collection
            at all. Raised at import so the mismatch is a startup failure rather
            than a route that answers with the wrong shape.
    """
    declared = {route: spec.kind for spec in READ_MODEL_BY_KIND.values() for route in spec.routes}
    defects: list[str] = []
    for route, collections in sorted(bindings.items()):
        kind = declared.get(route)
        if kind is None:
            defects.append(f"route {route!r} is bound to collections but no read model names it")
            continue
        if not READ_MODEL_BY_KIND[kind].projection_backed:
            defects.append(f"route {route!r} renders {kind}, which no projection carries")
        if not collections:
            defects.append(f"route {route!r} binds no collection, so it has nothing to render")
    if defects:
        raise ValueError(f"route bindings are invalid: {'; '.join(defects)}")
    return MappingProxyType({route: declared[route] for route in bindings})


#: The read model each bound route renders, derived from the declarations so the
#: binding and the table cannot name different models for one route.
ROUTE_READ_MODELS: Final[Mapping[str, ReadModelKind]] = _compile_route_read_models(
    ROUTE_COLLECTIONS
)


class ProjectionRow(_ProjectionViewModel):
    """One record a route renders, as of the cursor its projection was built at.

    Attributes:
        key: The record's public key, which is also the patch key it is updated by.
        urn: The record's canonical address.
        collection: The document collection the row was read from.
        revision: The record's compare-and-swap token when it was read.
        status: The record's lifecycle status as a truth field, so a row that states
            none renders as unknown rather than as a blank.
    """

    key: NonEmptyStr
    urn: NonEmptyStr
    collection: Epoch2Collection
    revision: StrictPositiveInt
    status: TruthField[str]


class RouteProjection(_ProjectionViewModel):
    """One console route's whole read model at one cursor.

    Attributes:
        schema_version: The projection schema this shape is spelled in.
        route: The console route key the rows were gathered for.
        header: The projection header, whose ``source_cursor`` is the committed
            ``canonical_sequence`` the rows were read through.
        digest: The digest two surfaces compare on. It covers the route, the cursor
            and the rows, so one cursor yields one digest however often it is read.
        rows: The rendered records, by collection in render order and by key within
            each collection.
    """

    schema_version: Literal["1.0"]
    route: NonEmptyStr
    header: ProjectionHeader
    digest: Sha256DigestStr
    rows: tuple[ProjectionRow, ...]


class PatchEntry(_ProjectionViewModel):
    """One record a keyed patch updates.

    A transition never removes a row, so an entry is always a replacement and
    carries no operation of its own.

    Attributes:
        key: The key the receiving projection replaces under.
        urn: The record's canonical address.
        collection: The document collection the record lives in.
        revision: The record's compare-and-swap token after the move.
        status: The record's lifecycle status after the move.
    """

    key: NonEmptyStr
    urn: NonEmptyStr
    collection: Epoch2Collection
    revision: StrictPositiveInt
    status: NonEmptyStr


class KeyedPatch(_ProjectionViewModel):
    """What one committed transition changes in one read model.

    Attributes:
        schema_version: The projection schema this shape is spelled in.
        projection_kind: The read model the patch applies to.
        routes: The routes of that read model the patch reaches, at least one.
        scope_id: The scope the patch was published for.
        canonical_sequence: The workspace-global ordinal the commit allocated. A
            subscriber reads a contiguous run of these, because the transaction
            allocates them contiguously and the bus preserves publish order.
        entries: The records the patch updates, at least one.
    """

    schema_version: Literal["1.0"]
    projection_kind: ReadModelKind
    routes: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]
    scope_id: NonEmptyStr
    canonical_sequence: StrictPositiveInt
    entries: Annotated[tuple[PatchEntry, ...], Field(min_length=1)]


def build_route_projection(
    *,
    route: str,
    document: dict[str, Any],
    cursor: int,
    scope_id: str,
    generated_at: datetime,
) -> RouteProjection:
    """Return one route's read model, read through *cursor*.

    Args:
        route: The console route key to render.
        document: The epoch-2 document as the daemon read it.
        cursor: The committed ``canonical_sequence`` the document stands at; ``0``
            for a workspace that has committed nothing.
        scope_id: The scope the projection is built for.
        generated_at: When the projection was generated. Supplied by the caller so
            the header's two stamps agree.

    Returns:
        The route's rows under a header whose ``source_cursor`` is *cursor*.

    Raises:
        ValueError: The route binds no collection, the cursor is negative, or the
            document holds a row the projection cannot render.
    """
    collections = ROUTE_COLLECTIONS.get(route)
    if collections is None:
        bound = ", ".join(sorted(ROUTE_COLLECTIONS))
        raise ValueError(
            f"route {route!r} renders no epoch-2 collection, so it carries no "
            f"projection; bound routes: {bound}"
        )
    if cursor < 0:
        raise ValueError(f"a projection cursor is a committed canonical_sequence, never {cursor}")
    rows = tuple(
        _projection_row(key=key, row=row, collection=collection)
        for collection in collections
        for key, row in sorted(document_rows(document, collection).items())
    )
    return RouteProjection(
        schema_version=PROJECTION_SCHEMA_VERSION,
        route=route,
        header=ProjectionHeader(
            schema_version=PROJECTION_SCHEMA_VERSION,
            projection_kind=ROUTE_READ_MODELS[route],
            scope_id=scope_id,
            # A projection read through a later cursor is a later revision of it.
            # The offset keeps the first projection of an empty workspace at one,
            # which is the lowest revision the header admits.
            projection_revision=cursor + 1,
            source_cursor=str(cursor),
            generated_at=generated_at,
            observed_at=generated_at,
            connection_state=ConnectionState.LIVE,
            completeness=Completeness.COMPLETE,
            freshness=Freshness.LIVE,
            producer_refs=(PROJECTION_PRODUCER,),
            policy_revision=PROJECTION_POLICY_REVISION,
        ),
        digest=_digest(route=route, cursor=cursor, rows=rows),
        rows=rows,
    )


def patches_for_event(envelope: Envelope) -> tuple[KeyedPatch, ...]:
    """Return the keyed patches one committed transition produces.

    Args:
        envelope: A published envelope, of any kind. The bus carries every kind, so
            this is asked of envelopes that are not transitions at all.

    Returns:
        One patch per read model whose routes render the moved record's collection,
        routes sorted and read models in their own order. Empty when the envelope
        carries no committed ordinal, or when no route renders that collection.

    Raises:
        ValueError: The envelope carries an ordinal that is not one, or carries one
            and then states no record it moved, so no patch a console could trust
            can be built from it.
    """
    payload = envelope.payload
    if CANONICAL_SEQUENCE_FIELD not in payload:
        return ()
    sequence = payload[CANONICAL_SEQUENCE_FIELD]
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise ValueError(
            f"event {envelope.id!r} states a {CANONICAL_SEQUENCE_FIELD} that is not a "
            "committed ordinal, so a patch carrying it could not be ordered"
        )
    urn, status, revision = _moved_record(envelope)
    collection = ENTITY_COLLECTIONS.get(urn.kind)
    if collection is None:
        logger.debug(f"patches_for_event unstored event={envelope.id} kind={urn.kind.value}")
        return ()
    routes = sorted(route for route, bound in ROUTE_COLLECTIONS.items() if collection in bound)
    if not routes:
        logger.debug(f"patches_for_event unrendered event={envelope.id} kind={urn.kind.value}")
        return ()
    entry = PatchEntry(
        key=urn.entity_key,
        urn=str(urn),
        collection=collection,
        revision=revision,
        status=status,
    )
    by_read_model: dict[ReadModelKind, list[str]] = {}
    for route in routes:
        by_read_model.setdefault(ROUTE_READ_MODELS[route], []).append(route)
    return tuple(
        KeyedPatch(
            schema_version=PROJECTION_SCHEMA_VERSION,
            projection_kind=kind,
            routes=tuple(kind_routes),
            scope_id=envelope.scope_id or str(urn),
            canonical_sequence=sequence,
            entries=(entry,),
        )
        for kind, kind_routes in sorted(by_read_model.items())
    )


def _moved_record(envelope: Envelope) -> tuple[QualifiedUrn, str, int]:
    """Return the address, new status and new revision of the record one event moved.

    Raises:
        ValueError: The event carries a committed ordinal but states no address, no
            status or no revision to patch the record to, or states an address that
            is not a qualified URN.
    """
    payload = envelope.payload
    entity_ref = payload.get("entity_ref")
    status = payload.get("to_status")
    revision = payload.get("revision_after")
    if not isinstance(entity_ref, str) or not entity_ref.strip():
        raise ValueError(
            f"event {envelope.id!r} carries a {CANONICAL_SEQUENCE_FIELD} but names no "
            "entity_ref, so nothing says which record it moved"
        )
    if not isinstance(status, str) or not status.strip():
        raise ValueError(f"event {envelope.id!r} moves {entity_ref} but states no to_status")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ValueError(
            f"event {envelope.id!r} moves {entity_ref} but states no positive revision_after"
        )
    try:
        return parse_qualified_urn(entity_ref), status, revision
    except IdentityError as error:
        raise ValueError(
            f"event {envelope.id!r} names an entity_ref that is not a qualified URN"
        ) from error


def _projection_row(*, key: str, row: Any, collection: Epoch2Collection) -> ProjectionRow:
    """Return one stored row as the record a route renders.

    Identity and revision are checked here rather than left to the model, so the
    refusal names the field and never the value: a daemon log of it then carries
    nothing the document held. Only the status may be absent, because a console can
    render an unknown status honestly but cannot render a row it cannot address.

    Raises:
        ValueError: The row is keyed by a blank, is not an object, or states no URN
            or no positive revision.
    """
    if not key.strip():
        raise ValueError(f"a {collection.value} row is keyed by a blank, so nothing selects it")
    if not isinstance(row, dict):
        raise ValueError(f"{collection.value} row {key!r} is a {type(row).__name__}, not an object")
    urn, revision = row.get("urn"), row.get("revision")
    if not isinstance(urn, str) or not urn.strip():
        raise ValueError(f"{collection.value} row {key!r} states no urn, so nothing addresses it")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ValueError(f"{collection.value} row {key!r} states no positive revision")
    return ProjectionRow(
        key=key,
        urn=urn,
        collection=collection,
        revision=revision,
        status=_status_field(status=row.get("status"), urn=urn, revision=revision),
    )


def _status_field(*, status: Any, urn: str, revision: int) -> TruthField[str]:
    """Return a row's status as a truth field, known or honestly missing."""
    stated = isinstance(status, str) and bool(status.strip())
    return TruthField[str](
        value=status if stated else None,
        state=TruthState.KNOWN if stated else TruthState.UNKNOWN,
        truth_kind=TruthKind.STORED,
        producer=PROJECTION_PRODUCER,
        producer_revision=revision,
        precision=Precision.EXACT if stated else Precision.UNAVAILABLE,
        measurement_quality=(
            MeasurementQuality.EXACT if stated else MeasurementQuality.UNAVAILABLE
        ),
        freshness=Freshness.LIVE,
        provenance_refs=(urn,),
        missing_reason=None if stated else MISSING_STATUS_REASON,
    )


def _digest(*, route: str, cursor: int, rows: tuple[ProjectionRow, ...]) -> str:
    """Return the digest of one route's rows at one cursor.

    The generation stamp is left out on purpose: a digest that moved with the clock
    could not be what two surfaces compare through.
    """
    encoded = json.dumps(
        {
            "route": route,
            "source_cursor": cursor,
            "rows": [row.model_dump(mode="json") for row in rows],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


__all__ = [
    "CANONICAL_SEQUENCE_FIELD",
    "DIAGNOSTICS_CORPUS",
    "MISSING_STATUS_REASON",
    "PROJECTION_POLICY_REVISION",
    "PROJECTION_PRODUCER",
    "PROJECTION_SCHEMA_VERSION",
    "ROUTE_COLLECTIONS",
    "ROUTE_READ_MODELS",
    "KeyedPatch",
    "PatchEntry",
    "ProjectionRow",
    "RouteProjection",
    "build_route_projection",
    "patches_for_event",
]
