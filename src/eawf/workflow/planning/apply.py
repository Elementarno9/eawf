"""The three plan-revision transactions, and the one commit they share.

Applying a plan creates three kinds of record at once, so the only safe
shape is one write. The whole successor document -- the new Milestone,
every Batch, every PLANNED Task, and the revision flipped to ``APPLIED``
-- is built in memory and handed to a single atomic document replace.
There is no ordering between the Milestone landing and its Tasks landing,
because they land in the same bytes; a partial apply is therefore not a
state the tree can be left in by a refusal, a crash between two writes,
or a lock lost midway.

What remains after the document replace is the journal, not the plan: the
WAL record, the idempotency receipt and the firehose row, written in the
transaction's own order. A crash between the replace and the receipt
leaves a fully applied tree with no receipt, and the retry then meets an
``APPLIED`` revision, which has no apply edge -- so it refuses and writes
nothing rather than applying twice. A crash before the replace leaves the
intent and nothing else, and the retry applies normally.

Reference resolution and drift are decided before the first byte. The
document is read under the locks the plan's own URNs name, the four bound
inputs are compared against it, and a mismatch returns a typed refusal
from a function that has not been given anything to write with.

A plan revision has no URN of its own, so the locks come from the plan it
carries. Finding them needs the record, and reading the record needs the
generation -- so the key is resolved in an unlocked read first and every
decision is then retaken under the locks. The unlocked read chooses which
locks to take and nothing else: a body cannot change after submission, and
a record that moved in the gap fails the compare-and-swap re-check.
"""

from __future__ import annotations

import copy
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from pydantic import ConfigDict

from eawf.kernel.identity import QualifiedUrn
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.spec.common import grade_criterion_grounding
from eawf.kernel.state.canonical_sequence import CanonicalSequenceAllocator
from eawf.kernel.state.enums import DecisionStatus, StoreKind
from eawf.kernel.state.epoch2.base import Epoch2Model, PrincipalKey
from eawf.kernel.state.epoch2.batch import BatchStatus, DeliveryBatch
from eawf.kernel.state.epoch2.milestone import Milestone, MilestoneStatus
from eawf.kernel.state.epoch2.plan_revision import (
    PlanApproval,
    PlanBody,
    PlanRevision,
    PlanRevisionKey,
    PlanRevisionStatus,
    plan_content_digest,
)
from eawf.kernel.state.epoch2.task import Task, TaskStatus
from eawf.kernel.state.epoch2.urns import AnyEntityUrn
from eawf.kernel.state.epoch2.values import EntityOrigin, OwnerPrincipal
from eawf.kernel.state.io import state_version
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.append import append_json_line
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.paths import store_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.logging.state_leak import state_leak_refusal
from eawf.runtime.daemon.epoch2_recovery import (
    canonical_params_digest,
    read_idempotency_receipt,
    record_idempotency_receipt,
)
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession
from eawf.runtime.daemon.epoch2_transaction import (
    CANONICAL_SEQUENCE_KEY,
    CommittedTransaction,
    MutationReceipt,
)
from eawf.runtime.daemon.wal import WalRecord, mark_applied, mark_fsynced, write_pending
from eawf.surfaces.cli.errors import UserError
from eawf.surfaces.cli.errors import ValidationError as CliValidationError
from eawf.workflow.evidence._io import load_state
from eawf.workflow.evidence.measured_contract import resolve_contract_citation
from eawf.workflow.planning.lenses import blocking_findings, run_plan_lenses
from eawf.workflow.planning.revision import (
    ObservedPlanWorld,
    PlanRefusal,
    PlanRefusalCode,
    PlanRevisionAdvanced,
    advance_plan_revision,
    detect_plan_drift,
)

logger = logging.getLogger(__name__)


#: The collection plan revisions are stored under, at the document tier.
PLAN_REVISION_COLLECTION: Final = Epoch2Collection.PLAN_REVISION

#: Version of the plan-event payload a firehose row carries.
PLAN_EVENT_SCHEMA_VERSION: Final = "1"

#: The file name the firehose path resolver is anchored on. An epoch-2
#: tree keeps its firehose at the epoch-1 location.
_TREE_ANCHOR_FILENAME: Final = "state.json"

#: The document field a repository row records its current head under.
_HEAD_FIELD: Final = "head_sha"

#: The compare-and-swap token a freshly drafted revision carries. A
#: submission is the first write of its key, so there is nothing earlier
#: for it to have moved from.
_DRAFT_REVISION: Final = 1

#: The origin every natively planned record carries.
_NATIVE_ORIGIN: Final = EntityOrigin(kind="native", mapping_basis="native", confidence="exact")


class PlanRevisionProposal(Epoch2Model):
    """The strict create document one planner submits.

    Status, digest, bindings and approval are absent by construction. A
    proposal that could name its own bindings could name the world it
    wished it had been written over, so the daemon observes all four from
    the locked document instead.

    Attributes:
        key: The ``PRV-####`` key the revision is filed under.
        author: Who emitted the proposal.
        body: What an apply of it would materialise.
        parent_key: The revision this one repairs, when it repairs one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: PlanRevisionKey
    author: OwnerPrincipal
    body: PlanBody
    parent_key: PlanRevisionKey | None = None


@dataclass(frozen=True, slots=True)
class _PlanWrite:
    """One decided plan mutation, before any byte of it is written.

    Attributes:
        document: The document as read, kept so the leak scrub has
            something to diff the proposal against.
        new_document: The whole document the commit leaves behind.
        record: The revision as it is after the edge.
        event_name: The event the commit appends.
        revision_before: The revision the record moved from.
        milestone_ref: The plan's Milestone, which is the event's subject.
        binding_refs: The proofs the move was taken against.
    """

    document: dict[str, Any]
    new_document: dict[str, Any]
    record: PlanRevision
    event_name: str
    revision_before: int
    milestone_ref: QualifiedUrn
    binding_refs: tuple[str, ...]


def _row_field(row: Any, name: str) -> Any:
    """Return one field of a stored row, or ``None`` when it has none.

    Sibling rows are read raw rather than validated: a drift predicate
    about a Track must not fail because an unrelated repository row is
    malformed, and every caller compares the result against a known value,
    so an unreadable row reads as not matching.

    Args:
        row: A stored row, or whatever the document held in its place.
        name: The field to read.

    Returns:
        The field's value, or ``None``.
    """
    return row.get(name) if isinstance(row, dict) else None


def _int_field(row: Any, name: str) -> int | None:
    """Return one integer field of a stored row, or ``None``."""
    value = _row_field(row, name)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str_field(row: Any, name: str) -> str | None:
    """Return one string field of a stored row, or ``None``."""
    value = _row_field(row, name)
    return value if isinstance(value, str) else None


def plan_lock_urns(body: PlanBody) -> tuple[str, ...]:
    """Return every entity URN an operation on *body* must hold a lock on.

    Args:
        body: The plan content.

    Returns:
        The Track, the Milestone, every Batch and every Task the plan
        names, as canonical URN strings. The session sorts and de-dupes
        them, so the order here is only for readability.
    """
    return (
        str(body.milestone.primary_track_ref),
        str(body.milestone_urn),
        *(str(batch.urn) for batch in body.batches),
        *(str(task.urn) for task in body.tasks),
    )


def grade_plan_grounding(body: PlanBody) -> PlanBody:
    """Return *body* with every criterion graded from the evidence it carries.

    A planner emits criteria without a grounding grade, which defaults to
    ``assumed``; grading them here, where every plan producer's proposal
    is admitted, means a criterion bound to a probe gate reaches approval
    graded ``measured`` while one with no probe stays ``assumed`` and is
    refused. See :func:`~eawf.kernel.spec.common.grade_criterion_grounding`.

    Args:
        body: The plan content the producer emitted.

    Returns:
        The graded body, or *body* itself when no criterion changes grade.
    """
    tasks = tuple(
        task.model_copy(
            update={"criteria": tuple(grade_criterion_grounding(c) for c in task.criteria)}
        )
        for task in body.tasks
    )
    if tasks == body.tasks:
        return body
    return body.model_copy(update={"tasks": tasks})


def validate_plan_proposal(
    document: dict[str, Any], *, proposal: PlanRevisionProposal, at: UtcDatetime
) -> PlanRevisionAdvanced | PlanRefusal:
    """Return the VALIDATED successor *proposal* earns, or why it earns none.

    Everything here reads the document and writes nothing, which is what
    lets a worker's proposal be judged by the same rules that admit an
    operator's submission. The two differ in what they may do with the
    verdict, not in how the verdict is reached: this function decides,
    and only :func:`submit_plan_revision` may record what it decided.
    A proposal naming a ``parent_key`` has that parent's stored body
    resolved and handed to the lens runner, so the semantic-diff lens can
    compare the repair against what it repairs.

    Args:
        document: The locked generation document the bindings are
            observed from.
        proposal: The strict create document the planner emitted.
        at: When the submission happened, stamped on the successor.

    Returns:
        The validated revision beside the event it emits, or the typed
        refusal. A refusal names the exact guard that produced it. The
        recorded body is the proposal's graded by
        :func:`grade_plan_grounding`.
    """
    body = grade_plan_grounding(proposal.body)
    if _stored_revision(document, key=proposal.key) is not None:
        return PlanRefusal(
            code=PlanRefusalCode.REVISION_CONFLICT,
            guard="plan_revision_key_free",
            detail=f"the document already holds a plan revision keyed {proposal.key!r}",
            remediation="Submit the repair as a child revision under a new key.",
        )
    world = observe_plan_world(document, body=body)
    if world.track_revision is None or world.policy_revision is None:
        return PlanRefusal(
            code=PlanRefusalCode.IDENTITY_NOT_FOUND,
            guard="primary_track_resolved",
            detail=(
                "the plan names Track "
                f"{body.milestone.primary_track_ref.entity_key!r}, which the document "
                "does not hold as a readable row"
            ),
            remediation="Create the Track before planning a Milestone under it.",
        )
    missing = sorted(ref for ref, head in world.heads.items() if head is None)
    if missing:
        return PlanRefusal(
            code=PlanRefusalCode.IDENTITY_NOT_FOUND,
            guard="repository_head_readable",
            detail=f"these repositories record no head to bind: {', '.join(missing)}",
            remediation="Record each repository's head before planning against it.",
        )
    parent_body: PlanBody | None = None
    if proposal.parent_key is not None:
        parent_record = _stored_revision(document, key=proposal.parent_key)
        if parent_record is None:
            return PlanRefusal(
                code=PlanRefusalCode.IDENTITY_NOT_FOUND,
                guard="parent_plan_revision_resolved",
                detail=(
                    f"the plan names parent revision {proposal.parent_key!r}, which the "
                    "document does not hold as a readable row"
                ),
                remediation="Submit the repair against a parent revision the document holds.",
            )
        parent_body = parent_record.body
    blocking = blocking_findings(run_plan_lenses(body, parent=parent_body))
    if blocking:
        finding = blocking[0]
        return PlanRefusal(
            code=PlanRefusalCode.TRANSITION_GUARD_FAILED,
            guard=f"plan_lens_{finding.lens.value}",
            detail=finding.message,
            remediation=finding.remediation,
        )
    draft = PlanRevision.model_validate(
        {
            "key": proposal.key,
            "revision": _DRAFT_REVISION,
            "status": PlanRevisionStatus.DRAFT.value,
            "author": proposal.author.model_dump(mode="json"),
            "created_at": at.isoformat(),
            "updated_at": at.isoformat(),
            "content_digest": plan_content_digest(body),
            "base_state_revision": world.track_revision,
            "policy_revision": world.policy_revision,
            "head_bindings": list(_head_bindings(world)),
            "body": body.model_dump(mode="json"),
            "parent_key": proposal.parent_key,
        }
    )
    return advance_plan_revision(draft, to=PlanRevisionStatus.VALIDATED, at=at)


def observe_plan_world(document: dict[str, Any], *, body: PlanBody) -> ObservedPlanWorld:
    """Return what *document* says about the inputs *body* binds.

    Args:
        document: The locked generation document.
        body: The plan content, which names the Track and repositories to
            look at.

    Returns:
        The observation. Every field a row cannot answer reads ``None``,
        which every drift predicate treats as a mismatch.
    """
    track_row = document_rows(document, Epoch2Collection.TRACK).get(
        body.milestone.primary_track_ref.entity_key
    )
    repositories = document_rows(document, Epoch2Collection.REPOSITORY)
    heads = {
        str(batch.repository_ref): _str_field(
            repositories.get(batch.repository_ref.entity_key), _HEAD_FIELD
        )
        for batch in body.batches
    }
    return ObservedPlanWorld(
        track_revision=_int_field(track_row, "revision"),
        track_status=_str_field(track_row, "status"),
        policy_revision=_int_field(_row_field(track_row, "policy"), "revision"),
        heads=heads,
        claimed_keys=_claimed_keys(document, body=body),
    )


def _claimed_keys(document: dict[str, Any], *, body: PlanBody) -> tuple[str, ...]:
    """Return the locators the document already holds that the plan creates."""
    wanted: dict[Epoch2Collection, tuple[str, ...]] = {
        Epoch2Collection.MILESTONE: (body.milestone_urn.entity_key,),
        Epoch2Collection.BATCH: tuple(batch.urn.entity_key for batch in body.batches),
        Epoch2Collection.TASK: tuple(task.urn.entity_key for task in body.tasks),
    }
    return tuple(
        f"{collection.value}/{key}"
        for collection, keys in wanted.items()
        for key in keys
        if key in document_rows(document, collection)
    )


def _head_bindings(world: ObservedPlanWorld) -> tuple[dict[str, str], ...]:
    """Return the observed heads as binding payloads, in repository order."""
    return tuple(
        {"repository_ref": ref, "head_sha": head}
        for ref, head in sorted(world.heads.items())
        if head is not None
    )


def _planned_milestone(body: PlanBody, *, at: UtcDatetime) -> Milestone:
    """Return the PLANNED Milestone *body* materialises."""
    spec = body.milestone.model_dump(mode="json")
    return Milestone.model_validate(
        {
            **spec,
            "uid": str(uuid.uuid5(uuid.NAMESPACE_URL, str(body.milestone_urn))),
            "urn": str(body.milestone_urn),
            "origin": _NATIVE_ORIGIN.model_dump(mode="json"),
            "revision": 1,
            "created_at": at.isoformat(),
            "updated_at": at.isoformat(),
            "status": MilestoneStatus.PLANNED.value,
        }
    )


def _planned_batches(body: PlanBody, *, at: UtcDatetime) -> tuple[DeliveryBatch, ...]:
    """Return the PLANNED Batches *body* materialises, in declared order."""
    return tuple(
        DeliveryBatch.model_validate(
            {
                "uid": str(uuid.uuid5(uuid.NAMESPACE_URL, str(batch.urn))),
                "key": batch.urn.entity_key,
                "urn": str(batch.urn),
                "origin": _NATIVE_ORIGIN.model_dump(mode="json"),
                "revision": 1,
                "created_at": at.isoformat(),
                "updated_at": at.isoformat(),
                "milestone_ref": str(body.milestone_urn),
                "repository_ref": str(batch.repository_ref),
                "task_refs": [str(task.urn) for task in body.tasks if task.batch_ref == batch.urn],
                "status": BatchStatus.PLANNED.value,
            }
        )
        for batch in body.batches
    )


def _planned_tasks(body: PlanBody, *, at: UtcDatetime) -> tuple[Task, ...]:
    """Return the PLANNED Tasks *body* materialises, in declared order."""
    return tuple(
        Task.model_validate(
            {
                "uid": str(uuid.uuid5(uuid.NAMESPACE_URL, str(task.urn))),
                "key": task.urn.entity_key,
                "urn": str(task.urn),
                "origin": _NATIVE_ORIGIN.model_dump(mode="json"),
                "revision": 1,
                "created_at": at.isoformat(),
                "updated_at": at.isoformat(),
                "batch_ref": str(task.batch_ref),
                "due_scope": str(body.milestone_urn),
                "priority": task.priority.value,
                "intent": task.intent,
                "contract_revision": 1,
                "criteria": [item.model_dump(mode="json") for item in task.criteria],
                "status": TaskStatus.PLANNED.value,
            }
        )
        for task in body.tasks
    )


def materialise_plan(
    document: dict[str, Any], *, record: PlanRevision, at: UtcDatetime
) -> dict[str, Any]:
    """Return the whole document an applied plan leaves behind.

    The successor is built by deep-copying and editing rather than by
    mutating in place, because the leak scrub diffs the two payloads and
    an in-place edit would leave it comparing one payload with itself.

    Args:
        document: The locked document as read.
        record: The revision, already advanced to ``APPLIED``.
        at: When the apply happened.

    Returns:
        The successor document, carrying the Milestone, its Batches, its
        PLANNED Tasks and the applied revision.

    Raises:
        ValidationError: A materialised record does not satisfy its own
            model, which refuses before anything is written.
    """
    body = record.body
    new_document = copy.deepcopy(document)
    milestone = _planned_milestone(body, at=at)
    new_document.setdefault(Epoch2Collection.MILESTONE.value, {})[milestone.key] = (
        milestone.model_dump(mode="json")
    )
    batches = new_document.setdefault(Epoch2Collection.BATCH.value, {})
    for batch in _planned_batches(body, at=at):
        batches[batch.key] = batch.model_dump(mode="json")
    tasks = new_document.setdefault(Epoch2Collection.TASK.value, {})
    for task in _planned_tasks(body, at=at):
        tasks[task.key] = task.model_dump(mode="json")
    new_document.setdefault(PLAN_REVISION_COLLECTION.value, {})[record.key] = record.model_dump(
        mode="json"
    )
    return new_document


def _stored_revision(document: dict[str, Any], *, key: str) -> PlanRevision | None:
    """Return the stored revision *key* addresses, or ``None``.

    Args:
        document: The document to read.
        key: The plan-revision key.

    Returns:
        The validated revision, or ``None`` when the document holds no
        such row or the row does not validate.
    """
    row = document_rows(document, PLAN_REVISION_COLLECTION).get(key)
    if row is None:
        return None
    try:
        return PlanRevision.model_validate(row)
    except ValueError:
        logger.warning(f"_stored_revision rejected a stored row plan_revision_key={key!r}")
        return None


def _stale(record: PlanRevision, *, expected_revision: int) -> PlanRefusal | None:
    """Return the compare-and-swap refusal when the record has moved on."""
    if record.revision == expected_revision:
        return None
    return PlanRefusal(
        code=PlanRefusalCode.REVISION_CONFLICT,
        guard="plan_revision_cas",
        detail=(
            f"plan revision {record.key} is at revision {record.revision} but the request "
            f"expects {expected_revision}"
        ),
        remediation="Re-read the revision and retry against its current revision.",
        revision=record.revision,
    )


def resolve_plan_body(context: Epoch2RootContext, *, key: str) -> PlanBody | None:
    """Return the plan body of a stored revision, read without locks.

    The result decides which locks the operation takes and nothing else.
    Every decision is retaken under those locks, and a body is immutable
    once submitted, so a stale answer here costs at most a lock on a
    record the compare-and-swap re-check then refuses.

    Args:
        context: The native context of the addressed root.
        key: The plan-revision key.

    Returns:
        The stored body, or ``None`` when the key resolves to nothing.

    Raises:
        NativeAuthorityRequiredError: The tree left epoch 2.
        MigrationDualAuthorityError: The tree's select is not whole.
    """
    authority = context.require_selected_generation()
    assert authority.target is not None, "an epoch-2 answer always carries its target"
    assert authority.generation_id is not None, "an epoch-2 answer always names a generation"
    path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    if not path.exists():
        return None
    record = _stored_revision(read_document(path), key=key)
    return None if record is None else record.body


def submit_plan_revision(
    context: Epoch2RootContext,
    *,
    proposal: PlanRevisionProposal,
    actor: PrincipalKey,
    idempotency_key: str,
    at: UtcDatetime,
) -> CommittedTransaction | PlanRefusal:
    """Record one proposal as a VALIDATED revision, or refuse it.

    Args:
        context: The native context of the addressed root.
        proposal: The strict create document.
        actor: Who asked, as an immutable qualified principal key.
        idempotency_key: The client's name for this request.
        at: When the submission happened.

    Returns:
        The commit, or the typed refusal. A refusal writes nothing.

    Raises:
        NativeAuthorityRequiredError: The tree left epoch 2.
        MigrationDualAuthorityError: The tree's select is not whole.
        LockTimeout: A lock stayed held past the lock timeout.
    """
    body = proposal.body
    digest = _request_digest(
        "submit", {"proposal": proposal.model_dump(mode="json"), "actor": actor}
    )
    with context.session(plan_lock_urns(body)) as session:
        replayed = _replayed(context, idempotency_key=idempotency_key, request_digest=digest)
        if replayed is not None:
            return replayed
        document = session.read_document()
        outcome = validate_plan_proposal(document, proposal=proposal, at=at)
        if isinstance(outcome, PlanRefusal):
            return outcome
        return _commit(
            context,
            session=session,
            write=_revision_only_write(document, outcome=outcome, revision_before=_DRAFT_REVISION),
            actor=actor,
            idempotency_key=idempotency_key,
            request_digest=digest,
            at=at,
        )


@dataclass(frozen=True, slots=True)
class _CitationResolvers:
    """The two grounding-citation resolvers one v1 state read builds.

    Attributes:
        contract_is_resolvable: Forwarded to
            :func:`~eawf.workflow.planning.revision.ungrounded_approval_criteria`
            as ``contract_is_resolvable``.
        decision_is_resolvable: Forwarded to the same call as
            ``decision_is_resolvable``.
    """

    contract_is_resolvable: Callable[[str], bool] | None
    decision_is_resolvable: Callable[[str], bool] | None


def _refuse_every_citation(_ref: str) -> bool:
    """Fail-closed resolver: refuse every citation.

    Used when the tree's ``state.json`` exists but could not be read --
    broken JSON, a schema-invalid payload, or a race with a concurrent
    writer. Such a store might resolve the reference or might not, and
    admitting a citation on a store this call could not read would let
    corruption forge grounding, so every citation refuses instead.
    """
    return False


def _build_citation_resolvers(context: Epoch2RootContext) -> _CitationResolvers:
    """Return the contract and Decision resolvers for *context*'s v1 state.

    A promoted :class:`~eawf.kernel.spec.measured_contract.MeasuredContract`
    and a recorded :class:`~eawf.kernel.state.models.Decision` are both
    registered in the v1 ``state.json`` this epoch-2 tree sits beside
    (``PLAN-025``'s evidence path), read once here rather than once per
    citation.

    A tree carrying no such file -- one that predates either store, or a
    canary that never provisioned one -- has nothing to honestly refuse a
    citation against, so both resolvers are ``None``, which skips the
    check entirely. A tree whose ``state.json`` exists but fails to load
    is a different case: the store was provisioned and something is
    wrong with it, so both resolvers become :func:`_refuse_every_citation`
    -- failing closed rather than treating an unreadable store the same
    as an absent one.

    Args:
        context: The native context of the addressed root.

    Returns:
        The two resolvers, both ``None`` when *state_path* is absent, or
        both :func:`_refuse_every_citation` when it exists but does not load.
    """
    state_path = context.identity.tree_root / "state.json"
    if not state_path.exists():
        return _CitationResolvers(contract_is_resolvable=None, decision_is_resolvable=None)
    try:
        state = load_state(state_path)
    except UserError, CliValidationError, OSError, ValueError:
        return _CitationResolvers(
            contract_is_resolvable=_refuse_every_citation,
            decision_is_resolvable=_refuse_every_citation,
        )

    def contract_resolves(ref: str) -> bool:
        try:
            resolve_contract_citation(state, ref)
        except UserError:
            return False
        return True

    def decision_resolves(ref: str) -> bool:
        # A superseded, reversed or obsolete Decision no longer stands
        # behind the risk it once accepted, so only a live one resolves.
        decision = state.decisions.get(ref)
        return (
            decision is not None
            and decision.status is DecisionStatus.ACTIVE
            and decision.superseded_by is None
        )

    return _CitationResolvers(
        contract_is_resolvable=contract_resolves, decision_is_resolvable=decision_resolves
    )


def approve_plan_revision(
    context: Epoch2RootContext,
    *,
    key: str,
    expected_revision: int,
    action_ref: AnyEntityUrn,
    approved_by: OwnerPrincipal,
    actor: PrincipalKey,
    idempotency_key: str,
    at: UtcDatetime,
) -> CommittedTransaction | PlanRefusal:
    """Seal a human principal's approval onto one VALIDATED revision.

    The receipt is built from what the document stores, never from what
    the request claims: the digest is recomputed from the stored body and
    the bindings are copied off the stored record, so approving is an act
    of consent to exactly what is filed and to nothing else.

    A criterion graded ``measured`` or ``accepted-risk`` is also checked
    here against the v1 state beside this tree -- see
    :func:`_build_citation_resolvers` -- so a citation naming a contract
    nobody promoted, or a Decision nobody recorded or that no longer stands
    (superseded, reversed, obsolete), refuses the same as a
    criterion still graded assumed.

    Args:
        context: The native context of the addressed root.
        key: The plan-revision key.
        expected_revision: The compare-and-swap token the caller read.
        action_ref: The PendingAction the approval is sealed as.
        approved_by: The human principal approving.
        actor: Who asked, as an immutable qualified principal key.
        idempotency_key: The client's name for this request.
        at: When the approval happened.

    Returns:
        The commit, or the typed refusal. A refusal writes nothing.

    Raises:
        NativeAuthorityRequiredError: The tree left epoch 2.
        MigrationDualAuthorityError: The tree's select is not whole.
        LockTimeout: A lock stayed held past the lock timeout.
        ValidationError: The approval is not sealed by a human principal
            or does not address a PendingAction, refused before any lock.
    """
    body = resolve_plan_body(context, key=key)
    if body is None:
        return _missing(key)
    digest = _request_digest(
        "approve",
        {
            "key": key,
            "expected_revision": expected_revision,
            "action_ref": str(action_ref),
            "approved_by": approved_by.model_dump(mode="json"),
            "actor": actor,
        },
    )
    with context.session(plan_lock_urns(body)) as session:
        replayed = _replayed(context, idempotency_key=idempotency_key, request_digest=digest)
        if replayed is not None:
            return replayed
        document = session.read_document()
        record = _stored_revision(document, key=key)
        if record is None:
            return _missing(key)
        stale = _stale(record, expected_revision=expected_revision)
        if stale is not None:
            return stale
        approval = PlanApproval(
            action_ref=action_ref,
            approved_by=approved_by,
            approved_at=at,
            content_digest=plan_content_digest(record.body),
            base_state_revision=record.base_state_revision,
            policy_revision=record.policy_revision,
            head_bindings=record.head_bindings,
        )
        resolvers = _build_citation_resolvers(context)
        outcome = advance_plan_revision(
            record,
            to=PlanRevisionStatus.APPROVED,
            at=at,
            approval=approval,
            contract_is_resolvable=resolvers.contract_is_resolvable,
            decision_is_resolvable=resolvers.decision_is_resolvable,
        )
        if isinstance(outcome, PlanRefusal):
            return outcome
        return _commit(
            context,
            session=session,
            write=_revision_only_write(document, outcome=outcome, revision_before=record.revision),
            actor=actor,
            idempotency_key=idempotency_key,
            request_digest=digest,
            at=at,
        )


def apply_plan_revision(
    context: Epoch2RootContext,
    *,
    key: str,
    expected_revision: int,
    actor: PrincipalKey,
    idempotency_key: str,
    at: UtcDatetime,
) -> CommittedTransaction | PlanRefusal:
    """Materialise one APPROVED revision, or refuse it having written nothing.

    Args:
        context: The native context of the addressed root.
        key: The plan-revision key.
        expected_revision: The compare-and-swap token the caller read.
        actor: Who asked, as an immutable qualified principal key.
        idempotency_key: The client's name for this request.
        at: When the apply happened.

    Returns:
        The commit, or the typed refusal. A refusal writes nothing, and a
        retry of a request this root already committed returns the
        original receipt without an envelope.

    Raises:
        NativeAuthorityRequiredError: The tree left epoch 2.
        MigrationDualAuthorityError: The tree's select is not whole.
        LockTimeout: A lock stayed held past the lock timeout.
    """
    body = resolve_plan_body(context, key=key)
    if body is None:
        return _missing(key)
    digest = _request_digest(
        "apply", {"key": key, "expected_revision": expected_revision, "actor": actor}
    )
    with context.session(plan_lock_urns(body)) as session:
        replayed = _replayed(context, idempotency_key=idempotency_key, request_digest=digest)
        if replayed is not None:
            return replayed
        document = session.read_document()
        record = _stored_revision(document, key=key)
        if record is None:
            return _missing(key)
        stale = _stale(record, expected_revision=expected_revision)
        if stale is not None:
            return stale
        outcome = advance_plan_revision(record, to=PlanRevisionStatus.APPLIED, at=at)
        if isinstance(outcome, PlanRefusal):
            return outcome
        drifted = detect_plan_drift(
            outcome.record, world=observe_plan_world(document, body=record.body)
        )
        if drifted is not None:
            logger.info(f"apply_plan_revision refused guard={drifted.guard} key={key!r}")
            return drifted
        write = _PlanWrite(
            document=document,
            new_document=materialise_plan(document, record=outcome.record, at=at),
            record=outcome.record,
            event_name=outcome.event_name,
            revision_before=record.revision,
            milestone_ref=record.body.milestone_urn,
            binding_refs=_binding_refs(outcome.record),
        )
        return _commit(
            context,
            session=session,
            write=write,
            actor=actor,
            idempotency_key=idempotency_key,
            request_digest=digest,
            at=at,
        )


def _missing(key: str) -> PlanRefusal:
    """Return the refusal of an operation on a revision nobody stored."""
    return PlanRefusal(
        code=PlanRefusalCode.IDENTITY_NOT_FOUND,
        guard="plan_revision_resolved",
        detail=f"the document holds no readable plan revision keyed {key!r}",
        remediation="Submit the plan revision before driving it.",
    )


def _binding_refs(record: PlanRevision) -> tuple[str, ...]:
    """Return the proofs a move on *record* was taken against."""
    return () if record.approval is None else (str(record.approval.action_ref),)


def _revision_only_write(
    document: dict[str, Any], *, outcome: PlanRevisionAdvanced, revision_before: int
) -> _PlanWrite:
    """Return the write of an edge that touches the revision row alone."""
    new_document = copy.deepcopy(document)
    new_document.setdefault(PLAN_REVISION_COLLECTION.value, {})[outcome.record.key] = (
        outcome.record.model_dump(mode="json")
    )
    return _PlanWrite(
        document=document,
        new_document=new_document,
        record=outcome.record,
        event_name=outcome.event_name,
        revision_before=revision_before,
        milestone_ref=outcome.record.body.milestone_urn,
        binding_refs=_binding_refs(outcome.record),
    )


def _request_digest(verb: str, payload: dict[str, Any]) -> str:
    """Return the digest that decides whether two requests are the same one.

    The verb is part of the digest, so one key naming a submit and an
    apply is a reuse rather than a replay.

    Args:
        verb: The operation the request asks for.
        payload: Every parameter that changes what the request leaves
            behind, in JSON mode.

    Returns:
        The sha256 hex digest of the sorted-key encoding.
    """
    return canonical_params_digest({"verb": verb, **payload})


def _replayed(
    context: Epoch2RootContext, *, idempotency_key: str, request_digest: str
) -> CommittedTransaction | PlanRefusal | None:
    """Return the commit this key already earned, if it earned one.

    Args:
        context: The native context of the addressed root.
        idempotency_key: The client's key.
        request_digest: The digest of the parameters asking for it.

    Returns:
        The original receipt with no envelope, so a retry republishes
        nothing; the refusal of a key that already committed other
        parameters; or ``None`` when the key has committed nothing here.

    Raises:
        ValueError: The stored receipt cannot be read, so whether this
            request already ran is unknown and answering "no" would run
            it twice.
    """
    stored = read_idempotency_receipt(
        context, namespaced_key=context.idempotency_key(idempotency_key)
    )
    if stored is None:
        return None
    if stored.params_digest != request_digest:
        return PlanRefusal(
            code=PlanRefusalCode.IDEMPOTENCY_CONFLICT,
            guard="idempotency_key_reused",
            detail=(
                f"idempotency key {idempotency_key!r} already committed a request with "
                "different parameters, so this one is not a retry of it"
            ),
            remediation="Retry with the original parameters, or choose a new idempotency key.",
        )
    return CommittedTransaction(
        receipt=MutationReceipt.model_validate(stored.receipt), envelope=None, replayed=True
    )


def _commit(
    context: Epoch2RootContext,
    *,
    session: RootSession,
    write: _PlanWrite,
    actor: PrincipalKey,
    idempotency_key: str,
    request_digest: str,
    at: UtcDatetime,
) -> CommittedTransaction | PlanRefusal:
    """Write the intent, the document, the receipt, the row and the mark.

    The document replace is the single atomic effect: everything the write
    creates lands in one ``os.replace`` or none of it does. The journal
    entries around it exist to make a crash recoverable, never to stage a
    half-applied plan.

    Args:
        context: The native context of the addressed root.
        session: The open, locked session.
        write: The decided mutation.
        actor: Who asked.
        idempotency_key: The client's key.
        request_digest: The digest a retry of this request is matched
            against.
        at: When the mutation happened.

    Returns:
        The commit receipt beside the envelope the caller publishes, or a
        refusal when the write would add free text carrying a leak shape.
    """
    leak = state_leak_refusal(write.document, write.new_document)
    if leak is not None:
        logger.warning(f"_commit refused for leaks plan_revision_key={write.record.key!r}")
        return PlanRefusal(
            code=PlanRefusalCode.SCHEMA_VALIDATION_FAILED,
            guard="state_leak_scrub",
            detail=leak,
            remediation="Remove the flagged text from the plan and re-submit it.",
            revision=write.revision_before,
        )
    allocator = CanonicalSequenceAllocator.recover(
        workspace_key=write.milestone_ref.workspace_key,
        high_water_mark=_high_water_mark(write.document),
    )
    with allocator.transaction() as sequences:
        sequence = sequences.allocate()
        write.new_document[CANONICAL_SEQUENCE_KEY] = sequence
        envelope = _plan_envelope(write, actor=actor, sequence=sequence, at=at, key=idempotency_key)
        wal_record = WalRecord(
            record_id=uuid.uuid4().hex,
            envelope=envelope,
            idempotency_key=idempotency_key,
            written_at=at,
            before_state_version=state_version(write.document),
            after_state_version=state_version(write.new_document),
            state_path=str(session.document_path),
        )
        receipt = MutationReceipt(
            event_name=write.event_name,
            entity_ref=write.milestone_ref,
            revision_before=write.revision_before,
            revision_after=write.record.revision,
            canonical_sequence=sequence,
            event_id=envelope.id,
            idempotency_key=idempotency_key,
            occurred_at=at,
            wal_record_id=wal_record.record_id,
        )
        write_pending(context.wal_dir, wal_record)
        session.write_document(write.new_document)
        mark_applied(context.wal_dir, wal_record.record_id)
        record_idempotency_receipt(
            context,
            namespaced_key=context.idempotency_key(idempotency_key),
            params_digest=request_digest,
            receipt=receipt.model_dump(mode="json"),
            recorded_at=at,
        )
        append_json_line(_firehose_path(context), envelope.model_dump_json())
        mark_fsynced(context.wal_dir, wal_record.record_id)
        logger.info(
            f"_commit plan event={receipt.event_name} sequence={sequence} "
            f"root={context.identity.root_id}"
        )
        return CommittedTransaction(receipt=receipt, envelope=envelope)


def _high_water_mark(document: dict[str, Any]) -> int:
    """Return the committed sequence *document* records.

    Raises:
        ValueError: The document holds a non-integer where the high-water
            mark belongs, so the next sequence cannot be derived.
    """
    recorded = document.get(CANONICAL_SEQUENCE_KEY, 0)
    if not isinstance(recorded, int) or isinstance(recorded, bool):
        raise ValueError(
            f"document {CANONICAL_SEQUENCE_KEY} holds a {type(recorded).__name__}, not an integer"
        )
    return recorded


def _plan_envelope(
    write: _PlanWrite, *, actor: PrincipalKey, sequence: int, at: UtcDatetime, key: str
) -> Envelope:
    """Return the one firehose row a plan mutation appends."""
    event_id = f"evt-{uuid.uuid4().hex}"
    body = write.record.body
    return Envelope(
        id=event_id,
        kind=StoreKind.EVENT,
        scope_id=str(write.milestone_ref),
        created_at=at,
        summary=f"{write.event_name} {write.record.key} -> {write.record.status.value}",
        payload={
            "schema_version": PLAN_EVENT_SCHEMA_VERSION,
            "name": write.event_name,
            "event_id": event_id,
            "occurred_at": at.isoformat(),
            "plan_revision_key": write.record.key,
            "to_status": write.record.status.value,
            "revision_before": write.revision_before,
            "revision_after": write.record.revision,
            "actor_ref": actor,
            "milestone_ref": str(body.milestone_urn),
            "batch_refs": [str(batch.urn) for batch in body.batches],
            "task_refs": [str(task.urn) for task in body.tasks],
            "citation_refs": [str(item.finding_ref) for item in body.citations],
            "binding_refs": list(write.binding_refs),
            "idempotency_key": key,
            "canonical_sequence": sequence,
        },
    )


def _firehose_path(context: Epoch2RootContext) -> Path:
    """Return the declared firehose of one root.

    Raises:
        UndeclaredPathError: The commit policy declares no row for it.
    """
    anchor = context.identity.tree_root / _TREE_ANCHOR_FILENAME
    return context.declared_path(store_path(anchor, StoreKind.EVENT))


__all__ = [
    "PLAN_EVENT_SCHEMA_VERSION",
    "PLAN_REVISION_COLLECTION",
    "PlanRevisionProposal",
    "apply_plan_revision",
    "approve_plan_revision",
    "materialise_plan",
    "observe_plan_world",
    "plan_lock_urns",
    "resolve_plan_body",
    "submit_plan_revision",
    "validate_plan_proposal",
]
