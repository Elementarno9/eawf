"""OpenPause: an operational pause the daemon observed, and what it renders as.

A pause is daemon-raised operational health -- provider loss, lease
ambiguity, a permission wait, recovery uncertainty -- and never an
epistemic choice. Its five persisted states project to seven situations,
computed by :func:`project_pause` and never stored, and every situation
names what would end it: a predicate the system evaluates, or a person.

The fields each situation reads are required where it reads them, so the
projection never infers them from the pause reason. A pause waiting on a
person names the record whose answer ends it; a ``HELD`` pause names who
placed the Hold and the Hold's id; an ``ESCALATED`` pause names its cause,
when it fired and the record it raised. ``HELD`` and ``ESCALATED`` never
project as one another, a ``policy`` pause is ``OPEN`` with a policy
predicate rather than held, and the lost-Run card whose control outcome is
unknown offers reconcile and let go, never retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Final, Self

from pydantic import ConfigDict, Field, StringConstraints, model_validator

from eawf.kernel.state.epoch2.base import (
    Epoch2Model,
    NonEmptyStr,
    PrincipalKey,
    StrictNonNegativeInt,
)
from eawf.kernel.state.epoch2.urns import (
    BatchUrn,
    CampaignUrn,
    EvidenceUrn,
    PendingActionUrn,
    QuestionUrn,
    ReleaseUrn,
    RunUrn,
    WorkspaceUrn,
    render_qualified_urn,
)
from eawf.kernel.state.types import UtcDatetime

#: A ``PAU-####`` pause key. Local grammar: a pause is addressed by key
#: inside the daemon's pause projection and has no URN kind of its own.
PauseKey = Annotated[str, StringConstraints(strict=True, pattern=r"^PAU-\d{4,}$")]

#: A bounded single-line label: a Hold id, an evaluator's name.
ShortText = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=120)
]


class PauseReason(StrEnum):
    """Why the daemon paused the work."""

    PERMISSION = "permission"
    USER = "user"
    PROVIDER = "provider"
    LEASE = "lease"
    POLICY = "policy"
    INFRASTRUCTURE = "infrastructure"
    EXTERNAL_TRUTH_AMBIGUOUS = "external_truth_ambiguous"


class PauseStatus(StrEnum):
    """The persisted states of a pause."""

    OPEN = "OPEN"
    HELD = "HELD"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"
    CANCELLED = "CANCELLED"


class EscalationCause(StrEnum):
    """What an escalation fired on."""

    BUDGET = "budget"
    DEADLINE = "deadline"
    AMBIGUITY = "ambiguity"


#: The reasons whose pause waits on a person's answer to a linked record.
PERSON_REASONS: Final = frozenset({PauseReason.PERMISSION, PauseReason.USER})

#: The reasons whose pause over a lost Run leaves the control outcome unknown.
UNKNOWN_OUTCOME_REASONS: Final = frozenset(
    {PauseReason.PROVIDER, PauseReason.EXTERNAL_TRUTH_AMBIGUOUS}
)


class ResumePredicate(Epoch2Model):
    """What would resume the work, and the system's latest reading of it.

    Attributes:
        description: The predicate in words.
        evaluator: What evaluates it.
        last_evaluated_at: When it was last evaluated.
        last_result: What that evaluation found.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    description: NonEmptyStr
    evaluator: ShortText
    last_evaluated_at: UtcDatetime | None = None
    last_result: bool | None = None

    @model_validator(mode="after")
    def _result_has_a_time(self) -> Self:
        if (self.last_evaluated_at is None) != (self.last_result is None):
            raise ValueError("last_evaluated_at and last_result are recorded together")
        return self


class PauseEscalation(Epoch2Model):
    """Why a pause escalated, and the record the escalation raised.

    Attributes:
        cause: What the escalation fired on.
        raised_at: When it fired.
        raised_ref: The pending action or successor pause it raised; the only ender.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    cause: EscalationCause
    raised_at: UtcDatetime
    raised_ref: PendingActionUrn | PauseKey


class RetryBudget(Epoch2Model):
    """Attempts spent against attempts the policy allows.

    Attributes:
        used: The attempts spent.
        allowed: The attempts allowed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    used: StrictNonNegativeInt
    allowed: StrictNonNegativeInt


class OpenPause(Epoch2Model):
    """One operational pause over named affected work.

    Attributes:
        key: The pause's key.
        scope_ref: The Run, Batch, Campaign, Release or Workspace it pauses.
        reason: Why it paused.
        health_evidence_refs: The daemon observations that opened it.
        resume_predicate: What would resume the work.
        deadline: When the policy bounds the wait.
        retry_budget: Attempts spent of attempts allowed.
        waiting_on_ref: The pending action or question a person-wait pause waits on.
        held_by: Who placed the Hold.
        hold_id: The Hold's id.
        escalation: Why the pause escalated and what it raised.
        status: The persisted state.
        opened_at: When the daemon opened it.
        resolved_at: When the resume predicate was observed.

    Raises:
        pydantic.ValidationError: A person-wait pause with no linked record,
            or another pause with one; a ``HELD`` pause without its holder
            and Hold id, or another pause with them; an ``ESCALATED`` pause
            without its escalation, or another pause with one; a resolution
            time outside ``RESOLVED``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: PauseKey
    scope_ref: RunUrn | BatchUrn | CampaignUrn | ReleaseUrn | WorkspaceUrn
    reason: PauseReason
    health_evidence_refs: tuple[EvidenceUrn, ...] = Field(min_length=1)
    resume_predicate: ResumePredicate
    deadline: UtcDatetime | None = None
    retry_budget: RetryBudget | None = None
    waiting_on_ref: PendingActionUrn | QuestionUrn | None = None
    held_by: PrincipalKey | None = None
    hold_id: ShortText | None = None
    escalation: PauseEscalation | None = None
    status: PauseStatus
    opened_at: UtcDatetime
    resolved_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _situation_fields_are_present(self) -> Self:
        """Require each field a situation reads exactly where it reads it.

        Raises:
            ValueError: One of the combinations the class docstring names.
        """
        if (self.reason in PERSON_REASONS) != (self.waiting_on_ref is not None):
            raise ValueError(
                f"waiting_on_ref is required for a permission or user pause only, "
                f"not {self.reason.value}"
            )
        held = self.status is PauseStatus.HELD
        if held != (self.held_by is not None) or held != (self.hold_id is not None):
            raise ValueError("held_by and hold_id are set exactly when the pause is HELD")
        if (self.status is PauseStatus.ESCALATED) != (self.escalation is not None):
            raise ValueError("escalation is set exactly when the pause is ESCALATED")
        if (self.status is PauseStatus.RESOLVED) != (self.resolved_at is not None):
            raise ValueError("resolved_at is set exactly when the pause is RESOLVED")
        return self


class PauseSituation(StrEnum):
    """The seven situations a pause projects to; a projection, never stored."""

    WAITING_ON_CHECK = "waiting_on_check"
    WAITING_ON_PERSON = "waiting_on_person"
    CONTROL_OUTCOME_UNKNOWN = "control_outcome_unknown"
    HELD = "held"
    ESCALATED = "escalated"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"


class PauseVerb(StrEnum):
    """What the lost-Run card lets an operator do."""

    RECONCILE = "reconcile"
    LET_GO = "let_go"


#: What would end each situation. Never empty: a pause frame with no ender
#: line is the defect this table exists to rule out.
ENDS_WHEN: Final = MappingProxyType(
    {
        PauseSituation.WAITING_ON_CHECK: "the predicate is observed",
        PauseSituation.WAITING_ON_PERSON: (
            "an eligible principal answers the linked pending action or question"
        ),
        PauseSituation.CONTROL_OUTCOME_UNKNOWN: (
            "the predicate is observed, or the operator reconciles or lets go"
        ),
        PauseSituation.HELD: (
            "the holder, or a principal of the same authority class, releases the Hold"
        ),
        PauseSituation.ESCALATED: (
            "this pause never ends; what ends is the record the escalation raised"
        ),
        PauseSituation.RESOLVED: "nothing - terminal",
        PauseSituation.CANCELLED: "nothing - terminal",
    }
)


@dataclass(frozen=True, slots=True)
class PauseProjection:
    """The situation a pause is in, and what its render reads.

    Attributes:
        situation: Which of the seven situations it is in.
        ends_when: What would end it.
        linked_ref: The record a person answers, or the record an escalation raised.
        held_by: Who holds it, when held.
        hold_id: The Hold's id, when held.
        escalation_cause: What an escalation fired on.
        verbs: What the card offers; retry is never among them.
    """

    situation: PauseSituation
    ends_when: str
    linked_ref: str | None = None
    held_by: str | None = None
    hold_id: str | None = None
    escalation_cause: EscalationCause | None = None
    verbs: tuple[PauseVerb, ...] = ()


def project_pause(pause: OpenPause, *, affected_run_lost: bool) -> PauseProjection:
    """Return the one situation *pause* is in.

    Args:
        pause: The persisted pause.
        affected_run_lost: Whether the affected Run is lost, which leaves the
            control outcome of a provider or ambiguity pause unknown.

    Returns:
        The projection; computed at every call and never stored.
    """
    status = pause.status
    if status is PauseStatus.HELD:
        situation = PauseSituation.HELD
        return PauseProjection(
            situation, ENDS_WHEN[situation], held_by=pause.held_by, hold_id=pause.hold_id
        )
    if status is PauseStatus.ESCALATED and pause.escalation is not None:
        raised = pause.escalation.raised_ref
        situation = PauseSituation.ESCALATED
        return PauseProjection(
            situation,
            ENDS_WHEN[situation],
            linked_ref=raised if isinstance(raised, str) else render_qualified_urn(raised),
            escalation_cause=pause.escalation.cause,
        )
    if status in (PauseStatus.RESOLVED, PauseStatus.CANCELLED):
        situation = PauseSituation(status.value.lower())
        return PauseProjection(situation, ENDS_WHEN[situation])
    if pause.waiting_on_ref is not None:
        situation = PauseSituation.WAITING_ON_PERSON
        return PauseProjection(
            situation, ENDS_WHEN[situation], linked_ref=render_qualified_urn(pause.waiting_on_ref)
        )
    if pause.reason in UNKNOWN_OUTCOME_REASONS and affected_run_lost:
        situation = PauseSituation.CONTROL_OUTCOME_UNKNOWN
        return PauseProjection(
            situation, ENDS_WHEN[situation], verbs=(PauseVerb.RECONCILE, PauseVerb.LET_GO)
        )
    situation = PauseSituation.WAITING_ON_CHECK
    return PauseProjection(situation, ENDS_WHEN[situation])


__all__ = [
    "ENDS_WHEN",
    "PERSON_REASONS",
    "UNKNOWN_OUTCOME_REASONS",
    "EscalationCause",
    "OpenPause",
    "PauseEscalation",
    "PauseProjection",
    "PauseReason",
    "PauseSituation",
    "PauseStatus",
    "PauseVerb",
    "ResumePredicate",
    "RetryBudget",
    "project_pause",
]
