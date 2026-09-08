"""Mapping the four epoch-1 lifecycle collections onto epoch-2 records.

A phase becomes a Milestone, an iter a DeliveryBatch, a wave a Task and a
backlog row the draft head of a Task. Each mapping is a table rather than
a procedure: one route per source field, checked total against the live
epoch-1 model, so adding a field to ``Phase`` fails here instead of
silently dropping it at cutover.

Three things the target models require, epoch 1 never recorded: a
Milestone's acceptance journey, a Batch's head binding, a Task's contract
revision. None of them is filled. A required target field with no source
fact is recorded as a :class:`DeferredField`, which names the field and
why the source cannot supply it, so a reader can tell an unanswered
question from an answer. Filling any of them would make the record
validate and make it wrong.

The same rule governs references. A claim-session id is carried as a
string on the envelope and only additionally resolved when the source
really holds that session; a Run is minted only from a claim that
resolves or from a recorded attempt; and a Track that the source cannot
name uniquely leaves the Milestone unassigned rather than guessing.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Annotated, Any

from pydantic import Field

from eawf.kernel.migration.epoch2.backlog import BacklogResolution
from eawf.kernel.migration.epoch2.criteria import ImportedCriterion
from eawf.kernel.migration.epoch2.errors import MigrationCountMismatchError
from eawf.kernel.migration.epoch2.rules import StrictMigrationModel, rule_digest
from eawf.kernel.migration.epoch2.status_map import (
    SourceLifecycle,
    apply_annotated_defaults,
    compact_annotations,
    map_source_status,
)
from eawf.kernel.state.epoch2.values import EntityOrigin, MappingBasis, OriginConfidence
from eawf.kernel.state.models import BacklogItem, Iter, Phase, Wave

logger = logging.getLogger(__name__)


#: The epoch-1 top-level key each lifecycle collection is read from.
SOURCE_COLLECTIONS: Mapping[SourceLifecycle, str] = {
    SourceLifecycle.PHASE: "phases",
    SourceLifecycle.ITER: "iters",
    SourceLifecycle.WAVE: "waves",
    SourceLifecycle.BACKLOG: "backlog",
}

#: The one place the importer reads a Task's intent prose from. The rest
#: of the source intent brief is carried on ``legacy_refs`` unchanged.
INTENT_NATIVE_SOURCE_KEY = "desired_outcome"

#: A run minted from an imported source is never claimed to have
#: succeeded: success needs a bound role report, which the source row
#: does not carry, so every imported run lands unclassified.
UNCLASSIFIED_RUN_STATUS = "TERMINAL_UNCLASSIFIED"

#: Both facts a run needs before it may claim success. Neither is
#: reconstructible from a lifecycle row alone.
RUN_SUCCESS_REQUIREMENTS: tuple[str, ...] = ("bound_role_report", "exit_status_zero")

#: Where a legacy claim-session id lands, and what it becomes when the
#: source really holds the session it names.
CLAIM_SESSION_SOURCE_FIELD = "claim_session_id"
CLAIM_SESSION_LEGACY_FIELD = "legacy_refs.claim_session_id"
CLAIM_SESSION_RESOLVING_FIELD = "legacy_session_ref"
EMPTY_CLAIM_IMPORTS_AS = "absent_field"

TRACK_UNASSIGNED_ANNOTATION = "track_unassigned_no_unique_source_candidate"
ATTEMPT_RUN_ID_SEPARATOR = "#attempt-"


class LifecycleTarget(StrEnum):
    """The three epoch-2 kinds the lifecycle collections convert into."""

    MILESTONE = "milestone"
    BATCH = "batch"
    TASK = "task"


#: Which epoch-2 kind each source collection converts into. A backlog row
#: and a wave both become Tasks: one identifier survives from first idea
#: to integrated delivery, so promotion never changes identity.
LIFECYCLE_TARGETS: Mapping[SourceLifecycle, LifecycleTarget] = {
    SourceLifecycle.PHASE: LifecycleTarget.MILESTONE,
    SourceLifecycle.ITER: LifecycleTarget.BATCH,
    SourceLifecycle.WAVE: LifecycleTarget.TASK,
    SourceLifecycle.BACKLOG: LifecycleTarget.TASK,
}


class LifecycleFieldDisposition(StrEnum):
    """Where one epoch-1 lifecycle field lands on the imported record."""

    IDENTITY = "identity"
    NATIVE_CONVERSION = "native_conversion"
    LEGACY_REF = "legacy_ref"


class LifecycleFieldRoute(StrictMigrationModel):
    """One source field's destination on the imported record."""

    disposition: LifecycleFieldDisposition
    target_key: Annotated[str, Field(min_length=1)]


def _identity(target_key: str) -> LifecycleFieldRoute:
    """Route a field onto the imported record's source identity."""
    return LifecycleFieldRoute(
        disposition=LifecycleFieldDisposition.IDENTITY, target_key=target_key
    )


def _native(target_key: str) -> LifecycleFieldRoute:
    """Route a field onto the epoch-2 field named ``target_key``."""
    return LifecycleFieldRoute(
        disposition=LifecycleFieldDisposition.NATIVE_CONVERSION, target_key=target_key
    )


def _legacy(target_key: str) -> LifecycleFieldRoute:
    """Route a field onto ``legacy_refs[target_key]``."""
    return LifecycleFieldRoute(
        disposition=LifecycleFieldDisposition.LEGACY_REF, target_key=target_key
    )


# Some native routes name a target whose value is converted rather than
# copied -- the status map, the Track resolver, the annotated defaults,
# the criteria disposition. Those targets are listed in
# ``COMPUTED_NATIVE_KEYS`` below and written by the mapper that owns the
# conversion, so the table stays a statement of where a field lands.
PHASE_FIELD_ROUTES: Mapping[str, LifecycleFieldRoute] = {
    "id": _identity("source_id"),
    "title": _native("title"),
    "description": _native("description"),
    "status": _native("status"),
    "track_id": _native("primary_track_ref"),
    "iter_ids": _native("required_batch_refs"),
    "opened_at": _native("created_at"),
    "scope_id": _legacy("scope_id"),
    "outcome_ids": _legacy("outcome_ids"),
    "depends_on": _legacy("depends_on"),
    "source_brief_ids": _legacy("source_brief_ids"),
    "release": _legacy("release"),
    "closed_at": _legacy("closed_at"),
    "audit_id": _legacy("audit_id"),
    "intent": _legacy("intent"),
}

# A DeliveryBatch carries no title and no description: it is an
# integration unit, not a narrative. Both are preserved as legacy refs
# rather than dropped, because the source did record them.
ITER_FIELD_ROUTES: Mapping[str, LifecycleFieldRoute] = {
    "id": _identity("source_id"),
    "phase_id": _native("milestone_ref"),
    "status": _native("status"),
    "wave_ids": _native("task_refs"),
    "opened_at": _native("created_at"),
    "title": _legacy("title"),
    "description": _legacy("description"),
    "trigger": _legacy("trigger"),
    "candidate_tag": _legacy("candidate_tag"),
    "estimate_id": _legacy("estimate_id"),
    "audit_id": _legacy("audit_id"),
    "closed_at": _legacy("closed_at"),
    "intent": _legacy("intent"),
}

WAVE_FIELD_ROUTES: Mapping[str, LifecycleFieldRoute] = {
    "id": _identity("source_id"),
    "iter_id": _native("batch_ref"),
    "status": _native("status"),
    "success_criteria": _native("criteria"),
    "intent": _native("intent"),
    "opened_at": _native("created_at"),
    "title": _legacy("title"),
    "description": _legacy("description"),
    "deps": _legacy("deps"),
    "blocks": _legacy("blocks"),
    "file_scopes": _legacy("file_scopes"),
    "gates": _legacy("gates"),
    "agent_role": _legacy("agent_role"),
    "effort_bucket": _legacy("effort_bucket"),
    "claim_session_id": _legacy("claim_session_id"),
    "worktree_id": _legacy("worktree_id"),
    "token_budget": _legacy("token_budget"),
    "tokens_consumed": _legacy("tokens_consumed"),
    "outcome": _legacy("outcome"),
    "commit": _legacy("commit"),
    "commit_identity_digest": _legacy("commit_identity_digest"),
    "claimed_at": _legacy("claimed_at"),
    "closed_at": _legacy("closed_at"),
    "runtime_baseline": _legacy("runtime_baseline"),
    "runtime_latest": _legacy("runtime_latest"),
    "runtime_carry": _legacy("runtime_carry"),
    "runtime_preference": _legacy("runtime_preference"),
    "dispatch_history": _legacy("dispatch_history"),
    "sessions": _legacy("sessions"),
    "criteria_floor_waiver": _legacy("criteria_floor_waiver"),
}

BACKLOG_FIELD_ROUTES: Mapping[str, LifecycleFieldRoute] = {
    "id": _identity("source_id"),
    "status": _native("status"),
    "priority": _native("priority"),
    "intent": _native("intent"),
    "created_at": _native("created_at"),
    "title": _legacy("title"),
    "description": _legacy("description"),
    "scope_id": _legacy("scope_id"),
    "closed_at": _legacy("closed_at"),
    "resolution": _legacy("resolution"),
    "commit": _legacy("commit"),
}

LIFECYCLE_FIELD_ROUTES: Mapping[SourceLifecycle, Mapping[str, LifecycleFieldRoute]] = {
    SourceLifecycle.PHASE: PHASE_FIELD_ROUTES,
    SourceLifecycle.ITER: ITER_FIELD_ROUTES,
    SourceLifecycle.WAVE: WAVE_FIELD_ROUTES,
    SourceLifecycle.BACKLOG: BACKLOG_FIELD_ROUTES,
}

#: Native targets whose value is converted rather than preserved, so the
#: routing pass leaves them to the mapper that owns the conversion.
COMPUTED_NATIVE_KEYS: frozenset[str] = frozenset(
    {"status", "primary_track_ref", "intent", "priority", "criteria"}
)

_SOURCE_MODEL_FIELDS: Mapping[SourceLifecycle, Iterable[str]] = {
    SourceLifecycle.PHASE: Phase.model_fields,
    SourceLifecycle.ITER: Iter.model_fields,
    SourceLifecycle.WAVE: Wave.model_fields,
    SourceLifecycle.BACKLOG: BacklogItem.model_fields,
}


def _assert_routes_total(lifecycle: SourceLifecycle) -> None:
    """Fail import unless the route table covers the source model exactly.

    Raises:
        ValueError: When a model field has no route, which would drop it
            at cutover, or a route names a field the model does not have,
            which would carry a fact nobody recorded.
    """
    declared = set(_SOURCE_MODEL_FIELDS[lifecycle])
    routed = set(LIFECYCLE_FIELD_ROUTES[lifecycle])
    if declared != routed:
        unrouted = sorted(declared - routed)
        unknown = sorted(routed - declared)
        raise ValueError(
            f"{lifecycle.value} field routes are not total: unrouted={unrouted}, unknown={unknown}"
        )


for _lifecycle in SourceLifecycle:
    _assert_routes_total(_lifecycle)


class DeferralReason(StrEnum):
    """Why a required epoch-2 field is left for an operator to supply."""

    SOURCE_HAS_NO_FIELD = "source_has_no_field"
    UNRESOLVED_SOURCE_REFERENCE = "unresolved_source_reference"
    AMBIGUOUS_SOURCE_CANDIDATES = "ambiguous_source_candidates"


class DeferredField(StrictMigrationModel):
    """One epoch-2 field the source cannot supply, named rather than filled.

    Attributes:
        target_field: The epoch-2 field left unset.
        reason: Why the source cannot supply it.
        candidates: The source values an operator may choose between,
            recorded in full when more than one would fit.
    """

    target_field: Annotated[str, Field(min_length=1)]
    reason: DeferralReason
    candidates: tuple[str, ...] = ()


#: Milestone fields no epoch-1 phase ever recorded. An acceptance journey
#: is the strongest of them: inventing one would let an imported
#: Milestone claim it can be accepted.
MILESTONE_UNRECORDED_FIELDS: tuple[str, ...] = (
    "outcome",
    "appetite",
    "exclusions",
    "acceptance_journey",
)

#: Milestone acceptance proof, required only where the status implies it.
MILESTONE_ACCEPTANCE_FIELDS: tuple[str, ...] = (
    "acceptance_bundle_revision",
    "accepted_binding",
)

#: Batch fields no epoch-1 iter recorded. The head binding is what a
#: merge authorisation is checked against, so a filled one would turn an
#: unproven merge into a proven-looking one.
BATCH_UNRECORDED_FIELDS: tuple[str, ...] = ("repository_ref", "target_branch")

BATCH_HEAD_BINDING_FIELD = "current_head_binding"

#: Task fields no epoch-1 wave recorded.
TASK_UNRECORDED_FIELDS: tuple[str, ...] = ("contract_revision",)

#: The epoch-2 statuses that make a target's proof of completion a fact.
COMPLETED_STATUS = "COMPLETED"
HEAD_BOUND_BATCH_STATUSES: frozenset[str] = frozenset(
    {"READY_TO_MERGE", "MERGING", "MERGED_PENDING_RECONCILIATION", COMPLETED_STATUS}
)

#: The Task statuses that carry no batch, no criteria and no due scope.
DRAFT_HEAD_STATUSES: frozenset[str] = frozenset({"DRAFT", "DEFERRED", "DROPPED"})


class RunSource(StrEnum):
    """The only two source facts a Run may be minted from."""

    RESOLVING_CLAIM = "resolving_claimed_wave_id"
    WAVE_ATTEMPT = "wave_attempt_entry"


class MintedRun(StrictMigrationModel):
    """One Run the source really supports, with the fact that supports it."""

    run_source: RunSource
    source_id: Annotated[str, Field(min_length=1)]
    status: Annotated[str, Field(min_length=1)]


class TrackAssignment(StrictMigrationModel):
    """Which Track owns a Milestone, when the source can say.

    Attributes:
        track_id: The resolved source track id, or ``None`` when the
            source names none uniquely.
        candidates: Every track the source holds, so an operator has the
            list to choose from without re-reading the corpus.
        unresolved_ref: A track the source named that no track row backs.
    """

    track_id: str | None
    candidates: tuple[str, ...]
    unresolved_ref: str | None


class LifecycleSourceIndex(StrictMigrationModel):
    """The populations the lifecycle mappers resolve references against.

    Attributes:
        source_schema_version: The epoch-1 schema version of the document
            the rows came from; it is stamped on every imported origin.
        track_ids: Every track the source holds, in source order.
        session_ids: Every agent-session id the source holds.
    """

    source_schema_version: Annotated[str, Field(min_length=1)]
    track_ids: tuple[str, ...]
    session_ids: frozenset[str]

    @classmethod
    def build(cls, document: Mapping[str, Any]) -> LifecycleSourceIndex:
        """Read the resolution populations out of one epoch-1 document.

        Args:
            document: The decoded epoch-1 state document.

        Returns:
            The index, ready to map rows against.

        Raises:
            MigrationCountMismatchError: When the document carries no
                usable ``schema_version``. Every imported origin has to
                name the schema it came from, and a guessed version would
                make the record unreproducible.
        """
        version = document.get("schema_version")
        if not isinstance(version, str) or not version:
            raise MigrationCountMismatchError(
                "the epoch-1 document carries no schema_version to stamp on imported origins"
            )
        tracks = document.get("tracks")
        sessions = document.get("agent_sessions")
        return cls(
            source_schema_version=version,
            track_ids=tuple(tracks) if isinstance(tracks, dict) else (),
            session_ids=frozenset(sessions) if isinstance(sessions, dict) else frozenset(),
        )

    def resolve_track(self, track_id: Any) -> TrackAssignment:
        """Resolve a phase's track reference against the source tracks.

        A reference is honoured only when the source really holds that
        track. When the phase names none, the Milestone stays unassigned
        even if exactly one track exists: "there is only one" is an
        inference about the operator's intent, not a recorded fact.

        Args:
            track_id: The raw ``track_id`` the phase row carried.

        Returns:
            The assignment, with the candidate list an operator picks
            from when the source cannot decide.
        """
        if isinstance(track_id, str) and track_id:
            if track_id in self.track_ids:
                return TrackAssignment(
                    track_id=track_id, candidates=self.track_ids, unresolved_ref=None
                )
            return TrackAssignment(
                track_id=None, candidates=self.track_ids, unresolved_ref=track_id
            )
        return TrackAssignment(track_id=None, candidates=self.track_ids, unresolved_ref=None)


class ImportedLifecycleRecord(StrictMigrationModel):
    """One epoch-1 lifecycle row as the importer will write it.

    Attributes:
        lifecycle: Which source collection the row came from.
        source_collection: That collection's top-level key.
        target: The epoch-2 kind the row converts into.
        target_status: The mapped epoch-2 status.
        origin: The legacy origin every imported record carries.
        record: The epoch-2 fields the source could supply, keyed by
            their epoch-2 names.
        legacy_refs: Every source field with no native home, preserved.
        legacy_session_ref: The claim session the source really holds,
            present only when the claimed id resolves.
        criteria: The wave's success criteria as epoch-2 criteria.
        minted_runs: The Runs the source supports, which may be none.
        deferred_fields: Required epoch-2 fields the source cannot fill.
        resolution: The classified fate of a closed backlog row.
        obsolete: Whether the row names something the cutover deletes.
        annotations: Every annotation the row earned at import.
        ledger_annotations: The subset that survives compaction.
    """

    lifecycle: SourceLifecycle
    source_collection: Annotated[str, Field(min_length=1)]
    target: LifecycleTarget
    target_status: Annotated[str, Field(min_length=1)]
    origin: EntityOrigin
    record: dict[str, Any]
    legacy_refs: dict[str, Any]
    legacy_session_ref: str | None
    criteria: tuple[ImportedCriterion, ...]
    minted_runs: tuple[MintedRun, ...]
    deferred_fields: tuple[DeferredField, ...]
    resolution: BacklogResolution | None
    obsolete: bool
    annotations: tuple[str, ...]
    ledger_annotations: tuple[str, ...]

    def deferred_field_names(self) -> tuple[str, ...]:
        """Return the epoch-2 fields left unfilled, in declaration order."""
        return tuple(field.target_field for field in self.deferred_fields)


def _source_digest(row: Mapping[str, Any]) -> str:
    """Return the prefixed sha256 digest of one source row.

    Args:
        row: The source row, read only.

    Returns:
        The digest in ``sha256:<hex>`` form, which is what an epoch-2
        origin's ``source_digest`` field admits.

    Raises:
        TypeError: When the row holds a value ``json`` cannot encode.
    """
    return f"sha256:{rule_digest(row)}"


def _origin(
    *,
    lifecycle: SourceLifecycle,
    source_id: str,
    row: Mapping[str, Any],
    index: LifecycleSourceIndex,
    confidence: OriginConfidence,
) -> EntityOrigin:
    """Build the legacy origin one imported lifecycle record carries.

    Args:
        lifecycle: Which source collection the row came from.
        source_id: The row's own key in that collection.
        row: The source row, digested so the import is reproducible.
        index: The source index carrying the epoch-1 schema version.
        confidence: How much the mapping of this row is trusted.

    Returns:
        The origin, always ``kind='legacy'`` with a mechanical basis: the
        conversion is a table lookup, not an observation or a judgement.

    Raises:
        ValidationError: When a field violates the origin contract.
    """
    basis: MappingBasis = "mechanical"
    return EntityOrigin(
        kind="legacy",
        source_schema_version=index.source_schema_version,
        source_kind=SOURCE_COLLECTIONS[lifecycle],
        source_id=source_id,
        source_digest=_source_digest(row),
        mapping_basis=basis,
        confidence=confidence,
    )


def _confidence(
    *, annotations: Iterable[str], deferred: Iterable[DeferredField]
) -> OriginConfidence:
    """Return how much a row's mapping is trusted.

    A mapping that had to choose between source candidates is
    ``ambiguous``; one that leaned on an annotated default or left a
    field for an operator is ``supported``; one that took every value
    verbatim is ``exact``.
    """
    fields = tuple(deferred)
    labels: tuple[str, ...] = tuple(annotations)
    if any(field.reason is DeferralReason.AMBIGUOUS_SOURCE_CANDIDATES for field in fields):
        return "ambiguous"
    if fields or labels:
        return "supported"
    return "exact"


def _route_row(
    *, lifecycle: SourceLifecycle, row: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split one source row into its native record and its legacy refs.

    Only the fields the row actually carries are routed: a field the
    source omitted is absent from both halves rather than present and
    null, so a later reader can tell "never recorded" from "recorded as
    nothing". Converted natives are left out entirely and written by the
    mapper that owns their conversion.

    Args:
        lifecycle: Which source collection the row came from.
        row: The source row.

    Returns:
        A ``(record, legacy_refs)`` pair.
    """
    record: dict[str, Any] = {}
    legacy_refs: dict[str, Any] = {}
    for field, route in LIFECYCLE_FIELD_ROUTES[lifecycle].items():
        if field not in row:
            continue
        value = row[field]
        if route.disposition is LifecycleFieldDisposition.LEGACY_REF:
            legacy_refs[route.target_key] = value
        elif (
            route.disposition is LifecycleFieldDisposition.NATIVE_CONVERSION
            and route.target_key not in COMPUTED_NATIVE_KEYS
        ):
            record[route.target_key] = value
    return record, legacy_refs


def _task_head(row: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Return a Task's intent and priority, with the annotations they earned.

    Both fields are required from the very first draft, and epoch 1
    recorded neither on a wave. The fills come from the shared annotated
    -default table rather than from a rule invented here, and the
    annotation travels on the record so a later reader can tell a
    defaulted field from a recorded one.

    Args:
        row: The source row.

    Returns:
        A ``(filled, annotations)`` pair.

    Raises:
        MigrationCountMismatchError: When the intent must be defaulted
            and the row carries no usable title to take it from.
    """
    fields: dict[str, Any] = {"title": row.get("title"), "priority": row.get("priority")}
    intent = _native_intent(row)
    if intent is not None:
        fields["intent"] = intent
    return apply_annotated_defaults(fields)


def _native_intent(row: Mapping[str, Any]) -> str | None:
    """Return the intent prose a Task can take from a source intent brief.

    Args:
        row: The source row.

    Returns:
        The brief's desired-outcome line, the intent verbatim when the
        source stored a bare string, or ``None`` when the source recorded
        no intent at all -- in which case the annotated default fills it
        from the title.
    """
    intent = row.get("intent")
    if isinstance(intent, str):
        return intent or None
    if isinstance(intent, Mapping):
        outcome = intent.get(INTENT_NATIVE_SOURCE_KEY)
        if isinstance(outcome, str) and outcome:
            return outcome
    return None


def _carry_intent_brief(*, row: Mapping[str, Any], legacy_refs: dict[str, Any]) -> None:
    """Preserve a source intent brief beside the one line the Task takes.

    An epoch-1 intent is a whole brief: a problem statement, planned
    steps, risks and evidence. Only its outcome line becomes the Task's
    intent, so the brief itself is carried rather than discarded with the
    fields the target has no home for.

    Args:
        row: The source row.
        legacy_refs: The record's legacy refs, updated in place.
    """
    intent = row.get("intent")
    if isinstance(intent, Mapping):
        legacy_refs["intent"] = intent


def _claim_session(
    *, row: Mapping[str, Any], index: LifecycleSourceIndex
) -> tuple[str | None, str | None, tuple[MintedRun, ...]]:
    """Read a wave's claimed session without minting one that never existed.

    Args:
        row: The source wave row.
        index: The source index holding the session population.

    Returns:
        A ``(carried, resolving, runs)`` triple. ``carried`` is the raw
        string kept on the envelope, or ``None`` when the source left the
        field empty or absent -- an empty string is an absent field, not
        an empty reference. ``resolving`` is set only when the source
        really holds that session, and ``runs`` is empty unless it does.
    """
    raw = row.get(CLAIM_SESSION_SOURCE_FIELD)
    if not isinstance(raw, str) or not raw:
        return None, None, ()
    if raw not in index.session_ids:
        return raw, None, ()
    return (
        raw,
        raw,
        (
            MintedRun(
                run_source=RunSource.RESOLVING_CLAIM,
                source_id=raw,
                status=UNCLASSIFIED_RUN_STATUS,
            ),
        ),
    )


def _attempt_runs(*, source_id: str, row: Mapping[str, Any]) -> tuple[MintedRun, ...]:
    """Mint one Run per recorded dispatch attempt, in attempt order.

    An attempt is a fact: the source says a subprocess ran. Its outcome
    is not, so every minted run is unclassified even when the attempt
    exited zero, because success also needs a bound role report the
    lifecycle row does not carry.

    Args:
        source_id: The wave's own id, used to address a nameless attempt.
        row: The source wave row.

    Returns:
        The minted runs, keyed in sorted attempt order so two passes over
        one row agree.
    """
    attempts = row.get("sessions")
    if not isinstance(attempts, Mapping):
        return ()
    runs: list[MintedRun] = []
    for key in sorted(attempts, key=str):
        attempt = attempts[key]
        session_id = attempt.get("session_id") if isinstance(attempt, Mapping) else None
        if not isinstance(session_id, str) or not session_id:
            session_id = f"{source_id}{ATTEMPT_RUN_ID_SEPARATOR}{key}"
        runs.append(
            MintedRun(
                run_source=RunSource.WAVE_ATTEMPT,
                source_id=session_id,
                status=UNCLASSIFIED_RUN_STATUS,
            )
        )
    return tuple(runs)


def _unrecorded(fields: Iterable[str]) -> list[DeferredField]:
    """Return one deferral per field epoch 1 never recorded."""
    return [
        DeferredField(target_field=field, reason=DeferralReason.SOURCE_HAS_NO_FIELD)
        for field in fields
    ]


def _track_deferral(assignment: TrackAssignment) -> list[DeferredField]:
    """Return the Milestone's Track deferral, when the source cannot assign one.

    Args:
        assignment: The resolved track assignment.

    Returns:
        An empty list when the source named a track that exists, else one
        deferral carrying every candidate an operator may pick from.
    """
    if assignment.track_id is not None:
        return []
    if assignment.unresolved_ref is not None:
        reason = DeferralReason.UNRESOLVED_SOURCE_REFERENCE
    elif len(assignment.candidates) > 1:
        reason = DeferralReason.AMBIGUOUS_SOURCE_CANDIDATES
    else:
        reason = DeferralReason.SOURCE_HAS_NO_FIELD
    return [
        DeferredField(
            target_field="primary_track_ref",
            reason=reason,
            candidates=assignment.candidates,
        )
    ]


def map_phase_row(
    *,
    source_id: str,
    row: Mapping[str, Any],
    index: LifecycleSourceIndex,
) -> ImportedLifecycleRecord:
    """Map one epoch-1 phase onto an imported Milestone.

    Acceptance is never inferred. A closed phase maps to ``COMPLETED``
    and still carries its acceptance proof as deferred, because epoch 1
    recorded that the phase stopped, not that anyone accepted it.

    Args:
        source_id: The phase's own id.
        row: The source phase row.
        index: The populations references resolve against.

    Returns:
        The imported record.

    Raises:
        MigrationCountMismatchError: When the phase status falls outside
            the closed status map.
    """
    target_status = map_source_status(SourceLifecycle.PHASE, str(row.get("status")))
    record, legacy_refs = _route_row(lifecycle=SourceLifecycle.PHASE, row=row)

    assignment = index.resolve_track(row.get("track_id"))
    if assignment.track_id is not None:
        record["primary_track_ref"] = assignment.track_id
    elif assignment.unresolved_ref is not None:
        # A track the source named but never held is still a source fact,
        # so it survives as a string rather than as a native reference to
        # a Track that does not exist.
        legacy_refs["track_id"] = assignment.unresolved_ref
    record["status"] = target_status

    deferred = _unrecorded(MILESTONE_UNRECORDED_FIELDS) + _track_deferral(assignment)
    if target_status == COMPLETED_STATUS:
        deferred.extend(_unrecorded(MILESTONE_ACCEPTANCE_FIELDS))

    annotations: tuple[str, ...] = ()
    if assignment.track_id is None:
        annotations = (TRACK_UNASSIGNED_ANNOTATION,)

    return ImportedLifecycleRecord(
        lifecycle=SourceLifecycle.PHASE,
        source_collection=SOURCE_COLLECTIONS[SourceLifecycle.PHASE],
        target=LifecycleTarget.MILESTONE,
        target_status=target_status,
        origin=_origin(
            lifecycle=SourceLifecycle.PHASE,
            source_id=source_id,
            row=row,
            index=index,
            confidence=_confidence(annotations=annotations, deferred=deferred),
        ),
        record=record,
        legacy_refs=legacy_refs,
        legacy_session_ref=None,
        criteria=(),
        minted_runs=(),
        deferred_fields=tuple(deferred),
        resolution=None,
        obsolete=False,
        annotations=annotations,
        ledger_annotations=compact_annotations(annotations),
    )


def map_iter_row(
    *,
    source_id: str,
    row: Mapping[str, Any],
    index: LifecycleSourceIndex,
) -> ImportedLifecycleRecord:
    """Map one epoch-1 iter onto an imported DeliveryBatch.

    The iter's merge and tag evidence -- its candidate release tag, its
    audit reference, the waves it integrated -- is preserved as fact. The
    head binding that would make the merge provable is not: epoch 1 never
    recorded one, so it is deferred rather than reconstructed.

    Args:
        source_id: The iter's own id.
        row: The source iter row.
        index: The populations references resolve against.

    Returns:
        The imported record.

    Raises:
        MigrationCountMismatchError: When the iter status falls outside
            the closed status map.
    """
    target_status = map_source_status(SourceLifecycle.ITER, str(row.get("status")))
    record, legacy_refs = _route_row(lifecycle=SourceLifecycle.ITER, row=row)
    record["status"] = target_status

    deferred = _unrecorded(BATCH_UNRECORDED_FIELDS)
    if target_status in HEAD_BOUND_BATCH_STATUSES:
        deferred.extend(_unrecorded((BATCH_HEAD_BINDING_FIELD,)))

    return ImportedLifecycleRecord(
        lifecycle=SourceLifecycle.ITER,
        source_collection=SOURCE_COLLECTIONS[SourceLifecycle.ITER],
        target=LifecycleTarget.BATCH,
        target_status=target_status,
        origin=_origin(
            lifecycle=SourceLifecycle.ITER,
            source_id=source_id,
            row=row,
            index=index,
            confidence=_confidence(annotations=(), deferred=deferred),
        ),
        record=record,
        legacy_refs=legacy_refs,
        legacy_session_ref=None,
        criteria=(),
        minted_runs=(),
        deferred_fields=tuple(deferred),
        resolution=None,
        obsolete=False,
        annotations=(),
        ledger_annotations=(),
    )


def map_wave_row(
    *,
    source_id: str,
    row: Mapping[str, Any],
    criteria: tuple[ImportedCriterion, ...],
    index: LifecycleSourceIndex,
) -> ImportedLifecycleRecord:
    """Map one epoch-1 wave onto an imported Task.

    Args:
        source_id: The wave's own id.
        row: The source wave row.
        criteria: The wave's success criteria, already converted through
            the criteria disposition row.
        index: The populations references resolve against.

    Returns:
        The imported record, carrying the claimed session as a string and
        minting a Run only where the source supports one.

    Raises:
        MigrationCountMismatchError: When the wave status falls outside
            the closed status map, or the row carries neither an intent
            nor a title to default it from.
    """
    target_status = map_source_status(SourceLifecycle.WAVE, str(row.get("status")))
    record, legacy_refs = _route_row(lifecycle=SourceLifecycle.WAVE, row=row)

    filled, annotations = _task_head(row)
    record["intent"] = filled["intent"]
    record["priority"] = filled["priority"]
    record["status"] = target_status
    _carry_intent_brief(row=row, legacy_refs=legacy_refs)

    carried, resolving, claim_runs = _claim_session(row=row, index=index)
    if carried is None:
        legacy_refs.pop(CLAIM_SESSION_SOURCE_FIELD, None)
    else:
        legacy_refs[CLAIM_SESSION_SOURCE_FIELD] = carried

    deferred = _unrecorded(TASK_UNRECORDED_FIELDS)
    if target_status not in DRAFT_HEAD_STATUSES:
        deferred.extend(_unrecorded(("due_scope",)))
        if not criteria:
            deferred.extend(_unrecorded(("criteria",)))
    if target_status == COMPLETED_STATUS:
        deferred.extend(_unrecorded(("integrated_binding",)))

    return ImportedLifecycleRecord(
        lifecycle=SourceLifecycle.WAVE,
        source_collection=SOURCE_COLLECTIONS[SourceLifecycle.WAVE],
        target=LifecycleTarget.TASK,
        target_status=target_status,
        origin=_origin(
            lifecycle=SourceLifecycle.WAVE,
            source_id=source_id,
            row=row,
            index=index,
            confidence=_confidence(annotations=annotations, deferred=deferred),
        ),
        record=record,
        legacy_refs=legacy_refs,
        legacy_session_ref=resolving,
        criteria=criteria,
        minted_runs=claim_runs + _attempt_runs(source_id=source_id, row=row),
        deferred_fields=tuple(deferred),
        resolution=None,
        obsolete=False,
        annotations=annotations,
        ledger_annotations=compact_annotations(annotations),
    )


def map_backlog_row(
    *,
    source_id: str,
    row: Mapping[str, Any],
    target_status: str,
    filled_record: Mapping[str, Any],
    annotations: tuple[str, ...],
    resolution: BacklogResolution | None,
    obsolete: bool,
    index: LifecycleSourceIndex,
) -> ImportedLifecycleRecord:
    """Lift one already-classified backlog row into an imported Task.

    The backlog rules -- the status map, the annotated defaults, the
    resolution classifier and the obsolescence sweep -- have already run
    over this row. This function adds only what a lifecycle target needs
    on top of them: the legacy origin and the field routing.

    Args:
        source_id: The backlog row's own id.
        row: The source backlog row.
        target_status: The mapped epoch-2 status.
        filled_record: The row with its annotated defaults applied.
        annotations: The annotations the row earned.
        resolution: The classified fate of a closed row.
        obsolete: Whether the row names something the cutover deletes.
        index: The populations references resolve against.

    Returns:
        The imported record. A draft-head Task carries no batch, no due
        scope and no criteria, so it defers none of them.
    """
    record, legacy_refs = _route_row(lifecycle=SourceLifecycle.BACKLOG, row=row)
    record["status"] = target_status
    record["priority"] = filled_record["priority"]
    # A source intent brief is prose plus provenance; only its outcome
    # line is the Task's intent, and the brief stays on ``legacy_refs``.
    narrowed = _native_intent(row)
    record["intent"] = filled_record["intent"] if narrowed is None else narrowed
    _carry_intent_brief(row=row, legacy_refs=legacy_refs)

    deferred = _unrecorded(TASK_UNRECORDED_FIELDS)
    if resolution is not None and resolution.ambiguous_delivered_by:
        # The classifier found a shorthand naming several waves. Picking
        # one would assert a delivery the source never named, so every
        # candidate is recorded and the reference is left for an operator.
        deferred.append(
            DeferredField(
                target_field="delivered_by_task_ref",
                reason=DeferralReason.AMBIGUOUS_SOURCE_CANDIDATES,
                candidates=tuple(
                    candidate
                    for shorthand in resolution.ambiguous_delivered_by
                    for candidate in shorthand.candidates
                ),
            )
        )

    return ImportedLifecycleRecord(
        lifecycle=SourceLifecycle.BACKLOG,
        source_collection=SOURCE_COLLECTIONS[SourceLifecycle.BACKLOG],
        target=LifecycleTarget.TASK,
        target_status=target_status,
        origin=_origin(
            lifecycle=SourceLifecycle.BACKLOG,
            source_id=source_id,
            row=row,
            index=index,
            confidence=_confidence(annotations=annotations, deferred=deferred),
        ),
        record=record,
        legacy_refs=legacy_refs,
        legacy_session_ref=None,
        criteria=(),
        minted_runs=(),
        deferred_fields=tuple(deferred),
        resolution=resolution,
        obsolete=obsolete,
        annotations=annotations,
        ledger_annotations=compact_annotations(annotations),
    )


def lifecycle_rule_payload() -> dict[str, Any]:
    """Return the digestable form of every lifecycle mapping table."""
    return {
        "targets": {
            lifecycle.value: target.value for lifecycle, target in LIFECYCLE_TARGETS.items()
        },
        "field_routes": {
            lifecycle.value: {
                field: route.model_dump(mode="json") for field, route in routes.items()
            }
            for lifecycle, routes in LIFECYCLE_FIELD_ROUTES.items()
        },
        "deferred_fields": {
            "milestone": [*MILESTONE_UNRECORDED_FIELDS, *MILESTONE_ACCEPTANCE_FIELDS],
            "batch": [*BATCH_UNRECORDED_FIELDS, BATCH_HEAD_BINDING_FIELD],
            "task": list(TASK_UNRECORDED_FIELDS),
            "reasons": sorted(reason.value for reason in DeferralReason),
        },
        "runs": {
            "mints_run_from": [source.value for source in RunSource],
            "default_run_status": UNCLASSIFIED_RUN_STATUS,
            "succeeded_requires": list(RUN_SUCCESS_REQUIREMENTS),
        },
        "claim_session": {
            "legacy_field": CLAIM_SESSION_LEGACY_FIELD,
            "resolving_extra_field": CLAIM_SESSION_RESOLVING_FIELD,
            "empty_string_imports_as": EMPTY_CLAIM_IMPORTS_AS,
            "canonical_reference": False,
        },
        "intent_native_source": f"intent.{INTENT_NATIVE_SOURCE_KEY}",
    }
