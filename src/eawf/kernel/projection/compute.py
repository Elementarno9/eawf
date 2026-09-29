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
from collections.abc import Mapping, Sequence
from datetime import datetime
from types import MappingProxyType
from typing import Annotated, Any, Final, Literal

from pydantic import ConfigDict, Field, ValidationError

from eawf.kernel.identity import IdentityError, QualifiedUrn, parse_qualified_urn
from eawf.kernel.migration.epoch2.continuation import entity_ref, read_legacy_row
from eawf.kernel.migration.epoch2.cutover import ROW_PAYLOAD_FIELD
from eawf.kernel.migration.epoch2.native_records import ImportedNativeRecord
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


#: The payload field that marks a row the cutover converted whole into a native
#: collection, as opposed to an imported lifecycle record that continues its lifecycle.
_NATIVE_IMPORT_KEY: Final = "record_key"

#: The one revision a converted native record stands at: the import wrote it once and no
#: lifecycle verb moves it.
_IMPORT_REVISION: Final = 1

#: The fields a record's title is read from, in preference order, where they are not
#: simply ``title``.
_TITLE_FIELDS: Final[Mapping[Epoch2Collection, tuple[str, ...]]] = MappingProxyType(
    {Epoch2Collection.TASK: ("title", "intent"), Epoch2Collection.PERMISSION: ("request_scope",)}
)

#: The stored field a pending action names the principal it is addressed to in, which is
#: also the transition-event field the move carries it in.
_ASSIGNEE_FIELD: Final = "assignee_ref"

#: The stored field a suspended Run names why it waits in.
_SUSPENSION_FIELD: Final = "suspension_reason"

#: Where each record names the record it is filed under, as a path into the stored row.
_PARENT_FIELD: Final[Mapping[Epoch2Collection, tuple[str, ...]]] = MappingProxyType(
    {
        Epoch2Collection.MILESTONE: ("primary_track_ref",),
        Epoch2Collection.BATCH: ("milestone_ref",),
        Epoch2Collection.TASK: ("batch_ref",),
        Epoch2Collection.RUN: ("scope", "task_ref"),
    }
)


#: The field a row spelled back for a replay carries its facts in. A stored document row
#: never states it, so a fresh read derives every fact again, while a replay -- whose
#: document holds only the rows the client already had -- keeps the facts it held rather
#: than losing the ones read off another collection.
FACTS_FIELD: Final = "projected_facts"

#: The instants a Run states about itself, read verbatim as the document stores them.
_RUN_INSTANTS: Final = ("created_at", "started_at", "ended_at", "updated_at")


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
        # a provider permission waits on a principal as a pending action does; its rows
        # are the open ones on the run ledger, which the daemon supplies beside the document
        "attention": (Epoch2Collection.PENDING_ACTION, Epoch2Collection.PERMISSION),
        "backlog": (Epoch2Collection.TASK,),
        # a Batch frame lists the Tasks filed under it, so it reads them beside the Batch
        "batch.detail": (Epoch2Collection.BATCH, Epoch2Collection.TASK),
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
        # the timeline draws one lane per Track and the release register beside them
        "roadmap": (
            Epoch2Collection.TRACK,
            Epoch2Collection.MILESTONE,
            Epoch2Collection.BATCH,
            Epoch2Collection.RELEASE,
        ),
        "run.detail": (Epoch2Collection.RUN,),
        "sandbox.log": (Epoch2Collection.SANDBOX_POLICY,),
        # home nests each Milestone under its Track and counts the Batches cut under
        # each Milestone, so its progress is read off rows it holds rather than guessed
        "scope.home": (Epoch2Collection.TRACK, Epoch2Collection.MILESTONE, Epoch2Collection.BATCH),
        "search": DIAGNOSTICS_CORPUS,
        # a Task frame lists the Runs of it, and a Track frame the Milestones filed under
        # it with the Batches cut under each, all found by their parent key
        "task.detail": (Epoch2Collection.TASK, Epoch2Collection.RUN),
        "track": (Epoch2Collection.TRACK, Epoch2Collection.MILESTONE, Epoch2Collection.BATCH),
        "transcript": (Epoch2Collection.RUN,),
        "trust": (Epoch2Collection.CLAIM,),
        "unattended": (Epoch2Collection.RUN,),
    }
)

#: The payload kind of a child Run admitted past a ``child_runs`` ceiling, as the run
#: ledger files it. It is a notice: it records an overrun nobody can answer.
CEILING_BREACH_KIND: Final = "child_ceiling_breach"

#: The collections a route lists notices from without binding the collection itself.
#: Attention lists a ceiling breach beside the calls waiting on a principal, but the
#: breach is a line on the run ledger, and listing the Runs it sits among would turn
#: the register of what needs a principal into a list of work.
ROUTE_NOTICE_COLLECTIONS: Final[Mapping[str, tuple[Epoch2Collection, ...]]] = MappingProxyType(
    {"attention": (Epoch2Collection.RUN,)}
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
        title: The record's own title as the document stores it; ``None`` when the
            record states none, which a frame draws as the key alone.
        parent_key: The key of the record this one is filed under -- a Milestone's
            Track, a Batch's Milestone, a Task's Batch, a Run's Task -- so a frame can
            nest rows without reading the document; ``None`` when none is stated.
        assignee_ref: The one principal a pending action is addressed to, as its
            record names it; ``None`` when it is addressed to every eligible principal
            or the record is not a pending action. Whose attention count a question
            belongs in is read from this, so it travels with the row.
        suspension_reason: Why a suspended Run waits, as its record names it; ``None``
            when the Run is not suspended, or its record names no reason.
        facts: The further facts the document states about the record, by name, each
            as stored: a Run's instants, purpose and failure, the title and
            status of the Task it runs and which attempt it is; an action's kind,
            question and subject; how many Runs a Track or Milestone holds. A
            fact the document does not state is absent, never blank, so a frame draws
            the unknown token for it.
    """

    key: NonEmptyStr
    urn: NonEmptyStr
    collection: Epoch2Collection
    revision: StrictPositiveInt
    status: TruthField[str]
    title: NonEmptyStr | None = None
    parent_key: NonEmptyStr | None = None
    assignee_ref: NonEmptyStr | None = None
    suspension_reason: NonEmptyStr | None = None
    facts: dict[str, NonEmptyStr] = Field(default_factory=dict)

    def __eq__(self, other: object) -> bool:
        """Return whether two rows are one record at one revision, whatever facts ride on them.

        The facts are read beside the record from other collections at build time, so a
        replay that learns of a record only through its keyed patch holds the record and
        not yet those facts; it is still the same record, and the same answer.
        """
        if not isinstance(other, ProjectionRow):
            return NotImplemented
        return self.model_dump(exclude={"facts"}) == other.model_dump(exclude={"facts"})

    __hash__ = _ProjectionViewModel.__hash__


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


class ControlMark(_ProjectionViewModel):
    """Where one Run control stands, as the ledger line that moved it states.

    Attributes:
        control_request_ref: The request the line belongs to, which is also the
            operation id a console sent it under.
        disposition: That request's rendered outcome after the line.
    """

    control_request_ref: NonEmptyStr
    disposition: NonEmptyStr


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
        control: The Run control the move records, for a control-ledger line; a
            reconnecting console reads an answer it lost from this rather than
            asking again.
        assignee_ref: The principal a pending action is addressed to after the move,
            ``None`` when it is addressed to every eligible principal. A replay must
            state the audience a clean read states, so the move carries it.
    """

    key: NonEmptyStr
    urn: NonEmptyStr
    collection: Epoch2Collection
    revision: StrictPositiveInt
    status: NonEmptyStr
    control: ControlMark | None = None
    assignee_ref: NonEmptyStr | None = None


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
    ledger_rows: Mapping[Epoch2Collection, Sequence[Mapping[str, Any]]] = MappingProxyType({}),
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
        ledger_rows: Extra rows for a rendered collection, read from its ledger by
            the caller -- a terminal record the document no longer holds, such as
            an accepted Milestone. A key the document already holds is the record
            still in flight and is never shadowed by one of these.

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
    links = _Links(document)
    rows = tuple(
        _with_facts(_projection_row(key=key, row=row, collection=collection), row, links)
        for collection in collections
        for key, row in sorted(
            _collection_rows(
                document=document, collection=collection, ledger_rows=ledger_rows
            ).items()
        )
    ) + tuple(
        _with_facts(_projection_row(key=key, row=row, collection=collection), row, links)
        for collection in ROUTE_NOTICE_COLLECTIONS.get(route, ())
        for key, row in sorted(
            _notice_rows(document=document, collection=collection, ledger_rows=ledger_rows).items()
        )
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
        control=_control_mark(payload),
        assignee_ref=_assignee_of(collection, payload),
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


def _assignee_of(collection: Epoch2Collection, fields: Mapping[str, Any]) -> str | None:
    """Return the principal a pending action is addressed to, or ``None`` for every other row."""
    if collection is not Epoch2Collection.PENDING_ACTION:
        return None
    value = fields.get(_ASSIGNEE_FIELD)
    return value if isinstance(value, str) and value.strip() else None


def _suspension_of(collection: Epoch2Collection, fields: Mapping[str, Any]) -> str | None:
    """Return why a suspended Run waits, or ``None`` for every other row."""
    if collection is not Epoch2Collection.RUN:
        return None
    value = fields.get(_SUSPENSION_FIELD)
    return value if isinstance(value, str) and value.strip() else None


def _control_mark(payload: Mapping[str, Any]) -> ControlMark | None:
    """Return the Run control a control-ledger row records, or ``None`` for any other row."""
    ref = payload.get("control_request_ref")
    if ref is None:
        return None
    return ControlMark.model_validate(
        {"control_request_ref": ref, "disposition": payload.get("control_disposition")}
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


def _collection_rows(
    *,
    document: dict[str, Any],
    collection: Epoch2Collection,
    ledger_rows: Mapping[Epoch2Collection, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    """Return one collection's rows: the live document, plus its ledger's extras.

    A key the document holds is the record still in flight, or one read at a
    later cursor than the caller's ledger pass; either way it wins over a row
    read from the ledger, which only fills a key the document no longer holds.
    """
    merged = dict(document_rows(document, collection))
    for row in ledger_rows.get(collection, ()):
        key = row.get("key")
        if isinstance(key, str) and key not in merged:
            merged[key] = row
    return merged


def _is_notice(row: Any) -> bool:
    """Return whether a stored row is a ceiling breach rather than a record of its collection.

    The ledger line states its payload kind; a row spelled back for a replay states the
    kind among the facts it was projected with.
    """
    if not isinstance(row, dict):
        return False
    carried = row.get(FACTS_FIELD)
    kind = carried.get("kind") if isinstance(carried, dict) else row.get("payload_kind")
    return kind == CEILING_BREACH_KIND


def _notice_rows(
    *,
    document: dict[str, Any],
    collection: Epoch2Collection,
    ledger_rows: Mapping[Epoch2Collection, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    """Return the notices a route lists from ``collection``, never its records.

    A fresh read finds them among the caller's ledger rows; a replay finds the ones it
    already held among the rows it spells back as its document.
    """
    merged = {
        key: row for key, row in document_rows(document, collection).items() if _is_notice(row)
    }
    for row in ledger_rows.get(collection, ()):
        key = row.get("key")
        if isinstance(key, str) and _is_notice(row):
            merged[key] = row
    return merged


def _projection_row(*, key: str, row: Any, collection: Epoch2Collection) -> ProjectionRow:
    """Return one stored row as the record a route renders.

    Identity and revision are checked here rather than left to the model, so the
    refusal names the field and never the value: a daemon log of it then carries
    nothing the document held. Only the status may be absent, because a console can
    render an unknown status honestly but cannot render a row it cannot address.

    A row the epoch-2 cutover imported states neither a URN nor a revision: the
    import wraps the epoch-1 record in a payload instead. It is addressed by the
    name the legacy continuation gives it, and its revision counts the import as
    one plus each continuation move it has made since.

    Raises:
        ValueError: The row is keyed by a blank, is not an object, states no URN
            or no positive revision, or is an import wrapper that does not
            validate as an imported record of this collection.
    """
    if not key.strip():
        raise ValueError(f"a {collection.value} row is keyed by a blank, so nothing selects it")
    if not isinstance(row, dict):
        raise ValueError(f"{collection.value} row {key!r} is a {type(row).__name__}, not an object")
    if "urn" not in row and ROW_PAYLOAD_FIELD in row:
        payload = row[ROW_PAYLOAD_FIELD]
        if isinstance(payload, dict) and _NATIVE_IMPORT_KEY in payload:
            return _native_import_row(key=key, row=row, collection=collection)
        legacy = read_legacy_row(collection, key, row)
        legacy_urn, legacy_revision = entity_ref(collection, key), 1 + len(legacy.continuation)
        return ProjectionRow(
            key=key,
            urn=legacy_urn,
            collection=collection,
            revision=legacy_revision,
            status=_status_field(status=legacy.status, urn=legacy_urn, revision=legacy_revision),
            title=_title_of(collection, legacy.record.record),
            parent_key=_parent_key_of(collection, legacy.record.record),
        )
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
        title=_title_of(collection, row),
        parent_key=_parent_key_of(collection, row),
        assignee_ref=_assignee_of(collection, row),
        suspension_reason=_suspension_of(collection, row),
    )


def _native_import_row(
    *, key: str, row: dict[str, Any], collection: Epoch2Collection
) -> ProjectionRow:
    """Return a row the cutover converted into a native collection rather than a lifecycle.

    A sandbox policy or a decision is imported whole as an
    :class:`~eawf.kernel.migration.epoch2.native_records.ImportedNativeRecord`: it has no
    lifecycle to continue, so it is addressed by the legacy name and stands at the one
    revision the import wrote.

    Raises:
        ValueError: The payload does not validate as a converted record of this collection.
    """
    try:
        record = ImportedNativeRecord.model_validate(row[ROW_PAYLOAD_FIELD])
    except ValidationError as error:
        raise ValueError(
            f"{entity_ref(collection, key)} does not validate as an imported native record"
        ) from error
    if record.target is not collection:
        raise ValueError(f"{entity_ref(collection, key)} is not a {collection.value} record")
    urn = entity_ref(collection, key)
    return ProjectionRow(
        key=key,
        urn=urn,
        collection=collection,
        revision=_IMPORT_REVISION,
        status=_status_field(status=row.get("status"), urn=urn, revision=_IMPORT_REVISION),
    )


def _title_of(collection: Epoch2Collection, fields: Mapping[str, Any]) -> str | None:
    """Return the title a stored record states, or ``None`` when it states none.

    A Task names itself by its intent rather than a title, so its intent is its title.
    """
    for name in _TITLE_FIELDS.get(collection, ("title",)):
        value = fields.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _parent_key_of(collection: Epoch2Collection, fields: Mapping[str, Any]) -> str | None:
    """Return the key of the record ``fields`` is filed under, or ``None`` when none is stated.

    The reference is a URN whose last path segment is the entity key, which is the key the
    parent's own row is stored under.
    """
    path = _PARENT_FIELD.get(collection)
    if path is None:
        return None
    value: Any = fields
    for name in path:
        value = value.get(name) if isinstance(value, dict) else None
    if not isinstance(value, str) or not value.strip():
        return None
    return value.rstrip("/").rsplit("/", 1)[-1] or None


#: The collections a record's containment chain climbs through, each to the next, in the
#: order the fields in :data:`_PARENT_FIELD` name them.
_CHAIN: Final[Mapping[Epoch2Collection, Epoch2Collection]] = MappingProxyType(
    {
        Epoch2Collection.RUN: Epoch2Collection.TASK,
        Epoch2Collection.TASK: Epoch2Collection.BATCH,
        Epoch2Collection.BATCH: Epoch2Collection.MILESTONE,
        Epoch2Collection.MILESTONE: Epoch2Collection.TRACK,
    }
)


def _text(value: Any) -> str | None:
    """Return ``value`` stripped when it is a non-blank string, else ``None``."""
    return value.strip() if isinstance(value, str) and value.strip() else None


def _key_of(ref: Any) -> str | None:
    """Return the entity key a URN reference ends in, or ``None`` when it states none."""
    text = _text(ref)
    if text is None:
        return None
    return text.rstrip("/").rsplit("/", 1)[-1] or None


class _Links:
    """The document's other records, looked up by key while one projection is built.

    A frame names the Task a Run runs, its status and the attempt the Run is; a Track
    counts the Runs under it. Those facts sit on records of other collections, so they are
    read here once per build rather than by the console, which holds only the route's own
    rows. Lookups are by key and cached, so a build reads each record at most once.
    """

    def __init__(self, document: Mapping[str, Any]) -> None:
        self._document = document
        self._fields: dict[tuple[Epoch2Collection, str], tuple[Mapping[str, Any], str | None]] = {}
        self._attempts: dict[str, list[str]] | None = None
        self._runs_under: dict[str, int] | None = None

    def _rows(self, collection: Epoch2Collection) -> Mapping[str, Any]:
        rows = self._document.get(collection.value)
        return rows if isinstance(rows, dict) else {}

    def fields(
        self, collection: Epoch2Collection, key: str
    ) -> tuple[Mapping[str, Any], str | None]:
        """Return a record's stored fields and its status; empty when it is not held."""
        found = self._fields.get((collection, key))
        if found is None:
            found = _stored_fields(collection, key, self._rows(collection).get(key))
            self._fields[(collection, key)] = found
        return found

    def parent(self, collection: Epoch2Collection, key: str) -> str | None:
        """Return the key of the record ``key`` is filed under, when it names one."""
        return _parent_key_of(collection, self.fields(collection, key)[0])

    def chain(self, collection: Epoch2Collection, key: str) -> dict[str, str]:
        """Return every record ``key`` is filed under, keyed by collection name."""
        chain: dict[str, str] = {}
        at: tuple[Epoch2Collection, str] | None = (collection, key)
        while at is not None and at[0] in _CHAIN:
            parent = self.parent(*at)
            if parent is None:
                break
            at = (_CHAIN[at[0]], parent)
            chain[at[0].value] = parent
        return chain

    def attempts(self, task: str) -> list[str]:
        """Return the Runs of ``task`` in the order they were created, oldest first."""
        if self._attempts is None:
            by_task: dict[str, list[tuple[str, str]]] = {}
            for key, row in self._rows(Epoch2Collection.RUN).items():
                fields, _status = self.fields(Epoch2Collection.RUN, key)
                owner = _parent_key_of(Epoch2Collection.RUN, fields)
                if owner is not None and isinstance(row, dict):
                    by_task.setdefault(owner, []).append(
                        (_text(fields.get("created_at")) or "", key)
                    )
            self._attempts = {
                task: [k for _at, k in sorted(runs)] for task, runs in by_task.items()
            }
        return self._attempts.get(task, [])

    def runs_under(self, key: str) -> int:
        """Return how many Runs are filed, through their Task, under the record ``key``."""
        if self._runs_under is None:
            tally: dict[str, int] = {}
            for run in self._rows(Epoch2Collection.RUN):
                for owner in self.chain(Epoch2Collection.RUN, run).values():
                    tally[owner] = tally.get(owner, 0) + 1
            self._runs_under = tally
        return self._runs_under.get(key, 0)


def _stored_fields(
    collection: Epoch2Collection, key: str, row: Any
) -> tuple[Mapping[str, Any], str | None]:
    """Return the fields a stored row states and its status, whatever form it was stored in.

    A native record states its fields directly; an imported one states them inside the
    record the cutover wrapped, and its status beside it. A row that is neither states
    nothing a fact could be read from.
    """
    if not isinstance(row, dict):
        return {}, None
    if "urn" in row or ROW_PAYLOAD_FIELD not in row:
        return row, _text(row.get("status"))
    payload = row[ROW_PAYLOAD_FIELD]
    if isinstance(payload, dict) and _NATIVE_IMPORT_KEY in payload:
        return {}, _text(row.get("status"))
    try:
        legacy = read_legacy_row(collection, key, row)
    except ValueError:
        return {}, None
    return legacy.record.record, _text(legacy.status)


def _run_facts(key: str, fields: Mapping[str, Any], links: _Links) -> dict[str, str]:
    """Return what the document states about a Run, its Task and its place among attempts."""
    facts = {name: _text(fields.get(name)) for name in _RUN_INSTANTS}
    scope = fields.get("scope")
    facts["purpose"] = _text(scope.get("purpose")) if isinstance(scope, dict) else None
    failure = fields.get("failure")
    if isinstance(failure, dict):
        facts["failure"] = _text(failure.get("message"))
        facts["failure_code"] = _text(failure.get("code"))
    task = _parent_key_of(Epoch2Collection.RUN, fields)
    if task is not None:
        task_fields, task_status = links.fields(Epoch2Collection.TASK, task)
        facts["task_title"] = _title_of(Epoch2Collection.TASK, task_fields)
        facts["task_status"] = task_status
        facts.update(links.chain(Epoch2Collection.TASK, task))
        attempts = links.attempts(task)
        if key in attempts:
            facts["attempt"] = str(attempts.index(key) + 1)
            facts["attempts"] = str(len(attempts))
    return {name: value for name, value in facts.items() if value}


def _breach_facts(fields: Mapping[str, Any]) -> dict[str, str]:
    """Return what a ceiling breach states: whose ceiling, which child, and by how much.

    The subject is the Run whose ceiling was passed, which is where the delegation that
    overran it was made.
    """
    child = _key_of(fields.get("child_run_ref"))
    ceiling, descendants = fields.get("ceiling"), fields.get("descendants")
    stated = isinstance(ceiling, int) and isinstance(descendants, int) and child is not None
    facts = {
        "kind": CEILING_BREACH_KIND,
        "subject": _key_of(fields.get("ancestor_run_ref")),
        "child": child,
        "question": (
            f"{child} made the subtree {descendants} Runs past child_runs={ceiling}"
            if stated
            else None
        ),
        "recorded_at": _text(fields.get("recorded_at")),
    }
    return {name: value for name, value in facts.items() if value}


def _permission_facts(fields: Mapping[str, Any]) -> dict[str, str]:
    """Return what a provider permission states: its Run, its deadline and who may decide it.

    The approve and deny authorities are stated apart, because they need not be one
    class, and whether the repository may approve is stated as the record resolved it,
    so a surface renders that affordance disabled with the approving classes named.
    """
    authority = fields.get("approval_authority")
    classes = authority if isinstance(authority, dict) else {}
    may = fields.get("repository_may_approve")
    facts = {
        "kind": "provider_permission",
        "subject": _key_of(fields.get("run_ref")),
        "question": _text(fields.get("request_scope")),
        "tool": _text(fields.get("tool_id")),
        "action_class": _text(fields.get("action_class")),
        "deadline_at": _text(fields.get("deadline_at")),
        "deadline_owner": _text(fields.get("deadline_owner")),
        "approve": ", ".join(str(c) for c in classes.get("approve", ())),
        "deny": ", ".join(str(c) for c in classes.get("deny", ())),
        "repository_may_approve": ("yes" if may else "no") if isinstance(may, bool) else None,
    }
    return {name: value for name, value in facts.items() if value}


def _action_facts(fields: Mapping[str, Any], links: _Links) -> dict[str, str]:
    """Return what the document states about a pending action and where its subject sits."""
    requested = fields.get("requested_by")
    subject_ref = _text(fields.get("subject_ref"))
    facts = {
        "kind": _text(fields.get("kind")),
        "question": _text(fields.get("question")),
        "requested_by": (
            _text(requested.get("principal_id")) if isinstance(requested, dict) else None
        ),
        "created_at": _text(fields.get("created_at")),
        "subject": _key_of(subject_ref),
    }
    if subject_ref is not None:
        kind = subject_ref.rstrip("/").rsplit("/", 2)
        collection = next(
            (c for c in Epoch2Collection if len(kind) == 3 and c.value == kind[1]), None
        )
        subject = facts["subject"]
        if collection is not None and subject is not None:
            facts["subject_kind"] = collection.value
            if collection in _CHAIN or collection is Epoch2Collection.TRACK:
                facts[collection.value] = subject
            facts.update(links.chain(collection, subject))
    return {name: value for name, value in facts.items() if value}


def _batch_facts(fields: Mapping[str, Any]) -> dict[str, str]:
    """Return what a Batch states about where it merges and the exact head it was bound at.

    A merging Batch is drawn as the facts it holds beside the questions it cannot answer,
    so its branch, its bound head and the instant it last moved are read here verbatim.
    """
    binding = fields.get("current_head_binding")
    failure = fields.get("failure")
    tasks = fields.get("task_refs")
    facts = {
        "target_branch": _text(fields.get("target_branch")),
        "head": _text(binding.get("head_sha")) if isinstance(binding, dict) else None,
        "updated_at": _text(fields.get("updated_at")),
        "failure": _text(failure.get("message")) if isinstance(failure, dict) else None,
        "tasks": str(len(tasks)) if isinstance(tasks, list | tuple) else None,
    }
    return {name: value for name, value in facts.items() if value}


def _task_facts(fields: Mapping[str, Any]) -> dict[str, str]:
    """Return what a stored Task states beyond its status, blanks left out.

    The graph fields travel with the row so a coordinator derives the
    concurrency plan from the Tasks it reads rather than being told one.
    """
    stated = {
        "due": _key_of(fields.get("due_scope")),
        "updated_at": _text(fields.get("updated_at")),
        "priority": _text(fields.get("priority")),
        "run": _key_of(fields.get("active_run_ref")),
    }
    criteria = fields.get("criteria")
    if isinstance(criteria, list | tuple) and criteria:
        stated["criteria"] = str(len(criteria))
    depends_on = fields.get("depends_on")
    if isinstance(depends_on, list | tuple):
        stated["depends_on"] = ",".join(key for ref in depends_on if (key := _key_of(ref)))
    claims = fields.get("write_claims")
    if isinstance(claims, list | tuple):
        stated["write_claims"] = ",".join(claim for item in claims if (claim := _text(item)))
    if fields.get("exclusive") is True:
        stated["exclusive"] = "true"
    return {name: value for name, value in stated.items() if value}


def _with_facts(row: ProjectionRow, stored: Any, links: _Links) -> ProjectionRow:
    """Return ``row`` with the facts the document states about it, beyond its status.

    A row spelled back for a replay carries the facts it was projected with; those are
    kept wherever the rebuilt document can no longer state them afresh.
    """
    carried = stored.get(FACTS_FIELD) if isinstance(stored, dict) else None
    facts = {
        name: value
        for name, value in (carried.items() if isinstance(carried, dict) else ())
        if isinstance(name, str) and _text(value)
    }
    fields, _status = _stored_fields(row.collection, row.key, stored)
    if _is_notice(stored):
        facts.update(_breach_facts(fields))
    elif row.collection is Epoch2Collection.RUN:
        facts.update(_run_facts(row.key, fields, links))
    elif row.collection is Epoch2Collection.PENDING_ACTION:
        facts.update(_action_facts(fields, links))
    elif row.collection is Epoch2Collection.PERMISSION:
        facts.update(_permission_facts(fields))
    elif row.collection is Epoch2Collection.TASK:
        facts.update(_task_facts(fields))
    elif row.collection is Epoch2Collection.BATCH:
        facts.update(_batch_facts(fields))
    elif row.collection in (Epoch2Collection.TRACK, Epoch2Collection.MILESTONE):
        runs = links.runs_under(row.key)
        if runs or FACTS_FIELD not in (stored if isinstance(stored, dict) else {}):
            facts["runs"] = str(runs)
    return row.model_copy(update={"facts": facts}) if facts else row


def row_document(row: ProjectionRow) -> dict[str, Any]:
    """Return ``row`` spelled as the stored row it would be projected from again.

    A replay rebuilds a projection from the rows it already holds, so what a row carries
    beyond its status -- its title and the record it is filed under -- has to be written
    back in the fields :func:`build_route_projection` reads them from, or a patch would
    strip them.
    """
    stored: dict[str, Any] = {"urn": row.urn, "revision": row.revision, "status": row.status.value}
    if row.title is not None:
        stored[_TITLE_FIELDS.get(row.collection, ("title",))[0]] = row.title
    path = _PARENT_FIELD.get(row.collection)
    if path is not None and row.parent_key is not None:
        nested = stored
        for name in path[:-1]:
            nested = nested.setdefault(name, {})
        nested[path[-1]] = row.parent_key
    if row.assignee_ref is not None:
        stored[_ASSIGNEE_FIELD] = row.assignee_ref
    if row.suspension_reason is not None:
        stored[_SUSPENSION_FIELD] = row.suspension_reason
    if row.facts:
        stored[FACTS_FIELD] = dict(row.facts)
    return stored


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
    could not be what two surfaces compare through. So are the rows' facts, which a
    replay may not hold for a record it learned of only through a patch.
    """
    encoded = json.dumps(
        {
            "route": route,
            "source_cursor": cursor,
            # the facts ride beside the record and are not what two surfaces agree on
            "rows": [row.model_dump(mode="json", exclude={"facts"}) for row in rows],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


__all__ = [
    "CANONICAL_SEQUENCE_FIELD",
    "CEILING_BREACH_KIND",
    "DIAGNOSTICS_CORPUS",
    "FACTS_FIELD",
    "MISSING_STATUS_REASON",
    "PROJECTION_POLICY_REVISION",
    "PROJECTION_PRODUCER",
    "PROJECTION_SCHEMA_VERSION",
    "ROUTE_COLLECTIONS",
    "ROUTE_NOTICE_COLLECTIONS",
    "ROUTE_READ_MODELS",
    "ControlMark",
    "KeyedPatch",
    "PatchEntry",
    "ProjectionRow",
    "RouteProjection",
    "build_route_projection",
    "patches_for_event",
    "row_document",
]
