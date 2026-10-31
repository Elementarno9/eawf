"""``runtime.delivery.waive_criterion``: waive one criterion of a promoted Task.

A Task's criteria are set when it is promoted, and a claimed Task never goes back to
DRAFT, so a criterion that stopped being provable after the Task was claimed -- its
verify command names a test file deleted with the code it tested -- would otherwise hold
the Task's Batch open for good. This verb sets that one criterion's ``waiver_reason``,
and nothing else: the prover then counts the criterion as satisfied without running it,
as the planning layer already does for a waived row's gates.

It is allowed from PLANNED through READY_TO_INTEGRATE and needs a reason, as a guarded
edge recording ``reason_recorded`` would. Only an operator principal waives, so a worker
cannot clear its own failing criterion, and the tree's ``verify.waiver_mode`` governs it:
``disabled`` refuses every waiver and ``C`` requires a standing decision behind it. A
criterion graded accepted-risk is refused, since a waiver and an accepted risk are
disjoint exits. A criterion graded measured only
because it runs a probe gate is regraded assumed, since a waived row runs no gate. A
retry under the same idempotency key replays its answer, and the same waiver sent under
another key changes nothing.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.errors.lifecycle import WAIVER_MODE_DISABLED
from eawf.kernel.spec.common import CriterionGrounding
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import (
    OPERATOR_PRINCIPAL_PREFIX,
    PrincipalKey,
    StrictPositiveInt,
)
from eawf.kernel.state.epoch2.decision import DecisionKey
from eawf.kernel.state.epoch2.task import Task, TaskStatus
from eawf.kernel.state.epoch2.transitions import DenialCode
from eawf.kernel.state.epoch2.urns import TaskUrn
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.ledger import effective_records, read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_recovery import publish_projection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import (
    TransactionRefusalCode,
    TransactionRefusedError,
    commit_row_write,
)
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.delivery import (
    IdempotencyKey,
    file_keyed_answer,
    keyed_answer,
)
from eawf.runtime.daemon.native_guard import native_mutator, native_params
from eawf.workflow.verify.readiness import load_active_verify_block

logger = logging.getLogger(__name__)

#: The verb that waives one criterion of a promoted Task.
DELIVERY_WAIVE_CRITERION_METHOD: Final = "runtime.delivery.waive_criterion"

#: The firehose name of a committed waiver.
CRITERION_WAIVED_EVENT: Final = "domain.task.criterion_waived"

#: The statuses a Task's criterion may be waived in: promoted, and not yet terminal.
WAIVABLE_STATUSES: Final = frozenset(
    {
        TaskStatus.PLANNED,
        TaskStatus.CLAIMED,
        TaskStatus.RUNNING,
        TaskStatus.READY_TO_INTEGRATE,
    }
)


class WaiveCriterionParams(BaseModel):
    """Params of :data:`DELIVERY_WAIVE_CRITERION_METHOD`.

    Attributes:
        urn: The Task.
        criterion_id: The criterion to waive.
        reason: Why the criterion no longer needs proving.
        expected_revision: The revision the caller read the Task at.
        actor: Who asked; an operator principal.
        idempotency_key: The client's name for this request.
        decision_ref: The standing decision the waiver leans on; required under waiver
            mode ``C``.
    """

    model_config = ConfigDict(extra="forbid")

    urn: TaskUrn
    criterion_id: Annotated[str, Field(min_length=1, max_length=64)]
    reason: Annotated[str, Field(min_length=1, max_length=500)]
    expected_revision: StrictPositiveInt
    actor: PrincipalKey
    idempotency_key: IdempotencyKey
    decision_ref: DecisionKey | None = Field(default=None, exclude_if=lambda value: value is None)


class WaiveCriterionAnswer(BaseModel):
    """What one waiver answers with.

    Attributes:
        task_ref: The Task.
        criterion_id: The criterion.
        waived: Whether this request changed the criterion; ``False`` when it already
            carried the same waiver.
        revision: The Task's revision after the request.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_ref: str
    criterion_id: str
    waived: bool
    revision: int


def _waived_criteria(task: Task, criterion_id: str, reason: str) -> list[dict[str, Any]] | None:
    """Return the Task's criteria with *criterion_id* waived, or ``None`` when it already is.

    Raises:
        DaemonValidationError: The Task holds no such criterion, or it is graded
            accepted-risk.
    """
    rows = [criterion.model_dump(mode="json") for criterion in task.criteria]
    row = next((row for row in rows if row["id"] == criterion_id), None)
    if row is None:
        known = ", ".join(criterion.id for criterion in task.criteria)
        raise DaemonValidationError(
            f"validation_failed: criterion_unknown: task {task.key} holds no criterion "
            f"{criterion_id}; it holds {known}"
        )
    if row.get("accepted_risk_decision_ref") is not None:
        raise DaemonValidationError(
            f"validation_failed: criterion_accepted_risk: {criterion_id} of task {task.key} "
            "is an accepted risk, which a waiver does not replace"
        )
    if row.get("waiver_reason") == reason:
        return None
    row["waiver_reason"] = reason
    if row.get("grounding") == CriterionGrounding.MEASURED.value and not row.get("contract_refs"):
        row["grounding"] = CriterionGrounding.ASSUMED.value
    return rows


def waive_criterion(
    context: Epoch2RootContext, args: WaiveCriterionParams, *, now: datetime
) -> tuple[WaiveCriterionAnswer, Envelope | None]:
    """Waive one criterion, replaying the answer a retry under the same key earned.

    Args:
        context: The native context of the tree the Task lives in.
        args: The validated request.
        now: When the waiver is recorded.

    Returns:
        The answer, beside the firehose row of the write, or ``None`` when nothing moved.

    Raises:
        TransactionRefusedError: The Task is not held, stands at another revision or in a
            status no criterion is waived in, the reason is blank, the actor is not an
            operator, the waiver mode refuses the waiver, or the decision is not held.
        DaemonValidationError: The criterion is unknown or an accepted risk, or the key
            already answered other parameters.
    """
    params = args.model_dump(mode="json")
    method = DELIVERY_WAIVE_CRITERION_METHOD
    replayed = keyed_answer(context, method=method, key=args.idempotency_key, params=params)
    if replayed is not None:
        return WaiveCriterionAnswer.model_validate(replayed), None
    answer, envelope = _waive(context, args, now=now)
    file_keyed_answer(
        context,
        method=method,
        key=args.idempotency_key,
        params=params,
        answer=answer.model_dump(mode="json"),
        at=now,
    )
    return answer, envelope


def _refused(urn: TaskUrn, code: str, detail: str, remediation: str) -> TransactionRefusedError:
    """Return the guard refusal a waiver its policy does not admit is answered with."""
    return TransactionRefusedError(
        code=TransactionRefusalCode.TRANSITION_GUARD_FAILED,
        detail=f"{code}: {detail}",
        entity_ref=str(urn),
        remediation=remediation,
    )


def _admit(context: Epoch2RootContext, args: WaiveCriterionParams) -> None:
    """Refuse a waiver its actor or the tree's waiver mode does not admit.

    Raises:
        TransactionRefusedError: The reason is blank, the actor is not an operator
            principal, the mode is ``disabled``, or mode ``C`` sees no decision.
    """
    urn = args.urn
    if not args.reason.strip():
        raise _refused(
            urn,
            DenialCode.TRANSITION_REASON_MISSING.value,
            "a waiver needs a reason",
            "Say why the criterion no longer needs proving.",
        )
    if not args.actor.startswith(OPERATOR_PRINCIPAL_PREFIX):
        raise _refused(
            urn,
            "waiver_operator_required",
            f"{args.actor} is not an operator principal, and only an operator waives",
            "Only an operator waives: run it again with --actor set to an operator "
            "principal (OP- followed by digits, such as OP-0001).",
        )
    anchor = context.identity.tree_root.parent
    block = load_active_verify_block(repo_root=anchor)
    mode = "B" if block is None else block.waiver_mode
    if mode == "disabled":
        raise _refused(
            urn,
            WAIVER_MODE_DISABLED,
            "verify.waiver_mode is disabled, so no criterion is waived",
            "Prove the criterion, or change verify.waiver_mode.",
        )
    if mode == "C" and args.decision_ref is None:
        raise _refused(
            urn,
            "waiver_decision_required",
            "verify.waiver_mode C requires a decision behind every waiver",
            "Name the standing decision the waiver leans on.",
        )


def _waive(
    context: Epoch2RootContext, args: WaiveCriterionParams, *, now: datetime
) -> tuple[WaiveCriterionAnswer, Envelope | None]:
    """Waive one criterion as :func:`waive_criterion` describes, judged under the Task's lock."""
    urn = args.urn
    _admit(context, args)
    with context.session([urn]) as session:
        if args.decision_ref is not None and not any(
            line.record_key == args.decision_ref
            for line in effective_records(
                read_ledger_records(session.ledger_path(Epoch2Collection.DECISION))
            )
        ):
            raise TransactionRefusedError(
                code=TransactionRefusalCode.IDENTITY_NOT_FOUND,
                detail=f"the decision ledger holds no decision {args.decision_ref}",
                entity_ref=str(urn),
                remediation="Name a decision the tree holds.",
            )
        row = document_rows(session.read_document(), Epoch2Collection.TASK).get(urn.entity_key)
        if row is None:
            raise TransactionRefusedError(
                code=TransactionRefusalCode.IDENTITY_NOT_FOUND,
                detail=f"no live task {urn.entity_key} is held",
                entity_ref=str(urn),
                remediation="Name a promoted Task that has not finished.",
            )
        task = Task.model_validate(row)
        if task.revision != args.expected_revision:
            raise TransactionRefusedError(
                code=TransactionRefusalCode.REVISION_CONFLICT,
                detail=f"task {task.key} stands at revision {task.revision}",
                entity_ref=str(urn),
                remediation="Read the Task again and retry at its revision.",
                revision=task.revision,
            )
        if task.status not in WAIVABLE_STATUSES:
            raise TransactionRefusedError(
                code=TransactionRefusalCode.ILLEGAL_TRANSITION,
                detail=f"a criterion of a {task.status.value} task is not waived",
                entity_ref=str(urn),
                remediation="Waive a criterion between PLANNED and READY_TO_INTEGRATE.",
            )
        criteria = _waived_criteria(task, args.criterion_id, args.reason)
        if criteria is None:
            answer = WaiveCriterionAnswer(
                task_ref=str(urn),
                criterion_id=args.criterion_id,
                waived=False,
                revision=task.revision,
            )
            return answer, None
        waived = Task.model_validate(
            {
                **task.model_dump(mode="json"),
                "criteria": criteria,
                "revision": task.revision + 1,
                "updated_at": now,
            }
        )
        envelope = commit_row_write(
            session,
            collection=Epoch2Collection.TASK,
            record_key=waived.key,
            row=waived.model_dump(mode="json"),
            event_name=CRITERION_WAIVED_EVENT,
            event_fields={
                "entity_ref": str(waived.urn),
                "to_status": waived.status.value,
                "revision_after": waived.revision,
                "actor": args.actor,
                "criterion_id": args.criterion_id,
                "reason": args.reason,
                "decision_ref": args.decision_ref,
            },
            compaction=None,
            now=now,
        )
    logger.info(f"waive_criterion task={waived.key} criterion={args.criterion_id}")
    answer = WaiveCriterionAnswer(
        task_ref=str(urn), criterion_id=args.criterion_id, waived=True, revision=waived.revision
    )
    return answer, envelope


@native_mutator(DELIVERY_WAIVE_CRITERION_METHOD)
async def _waive_criterion(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Waive one criterion of a promoted Task."""
    args = native_params(WaiveCriterionParams, params)
    context = ctx.native_root_context(authority.root)
    answer, envelope = await asyncio.to_thread(
        waive_criterion, context, args, now=datetime.now(UTC)
    )
    if envelope is not None:
        publish_projection(ctx.bus, envelope)
    return answer.model_dump(mode="json")


__all__ = [
    "CRITERION_WAIVED_EVENT",
    "DELIVERY_WAIVE_CRITERION_METHOD",
    "OPERATOR_PRINCIPAL_PREFIX",
    "WAIVABLE_STATUSES",
    "WaiveCriterionAnswer",
    "WaiveCriterionParams",
    "waive_criterion",
]
