"""Decision: a ratified choice that keeps its alternatives, evidence and chain.

A Decision records what was chosen, what else was on the table, what the
choice stands on and which questions it settled, so a later reader can
rebuild the reasoning rather than trust the conclusion. It moves through
four states and has no reversal edge: changing a decision is a new
Decision that supersedes the old one, which stays immutable and keeps its
place in the chain. A chain that cycles back on itself would make "which
decision is in force" unanswerable, so :func:`validate_decision_chain`
refuses one.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Annotated, Final, Self

from pydantic import ConfigDict, StringConstraints, model_validator

from eawf.kernel.state.epoch2.base import (
    Epoch2Model,
    NonEmptyStr,
    PrincipalKey,
    StrictPositiveInt,
    TitleStr,
)
from eawf.kernel.state.epoch2.urns import AnyEntityUrn, EvidenceUrn, QuestionUrn
from eawf.kernel.state.models import HypothesisIdStr
from eawf.kernel.state.types import UtcDatetime

#: A decision key. The existing ``D<NN>`` ids are retained as the key
#: grammar, so a decision already cited by that id keeps its identity.
DecisionKey = Annotated[str, StringConstraints(strict=True, pattern=r"^D\d{2,}$")]

#: An option key, compared rather than displayed.
OptionKey = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_]{0,31}$")]

#: The fewest alternatives a decision weighs unless it says why there was no other.
MIN_ALTERNATIVES: Final = 2


class DecisionStatus(StrEnum):
    """The persisted states of a Decision; there is deliberately no reversed state."""

    PROPOSED = "PROPOSED"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    OBSOLETE = "OBSOLETE"


class DecisionOption(Epoch2Model):
    """One alternative a decision weighed.

    Attributes:
        key: The option's stable key.
        label: The option in words.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: OptionKey
    label: TitleStr


class DecisionError(ValueError):
    """A decision move was refused.

    Attributes:
        code: The stable refusal code a caller branches on.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class Decision(Epoch2Model):
    """One decision, from proposal to supersession.

    Attributes:
        key: The decision's key.
        scope_ref: The entity or surface the decision governs.
        title: The decision in one line.
        decision: What was decided.
        rationale: Why.
        alternatives: The options weighed.
        no_alternative_reason: Why fewer than two options were weighed.
        chosen_option_key: The option chosen.
        consequences: What becomes easier or harder; required before activation.
        evidence_refs: The evidence the choice stands on.
        hypothesis_refs: The Hypotheses whose verdicts it rests on.
        resolved_question_refs: The questions it settled.
        effective_policy_revision: The policy revision it applies from.
        status: The persisted state.
        supersedes: The decision this one replaces.
        superseded_by: The decision that replaced this one.
        ratified_by: The operator who ratified it.
        ratified_at: When.
        created_at: When it was proposed.

    Raises:
        pydantic.ValidationError: Fewer than two options with no reason, a
            repeated or unresolved option key, no evidence, no consequences
            once active, or ratification and supersession fields outside
            the states that carry them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: DecisionKey
    scope_ref: AnyEntityUrn
    title: TitleStr
    decision: NonEmptyStr
    rationale: NonEmptyStr
    alternatives: tuple[DecisionOption, ...]
    no_alternative_reason: NonEmptyStr | None = None
    chosen_option_key: OptionKey
    consequences: tuple[NonEmptyStr, ...] = ()
    evidence_refs: tuple[EvidenceUrn, ...]
    hypothesis_refs: tuple[HypothesisIdStr, ...] = ()
    resolved_question_refs: tuple[QuestionUrn, ...] = ()
    effective_policy_revision: StrictPositiveInt
    status: DecisionStatus = DecisionStatus.PROPOSED
    supersedes: DecisionKey | None = None
    superseded_by: DecisionKey | None = None
    ratified_by: PrincipalKey | None = None
    ratified_at: UtcDatetime | None = None
    created_at: UtcDatetime

    @model_validator(mode="after")
    def _options_and_evidence_are_complete(self) -> Self:
        """Refuse a decision whose options or evidence are incomplete.

        Raises:
            ValueError: One of the shapes the class docstring names.
        """
        keys = [o.key for o in self.alternatives]
        if len(keys) < MIN_ALTERNATIVES and self.no_alternative_reason is None:
            raise ValueError(
                f"decision {self.key} weighs {len(keys)} option(s); name at least "
                f"{MIN_ALTERNATIVES} or say why there was no alternative"
            )
        if len(set(keys)) != len(keys):
            raise ValueError(f"decision {self.key} names the same option key twice")
        if self.chosen_option_key not in keys:
            raise ValueError(f"chosen_option_key {self.chosen_option_key!r} is not an option")
        if not self.evidence_refs:
            raise ValueError(f"decision {self.key} names no evidence its choice stands on")
        return self

    @model_validator(mode="after")
    def _lifecycle_fields_match_status(self) -> Self:
        """Require the ratification and supersession fields exactly where the status admits them.

        Raises:
            ValueError: One of the shapes the class docstring names.
        """
        if self.supersedes == self.key or self.superseded_by == self.key:
            raise ValueError(f"decision {self.key} cannot supersede itself")
        ratified = self.status in (DecisionStatus.ACTIVE, DecisionStatus.SUPERSEDED)
        if ratified and not self.consequences:
            raise ValueError(f"decision {self.key} names its consequences before activation")
        if self.ratified_by is not None and self.ratified_at is None:
            raise ValueError("ratified_by and ratified_at are recorded together")
        # An obsolete decision may or may not have been ratified first.
        if ratified and self.ratified_by is None:
            raise ValueError(f"a {self.status.value} decision names who ratified it")
        if self.status is DecisionStatus.PROPOSED and self.ratified_by is not None:
            raise ValueError("a proposed decision has not been ratified")
        if (self.status is DecisionStatus.SUPERSEDED) != (self.superseded_by is not None):
            raise ValueError("superseded_by is set exactly when the decision is SUPERSEDED")
        return self


def ratify(decision: Decision, *, by: str, at: UtcDatetime) -> Decision:
    """Return *decision* ratified by operator *by*.

    Args:
        decision: A proposed decision.
        by: The ratifying operator's principal key.
        at: When.

    Returns:
        The successor record at ``ACTIVE``.

    Raises:
        DecisionError: ``decision_transition_illegal`` unless *decision* is proposed.
        pydantic.ValidationError: It names no consequences yet.
    """
    _require(decision, DecisionStatus.ACTIVE, DecisionStatus.PROPOSED)
    return decision.model_validate(
        {
            **decision.model_dump(),
            "status": DecisionStatus.ACTIVE,
            "ratified_by": by,
            "ratified_at": at,
        }
    )


def supersede(old: Decision, new: Decision) -> Decision:
    """Return *old* superseded by the ratified *new*, which must cite it.

    Args:
        old: The decision in force.
        new: The active decision replacing it.

    Returns:
        *old*'s successor record at ``SUPERSEDED``; *new* is unchanged.

    Raises:
        DecisionError: ``decision_transition_illegal`` unless *old* is active;
            ``decision_supersession_invalid`` unless *new* is active, cites
            *old* and governs the same scope.
    """
    _require(old, DecisionStatus.SUPERSEDED, DecisionStatus.ACTIVE)
    if new.status is not DecisionStatus.ACTIVE or new.supersedes != old.key:
        raise DecisionError(
            "decision_supersession_invalid",
            f"{old.key} is superseded only by an active decision that cites it",
        )
    if new.scope_ref != old.scope_ref:
        raise DecisionError(
            "decision_supersession_invalid", f"{new.key} governs another scope than {old.key}"
        )
    return old.model_validate(
        {**old.model_dump(), "status": DecisionStatus.SUPERSEDED, "superseded_by": new.key}
    )


def obsolete(decision: Decision) -> Decision:
    """Return *decision* retired because its scope no longer applies.

    Args:
        decision: A proposed or active decision.

    Returns:
        The successor record at ``OBSOLETE``.

    Raises:
        DecisionError: ``decision_transition_illegal`` from a terminal state.
    """
    _require(decision, DecisionStatus.OBSOLETE, DecisionStatus.PROPOSED, DecisionStatus.ACTIVE)
    return decision.model_validate({**decision.model_dump(), "status": DecisionStatus.OBSOLETE})


def _require(decision: Decision, to: DecisionStatus, *sources: DecisionStatus) -> None:
    """Refuse moving *decision* to *to* unless it is in one of *sources*."""
    if decision.status not in sources:
        raise DecisionError(
            "decision_transition_illegal",
            f"decision {decision.key} is {decision.status.value}; it does not move to {to.value}",
        )


def validate_decision_chain(decisions: Iterable[Decision]) -> None:
    """Refuse a set of decisions whose supersession links dangle, disagree or cycle.

    Args:
        decisions: Every decision in the set.

    Raises:
        DecisionError: ``decision_chain_invalid`` when a link names a missing
            decision, the two ends of a link disagree, or following
            ``superseded_by`` returns to a decision already visited.
    """
    by_key = {d.key: d for d in decisions}
    for d in by_key.values():
        if d.superseded_by is not None:
            target = by_key.get(d.superseded_by)
            if target is None or target.supersedes != d.key:
                raise DecisionError(
                    "decision_chain_invalid",
                    f"{d.key} is superseded by {d.superseded_by}, which does not cite it",
                )
        if d.supersedes is not None and d.supersedes not in by_key:
            raise DecisionError(
                "decision_chain_invalid", f"{d.key} supersedes unknown {d.supersedes}"
            )
    for start in by_key.values():
        seen = {start.key}
        cursor = start.superseded_by
        while cursor is not None:
            if cursor in seen:
                raise DecisionError(
                    "decision_chain_invalid", f"supersession cycles through {cursor}"
                )
            seen.add(cursor)
            cursor = by_key[cursor].superseded_by


__all__ = [
    "Decision",
    "DecisionError",
    "DecisionKey",
    "DecisionOption",
    "DecisionStatus",
    "obsolete",
    "ratify",
    "supersede",
    "validate_decision_chain",
]
