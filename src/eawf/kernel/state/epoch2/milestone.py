"""Milestone, its leaf objects, and the strict create document.

A Milestone is the unit an operator accepts, so the three fields that
decide whether acceptance is possible are validated the hardest: what is
explicitly out of scope, what journey proves the outcome, and which
Batches must complete first.

``exclusions`` is required and never empty. An absent exclusion list and
a considered-and-empty one look identical once persisted, and the
difference is exactly what a reviewer needs: the first is an unanswered
question and the second is an answer. The reserved sole marker records
the answer, and it cannot sit beside a real exclusion because the pair
would assert both at once.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Literal, Self

from pydantic import StrictBool, field_validator, model_validator

from eawf.kernel.spec.common import EvidenceKind
from eawf.kernel.state.enums import EffortBucket
from eawf.kernel.state.epoch2.base import (
    AcceptanceStepId,
    Epoch2Model,
    MilestoneKey,
    NonEmptyStr,
    OutcomeStr,
    StrictPositiveInt,
    TitleStr,
    normalize_phrase,
    reject_normalized_duplicates,
)
from eawf.kernel.state.epoch2.urns import BatchUrn, MilestoneUrn, TrackUrn
from eawf.kernel.state.epoch2.values import Epoch2Record, ExactRevisionBinding

#: The sole reserved ``exclusions`` entry recording that exclusions were
#: considered and none apply. Canonical serialisation keeps the marker;
#: only the renderer spells it as prose.
EXCLUSIONS_NONE_MARKER: Final = "none"

#: The prefix every acceptance-step id carries, stripped to read the
#: ordinal that fixes the step's position in the journey.
_STEP_ID_PREFIX: Final = "AS-"


class MilestoneKind(StrEnum):
    """What sort of outcome a Milestone delivers.

    Product configuration, not lifecycle: a kind selects which Milestones
    a Track's policy admits and never adds a state to the transition
    table.
    """

    PRODUCT = "product"
    PLATFORM = "platform"
    MIGRATION = "migration"
    SECURITY = "security"
    OPERATIONS = "operations"
    RESEARCH = "research"


class MilestoneStatus(StrEnum):
    """The stored lifecycle states of a Milestone."""

    PLANNED = "PLANNED"
    ACTIVE = "ACTIVE"
    ACCEPTANCE_REVIEW = "ACCEPTANCE_REVIEW"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


#: The states in which the acceptance bundle has been sealed, so its
#: revision is a recorded fact rather than a projection.
_BUNDLE_SEALED: Final = frozenset({MilestoneStatus.ACCEPTANCE_REVIEW, MilestoneStatus.COMPLETED})


class DurationBudget(Epoch2Model):
    """An appetite expressed as a bounded stretch of calendar time.

    The declared unit is stored as declared. Normalising weeks into hours
    at write time would make the rendered appetite disagree with what the
    operator typed, and the normalisation a renderer wants is cheap to
    redo.
    """

    budget_kind: Literal["duration"]
    amount: StrictPositiveInt
    unit: Literal["hours", "days", "weeks"]


def validate_evidence_kinds(value: tuple[EvidenceKind, ...]) -> tuple[EvidenceKind, ...]:
    """Return *value* when it names at least one distinct kind of proof.

    Args:
        value: The evidence kinds a step yields.

    Returns:
        *value* unchanged.

    Raises:
        ValueError: The tuple is empty or names one kind twice.
    """
    if not value:
        raise ValueError("evidence_kinds must name at least one kind of proof")
    if len(set(value)) != len(value):
        raise ValueError(f"evidence_kinds repeats a kind: {value}")
    return value


class AcceptanceStep(Epoch2Model):
    """One step of the journey that demonstrates a Milestone's outcome.

    ``evidence_kinds`` is non-empty because a step naming no evidence
    cannot be shown to have happened. An optional step stays visible so
    the journey reads completely, but it can never satisfy a required
    acceptance gate.
    """

    step_id: AcceptanceStepId
    actor: Literal["operator", "system"]
    action: NonEmptyStr
    expected_observation: NonEmptyStr
    evidence_kinds: tuple[EvidenceKind, ...]
    required: StrictBool = True

    @field_validator("evidence_kinds")
    @classmethod
    def _evidence_kinds_present_and_unique(
        cls, value: tuple[EvidenceKind, ...]
    ) -> tuple[EvidenceKind, ...]:
        """Require at least one distinct evidence kind.

        Raises:
            ValueError: The tuple is empty or names one kind twice.
        """
        return validate_evidence_kinds(value)


def validate_exclusions(value: tuple[str, ...]) -> tuple[str, ...]:
    """Return *value* when it forms a legal exclusion list.

    Args:
        value: The declared exclusions, in author order.

    Returns:
        *value* unchanged.

    Raises:
        ValueError: The list is empty, repeats a normalised phrase, or
            puts the reserved marker beside a real exclusion.
    """
    if not value:
        raise ValueError(
            "exclusions must be non-empty; use the reserved "
            f"{EXCLUSIONS_NONE_MARKER!r} marker when none apply"
        )
    reject_normalized_duplicates(value, field="exclusions")
    marker = normalize_phrase(EXCLUSIONS_NONE_MARKER)
    if len(value) > 1 and any(normalize_phrase(entry) == marker for entry in value):
        raise ValueError(
            f"the reserved {EXCLUSIONS_NONE_MARKER!r} marker cannot sit beside a real exclusion"
        )
    return value


def validate_acceptance_journey(value: tuple[AcceptanceStep, ...]) -> tuple[AcceptanceStep, ...]:
    """Return *value* when its steps are unique and in ascending order.

    Args:
        value: The acceptance steps, in the order they are performed.

    Returns:
        *value* unchanged.

    Raises:
        ValueError: The journey is empty, repeats a step id, or lists the
            steps out of id order.
    """
    if not value:
        raise ValueError("acceptance_journey must name at least one step")
    step_ids = [step.step_id for step in value]
    if len(set(step_ids)) != len(step_ids):
        raise ValueError(f"acceptance_journey repeats a step id: {step_ids}")
    ordinals = [int(step_id.removeprefix(_STEP_ID_PREFIX)) for step_id in step_ids]
    if ordinals != sorted(ordinals):
        raise ValueError(f"acceptance_journey lists steps out of id order: {step_ids}")
    return value


def reject_duplicate_refs(value: tuple[object, ...], *, field: str) -> None:
    """Raise when *value* names the same record twice.

    Args:
        value: The reference tuple to check.
        field: The field name to name in the rejection message.

    Raises:
        ValueError: Two entries address the same record.
    """
    if len(set(value)) != len(value):
        raise ValueError(f"{field} names the same record twice")


def _check_distinct_placement(
    *,
    primary_track_ref: object,
    contributing_track_refs: tuple[object, ...],
    required_batch_refs: tuple[object, ...],
) -> None:
    """Raise when a Milestone's placement references overlap or repeat.

    Args:
        primary_track_ref: The Track that leads the Milestone.
        contributing_track_refs: Tracks that contribute to it.
        required_batch_refs: Batches that must complete before acceptance.

    Raises:
        ValueError: A reference repeats, or the primary Track also
            appears as a contributor.
    """
    reject_duplicate_refs(contributing_track_refs, field="contributing_track_refs")
    reject_duplicate_refs(required_batch_refs, field="required_batch_refs")
    if primary_track_ref in contributing_track_refs:
        raise ValueError("contributing_track_refs repeats the primary Track")


class MilestoneCreateSpec(Epoch2Model):
    """The strict create document for a Milestone.

    Status, identity and acceptance proof are absent by construction: they
    are outcomes of the create transaction, and accepting them as inputs
    would let a caller declare a Milestone already accepted. Unknown keys
    are refused, so supplying one is a loader rejection rather than a
    silently dropped field.
    """

    key: MilestoneKey
    primary_track_ref: TrackUrn
    contributing_track_refs: tuple[TrackUrn, ...] = ()
    title: TitleStr
    description: NonEmptyStr | None = None
    outcome: OutcomeStr
    appetite: EffortBucket | DurationBudget
    exclusions: tuple[NonEmptyStr, ...]
    acceptance_journey: tuple[AcceptanceStep, ...]
    required_batch_refs: tuple[BatchUrn, ...] = ()

    @field_validator("exclusions")
    @classmethod
    def _exclusions_are_legal(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Require a non-empty, duplicate-free exclusion list.

        Raises:
            ValueError: The list breaks an exclusion rule.
        """
        return validate_exclusions(value)

    @field_validator("acceptance_journey")
    @classmethod
    def _journey_is_ordered(cls, value: tuple[AcceptanceStep, ...]) -> tuple[AcceptanceStep, ...]:
        """Require a non-empty journey of unique, ascending steps.

        Raises:
            ValueError: The journey breaks a step-ordering rule.
        """
        return validate_acceptance_journey(value)

    @model_validator(mode="after")
    def _placement_refs_are_distinct(self) -> Self:
        """Require the placement references to be unique and disjoint.

        Raises:
            ValueError: A reference repeats or the primary Track also
                contributes.
        """
        _check_distinct_placement(
            primary_track_ref=self.primary_track_ref,
            contributing_track_refs=self.contributing_track_refs,
            required_batch_refs=self.required_batch_refs,
        )
        return self


class Milestone(Epoch2Record):
    """One accepted-or-not outcome owned by a primary Track.

    Placement is independent of identity: the Milestone keeps its key and
    URN when a contributing Track is added, when a required Batch is
    repaired, and when a Release packages it.
    """

    key: MilestoneKey
    urn: MilestoneUrn
    primary_track_ref: TrackUrn
    contributing_track_refs: tuple[TrackUrn, ...] = ()
    title: TitleStr
    description: NonEmptyStr | None = None
    outcome: OutcomeStr
    appetite: EffortBucket | DurationBudget
    exclusions: tuple[NonEmptyStr, ...]
    acceptance_journey: tuple[AcceptanceStep, ...]
    required_batch_refs: tuple[BatchUrn, ...] = ()
    status: MilestoneStatus
    acceptance_bundle_revision: StrictPositiveInt | None = None
    accepted_binding: ExactRevisionBinding | None = None

    @field_validator("exclusions")
    @classmethod
    def _exclusions_are_legal(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Require a non-empty, duplicate-free exclusion list.

        Raises:
            ValueError: The list breaks an exclusion rule.
        """
        return validate_exclusions(value)

    @field_validator("acceptance_journey")
    @classmethod
    def _journey_is_ordered(cls, value: tuple[AcceptanceStep, ...]) -> tuple[AcceptanceStep, ...]:
        """Require a non-empty journey of unique, ascending steps.

        Raises:
            ValueError: The journey breaks a step-ordering rule.
        """
        return validate_acceptance_journey(value)

    @model_validator(mode="after")
    def _placement_refs_are_distinct(self) -> Self:
        """Require the placement references to be unique and disjoint.

        Raises:
            ValueError: A reference repeats or the primary Track also
                contributes.
        """
        _check_distinct_placement(
            primary_track_ref=self.primary_track_ref,
            contributing_track_refs=self.contributing_track_refs,
            required_batch_refs=self.required_batch_refs,
        )
        return self

    @model_validator(mode="after")
    def _acceptance_proof_matches_status(self) -> Self:
        """Require the acceptance proof exactly where the status implies it.

        Raises:
            ValueError: A sealed-bundle status carries no bundle revision,
                a completed Milestone carries no accepted binding, or an
                unfinished Milestone carries one.
        """
        if self.status in _BUNDLE_SEALED and self.acceptance_bundle_revision is None:
            raise ValueError(f"status {self.status.value} requires acceptance_bundle_revision")
        completed = self.status is MilestoneStatus.COMPLETED
        if completed and self.accepted_binding is None:
            raise ValueError("a COMPLETED Milestone requires accepted_binding")
        if not completed and self.accepted_binding is not None:
            raise ValueError(
                f"accepted_binding belongs to a COMPLETED Milestone, not {self.status.value}"
            )
        return self


__all__ = [
    "EXCLUSIONS_NONE_MARKER",
    "AcceptanceStep",
    "DurationBudget",
    "Milestone",
    "MilestoneCreateSpec",
    "MilestoneKind",
    "MilestoneStatus",
    "reject_duplicate_refs",
    "validate_acceptance_journey",
    "validate_evidence_kinds",
    "validate_exclusions",
]
