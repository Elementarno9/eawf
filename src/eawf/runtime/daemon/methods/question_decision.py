"""Opening an operator decision, and reading the decisions that wait on the operator.

``runtime.question.open_decision``: an agent or skill that reaches a choice
it may not take files it here and stops. The verb resolves the repository's
configuration -- the ``preferences.auto_choose`` policy and, when the
question is about a configuration leaf, that leaf's value -- builds the
waiting ``operator_decision`` pending action from it, and presents it as
the host will before anything is written, so a question an operator could
not answer as shown is refused and never filed. Asking again under the
same idempotency key finds the question already filed and writes nothing;
the same key naming a different question is refused.

The answer is sealed through ``runtime.delivery.seal_acceptance_approval``,
which seals any waiting pending action by its URN, so a decision has no
seal verb of its own. A host without a native picker prints the answer's
numbered prompt, and ``runtime.question.answer_numbered`` reads the
operator's reply back to the persisted option id and seals through that
same verb. The console answers the same record the same way, so whichever
answer lands first wins and the other is recorded as superseded.

``projection.question.decisions``: the console's question card reads every
waiting decision in full, options and default included, because the
Attention register carries a row's facts rather than its offered answers.

The commit goes through the same transaction step every pending-action
commit does, so it writes one WAL intent, one document rewrite, one
receipt and one firehose row, and a refusal writes nothing at all.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.config.layered import get_dotted, merge_config
from eawf.kernel.config.schema import AutoChoose
from eawf.kernel.delivery.integration import IdempotencyKey
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import PrincipalKey, StrictPositiveInt
from eawf.kernel.state.epoch2.pending_action import (
    HumanPrincipal,
    PendingAction,
    PendingActionKind,
    PendingActionStatus,
)
from eawf.kernel.state.epoch2.urns import AnyEntityUrn, EvidenceUrn
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import TransactionRefusalCode, TransactionRefusedError
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, register
from eawf.runtime.daemon.methods.delivery_approval import (
    APPROVAL_OPENED_EVENT,
    ActionCommitRequest,
    ApprovalCommit,
    ApprovalSealParams,
    action_answer,
    action_urn,
    commit_action,
    held_actions,
    presented_question,
    publish_commits,
    seal_acceptance_approval,
)
from eawf.runtime.daemon.methods.projection import document_path
from eawf.runtime.daemon.native_guard import native_mutator, native_params, require_native_call
from eawf.workflow.decision_question import (
    AUTO_CHOOSE_KEY,
    QUESTION_DECISIONS_METHOD,
    ConfigAxis,
    DecisionRefusedError,
    DecisionRequest,
    decision_question,
)
from eawf.workflow.delivery.acceptance_approval import next_action_key
from eawf.workflow.host_question import QuestionPresentationError, numbered_option

logger = logging.getLogger(__name__)

#: The verb an asker files an operator decision through.
QUESTION_OPEN_DECISION_METHOD: Final = "runtime.question.open_decision"

#: The verb a numbered-prompt reply is sealed through.
QUESTION_ANSWER_NUMBERED_METHOD: Final = "runtime.question.answer_numbered"


class NumberedAnswerParams(BaseModel):
    """Params of :data:`QUESTION_ANSWER_NUMBERED_METHOD`.

    Attributes:
        urn: The pending action the prompt was presented from.
        expected_revision: The revision the prompt names.
        idempotency_key: The client's name for this answer.
        actor: Who relays the answer.
        resolver: The person who answered; it must be the actor.
        reply: What the operator typed at the numbered prompt.
        receipt_ref: The evidence row recording the answer.
    """

    model_config = ConfigDict(extra="forbid")

    urn: AnyEntityUrn
    expected_revision: StrictPositiveInt
    idempotency_key: IdempotencyKey
    actor: PrincipalKey
    resolver: HumanPrincipal
    reply: str = Field(max_length=200)
    receipt_ref: EvidenceUrn


class DecisionOpenParams(DecisionRequest):
    """Params of :data:`QUESTION_OPEN_DECISION_METHOD`.

    Attributes:
        actor: Who files the decision.
    """

    actor: PrincipalKey


class DecisionsParams(BaseModel):
    """Params of :data:`QUESTION_DECISIONS_METHOD`.

    Attributes:
        repo_root: The repository whose tree to answer for; the daemon's
            bound tree when absent.
    """

    model_config = ConfigDict(extra="forbid")

    repo_root: str | None = None


def _config_text(value: object) -> str:
    """Return a resolved configuration value as the text an axis maps options to."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def resolved_preferences(tree_root: Path, axis: ConfigAxis | None) -> tuple[AutoChoose, str | None]:
    """Return the auto-choose policy and the axis leaf's value the repository resolves.

    The native root is the ``.ea`` directory, so the repository the layered
    config anchors on is its parent.

    Args:
        tree_root: The tree's ``.ea`` directory.
        axis: The leaf the decision is about, or ``None``.

    Returns:
        The policy, and the axis value as text -- ``None`` when there is no
        axis or no layer and no default sets its leaf.
    """
    anchor = tree_root.parent
    merged, _sources = merge_config(repo=anchor, workspace=anchor)
    policy = AutoChoose(get_dotted(merged, AUTO_CHOOSE_KEY))
    if axis is None:
        return policy, None
    try:
        return policy, _config_text(get_dotted(merged, axis.key))
    except KeyError:
        return policy, None


def _standing(held: dict[str, PendingAction], args: DecisionOpenParams) -> PendingAction | None:
    """Return the decision already filed under the request's key, if one was.

    Raises:
        TransactionRefusedError: The key names a different question.
    """
    standing = next(
        (item for item in held.values() if item.idempotency_key == args.idempotency_key), None
    )
    if standing is None:
        return None
    asked = (str(args.urn), args.question, tuple(o.option_id for o in args.options))
    if (str(standing.subject_ref), standing.question, standing.option_ids) != asked:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.IDEMPOTENCY_CONFLICT,
            detail=f"{args.idempotency_key} already filed {standing.id}, a different question",
            entity_ref=str(standing.urn),
            remediation="Ask a new question under a new idempotency key.",
        )
    return standing


def open_decision(
    context: Epoch2RootContext, args: DecisionOpenParams, *, now: datetime
) -> ApprovalCommit:
    """File the waiting operator decision *args* asks for.

    Args:
        context: The native context of the tree the decision is filed in.
        args: The validated request.
        now: When the decision was asked.

    Returns:
        The decision with the host question it presents as, and the row to
        publish. A decision already filed under the same key is returned
        with nothing written.

    Raises:
        TransactionRefusedError: The key already names a different
            question, or the question carries free text with a leak shape.
        DaemonValidationError: The configuration axis cannot be honoured,
            or the question is not presentable as it stands.
    """
    policy, configured = resolved_preferences(context.identity.tree_root, args.config_axis)
    with context.session([str(args.urn)]) as session:
        document = session.read_document()
        standing = _standing(held_actions(document), args)
        if standing is not None:
            logger.info(f"open_decision standing action={standing.id}")
            return ApprovalCommit(
                answer=action_answer(
                    standing,
                    urn=standing.urn,
                    bundle=None,
                    receipt=None,
                    reason=f"{standing.id} already asks this question",
                    host_question=presented_question(standing),
                )
            )
        key = next_action_key(document_rows(document, Epoch2Collection.PENDING_ACTION))
        urn = action_urn(args.urn, key)
        try:
            action = decision_question(
                args, key=key, urn=urn, configured=configured, auto_choose=policy, at=now
            )
        except DecisionRefusedError as error:
            raise DaemonValidationError(f"validation_failed: {error}") from error
        # presented before the commit, so an unpresentable question is never filed
        host_question = presented_question(action)
        committed = commit_action(
            context=context,
            session=session,
            request=ActionCommitRequest(
                urn=urn,
                idempotency_key=action.idempotency_key,
                actor=args.actor,
                operation="open",
                detail={"subject_ref": str(args.urn)},
            ),
            before=None,
            after=action,
            event_name=APPROVAL_OPENED_EVENT,
            now=now,
        )
    assert committed.envelope is not None, "a fresh commit always carries its firehose row"
    logger.info(
        f"open_decision action={action.id} default={action.default_on_timeout} "
        f"sequence={committed.receipt.canonical_sequence}"
    )
    return ApprovalCommit(
        answer=action_answer(
            action,
            urn=urn,
            bundle=None,
            receipt=committed.receipt,
            reason=f"{action.id} asks the operator to decide",
            host_question=host_question,
        ),
        envelopes=(committed.envelope,),
    )


def waiting_decisions(authority: RootAuthority) -> tuple[PendingAction, ...]:
    """Return every operator decision in the tree that waits on an answer, by key.

    A row that does not read back is left out: it is not a question anybody
    can answer, and the Attention register already names it.
    """
    held: list[PendingAction] = []
    document = read_document(document_path(authority))
    for key, row in sorted(document_rows(document, Epoch2Collection.PENDING_ACTION).items()):
        try:
            action = PendingAction.model_validate(row)
        except ValidationError:
            logger.warning(f"pending action unreadable key={key!r}")
            continue
        if (
            action.kind is PendingActionKind.OPERATOR_DECISION
            and action.status is PendingActionStatus.WAITING
        ):
            held.append(action)
    return tuple(held)


@native_mutator(QUESTION_OPEN_DECISION_METHOD)
async def _open_decision(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """File a waiting operator decision."""
    args = native_params(DecisionOpenParams, params)
    context = ctx.native_root_context(authority.root)
    commit = await asyncio.to_thread(open_decision, context, args, now=datetime.now(UTC))
    publish_commits(ctx, commit.envelopes)
    return commit.answer.model_dump(mode="json")


def answer_numbered(
    context: Epoch2RootContext, args: NumberedAnswerParams, *, now: datetime
) -> ApprovalCommit:
    """Seal the option a numbered-prompt reply chooses.

    The options are read from the record rather than from the prompt, and a
    filed action's options never change, so the reply binds to exactly what
    the prompt showed; the seal then re-checks the revision under its lock.

    Args:
        context: The native context of the tree the action lives in.
        args: The validated request.
        now: When the answer was given.

    Returns:
        What the seal answers: the sealed action, or the standing seal and
        this answer's own superseded disposition when another answer won.

    Raises:
        TransactionRefusedError: The tree holds no such action, or the seal
            refuses (a moved revision, an unheld receipt).
        DaemonValidationError: The reply is not a bare number of an offered
            option.
    """
    document = read_document(document_path(context.require_selected_generation()))
    action = next((item for item in held_actions(document).values() if item.urn == args.urn), None)
    if action is None:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.IDENTITY_NOT_FOUND,
            detail=f"the tree holds no pending action {args.urn}",
            entity_ref=str(args.urn),
            remediation="Answer a question the tree holds.",
        )
    try:
        option_id = numbered_option(action, args.reply)
    except QuestionPresentationError as error:
        raise DaemonValidationError(f"validation_failed: {error}") from error
    seal = ApprovalSealParams.model_validate(
        {**args.model_dump(exclude={"reply"}), "option_id": option_id}
    )
    return seal_acceptance_approval(context, seal, now=now)


@native_mutator(QUESTION_ANSWER_NUMBERED_METHOD)
async def _answer_numbered(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Seal the option a numbered-prompt reply chooses."""
    args = native_params(NumberedAnswerParams, params)
    context = ctx.native_root_context(authority.root)
    commit = await asyncio.to_thread(answer_numbered, context, args, now=datetime.now(UTC))
    publish_commits(ctx, commit.envelopes)
    return commit.answer.model_dump(mode="json")


@register(QUESTION_DECISIONS_METHOD)
async def read_decisions(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return every waiting operator decision in full. Nothing is written.

    Raises:
        NativeAuthorityRefusedError: The request addresses no epoch-2 tree.
        DaemonValidationError: The parameters carry an unknown field.
    """
    try:
        DecisionsParams.model_validate(params)
    except ValidationError as error:
        raise DaemonValidationError(
            f"validation_failed: schema_validation_failed: {error.error_count()} bad "
            f"parameter(s) for {QUESTION_DECISIONS_METHOD}"
        ) from error
    authority = require_native_call(ctx, params)
    decisions = await asyncio.to_thread(waiting_decisions, authority)
    logger.debug(f"read_decisions waiting={len(decisions)}")
    return {"decisions": [item.model_dump(mode="json") for item in decisions]}


__all__ = [
    "QUESTION_ANSWER_NUMBERED_METHOD",
    "QUESTION_DECISIONS_METHOD",
    "QUESTION_OPEN_DECISION_METHOD",
    "DecisionOpenParams",
    "DecisionsParams",
    "NumberedAnswerParams",
    "answer_numbered",
    "open_decision",
    "read_decisions",
    "resolved_preferences",
    "waiting_decisions",
]
