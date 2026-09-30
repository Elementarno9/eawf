"""OpenQuestion: a knowledge gap, what it renders as, and a reply in the operator's words.

The six persisted states stand; what a surface shows is a projection of
them. :func:`project_question` computes which of ten situations a question
is in from its persisted fields and the asking Run's reachability at the
moment it is asked, and it never stores the answer beside the record. Two
obligations drive the projection. A question the Run defaulted past was
answered by nobody, so ``AUTO_RESOLVED`` and ``SEALED`` project as
defaulted and never as answered. A replaced question names its
replacement, so an answer cannot be given to a question that no longer
exists. Nothing in the machine expires a question: a timer produces
``AUTO_RESOLVED`` or the blocking urgency self-edge, never an ending, and
``expired`` is not a situation.

An operator may answer in their own words through a :class:`QuestionReply`.
A reply is the answer to a question with no options, or to one whose every
option the operator declined; it is immutable once submitted. A pending
action is not a question in this sense: the daemon classified its effect
over exactly the options it offers, so a reply body submitted against one
is refused with ``question_kind_mismatch``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Final, Literal, Self

from pydantic import ConfigDict, Field, StringConstraints, model_validator

from eawf.kernel.state.enums import OpenQuestionDropReason, OpenQuestionStatus, Urgency
from eawf.kernel.state.epoch2.base import (
    Epoch2Model,
    NonEmptyStr,
    PrincipalKey,
    TitleStr,
)
from eawf.kernel.state.epoch2.pending_action import PendingAction
from eawf.kernel.state.epoch2.urns import (
    AnyEntityUrn,
    ClaimUrn,
    EvidenceUrn,
    QuestionUrn,
    render_qualified_urn,
)
from eawf.kernel.state.epoch2.values import Epoch2Record
from eawf.kernel.state.types import UtcDatetime

#: The resolver a policy default is recorded under, never a principal key:
#: a principal key is uppercase, so the two cannot be confused.
POLICY_ACTOR: Final = "policy"

#: Who resolved a question: a person by key, or the policy that selected a default.
ResolutionActor = PrincipalKey | Literal["policy"]

#: An option key, compared rather than displayed.
OptionKey = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_]{0,31}$")]

#: The most options a question offers; with options, the fewest is two.
MAX_OPTIONS: Final = 4
MIN_OPTIONS: Final = 2

#: The longest reply an operator may send.
MAX_REPLY_LENGTH: Final = 2000

_LIVE: Final = frozenset({OpenQuestionStatus.OPEN, OpenQuestionStatus.BLOCKED})
_RESOLVED: Final = frozenset(
    {
        OpenQuestionStatus.ANSWERED,
        OpenQuestionStatus.AUTO_RESOLVED,
        OpenQuestionStatus.SEALED,
        OpenQuestionStatus.DROPPED,
    }
)
_DEFAULTED: Final = frozenset({OpenQuestionStatus.AUTO_RESOLVED, OpenQuestionStatus.SEALED})


class QuestionOption(Epoch2Model):
    """One answer a question offers.

    Attributes:
        key: The option's key, which an answer names.
        label: The option in words.
        recommended: Whether the asker recommends it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: OptionKey
    label: TitleStr
    recommended: bool = False


class QuestionReply(Epoch2Model):
    """An operator's answer in their own words, immutable once submitted.

    Attributes:
        question_ref: The question it answers.
        text: The reply, delivered verbatim to the asking Run.
        principal: Who replied.
        submitted_at: When it was sent.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    question_ref: QuestionUrn
    text: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=MAX_REPLY_LENGTH)]
    principal: PrincipalKey
    submitted_at: UtcDatetime


class LateAnswer(Epoch2Model):
    """A principal's answer that arrived after another had resolved the question.

    Attributes:
        principal: Who answered late.
        chosen_option_key: The option they chose, when they chose one.
        submitted_at: When they answered.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal: PrincipalKey
    chosen_option_key: OptionKey | None = None
    submitted_at: UtcDatetime


class OpenQuestion(Epoch2Record):
    """A knowledge gap a Run raised, with how it was resolved.

    Attributes:
        urn: The question's own address.
        scope_ref: The Campaign, plan or entity the question was asked under.
        question: The question in full.
        rationale: Why it is asked.
        options: The offered answers: none, or two to four with at most one recommended.
        blocking: Whether an unanswered question halts work; a blocking question never defaults.
        urgency: The shared time-pressure ladder.
        escalated_by_deadline: The deadline that raised a blocked question's urgency.
        default_key: The option policy selects when nobody answers.
        override_until: When an operator may still override a policy default.
        answer_refs: The claims or evidence an answer rests on.
        reply: The operator's own words, when the answer was a reply.
        resolution_actor: Who resolved or dropped it: a person, or the policy.
        chosen_option_key: The option it was resolved with.
        late_answers: Answers that arrived after it was resolved.
        superseded_by_question_ref: The question that replaced it.
        drop_reason: Why it was dropped.
        status: The persisted state.
        resolved_at: When it reached a terminal or defaulted state.

    Raises:
        pydantic.ValidationError: Any field its status or blocking bit does
            not admit, or an option set outside the offered shape.
    """

    urn: QuestionUrn
    scope_ref: AnyEntityUrn
    question: NonEmptyStr
    rationale: NonEmptyStr | None = None
    options: tuple[QuestionOption, ...] = Field(default=(), max_length=MAX_OPTIONS)
    blocking: bool = False
    urgency: Urgency = Urgency.NORMAL
    escalated_by_deadline: UtcDatetime | None = None
    default_key: OptionKey | None = None
    override_until: UtcDatetime | None = None
    answer_refs: tuple[ClaimUrn | EvidenceUrn, ...] = ()
    reply: QuestionReply | None = None
    resolution_actor: ResolutionActor | None = None
    chosen_option_key: OptionKey | None = None
    late_answers: tuple[LateAnswer, ...] = ()
    superseded_by_question_ref: QuestionUrn | None = None
    drop_reason: OpenQuestionDropReason | None = None
    status: OpenQuestionStatus
    resolved_at: UtcDatetime | None = None

    @property
    def option_keys(self) -> tuple[str, ...]:
        """Return the offered option keys, in order."""
        return tuple(o.key for o in self.options)

    @model_validator(mode="after")
    def _options_are_well_formed(self) -> Self:
        """Refuse one option, a repeated key, two recommendations, or a stray key.

        Raises:
            ValueError: One of the named shapes.
        """
        keys = self.option_keys
        if len(keys) == 1:
            raise ValueError("a question offers no options or at least two")
        if len(set(keys)) != len(keys):
            raise ValueError("a question offers the same option key twice")
        if sum(o.recommended for o in self.options) > 1:
            raise ValueError("a question recommends at most one option")
        for name in ("default_key", "chosen_option_key"):
            key = getattr(self, name)
            if key is not None and key not in keys:
                raise ValueError(f"{name} {key!r} is not one of the offered options")
        return self

    @model_validator(mode="after")
    def _fields_match_status(self) -> Self:
        """Require exactly the resolution fields the status admits.

        Raises:
            ValueError: A default on a blocking question, a resolver on a
                live one, a person recorded as a default, a policy recorded
                as an answer, or drop and reply fields outside their states.
        """
        status = self.status
        if (self.default_key is not None or self.override_until is not None) and self.blocking:
            raise ValueError("a blocking question cannot carry a default")
        if status in _DEFAULTED and (self.default_key is None or self.override_until is None):
            raise ValueError(f"a {status.value} question names its default and override window")
        if (status in _RESOLVED) != (self.resolution_actor is not None):
            raise ValueError(f"resolution_actor is set exactly when resolved, not in {status}")
        if (status in _RESOLVED) != (self.resolved_at is not None):
            raise ValueError(f"resolved_at is set exactly when resolved, not in {status}")
        if status in _DEFAULTED and (
            self.resolution_actor != POLICY_ACTOR or self.chosen_option_key != self.default_key
        ):
            raise ValueError(f"a {status.value} question was resolved by policy to its default")
        if status is OpenQuestionStatus.ANSWERED and self.resolution_actor == POLICY_ACTOR:
            raise ValueError("an answered question was answered by a person, never by policy")
        if self.escalated_by_deadline is not None and status is not OpenQuestionStatus.BLOCKED:
            raise ValueError("only a blocked question is escalated by a deadline")
        self._check_answer()
        self._check_drop()
        return self

    def _check_answer(self) -> None:
        answered = self.status is OpenQuestionStatus.ANSWERED
        if not answered and (self.reply is not None or self.answer_refs or self.late_answers):
            raise ValueError("reply, answer_refs and late_answers belong to an answered question")
        if not answered:
            return
        if self.reply is not None:
            if self.chosen_option_key is not None:
                raise ValueError("a reply answers by declining every option, so it chooses none")
            if self.reply.question_ref != self.urn or self.reply.principal != self.resolution_actor:
                raise ValueError("the reply is this question's, from the principal who resolved it")
        elif self.chosen_option_key is None and not self.answer_refs:
            raise ValueError("an answered question names its option, its reply or its evidence")

    def _check_drop(self) -> None:
        dropped = self.status is OpenQuestionStatus.DROPPED
        if dropped != (self.drop_reason is not None):
            raise ValueError("drop_reason is set exactly when the question is dropped")
        replaced = self.drop_reason is OpenQuestionDropReason.SUPERSEDED
        if replaced != (self.superseded_by_question_ref is not None):
            raise ValueError("superseded_by_question_ref is set exactly for a replacement drop")
        if self.superseded_by_question_ref == self.urn:
            raise ValueError("a question cannot replace itself")


class QuestionSituation(StrEnum):
    """The ten situations a question projects to; a projection, never stored."""

    OPEN = "open"
    OPEN_BLOCKING = "open_blocking"
    OPEN_ESCALATED = "open_escalated"
    ANSWERED_BY_YOU = "answered_by_you"
    ANSWERED_ELSEWHERE = "answered_elsewhere"
    DEFAULTED_OVERRIDE_OPEN = "defaulted_override_open"
    DEFAULTED_SEALED = "defaulted_sealed"
    WITHDRAWN = "withdrawn"
    REPLACED = "replaced"
    UNANSWERABLE = "unanswerable"


#: What would end each situation, so an overlay's ender is a fact of the record.
ENDS_WHEN: Final = MappingProxyType(
    {
        QuestionSituation.OPEN: (
            "an operator or claim evidence answers it, or policy selects its declared default"
        ),
        QuestionSituation.OPEN_BLOCKING: "an operator or evidence answers it",
        QuestionSituation.OPEN_ESCALATED: "an operator or evidence answers it",
        QuestionSituation.ANSWERED_BY_YOU: "nothing - terminal",
        QuestionSituation.ANSWERED_ELSEWHERE: "nothing - terminal",
        QuestionSituation.DEFAULTED_OVERRIDE_OPEN: (
            "the override window closes, or an operator overrides inside it"
        ),
        QuestionSituation.DEFAULTED_SEALED: "nothing - terminal",
        QuestionSituation.WITHDRAWN: "nothing - terminal",
        QuestionSituation.REPLACED: "nothing - terminal",
        QuestionSituation.UNANSWERABLE: "the asking Run is recovered or let go",
    }
)


@dataclass(frozen=True, slots=True)
class QuestionProjection:
    """The situation a question is in for one principal, and what the render carries.

    Attributes:
        situation: Which of the ten situations it is in.
        ends_when: What would end it.
        resolution_actor: The winning or dropping actor, when resolved.
        chosen_option_key: The winning or defaulted choice, when there is one.
        own_late_answer_superseded: Whether this principal answered after the
            winner, which renders as the control outcome ``superseded``.
        drop_reason: The drop word, moot or out of scope, never merged.
        successor_ref: The replacing question, a cursor stop.
    """

    situation: QuestionSituation
    ends_when: str
    resolution_actor: str | None = None
    chosen_option_key: str | None = None
    own_late_answer_superseded: bool = False
    drop_reason: OpenQuestionDropReason | None = None
    successor_ref: str | None = None


def project_question(
    question: OpenQuestion, *, principal: str, asking_run_unreachable: bool
) -> QuestionProjection:
    """Return the one situation *question* is in for *principal*.

    Args:
        question: The persisted question.
        principal: The principal the projection is computed for.
        asking_run_unreachable: Whether the asking Run is lost or suspended at a
            context boundary, which makes a live question unanswerable.

    Returns:
        The projection; computed at every call and never stored.
    """
    status = question.status
    if status in _LIVE:
        if asking_run_unreachable:
            situation = QuestionSituation.UNANSWERABLE
        elif status is OpenQuestionStatus.OPEN:
            situation = QuestionSituation.OPEN
        elif question.escalated_by_deadline is not None:
            situation = QuestionSituation.OPEN_ESCALATED
        else:
            situation = QuestionSituation.OPEN_BLOCKING
        return QuestionProjection(situation, ENDS_WHEN[situation])
    actor = question.resolution_actor
    if status is OpenQuestionStatus.DROPPED:
        replaced = question.superseded_by_question_ref
        situation = QuestionSituation.WITHDRAWN if replaced is None else QuestionSituation.REPLACED
        return QuestionProjection(
            situation,
            ENDS_WHEN[situation],
            resolution_actor=actor,
            drop_reason=question.drop_reason,
            successor_ref=None if replaced is None else render_qualified_urn(replaced),
        )
    late = False
    if status is OpenQuestionStatus.AUTO_RESOLVED:
        situation = QuestionSituation.DEFAULTED_OVERRIDE_OPEN
    elif status is OpenQuestionStatus.SEALED:
        situation = QuestionSituation.DEFAULTED_SEALED
    elif actor == principal:
        situation = QuestionSituation.ANSWERED_BY_YOU
    else:
        situation = QuestionSituation.ANSWERED_ELSEWHERE
        late = any(a.principal == principal for a in question.late_answers)
    return QuestionProjection(
        situation,
        ENDS_WHEN[situation],
        resolution_actor=actor,
        chosen_option_key=question.chosen_option_key,
        own_late_answer_superseded=late,
    )


class QuestionRefusedError(ValueError):
    """An answer was refused.

    Attributes:
        code: The stable refusal code a caller branches on.
        remediation: What the caller can do instead.
    """

    def __init__(self, code: str, message: str, *, remediation: str) -> None:
        super().__init__(message)
        self.code = code
        self.remediation = remediation


def answer_with_reply(target: OpenQuestion | PendingAction, reply: QuestionReply) -> OpenQuestion:
    """Return *target* answered by *reply*, or refuse the reply.

    The returned record is the ``ANSWERED`` transition with the reply and
    its principal as the resolution actor, written together so neither
    exists without the other. An override of a policy default keeps the
    default it overrode.

    Args:
        target: The record the reply was submitted against.
        reply: The operator's words.

    Returns:
        The answered question.

    Raises:
        QuestionRefusedError: ``question_kind_mismatch`` when *target* is a pending
            action (the remediation names its option keys) or a replaced
            question (the remediation names its successor);
            ``question_ref_mismatch`` when the reply names another question;
            ``question_already_resolved`` when it was already answered,
            sealed or withdrawn (the message carries the existing answer);
            ``override_window_closed`` when a defaulted question's window
            has closed.
    """
    if isinstance(target, PendingAction):
        raise QuestionRefusedError(
            "question_kind_mismatch",
            f"{target.id} is a pending action and is answered only by one of its options",
            remediation=f"answer with one of: {', '.join(target.option_ids)}",
        )
    if reply.question_ref != target.urn:
        raise QuestionRefusedError(
            "question_ref_mismatch",
            f"the reply names {render_qualified_urn(reply.question_ref)}, not {target.key}",
            remediation=f"submit the reply against {render_qualified_urn(target.urn)}",
        )
    _require_answerable(target, reply.submitted_at)
    return target.model_validate(
        {
            **target.model_dump(),
            "status": OpenQuestionStatus.ANSWERED,
            "reply": reply,
            "resolution_actor": reply.principal,
            "chosen_option_key": None,
            "escalated_by_deadline": None,
            "resolved_at": reply.submitted_at,
        }
    )


def answer_with_option(
    target: OpenQuestion, option_key: str, *, principal: str, submitted_at: datetime
) -> OpenQuestion:
    """Return *target* answered by one of its own options, or refuse the answer.

    Args:
        target: The question answered.
        option_key: The option chosen.
        principal: Who chose it.
        submitted_at: When they chose it.

    Returns:
        The answered question, the principal its resolution actor.

    Raises:
        QuestionRefusedError: ``question_option_unknown`` when the question offers no
            such option, or any refusal :func:`answer_with_reply` names for a question
            that can no longer be answered.
    """
    if option_key not in target.option_keys:
        offered = ", ".join(target.option_keys) or "none; answer with a reply"
        raise QuestionRefusedError(
            "question_option_unknown",
            f"{target.key} offers no option {option_key!r}",
            remediation=f"answer with one of: {offered}",
        )
    _require_answerable(target, submitted_at)
    return target.model_validate(
        {
            **target.model_dump(),
            "status": OpenQuestionStatus.ANSWERED,
            "reply": None,
            "resolution_actor": principal,
            "chosen_option_key": option_key,
            "escalated_by_deadline": None,
            "resolved_at": submitted_at,
        }
    )


def _require_answerable(target: OpenQuestion, at: datetime) -> None:
    """Refuse an answer to a replaced, resolved or no longer overridable question.

    Raises:
        QuestionRefusedError: One of the codes :func:`answer_with_reply` names.
    """
    successor = target.superseded_by_question_ref
    if successor is not None:
        rendered = render_qualified_urn(successor)
        raise QuestionRefusedError(
            "question_kind_mismatch",
            f"{target.key} was replaced by {rendered}",
            remediation=f"answer {rendered} instead",
        )
    if target.status not in (*_LIVE, OpenQuestionStatus.AUTO_RESOLVED):
        existing = target.reply.text if target.reply is not None else target.chosen_option_key
        raise QuestionRefusedError(
            "question_already_resolved",
            f"{target.key} is already {target.status.value.lower()}: {existing!r}",
            remediation="read the existing answer; a changed answer is a new question",
        )
    if target.status is OpenQuestionStatus.AUTO_RESOLVED and (
        target.override_until is None or at > target.override_until
    ):
        raise QuestionRefusedError(
            "override_window_closed",
            f"{target.key}'s override window closed at {target.override_until}",
            remediation="the default stands; a changed answer is a new question",
        )


__all__ = [
    "ENDS_WHEN",
    "MAX_REPLY_LENGTH",
    "POLICY_ACTOR",
    "LateAnswer",
    "OpenQuestion",
    "QuestionOption",
    "QuestionProjection",
    "QuestionRefusedError",
    "QuestionReply",
    "QuestionSituation",
    "answer_with_option",
    "answer_with_reply",
    "project_question",
]
