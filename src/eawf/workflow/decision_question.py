"""Filing an operator decision: a reversible choice asked once, as the host will show it.

An agent that reaches a choice it may not take asks the operator. What it
files is a :class:`~eawf.kernel.state.epoch2.pending_action.PendingAction`
of the ``operator_decision`` kind, born waiting, and this module decides
what that record says. It writes nothing and reads no configuration: the
daemon resolves the configuration and hands the values in, so the rules
below are the whole of the decision.

Configuration outranks the asker's recommendation. When the question is
about a configuration leaf, the caller maps each option to the value it
stands for. The resolved value then picks the recommended option, and the
rationale says the configuration chose it. A resolved value no option
stands for is refused: the question would recommend something the
configuration does not honour, or offer nothing it does.

A timeout default is set only when the persisted policy permits it.
``preferences.auto_choose`` is that policy: under ``off`` nothing defaults,
and otherwise the recommended option stands once the caller's override
window closes. The window and the policy are filed with the default, so
the host shows both. ``recommended`` and ``always`` differ only on a
question with no recommendation, and a decision without one is refused
before it is filed, so here they act alike.

A decision that consents to something names it, and its answer is bound
to it by digest: a criterion waiver of the subject Task, or the plan
revision of the subject Milestone whose approval it asks for. The verb the
consent is spent on compares the digest, so the answer cannot be carried
over to another waiver or another revision.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Final

import orjson
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from eawf.kernel.config.schema import AutoChoose
from eawf.kernel.delivery.integration import IdempotencyKey
from eawf.kernel.identity import EntityKind, QualifiedUrn
from eawf.kernel.state.epoch2.base import NonEmptyStr, PrincipalKey
from eawf.kernel.state.epoch2.pending_action import (
    MAX_OPTIONS,
    MIN_OPTIONS,
    ActionPrincipal,
    OptionId,
    PendingAction,
    PendingActionKind,
    PendingActionOption,
    PendingActionStatus,
    TermExpansion,
)
from eawf.kernel.state.epoch2.plan_revision import (
    PlanRevision,
    PlanRevisionKey,
    plan_content_digest,
)
from eawf.kernel.state.epoch2.urns import AnyEntityUrn, PendingActionUrn

#: The read that returns every waiting decision in full. Named here, beside the rules the
#: decision is built by, because the daemon that serves it and the console that reads it
#: must spell it alike without either importing the other.
QUESTION_DECISIONS_METHOD: Final = "projection.question.decisions"

#: The policy leaf that permits a timeout default.
AUTO_CHOOSE_KEY: Final = "preferences.auto_choose"

#: The longest override window a decision may ask for: a week. Past that a
#: default is not a reversible choice waiting briefly for a person, it is a
#: standing answer nobody gave.
MAX_OVERRIDE_WINDOW_MINUTES: Final = 7 * 24 * 60

#: A dotted configuration leaf, as the layered config spells it.
ConfigKey = Annotated[
    str, StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
]


class DecisionRefusal(StrEnum):
    """Why a decision was not filed."""

    AXIS_UNRESOLVED = "config_axis_unresolved"
    AXIS_OPTION_UNOFFERED = "config_axis_option_unoffered"
    CONFIG_UNHONOURED = "config_value_unhonoured"
    WAIVER_SUBJECT_NOT_TASK = "waiver_subject_not_task"
    PLAN_REVISION_UNHELD = "plan_revision_unheld"
    PLAN_SUBJECT_MISMATCH = "plan_subject_mismatch"


class DecisionRefusedError(ValueError):
    """A decision cannot be filed as asked.

    Attributes:
        code: The stable refusal a caller branches on.
    """

    def __init__(self, code: DecisionRefusal, message: str) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code


class ConfigAxis(BaseModel):
    """The configuration leaf a decision is about, and the value each option stands for.

    Attributes:
        key: The dotted leaf, such as ``vcs.integration_commit_unit``.
        values: The configured value each option stands for, by option id.
            An option left out stands for no configured value.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: ConfigKey
    values: dict[OptionId, NonEmptyStr] = Field(min_length=1)


class WaiverRequest(BaseModel):
    """The criterion waiver a decision asks the operator to consent to.

    The bounds are the waiver verb's own, so a consent filed here is one the
    verb can be sent.

    Attributes:
        criterion_id: The criterion of the subject Task to waive.
        reason: Why the criterion no longer needs proving, exactly as it will be
            recorded.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_id: Annotated[str, Field(min_length=1, max_length=64, strict=True)]
    reason: Annotated[str, Field(min_length=1, max_length=500, strict=True)]


def waiver_digest(task_urn: QualifiedUrn, criterion_id: str, reason: str) -> str:
    """Return the digest that binds a consent to one waiver of one criterion.

    The Task is taken as it is spelled, so the waiver must name it as the
    decision did.

    Args:
        task_urn: The Task whose criterion is waived.
        criterion_id: The criterion waived.
        reason: The recorded reason.

    Returns:
        ``sha256:`` and the hex digest of the sorted-key compact JSON of the
        three facts.
    """
    body = {"criterion_id": criterion_id, "reason": reason, "task_ref": str(task_urn)}
    return f"sha256:{hashlib.sha256(orjson.dumps(body, option=orjson.OPT_SORT_KEYS)).hexdigest()}"


class DecisionRequest(BaseModel):
    """What an asker files: the question, its options and how it may default.

    Attributes:
        urn: The record the decision is about.
        idempotency_key: The asker's name for this question; asking again
            under it finds the question already filed.
        requested_by: Who asks.
        question: The one decision, in plain words.
        options: The two to four answers, each with its consequence and a
            rendering of what choosing it produces.
        recommended_option_id: The asker's recommendation, which a
            configuration axis overrides.
        recommendation_rationale: Its one-sentence reason.
        terms: What each identifier the question shows stands for.
        config_axis: The configuration leaf the decision is about, if any.
        override_window_minutes: How long an answer still overrides a
            timeout default; ``None`` asks for no default.
        assignee: The one principal the question is addressed to, or
            ``None`` for every eligible principal.
        waiver: The criterion waiver of the subject Task the decision
            consents to, or ``None`` for a decision that consents to none.
        plan_revision: The plan revision of the subject Milestone whose
            approval the decision asks for, or ``None`` for a decision that
            approves no plan.
    """

    model_config = ConfigDict(extra="forbid")

    urn: AnyEntityUrn
    idempotency_key: IdempotencyKey
    requested_by: ActionPrincipal
    question: NonEmptyStr
    options: tuple[PendingActionOption, ...] = Field(min_length=MIN_OPTIONS, max_length=MAX_OPTIONS)
    recommended_option_id: OptionId | None = None
    recommendation_rationale: NonEmptyStr | None = None
    terms: tuple[TermExpansion, ...] = ()
    config_axis: ConfigAxis | None = None
    override_window_minutes: int | None = Field(
        default=None, gt=0, le=MAX_OVERRIDE_WINDOW_MINUTES, strict=True
    )
    assignee: PrincipalKey | None = None
    waiver: WaiverRequest | None = None
    plan_revision: PlanRevisionKey | None = None


def bound_digest(request: DecisionRequest, *, plan: PlanRevision | None) -> str | None:
    """Return the digest a decision *request* is bound to, if it consents to anything.

    A waiver binds its :func:`waiver_digest`; a plan approval binds the content
    digest of the revision it approves, recomputed from the body, so the answer
    approves exactly that plan and no other revision of the Milestone.

    Args:
        request: What the asker files.
        plan: The stored revision ``request.plan_revision`` names, or ``None``
            when the tree holds none or the request names none.

    Returns:
        The digest, or ``None`` for a decision that consents to neither.

    Raises:
        DecisionRefusedError: ``waiver_subject_not_task`` for a waiver asked about
            a subject that is not a Task; ``plan_revision_unheld`` for a revision
            the tree does not hold; ``plan_subject_mismatch`` for a revision of
            another subject than the request's.
    """
    # A waiver is asked about a Task and a plan about a Milestone, so a request
    # naming both fails one of the subject checks and binds neither.
    if request.waiver is not None and request.urn.kind is not EntityKind.TASK:
        raise DecisionRefusedError(
            DecisionRefusal.WAIVER_SUBJECT_NOT_TASK,
            f"a criterion waiver is asked about a Task, not the {request.urn.kind.value} "
            f"{request.urn.entity_key}",
        )
    if request.plan_revision is None:
        if request.waiver is None:
            return None
        return waiver_digest(request.urn, request.waiver.criterion_id, request.waiver.reason)
    if plan is None:
        raise DecisionRefusedError(
            DecisionRefusal.PLAN_REVISION_UNHELD,
            f"the tree holds no plan revision {request.plan_revision}",
        )
    if not request.urn.same_entity(plan.body.milestone_urn):
        raise DecisionRefusedError(
            DecisionRefusal.PLAN_SUBJECT_MISMATCH,
            f"{plan.key} plans {plan.body.milestone_urn.entity_key}, so its approval is asked "
            f"about that Milestone, not {request.urn.entity_key}",
        )
    return plan_content_digest(plan.body)


def _configured_recommendation(
    axis: ConfigAxis, configured: str | None, *, offered: tuple[str, ...]
) -> tuple[str, str]:
    """Return the option the resolved configuration picks, and the sentence that says so.

    Args:
        axis: The leaf the decision is about.
        configured: The leaf's resolved value as text, or ``None`` when no
            layer and no default sets it.
        offered: The option ids the question offers.

    Returns:
        The option id standing for the configured value, and a one-sentence
        rationale naming the leaf and its value.

    Raises:
        DecisionRefusedError: ``config_axis_unresolved`` when nothing sets
            the leaf, so nothing on the path would honour an answer;
            ``config_axis_option_unoffered`` when the axis maps an option
            the question does not offer; ``config_value_unhonoured`` when no
            option stands for the configured value.
    """
    unoffered = sorted(set(axis.values) - set(offered))
    if unoffered:
        raise DecisionRefusedError(
            DecisionRefusal.AXIS_OPTION_UNOFFERED,
            f"{axis.key} maps {', '.join(unoffered)}, which the question does not offer",
        )
    if configured is None:
        raise DecisionRefusedError(
            DecisionRefusal.AXIS_UNRESOLVED,
            f"no configuration layer or default sets {axis.key}, so nothing would honour "
            "the answer",
        )
    chosen = [option for option, value in axis.values.items() if value == configured]
    if not chosen:
        raise DecisionRefusedError(
            DecisionRefusal.CONFIG_UNHONOURED,
            f"{axis.key} is configured as {configured!r}, which no option stands for",
        )
    return (
        chosen[0],
        f"Your configuration already sets {axis.key} to {configured}, and this option keeps it.",
    )


def decision_question(
    request: DecisionRequest,
    *,
    key: str,
    urn: PendingActionUrn,
    configured: str | None,
    auto_choose: AutoChoose,
    at: datetime,
    plan: PlanRevision | None = None,
) -> PendingAction:
    """Return the waiting operator decision *request* files.

    Args:
        request: What the asker files.
        key: The ``ACT-####`` key the decision is filed under.
        urn: The decision's own canonical address.
        configured: The resolved value of the request's configuration
            axis as text; ignored when it names none.
        auto_choose: The resolved ``preferences.auto_choose`` policy.
        at: When the decision was asked.
        plan: The stored revision the request's ``plan_revision`` names, or
            ``None`` when the tree holds none or the request names none.

    Returns:
        The decision, born waiting: committing it is what puts it in front
        of the operator, so it never exists unshown. A waiver or plan-approval
        decision carries the :func:`bound_digest` of what it consents to as
        its bundle digest.

    Raises:
        DecisionRefusedError: The configuration axis cannot be honoured, or
            what the decision consents to is not of its subject; see
            :func:`bound_digest`.
    """
    digest = bound_digest(request, plan=plan)
    recommended = request.recommended_option_id
    rationale = request.recommendation_rationale
    if request.config_axis is not None:
        offered = tuple(option.option_id for option in request.options)
        recommended, rationale = _configured_recommendation(
            request.config_axis, configured, offered=offered
        )
    default: dict[str, object] = {}
    window = request.override_window_minutes
    if auto_choose is not AutoChoose.OFF and window is not None and recommended is not None:
        default = {
            "default_on_timeout": recommended,
            "override_until": at + timedelta(minutes=window),
            "default_policy": f"{AUTO_CHOOSE_KEY}={auto_choose.value}",
        }
    return PendingAction.model_validate(
        {
            "id": key,
            "urn": urn,
            "kind": PendingActionKind.OPERATOR_DECISION,
            "subject_ref": request.urn,
            "question": request.question,
            "bundle_digest": digest,
            "options": request.options,
            "recommended_option_id": recommended,
            "recommendation_rationale": rationale,
            "terms": request.terms,
            "idempotency_key": request.idempotency_key,
            "status": PendingActionStatus.WAITING,
            "requested_by": request.requested_by,
            "assignee_ref": request.assignee,
            "created_at": at,
            "updated_at": at,
            **default,
        }
    )


__all__ = [
    "AUTO_CHOOSE_KEY",
    "MAX_OVERRIDE_WINDOW_MINUTES",
    "QUESTION_DECISIONS_METHOD",
    "ConfigAxis",
    "DecisionRefusal",
    "DecisionRefusedError",
    "DecisionRequest",
    "WaiverRequest",
    "bound_digest",
    "decision_question",
    "waiver_digest",
]
