"""Reconciling an observed merge, and revising what an operator accepts.

Two verbs live here rather than beside the integration verbs, because
:mod:`eawf.runtime.daemon.methods.delivery` is already at the size the
module-length lint admits and a waiver would buy nothing a name does not.
Both verbs are about what happens after a delivery exists: what the host
did with it, and what the operator makes of it.

``runtime.delivery.reconcile_merge``: what the host actually did.

The verb reads the Batch from the tree and is handed the one half no
native record holds: what reading the target branch back found. Nothing
in the tree talks to a host, so the observation is presented -- and
presenting it buys a caller nothing, because a landing is decided by the
Batch's own pinned head appearing among the commits the observation
reports, not by the observation saying so. A refusal must name its cause
or the Batch would go back to work with nothing to act on, and an
observation that settles nothing files a line and leaves the Batch
exactly where it was.

The decision is filed under a key derived from the Batch, its pinned head
and the outcome, so the same observation presented twice is one standing
line rather than a growing pile of identical answers.

``runtime.delivery.request_acceptance_repair``: the operator asked for changes.

The verb reads the sealed PendingAction and the Milestone's bundle
revisions from the tree, resolves every ``EVD-####`` the repaired journey
cites against the evidence already recorded, and appends the successor
revision. The revision the operator read is not touched: it is an
appended line, the successor carries its digest, and the chain refuses to
validate if it were ever rewritten.

Neither verb moves a lifecycle record. Performing the Batch's return to
work, its completion, or the Milestone's acceptance belongs to the
lifecycle machine; these verbs produce the proof those moves consult.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.delivery.acceptance import (
    AcceptanceBundleLedger,
    AcceptanceStepOutcome,
    EvidenceView,
    MilestoneAcceptanceBundle,
)
from eawf.kernel.delivery.integration import IdempotencyKey
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import PrincipalKey
from eawf.kernel.state.epoch2.batch import DeliveryBatch
from eawf.kernel.state.epoch2.pending_action import PendingAction
from eawf.kernel.state.epoch2.urns import AnyEntityUrn, BatchUrn, MilestoneUrn
from eawf.kernel.state.epoch2.values import ExactRevisionBinding
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.ledger import (
    LedgerRecord,
    effective_records,
    read_ledger_records,
)
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.native_guard import REPO_ROOT_PARAM, native_mutator
from eawf.workflow.delivery.acceptance import (
    AcceptanceRefusal,
    AcceptanceRefusedError,
    acceptance_evidence,
    evidence_view,
    request_repair,
)
from eawf.workflow.integration.reconcile import (
    HostMergeObservation,
    MergeReconciliation,
    ReconciliationRefusal,
    ReconciliationRefusedError,
    reconcile_merge,
    reconciliation_record_key,
)

logger = logging.getLogger(__name__)


#: The verb that decides what a read-back of the target branch established.
DELIVERY_RECONCILE_MERGE_METHOD: Final = "runtime.delivery.reconcile_merge"

#: The verb that opens the successor revision a repair request earns.
DELIVERY_ACCEPTANCE_REPAIR_METHOD: Final = "runtime.delivery.request_acceptance_repair"

#: The ledger-line key prefix an acceptance bundle revision is filed under.
BUNDLE_KEY_PREFIX: Final = "MAB-"

#: The status a reconciliation line records: the outcome it established.
#: A line whose status is ``unknown`` is the record of a question still
#: open, which is exactly what a surface listing unresolved merges reads.
RECONCILIATION_UNRESOLVED: Final = "unknown"


class MergeReconcileParams(BaseModel):
    """Params of :data:`DELIVERY_RECONCILE_MERGE_METHOD`.

    Attributes:
        urn: The Batch whose merge is in flight.
        actor: Who asked.
        idempotency_key: The client's name for this request.
        observation: What reading the target branch back found. Presented
            because nothing in the tree talks to a host; it decides
            nothing on its own, because a landing is the Batch's own
            pinned head being among the commits it reports.
    """

    model_config = ConfigDict(extra="forbid")

    urn: BatchUrn
    actor: PrincipalKey
    idempotency_key: IdempotencyKey
    observation: HostMergeObservation


class MergeReconcileAnswer(BaseModel):
    """What one reconciliation pass answers with.

    Attributes:
        batch_ref: The Batch that was reconciled.
        outcome: What the read-back established.
        from_status: The status the Batch stood in.
        to_status: The status the decision lands on; unchanged when the
            outcome is unknown.
        path: The registered verbs the decision walks, in order.
        observed_facts: The facts the outcome supplies to the registry.
        cause_code: The named cause of a refusal, or ``None``.
        awaited_head_sha: The commit an unresolved decision is waiting to
            see on the target branch.
        matched_head_sha: The commit a landing was matched on.
        resolved: Whether the read-back settled what the host did.
        ambiguity: How a surface labels the status this lands on.
        record_key: The ledger key the decision was filed under.
        reason: One sentence an operator reads.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_ref: str
    outcome: str
    from_status: str
    to_status: str
    path: tuple[str, ...] = ()
    observed_facts: tuple[str, ...] = ()
    cause_code: str | None = None
    awaited_head_sha: str | None = None
    matched_head_sha: str | None = None
    resolved: bool
    ambiguity: str | None = None
    record_key: str
    reason: str


class AcceptanceRepairParams(BaseModel):
    """Params of :data:`DELIVERY_ACCEPTANCE_REPAIR_METHOD`.

    Attributes:
        urn: The Milestone whose bundle is being revised.
        actor: Who asked.
        idempotency_key: The client's name for this request.
        approval_ref: The sealed PendingAction the operator answered. It
            is read from the tree, never presented, so a caller cannot
            supply its own repair request.
        steps: What the repaired acceptance journey shows.
        accepted_binding: The exact tree the successor is taken on.
    """

    model_config = ConfigDict(extra="forbid")

    urn: MilestoneUrn
    actor: PrincipalKey
    idempotency_key: IdempotencyKey
    approval_ref: AnyEntityUrn
    steps: tuple[AcceptanceStepOutcome, ...] = Field(min_length=1)
    accepted_binding: ExactRevisionBinding


class AcceptanceRepairAnswer(BaseModel):
    """What one repair request answers with.

    Attributes:
        milestone_ref: The Milestone whose bundle was revised.
        from_revision: The revision the operator read.
        to_revision: The successor the request opened.
        prior_digest: The digest of the revision that was read, unchanged
            by this request.
        successor_digest: The digest of the revision that was opened.
        evidence_keys: Every ``EVD-####`` the successor cites, each of
            which the evidence view was found to hold.
        reason: One sentence an operator reads.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    milestone_ref: str
    from_revision: int
    to_revision: int
    prior_digest: str
    successor_digest: str
    evidence_keys: tuple[str, ...] = ()
    reason: str


def _refused(code: ReconciliationRefusal | AcceptanceRefusal, detail: str) -> DaemonValidationError:
    """Return the wire form of one refusal."""
    return DaemonValidationError(f"validation_failed: {code.value}: {detail}")


def _validated[T: BaseModel](model: type[T], params: dict[str, Any]) -> T:
    """Validate request params, dropping the key the fence already used.

    Raises:
        DaemonValidationError: The request does not parse. The pydantic
            detail is reduced to field paths so the refusal never repeats
            a submitted value into a log.
    """
    try:
        return model.model_validate(
            {key: value for key, value in params.items() if key != REPO_ROOT_PARAM}
        )
    except ValidationError as error:
        fields = sorted({".".join(str(part) for part in row["loc"]) for row in error.errors()})
        raise DaemonValidationError(
            f"validation_failed: schema_validation_failed: check {', '.join(fields)}"
        ) from error


def _batch_of(session: RootSession, urn: BatchUrn) -> DeliveryBatch:
    """Return the Batch as the tree holds it, from either storage tier.

    A merged or abandoned Batch is compacted out of the document into its
    ledger, so both are read: a reader that looked only in the document
    would answer "no such Batch" for exactly the Batches whose merge is
    being asked about.

    Raises:
        DaemonValidationError: No readable Batch is filed under *urn*.
    """
    wanted = str(urn)
    row = document_rows(session.read_document(), Epoch2Collection.BATCH).get(urn.entity_key)
    payloads = [] if row is None else [row]
    if not payloads:
        lines = read_ledger_records(session.ledger_path(Epoch2Collection.BATCH))
        payloads = [
            item.payload for item in effective_records(lines) if item.payload.get("urn") == wanted
        ]
    for payload in payloads:
        try:
            return DeliveryBatch.model_validate(payload)
        except ValidationError:
            continue
    raise _refused(
        ReconciliationRefusal.OBSERVATION_MISBOUND,
        f"the tree holds no readable batch {urn.entity_key} to reconcile",
    )


def _append_reconciliation(session: RootSession, decision: MergeReconciliation) -> None:
    """File one reconciliation as a line of the Batch ledger."""
    commit_ledger_append(
        session,
        LedgerRecord(
            collection=Epoch2Collection.BATCH,
            record_key=reconciliation_record_key(decision),
            status=decision.outcome.value,
            recorded_at=decision.observed_at,
            payload=decision.model_dump(mode="json"),
        ),
    )


def _reconcile_answer(decision: MergeReconciliation) -> MergeReconcileAnswer:
    """Build the answer one reconciliation pass returns."""
    label = decision.label
    return MergeReconcileAnswer(
        batch_ref=str(decision.batch_ref),
        outcome=decision.outcome.value,
        from_status=decision.from_status.value,
        to_status=decision.to_status.value,
        path=tuple(item.value for item in decision.path),
        observed_facts=tuple(item.value for item in decision.observed_facts),
        cause_code=None if decision.cause is None else decision.cause.code,
        awaited_head_sha=decision.awaited_head_sha,
        matched_head_sha=decision.matched_head_sha,
        resolved=decision.resolved,
        ambiguity=None if label is None else label.value,
        record_key=reconciliation_record_key(decision),
        reason=decision.reason,
    )


def reconcile_batch_merge(
    context: Epoch2RootContext, args: MergeReconcileParams
) -> MergeReconcileAnswer:
    """Decide what one read-back of the target branch says about a merging Batch.

    Args:
        context: The native context of the tree the Batch lives in.
        args: The validated request.

    Returns:
        The outcome, the registered path it takes, and where it was filed.

    Raises:
        DaemonValidationError: The tree holds no readable Batch, the Batch
            is not merging, the observation is of another Batch or
            branch, the Batch pinned no exact head, or a refusal names no
            cause.
    """
    with context.session([str(args.urn)]) as session:
        batch = _batch_of(session, args.urn)
    try:
        decision = reconcile_merge(batch, args.observation)
    except ReconciliationRefusedError as error:
        raise DaemonValidationError(f"validation_failed: {error}") from error
    with context.session([str(args.urn)]) as session:
        _append_reconciliation(session, decision)
    logger.info(
        f"reconcile_merge batch={args.urn.entity_key} outcome={decision.outcome.value} "
        f"to={decision.to_status.value} resolved={decision.resolved}"
    )
    return _reconcile_answer(decision)


@native_mutator(DELIVERY_RECONCILE_MERGE_METHOD)
async def _reconcile_merge(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Reconcile one Batch's in-flight merge against what the branch holds."""
    args = _validated(MergeReconcileParams, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(reconcile_batch_merge, context, args)
    return answer.model_dump(mode="json")


def _bundle_record_key(milestone_ref: MilestoneUrn, revision: int) -> str:
    """Return the Milestone-ledger key one bundle revision is filed under."""
    return f"{BUNDLE_KEY_PREFIX}{revision:04d}-{milestone_ref.entity_key}"


def _read_bundle_ledger(path: Path, milestone_ref: MilestoneUrn) -> AcceptanceBundleLedger:
    """Return one Milestone's bundle revisions as the ledger holds them.

    The lines are read in filed order, which is the order the revisions
    were opened in; the chain check in
    :class:`~eawf.kernel.delivery.acceptance.AcceptanceBundleLedger`
    refuses any other.

    Raises:
        pydantic.ValidationError: A filed line is not a bundle, or the
            chain of digests is broken.
    """
    wanted = str(milestone_ref)
    payloads = [
        item.payload
        for item in read_ledger_records(path)
        if item.record_key.startswith(BUNDLE_KEY_PREFIX)
        and item.payload.get("milestone_ref") == wanted
    ]
    return AcceptanceBundleLedger.model_validate({"milestone_ref": wanted, "bundles": payloads})


def _append_bundle(session: RootSession, bundle: MilestoneAcceptanceBundle) -> None:
    """File one bundle revision as a line of the Milestone ledger."""
    commit_ledger_append(
        session,
        LedgerRecord(
            collection=Epoch2Collection.MILESTONE,
            record_key=_bundle_record_key(bundle.milestone_ref, bundle.revision),
            status=f"revision-{bundle.revision}",
            recorded_at=bundle.sealed_at,
            payload=bundle.model_dump(mode="json"),
        ),
    )


def _read_evidence(path: Path) -> EvidenceView:
    """Return the view over every ``EVD-####`` the tree has recorded.

    Raises:
        DaemonValidationError: A filed line under an evidence key does not
            read as an evidence row, so the view would silently answer
            for fewer keys than the tree holds.
    """
    payloads = [
        item.payload for item in read_ledger_records(path) if item.record_key.startswith("EVD-")
    ]
    try:
        return evidence_view(payloads)
    except ValidationError as error:
        reasons = sorted({str(row["msg"]) for row in error.errors()})
        raise _refused(
            AcceptanceRefusal.EVIDENCE_UNHELD,
            f"the evidence ledger does not read back: {'; '.join(reasons)}",
        ) from error


def _action_of(session: RootSession, ref: AnyEntityUrn) -> PendingAction:
    """Return the sealed PendingAction the tree holds under *ref*.

    Raises:
        DaemonValidationError: The tree holds no such action, or the row
            filed under it is not a readable pending action.
    """
    row = document_rows(session.read_document(), Epoch2Collection.PENDING_ACTION).get(
        ref.entity_key
    )
    if row is None:
        raise _refused(
            AcceptanceRefusal.APPROVAL_UNSEALED,
            f"the tree holds no {ref.entity_key}, so nobody asked for this repair",
        )
    try:
        return PendingAction.model_validate(row)
    except ValidationError as error:
        reasons = sorted({str(item["msg"]) for item in error.errors()})
        raise _refused(
            AcceptanceRefusal.APPROVAL_UNSEALED,
            f"{ref.entity_key} does not read as a pending action: {'; '.join(reasons)}",
        ) from error


def _repair_answer(
    *,
    prior: MilestoneAcceptanceBundle,
    successor: MilestoneAcceptanceBundle,
    cited: Sequence[str],
) -> AcceptanceRepairAnswer:
    """Build the answer one repair request returns."""
    return AcceptanceRepairAnswer(
        milestone_ref=str(successor.milestone_ref),
        from_revision=prior.revision,
        to_revision=successor.revision,
        prior_digest=prior.digest(),
        successor_digest=successor.digest(),
        evidence_keys=tuple(cited),
        reason=(
            f"revision {prior.revision} of {successor.milestone_ref.entity_key} is unchanged and "
            f"revision {successor.revision} supersedes it"
        ),
    )


def open_acceptance_repair(
    context: Epoch2RootContext, args: AcceptanceRepairParams, *, now: datetime
) -> AcceptanceRepairAnswer:
    """Open the successor bundle revision an operator's repair request earns.

    Args:
        context: The native context of the tree the Milestone lives in.
        args: The validated request.
        now: The stamp the successor is sealed at.

    Returns:
        Both revisions' digests, and the evidence the successor cites.

    Raises:
        DaemonValidationError: The tree holds no sealed action, the action
            was not answered with the repair option, the Milestone has no
            revision to supersede, or the successor cites evidence the
            tree does not hold.
    """
    with context.session([str(args.urn)]) as session:
        action = _action_of(session, args.approval_ref)
        milestone_ledger = session.ledger_path(Epoch2Collection.MILESTONE)
        ledger = _read_bundle_ledger(milestone_ledger, args.urn)
        view = _read_evidence(session.ledger_path(Epoch2Collection.EVIDENCE))
    prior = ledger.head
    try:
        revised = request_repair(
            ledger,
            action=action,
            steps=args.steps,
            accepted_binding=args.accepted_binding,
            at=now,
        )
        successor = revised.bundles[-1]
        cited = acceptance_evidence(view, successor)
    except AcceptanceRefusedError as error:
        raise DaemonValidationError(f"validation_failed: {error}") from error
    assert prior is not None, "a repair always supersedes the revision it was asked about"
    with context.session([str(args.urn)]) as session:
        _append_bundle(session, successor)
    logger.info(
        f"request_acceptance_repair milestone={args.urn.entity_key} "
        f"from_revision={prior.revision} to_revision={successor.revision} cited={len(cited)}"
    )
    return _repair_answer(prior=prior, successor=successor, cited=cited)


@native_mutator(DELIVERY_ACCEPTANCE_REPAIR_METHOD)
async def _request_acceptance_repair(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Open the successor acceptance bundle revision and file it."""
    args = _validated(AcceptanceRepairParams, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(open_acceptance_repair, context, args, now=datetime.now(UTC))
    return answer.model_dump(mode="json")


__all__ = [
    "BUNDLE_KEY_PREFIX",
    "DELIVERY_ACCEPTANCE_REPAIR_METHOD",
    "DELIVERY_RECONCILE_MERGE_METHOD",
    "RECONCILIATION_UNRESOLVED",
    "AcceptanceRepairAnswer",
    "AcceptanceRepairParams",
    "MergeReconcileAnswer",
    "MergeReconcileParams",
    "open_acceptance_repair",
    "reconcile_batch_merge",
]
