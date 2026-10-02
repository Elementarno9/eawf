"""The effort-mapping re-fit: store an applied revision, or ask the operator.

``runtime.estimation.refit`` re-fits the mapping in force against the actual
ledger. A fit that moves the effort constant by no more than the mapping's
threshold is stored on the estimate ledger as the next revision, with its
fit and a notice. A larger move is stored as a proposal and filed as an
operator decision through the decision path every other question takes; the
mapping in force does not change. A later re-fit adopts the proposal once
the operator approves it, and asks nothing again while the question waits.

The same request sent again answers with what the first one stored.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from eawf.kernel.identity import QualifiedUrn, parse_qualified_urn
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.pending_action import (
    HumanPrincipal,
    PendingAction,
    PendingActionStatus,
)
from eawf.kernel.state.models import ActualSummary
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.ledger import LedgerRecord, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.delivery_approval import publish_commits
from eawf.runtime.daemon.methods.memory import project_subject
from eawf.runtime.daemon.methods.projection import document_path
from eawf.runtime.daemon.methods.question_decision import DecisionOpenParams, open_decision
from eawf.runtime.daemon.native_guard import native_mutator, native_params
from eawf.workflow.estimation.calibration import RefitDisposition, RefitOutcome, refit_mapping
from eawf.workflow.estimation.mapping import EffortMapping
from eawf.workflow.estimation.mapping_revisions import (
    AppliedMapping,
    ProposedMapping,
    RefitAnswer,
    RefitResult,
    applied_mappings,
    mapping_in_force,
    proposal_key,
    proposed_mappings,
    revision_record_key,
)

logger = logging.getLogger(__name__)

#: The request that re-fits the effort-unit mapping.
EFFORT_REFIT_METHOD: Final = "runtime.estimation.refit"

#: The option an operator adopts a proposed revision with.
ADOPT_OPTION: Final = "adopt_refit"


class RefitParams(BaseModel):
    """Params of :data:`EFFORT_REFIT_METHOD`.

    Attributes:
        actor: The principal asking; a person, since only a person answers
            the decision a large move files.
        idempotency_key: The id the request is sent under.
    """

    model_config = ConfigDict(extra="forbid")

    actor: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]
    idempotency_key: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]


def _actuals(records: Sequence[LedgerRecord]) -> tuple[dict[str, ActualSummary], int]:
    """Return the actuals the ledger holds by subject, and how many did not read back.

    An imported row carries its epoch-1 source under ``payload``; a native
    row is the actual itself.
    """
    actuals: dict[str, ActualSummary] = {}
    unreadable = 0
    for record in records:
        source = record.payload.get("payload", record.payload)
        try:
            actuals[record.record_key] = ActualSummary.model_validate(source)
        except ValidationError:
            unreadable += 1
    return actuals, unreadable


def _action(document: dict[str, Any], key: str) -> PendingAction | None:
    """Return the pending action filed under idempotency *key*, if one was."""
    for row in document_rows(document, Epoch2Collection.PENDING_ACTION).values():
        if row.get("idempotency_key") == key:
            return PendingAction.model_validate(row)
    return None


def _repository_subject(document: dict[str, Any]) -> QualifiedUrn:
    """Return the one repository the tree admits, which a re-fit decision is about.

    Raises:
        DaemonValidationError: The tree admits no repository, or several.
    """
    rows = document_rows(document, Epoch2Collection.REPOSITORY)
    if len(rows) != 1:
        raise DaemonValidationError(
            f"validation_failed: identity_not_found: the tree admits {len(rows)} "
            "repositories, so which one a re-fit decision is about is not one row"
        )
    (row,) = rows.values()
    return parse_qualified_urn(row["urn"])


def _answer(result: RefitResult, mapping: EffortMapping, **fields: Any) -> RefitAnswer:
    return RefitAnswer(result=result, revision=mapping.revision, digest=mapping.digest, **fields)


def _store_applied(
    context: Epoch2RootContext,
    args: RefitParams,
    mapping: EffortMapping,
    *,
    notice: str,
    decision_ref: str | None,
    now: datetime,
) -> None:
    """Append *mapping* to the estimate ledger as the revision in force.

    Raises:
        DaemonValidationError: ``refit_raced`` when another revision was
            stored after the re-fit read the one it started from.
    """
    row = AppliedMapping(
        mapping=mapping,
        digest=mapping.digest,
        request_ref=args.idempotency_key,
        actor=args.actor,
        recorded_at=now,
        notice=notice,
        decision_ref=decision_ref,
    )
    with context.session([project_subject(context)]) as session:
        stored = mapping_in_force(
            read_ledger_records(session.ledger_path(Epoch2Collection.ESTIMATE))
        )
        if stored.revision != mapping.revision - 1:
            raise DaemonValidationError(
                f"validation_failed: refit_raced: revision {stored.revision} was stored "
                f"while revision {mapping.revision} was being fitted"
            )
        commit_ledger_append(
            session,
            LedgerRecord(
                collection=Epoch2Collection.ESTIMATE,
                record_key=revision_record_key(mapping.revision, proposed=False),
                status="applied",
                recorded_at=now,
                payload=row.model_dump(mode="json"),
            ),
        )
    logger.info(f"effort_refit applied revision={mapping.revision} decision={decision_ref}")


def _store_proposed(
    context: Epoch2RootContext, args: RefitParams, outcome: RefitOutcome, key: str, now: datetime
) -> None:
    """Append the over-threshold fit to the estimate ledger as a proposal."""
    assert outcome.proposed is not None and outcome.relative_change is not None
    row = ProposedMapping(
        mapping=outcome.proposed,
        relative_change=outcome.relative_change,
        action_key=key,
        request_ref=args.idempotency_key,
        actor=args.actor,
        recorded_at=now,
    )
    with context.session([project_subject(context)]) as session:
        commit_ledger_append(
            session,
            LedgerRecord(
                collection=Epoch2Collection.ESTIMATE,
                record_key=revision_record_key(outcome.proposed.revision, proposed=True),
                status="proposed",
                recorded_at=now,
                payload=row.model_dump(mode="json"),
            ),
        )


def refit(
    ctx: MethodContext, authority: RootAuthority, args: RefitParams, *, now: datetime
) -> RefitAnswer:
    """Re-fit the mapping in force, and store or ask as the outcome requires.

    Args:
        ctx: The daemon context, used to publish a filed decision.
        authority: The fence-cleared tree.
        args: The validated request.
        now: The recording clock.

    Returns:
        What the request did and the revision in force afterwards.
    """
    context = ctx.native_root_context(authority.root)
    path = document_path(authority)
    estimates = read_ledger_records(ledger_path(path, Epoch2Collection.ESTIMATE))
    current = mapping_in_force(estimates)
    for row in applied_mappings(estimates):
        if row.request_ref == args.idempotency_key:
            return _answer(RefitResult.APPLIED, current, notice=row.notice)
    document = read_document(path)
    settled, declined = _settle_proposals(context, args, document, estimates, current, now=now)
    if settled is not None:
        return settled
    actuals, unreadable = _actuals(read_ledger_records(ledger_path(path, Epoch2Collection.ACTUAL)))
    outcome = refit_mapping(current, actuals, today=now.date(), unreadable=unreadable)
    if outcome.disposition is RefitDisposition.NOT_DUE:
        return _answer(RefitResult.NOT_DUE, current, outcome=outcome)
    assert outcome.proposed is not None, "a due re-fit always fits a revision"
    if outcome.disposition is RefitDisposition.APPLY:
        assert outcome.notice is not None
        _store_applied(
            context, args, outcome.proposed, notice=outcome.notice, decision_ref=None, now=now
        )
        return _answer(
            RefitResult.APPLIED, outcome.proposed, outcome=outcome, notice=outcome.notice
        )
    key = proposal_key(current, outcome.proposed)
    if key in declined:
        return _answer(RefitResult.DECLINED, current, outcome=outcome)
    _store_proposed(context, args, outcome, key, now)
    return _ask(ctx, context, args, document, outcome, current, key, now=now)


def _settle_proposals(
    context: Epoch2RootContext,
    args: RefitParams,
    document: dict[str, Any],
    estimates: Sequence[LedgerRecord],
    current: EffortMapping,
    *,
    now: datetime,
) -> tuple[RefitAnswer | None, set[str]]:
    """Adopt a proposal the operator approved, or wait on one still asked.

    Returns:
        The answer when a proposal decided the request -- adopted, or still
        waiting -- else ``None``; and the decision keys the operator refused.
    """
    declined: set[str] = set()
    for proposal in proposed_mappings(estimates):
        if proposal.mapping.revision != current.revision + 1:
            continue
        action = _action(document, proposal.action_key)
        if action is None:
            continue
        if action.status is not PendingActionStatus.SEALED:
            waiting = _answer(RefitResult.AWAITING_DECISION, current, action_ref=str(action.urn))
            return waiting, declined
        if action.selected_option_id != ADOPT_OPTION:
            declined.add(proposal.action_key)
            continue
        notice = f"effort mapping revision {proposal.mapping.revision} adopted by {action.id}"
        _store_applied(
            context, args, proposal.mapping, notice=notice, decision_ref=str(action.urn), now=now
        )
        adopted = _answer(
            RefitResult.APPLIED, proposal.mapping, notice=notice, action_ref=str(action.urn)
        )
        return adopted, declined
    return None, declined


def _ask(
    ctx: MethodContext,
    context: Epoch2RootContext,
    args: RefitParams,
    document: dict[str, Any],
    outcome: RefitOutcome,
    current: EffortMapping,
    key: str,
    *,
    now: datetime,
) -> RefitAnswer:
    """File the operator decision an over-threshold fit asks, and answer with it."""
    assert outcome.question is not None, "an over-threshold fit always carries its question"
    commit = open_decision(
        context,
        DecisionOpenParams(
            urn=_repository_subject(document),
            idempotency_key=key,
            requested_by=HumanPrincipal(principal_kind="human", principal_id=args.actor),
            question=outcome.question,
            options=outcome.options,
            recommended_option_id=outcome.recommended_option_id,
            recommendation_rationale=outcome.recommendation_rationale,
            terms=outcome.terms,
            actor=args.actor,
        ),
        now=now,
    )
    publish_commits(ctx, commit.envelopes)
    logger.info(f"effort_refit decision action={commit.answer.action_ref} key={key}")
    return _answer(
        RefitResult.DECISION_OPENED,
        current,
        outcome=outcome,
        action_ref=commit.answer.action_ref,
        decision=commit.answer.model_dump(mode="json"),
    )


@native_mutator(EFFORT_REFIT_METHOD)
async def _refit(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Re-fit the effort-unit mapping of the addressed tree."""
    args = native_params(RefitParams, params)
    answer = await asyncio.to_thread(refit, ctx, authority, args, now=datetime.now(UTC))
    return answer.model_dump(mode="json")


__all__ = [
    "EFFORT_REFIT_METHOD",
    "RefitParams",
    "refit",
]
