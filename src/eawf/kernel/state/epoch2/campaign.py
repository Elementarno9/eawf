"""Campaign: a research plan of bounded steps, with the artifacts they wrote.

A Campaign's plan is an ordered list of steps, each one bounded Run over one
question of the Campaign's graph. A step stores only ``pending``, ``running``
or ``done``; ``blocked`` is what a pending step *is* while a dependency is
unmet or an open contradiction stands against it, so it is derived by
:func:`step_blockers` and never persisted, and a blocked step always names
what blocks it.

Every bound is an axis pair: a declared ``limit`` and the observed ``spent``
in one shared unit, so a route renders spend against its limit and derives
the remainder rather than storing one. A step's limit on an axis never
exceeds the Campaign's limit on the same axis.

A plan orders its methods by the Campaign's :class:`CampaignMethodPolicy`: a
step never works a method the policy places before the method of a step it
depends on, so a synthesis never feeds a survey. A Campaign that stops says
why in its :class:`CampaignStop`, so a Campaign a hard budget axis starved is
never read as one that converged.

The artifacts a Campaign keeps are addressed by revision: an
:data:`ArtifactRevisionRef` names one immutable
:class:`~eawf.kernel.state.epoch2.artifact_revision.ArtifactRevision`, and the
Campaign and its steps list those references append-only.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from eawf.kernel.spec.release import Sha256DigestStr
from eawf.kernel.state.enums import CampaignStatus
from eawf.kernel.state.epoch2.base import (
    Epoch2Model,
    StrictNonNegativeInt,
    StrictPositiveInt,
)
from eawf.kernel.state.epoch2.urns import (
    CampaignUrn,
    ClaimUrn,
    EvidenceUrn,
    QuestionUrn,
    RunUrn,
    TrackUrn,
)
from eawf.kernel.state.epoch2.values import Epoch2Record
from eawf.kernel.state.types import UtcDatetime

#: One artifact revision: the artifact's URN with the revision as its fragment.
ArtifactRevisionRef = Annotated[
    str,
    StringConstraints(
        strict=True, max_length=300, pattern=r"^urn:eawf:v1:artifact:[^:?#/]+/ART-\d{4,}#r[1-9]\d*$"
    ),
]

#: A short unit or progress label, such as ``runs replayed`` or ``h``.
ShortText = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=40)
]

#: How a spend or a progress count was observed; a derived or estimated
#: value renders with its marker rather than as a bare number.
Quality = Literal["measured", "derived", "estimated", "unavailable"]

#: The one line a done step states it showed.
StepOutcome = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=500)
]

StepTitle = Annotated[
    str,
    StringConstraints(
        strict=True, strip_whitespace=True, min_length=1, max_length=72, pattern=r"^.*[^.]$"
    ),
]


def revision_ref(artifact_ref: str, revision: int) -> str:
    """Return the reference that names *revision* of *artifact_ref*."""
    return f"{artifact_ref}#r{revision}"


class CampaignMethod(StrEnum):
    """How a step works its question."""

    SURVEY = "survey"
    DEPTH = "depth"
    ADVERSARIAL = "adversarial"
    GAP = "gap"
    SYNTHESIS = "synthesis"


class StepState(StrEnum):
    """What a step's record stores; ``blocked`` is derived and never one of these."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"


BudgetAxisKind = Literal["wall_time", "tokens", "cost", "sources", "rounds"]

#: Why a Campaign stopped dispatching.
StopReason = Literal["budget_exhausted", "converged", "cancelled"]


class CampaignMethodPolicy(Epoch2Model):
    """The order a Campaign's plan may work its methods in.

    Attributes:
        order: The allowed methods, earliest first. A step may work only a
            listed method, and never one listed before the method of a step
            it depends on.

    Raises:
        pydantic.ValidationError: No method, or one method twice.
    """

    order: Annotated[tuple[CampaignMethod, ...], Field(min_length=1)] = tuple(CampaignMethod)

    @model_validator(mode="after")
    def _each_method_once(self) -> Self:
        """Refuse a method listed twice, since its position would be ambiguous."""
        if len(set(self.order)) != len(self.order):
            raise ValueError(f"the method order repeats a method: {list(self.order)}")
        return self

    def violations(self, steps: tuple[CampaignPlanStep, ...]) -> tuple[str, ...]:
        """Return each step whose method the policy does not allow where it stands.

        Args:
            steps: Every step of a plan.

        Returns:
            One sentence per refused step; empty when the plan keeps the order.
        """
        rank = {method: index for index, method in enumerate(self.order)}
        methods = {step.ordinal: step.method for step in steps}
        refused: list[str] = []
        for step in steps:
            if step.method not in rank:
                refused.append(f"step {step.ordinal} works {step.method.value}, outside the policy")
                continue
            for ordinal in step.depends_on:
                before = methods.get(ordinal)
                if before is not None and rank.get(before, -1) > rank[step.method]:
                    refused.append(
                        f"step {step.ordinal} works {step.method.value} after step {ordinal}'s "
                        f"{before.value}, which the policy orders later"
                    )
        return tuple(refused)


class BudgetAxis(Epoch2Model):
    """One bounded axis: a limit and the spend against it, in one unit.

    Attributes:
        axis_kind: What the axis measures.
        limit: The declared ceiling.
        spent: The observed consumption; never decremented.
        unit: The unit both sides are in.
        spent_quality: How the spend was observed.
        hard: Whether reaching the limit stops the Campaign.
    """

    axis_kind: BudgetAxisKind
    limit: StrictPositiveInt
    spent: StrictNonNegativeInt = 0
    unit: ShortText
    spent_quality: Quality = "measured"
    hard: bool = True


class ResearchBudget(Epoch2Model):
    """The bounded axes of a Campaign or of one of its steps.

    Raises:
        pydantic.ValidationError: No axis, or one axis kind twice.
    """

    axes: Annotated[tuple[BudgetAxis, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _one_row_per_kind(self) -> Self:
        """Refuse an axis kind declared twice, since its pair would be ambiguous."""
        kinds = [axis.axis_kind for axis in self.axes]
        if len(kinds) != len(set(kinds)):
            raise ValueError(f"budget axes repeat a kind: {sorted(kinds)}")
        return self

    def axis(self, kind: str) -> BudgetAxis | None:
        """Return the axis of *kind*, or ``None`` when it is not bounded."""
        return next((axis for axis in self.axes if axis.axis_kind == kind), None)


class NamedProgress(Epoch2Model):
    """A named numerator; a bare percentage is never stored.

    Raises:
        pydantic.ValidationError: More done than there is in total.
    """

    done: StrictNonNegativeInt
    total: StrictPositiveInt
    unit: ShortText
    quality: Quality

    @model_validator(mode="after")
    def _done_within_total(self) -> Self:
        """Refuse a count past its total."""
        if self.done > self.total:
            raise ValueError(f"progress {self.done} of {self.total} counts past its total")
        return self


class CampaignPlanStep(Epoch2Model):
    """One step of a Campaign plan: one bounded Run over one question.

    Attributes:
        ordinal: The step's place and public name, from one.
        title: The step in words.
        method: How it works its question.
        question_ref: The question node it works.
        depends_on: The steps that must be done before it starts.
        blocking_contradiction_refs: Open contradictions that block its start.
        state: What its record stores.
        run_refs: Its runners, append-only; the last is the current one.
        bound: Its axis pairs, each within the Campaign's.
        started_at: When it first ran.
        ended_at: When it finished.
        outcome: What it showed; required once done.
        produced: The evidence and artifact revisions it produced, append-only.
        progress: The runner's named progress.
        revision: The step's compare-and-swap token.

    Raises:
        pydantic.ValidationError: The step depends on itself, or its state
            disagrees with its timestamps, runner or outcome. Dependencies on
            other steps are judged with the whole plan.
    """

    ordinal: StrictPositiveInt
    title: StepTitle
    method: CampaignMethod
    question_ref: QuestionUrn
    depends_on: tuple[StrictPositiveInt, ...] = ()
    blocking_contradiction_refs: tuple[ClaimUrn, ...] = ()
    state: StepState = StepState.PENDING
    run_refs: tuple[RunUrn, ...] = ()
    bound: ResearchBudget
    started_at: UtcDatetime | None = None
    ended_at: UtcDatetime | None = None
    outcome: StepOutcome | None = None
    produced: tuple[EvidenceUrn | ArtifactRevisionRef, ...] = ()
    progress: NamedProgress | None = None
    revision: StrictPositiveInt = 1

    @model_validator(mode="after")
    def _state_agrees_with_its_facts(self) -> Self:
        """Keep the timestamps, the runner and the outcome in step with the state.

        Raises:
            ValueError: A running or done step with no start or no runner, a
                done step with no end or no outcome, or an outcome before done.
        """
        if self.ordinal in self.depends_on:
            raise ValueError(f"step {self.ordinal} depends on itself")
        started = self.state is not StepState.PENDING
        if started and (self.started_at is None or not self.run_refs):
            raise ValueError(f"a {self.state.value} step states when it started and its runner")
        done = self.state is StepState.DONE
        if done != (self.outcome is not None) or done != (self.ended_at is not None):
            raise ValueError("a done step states its outcome and its end, and no other step does")
        return self


class CampaignStop(Epoch2Model):
    """Why a Campaign stopped dispatching steps, and when.

    Attributes:
        reason: What stopped it.
        axis_kind: The hard axis that reached its limit, for a budget stop.
        detail: The stop in one line.
        stopped_at: When it stopped.

    Raises:
        pydantic.ValidationError: A budget stop that names no axis, or
            another stop that names one.
    """

    reason: StopReason
    axis_kind: BudgetAxisKind | None = None
    detail: StepOutcome
    stopped_at: UtcDatetime

    @model_validator(mode="after")
    def _budget_stop_names_its_axis(self) -> Self:
        """Name the exhausted axis exactly when the budget stopped the Campaign."""
        if (self.reason == "budget_exhausted") != (self.axis_kind is not None):
            raise ValueError("a budget stop names its axis, and no other stop does")
        return self


def exhausted_axis(budget: ResearchBudget) -> BudgetAxis | None:
    """Return the first hard axis whose spend reached its limit, or ``None``."""
    return next((a for a in budget.axes if a.hard and a.spent >= a.limit), None)


def step_blockers(step: CampaignPlanStep, steps: tuple[CampaignPlanStep, ...]) -> tuple[str, ...]:
    """Return what keeps a pending step from starting, by name; empty when nothing does.

    Args:
        step: The step asked about.
        steps: Every step of its plan.

    Returns:
        ``step <n>`` for each unmet dependency, then each open contradiction's key.
        Empty for a step that is not pending.
    """
    if step.state is not StepState.PENDING:
        return ()
    done = {other.ordinal for other in steps if other.state is StepState.DONE}
    unmet = tuple(f"step {n}" for n in step.depends_on if n not in done)
    return unmet + tuple(ref.entity_key for ref in step.blocking_contradiction_refs)


def _check_plan(steps: tuple[CampaignPlanStep, ...], budget: ResearchBudget) -> None:
    """Refuse a plan whose ordinals, dependencies or bounds do not hold.

    Raises:
        ValueError: Ordinals not ``1..n`` in order, a dependency on a missing
            step, a dependency cycle, or a step axis outside the Campaign's.
    """
    ordinals = [step.ordinal for step in steps]
    if ordinals != list(range(1, len(steps) + 1)):
        raise ValueError(f"plan step ordinals must run 1..{len(steps)} in order, got {ordinals}")
    graph = {step.ordinal: set(step.depends_on) for step in steps}
    for step in steps:
        missing = graph[step.ordinal] - set(graph)
        if missing:
            raise ValueError(f"step {step.ordinal} depends on missing steps {sorted(missing)}")
    placed: set[int] = set()
    while len(placed) < len(graph):
        ready = {n for n, deps in graph.items() if n not in placed and deps <= placed}
        if not ready:
            raise ValueError(f"plan steps {sorted(set(graph) - placed)} depend on each other")
        placed |= ready
    for step in steps:
        for axis in step.bound.axes:
            ceiling = budget.axis(axis.axis_kind)
            if ceiling is None or ceiling.unit != axis.unit:
                raise ValueError(
                    f"step {step.ordinal} bounds {axis.axis_kind} in {axis.unit!r}, "
                    "which the Campaign does not bound in that unit"
                )
            if axis.limit > ceiling.limit:
                raise ValueError(
                    f"step {step.ordinal} limits {axis.axis_kind} to {axis.limit}, "
                    f"above the Campaign's {ceiling.limit}"
                )


class Campaign(Epoch2Record):
    """A research Campaign with its approved plan, keyed ``CAM-####``.

    Attributes:
        urn: The Campaign's address.
        track_ref: The one Track that owns it.
        title: What it researches, in one line.
        status: Where it stands.
        stop: Why it stopped dispatching; set before it leaves ``active`` and
            whenever a hard axis reached its limit.
        evidence_budget: Its axis pairs.
        method_policy: The order its plan may work methods in.
        plan_revision: Which approved plan the steps are.
        approved_plan_digest: The digest of the approved steps.
        plan_steps: The approved plan, in order.
        artifact_revision_refs: Every artifact revision it keeps, append-only.

    Raises:
        pydantic.ValidationError: The plan's ordinals, dependencies, methods
            or bounds do not hold, or the status and the stop disagree.
    """

    urn: CampaignUrn
    track_ref: TrackUrn
    title: StepTitle
    status: CampaignStatus = CampaignStatus.ACTIVE
    stop: CampaignStop | None = None
    evidence_budget: ResearchBudget
    method_policy: CampaignMethodPolicy = CampaignMethodPolicy()
    plan_revision: StrictPositiveInt = 1
    approved_plan_digest: Sha256DigestStr
    plan_steps: Annotated[tuple[CampaignPlanStep, ...], Field(min_length=1)]
    artifact_revision_refs: tuple[ArtifactRevisionRef, ...] = ()

    @model_validator(mode="after")
    def _plan_holds(self) -> Self:
        """Check the plan against itself, the method policy and the Campaign's bounds."""
        _check_plan(self.plan_steps, self.evidence_budget)
        refused = self.method_policy.violations(self.plan_steps)
        if refused:
            raise ValueError(f"the plan breaks the method policy: {'; '.join(refused)}")
        return self

    @model_validator(mode="after")
    def _status_agrees_with_its_stop(self) -> Self:
        """Require a stop off ``active``, and a cancel stop exactly when cancelled.

        Raises:
            ValueError: A converged or cancelled Campaign that states no stop,
                or a cancel stop on a Campaign that is not cancelled.
        """
        if self.status is not CampaignStatus.ACTIVE and self.stop is None:
            raise ValueError(f"a {self.status.value} Campaign states why it stopped")
        cancelled = self.stop is not None and self.stop.reason == "cancelled"
        if cancelled != (self.status is CampaignStatus.CANCELLED):
            raise ValueError("a cancel stop belongs to a cancelled Campaign, and only to one")
        return self

    def step(self, ordinal: int) -> CampaignPlanStep | None:
        """Return step *ordinal*, or ``None`` when the plan has none."""
        return next((step for step in self.plan_steps if step.ordinal == ordinal), None)


__all__ = [
    "ArtifactRevisionRef",
    "BudgetAxis",
    "Campaign",
    "CampaignMethod",
    "CampaignMethodPolicy",
    "CampaignPlanStep",
    "CampaignStop",
    "NamedProgress",
    "ResearchBudget",
    "StepState",
    "StopReason",
    "exhausted_axis",
    "revision_ref",
    "step_blockers",
]
