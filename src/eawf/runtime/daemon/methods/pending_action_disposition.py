"""One principal's snooze of a pending action, and who a pending action is addressed to.

``runtime.pending_action.snooze``: a principal hides a waiting question from their own
attention until a deadline. It answers nothing and nobody else's view moves: the snooze
is that principal's own row beside the shared status, so the question's revision stays
where it was and an answer another principal is about to give is not made stale by it.

``runtime.pending_action.assign``: a principal addresses a waiting question to one
principal, or back to everyone. Assignment transfers no authority -- anyone eligible may
still answer -- but it moves whose attention count the question lands in, so it is one
revision of the record.

Both are sent at the revision the caller was shown; a question that moved since is
refused with ``revision_conflict`` carrying the revision it stands at, so the caller
reloads rather than acting on a question it never saw. Both commit through the same
transaction step every pending-action commit does -- one WAL intent, one document
rewrite, one receipt and one firehose row -- and a refusal writes nothing. The same
request retried under its idempotency key returns the first receipt.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from eawf.kernel.delivery.integration import IdempotencyKey
from eawf.kernel.identity import EntityKind
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import PrincipalKey, StrictPositiveInt
from eawf.kernel.state.epoch2.pending_action import PendingAction, PendingActionStatus
from eawf.kernel.state.epoch2.urns import AnyEntityUrn
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import (
    TransactionRefusalCode,
    TransactionRefusedError,
    _replayed_receipt,
)
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.delivery_approval import (
    ActionCommitRequest,
    ApprovalCommit,
    action_answer,
    action_at,
    commit_action,
    publish_commits,
)
from eawf.runtime.daemon.native_guard import native_mutator, native_params

logger = logging.getLogger(__name__)

#: The verb a principal snoozes a waiting question for themselves through.
ACTION_SNOOZE_METHOD: Final = "runtime.pending_action.snooze"

#: The verb a waiting question is addressed to one principal, or everyone, through.
ACTION_ASSIGN_METHOD: Final = "runtime.pending_action.assign"

#: The event a principal's own snooze is recorded under. The status does not move, so it
#: names its own event rather than claiming a transition.
ACTION_SNOOZED_EVENT: Final = f"resolution.{Epoch2Collection.PENDING_ACTION.value}.snoozed"

#: The event a question is re-addressed under.
ACTION_ASSIGNED_EVENT: Final = f"resolution.{Epoch2Collection.PENDING_ACTION.value}.assigned"


class _ActionParams(BaseModel):
    """The address every disposition of one pending action is sent under.

    Attributes:
        urn: The pending action.
        expected_revision: The revision the caller was shown.
        idempotency_key: The client's name for this request.
        actor: Who acts; a snooze is recorded as theirs alone.
    """

    model_config = ConfigDict(extra="forbid")

    urn: AnyEntityUrn
    expected_revision: StrictPositiveInt
    idempotency_key: IdempotencyKey
    actor: PrincipalKey

    @model_validator(mode="after")
    def _addresses_a_pending_action(self) -> Self:
        """Refuse a URN of another kind.

        Raises:
            ValueError: The URN addresses a record that is not a pending action.
        """
        if self.urn.kind is not EntityKind.PENDING_ACTION:
            raise ValueError(f"urn addresses a {self.urn.kind.value}, not a pending action")
        return self


class ActionSnoozeParams(_ActionParams):
    """Params of :data:`ACTION_SNOOZE_METHOD`.

    Attributes:
        snooze_until: When the actor's snooze lapses.
    """

    snooze_until: UtcDatetime


class ActionAssignParams(_ActionParams):
    """Params of :data:`ACTION_ASSIGN_METHOD`.

    Attributes:
        assignee: The principal the question is addressed to; ``None`` addresses it to
            every eligible principal.
    """

    assignee: PrincipalKey | None = None


def _refused(
    action: PendingAction, urn: object, code: TransactionRefusalCode, detail: str
) -> TransactionRefusedError:
    """Return the wire refusal of one disposition, carrying the revision it stands at."""
    return TransactionRefusedError(
        code=code,
        detail=detail,
        entity_ref=str(urn),
        remediation="Re-read the question and act on it as it now stands.",
        revision=action.revision,
    )


def _dispose(
    context: Epoch2RootContext,
    args: _ActionParams,
    *,
    operation: Literal["snooze", "assign"],
    event_name: str,
    change: Callable[[PendingAction], PendingAction],
    reason: str,
    now: datetime,
) -> ApprovalCommit:
    """Commit one disposition of a waiting question at the revision the caller was shown.

    Raises:
        TransactionRefusedError: The tree holds no such question, it is sealed, it moved
            past the revision the caller was shown, or the change breaks a record rule.
            Nothing was written.
    """
    request = ActionCommitRequest(
        urn=args.urn,
        idempotency_key=args.idempotency_key,
        actor=args.actor,
        operation=operation,
        detail=args.model_dump(mode="json", exclude={"urn", "idempotency_key", "actor"}),
    )
    with context.session([str(args.urn)]) as session:
        replayed = _replayed_receipt(context, request=request)
        action = action_at(session.read_document(), args.urn)
        if replayed is not None:
            return ApprovalCommit(
                answer=action_answer(
                    action, urn=args.urn, bundle=None, receipt=replayed, reason=reason
                )
            )
        if action.status is PendingActionStatus.SEALED:
            raise _refused(
                action,
                args.urn,
                TransactionRefusalCode.ILLEGAL_TRANSITION,
                f"{action.id} is sealed; a resolved question is immutable",
            )
        if action.revision != args.expected_revision:
            raise _refused(
                action,
                args.urn,
                TransactionRefusalCode.REVISION_CONFLICT,
                f"{action.id} is at revision {action.revision}, not the "
                f"{args.expected_revision} it was {operation}d against",
            )
        try:
            after = change(action)
        except ValueError as error:
            raise _refused(
                action, args.urn, TransactionRefusalCode.SCHEMA_VALIDATION_FAILED, str(error)
            ) from error
        committed = commit_action(
            context=context,
            session=session,
            request=request,
            before=action,
            after=after,
            event_name=event_name,
            now=now,
        )
    assert committed.envelope is not None, "a fresh commit always carries its firehose row"
    logger.info(
        f"_dispose operation={operation} action={after.id} actor={args.actor} "
        f"sequence={committed.receipt.canonical_sequence}"
    )
    return ApprovalCommit(
        answer=action_answer(
            after, urn=args.urn, bundle=None, receipt=committed.receipt, reason=reason
        ),
        envelopes=(committed.envelope,),
    )


def snooze_action(
    context: Epoch2RootContext, args: ActionSnoozeParams, *, now: datetime
) -> ApprovalCommit:
    """Record the actor's own snooze of a waiting question.

    Args:
        context: The native context of the tree the question lives in.
        args: The validated request.
        now: When the actor snoozed it.

    Returns:
        The question carrying the actor's snooze, at the revision it already stood at.

    Raises:
        TransactionRefusedError: See :func:`_dispose`; also when the deadline is not
            after ``now``.
    """
    return _dispose(
        context,
        args,
        operation="snooze",
        event_name=ACTION_SNOOZED_EVENT,
        change=lambda action: action.with_snooze(
            principal_id=args.actor, until=args.snooze_until, at=now
        ),
        reason=f"hidden for {args.actor} only until {args.snooze_until:%H:%M} UTC · "
        "other principals still see it",
        now=now,
    )


def assign_action(
    context: Epoch2RootContext, args: ActionAssignParams, *, now: datetime
) -> ApprovalCommit:
    """Address a waiting question to one principal, or to every eligible principal.

    Args:
        context: The native context of the tree the question lives in.
        args: The validated request.
        now: When it was assigned.

    Returns:
        The re-addressed question, one revision on.

    Raises:
        TransactionRefusedError: See :func:`_dispose`.
    """
    return _dispose(
        context,
        args,
        operation="assign",
        event_name=ACTION_ASSIGNED_EVENT,
        change=lambda action: action.assigned(assignee=args.assignee, at=now),
        reason=f"addressed to {args.assignee or 'every eligible principal'} · "
        "anyone eligible may still answer",
        now=now,
    )


@native_mutator(ACTION_SNOOZE_METHOD)
async def _snooze_action(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Record one principal's own snooze of a waiting question."""
    args = native_params(ActionSnoozeParams, params)
    context = ctx.native_root_context(authority.root)
    commit = await asyncio.to_thread(snooze_action, context, args, now=datetime.now(UTC))
    publish_commits(ctx, commit.envelopes)
    return commit.answer.model_dump(mode="json")


@native_mutator(ACTION_ASSIGN_METHOD)
async def _assign_action(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Address a waiting question to one principal, or back to everyone."""
    args = native_params(ActionAssignParams, params)
    context = ctx.native_root_context(authority.root)
    commit = await asyncio.to_thread(assign_action, context, args, now=datetime.now(UTC))
    publish_commits(ctx, commit.envelopes)
    return commit.answer.model_dump(mode="json")


__all__ = [
    "ACTION_ASSIGNED_EVENT",
    "ACTION_ASSIGN_METHOD",
    "ACTION_SNOOZED_EVENT",
    "ACTION_SNOOZE_METHOD",
    "ActionAssignParams",
    "ActionSnoozeParams",
    "assign_action",
    "snooze_action",
]
