"""The in-flight governor: admission across Runs, decided before any starts.

A Run's compiled ceilings bound one attempt; they do not bound a fleet.
The governor bounds the work in flight across a root, and it decides
before a provider starts: a Run that would breach a ceiling is queued or
denied, never started and then cancelled.

Admission reserves what a Run may spend, not merely that it runs. A Run
reserves its sealed token and cost caps, which the in-flight meter
enforces exactly, and an in-flight Run holds the larger of that
reservation and what it has already accrued. A Run with no cap on an axis
the governor bounds cannot be admitted, because no reservation could hold
it. A null ceiling is unavailable, never unlimited, which is why a
governor that bounds no spend at all is refused when it is loaded.

The governor only decides. It returns a receipt saying why, and it never
touches a Run's lifecycle: a queued Run stays exactly as it was.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Any, Final, Literal, Self

from pydantic import Field, StrictInt, model_validator

from eawf.kernel.economics.prompt_budget import (
    DEFAULT_PROMPT_BUDGET,
    PromptBudgetOutcome,
    PromptBudgetPolicy,
)
from eawf.kernel.runtime.compiled import BoundedText, canonical_digest
from eawf.kernel.runtime.provider import Digest, RuntimeRecord
from eawf.kernel.state.epoch2.urns import RunUrn
from eawf.kernel.state.types import UtcDatetime

_Ceiling = Annotated[StrictInt, Field(ge=0)] | None


class InFlightGovernor(RuntimeRecord):
    """The ceilings on work in flight across one root.

    Attributes:
        max_concurrent_runs: How many admitted Runs may be live at once.
        max_in_flight_tokens: The token spend live Runs may hold, or
            ``None`` when unbounded on this axis -- which is unavailable,
            not permission.
        max_in_flight_cost_microusd: The same ceiling in cost, with the
            same null meaning.
        admission: Whether a Run that would breach a ceiling waits in the
            queue or is refused outright.
    """

    max_concurrent_runs: Annotated[StrictInt, Field(ge=1)]
    max_in_flight_tokens: _Ceiling = None
    max_in_flight_cost_microusd: _Ceiling = None
    admission: Literal["queue", "deny"]

    @property
    def governor_digest(self) -> Digest:
        """The digest an admission binds, so a later revision is detectable."""
        return canonical_digest(self.model_dump(mode="json"))

    @model_validator(mode="after")
    def _some_ceiling_binds_spend(self) -> Self:
        """Refuse a governor that bounds how many Runs run but not what they spend.

        Raises:
            ValueError: Both spend ceilings are null.
        """
        if self.max_in_flight_tokens is None and self.max_in_flight_cost_microusd is None:
            raise ValueError(
                "a governor bounds spend: set max_in_flight_tokens or "
                "max_in_flight_cost_microusd; a count ceiling alone binds no spend"
            )
        return self


class RunReservation(RuntimeRecord):
    """What one Run reserves against the governor, and what it has accrued.

    Attributes:
        run_ref: The Run.
        tokens: Its sealed token cap, or ``None`` when it has none.
        cost_microusd: Its sealed cost cap, or ``None`` when it has none.
        accrued_tokens: The tokens it has spent so far, when known.
        accrued_cost_microusd: The cost it has accrued so far, when known.
    """

    run_ref: RunUrn
    tokens: _Ceiling = None
    cost_microusd: _Ceiling = None
    accrued_tokens: _Ceiling = None
    accrued_cost_microusd: _Ceiling = None

    def held(self, axis: AdmissionAxis) -> int | None:
        """Return what this Run holds on a spend *axis*, ``None`` when unbounded.

        A live Run holds its reservation, or what it has accrued if that
        is more: a cap bounds what the Run is allowed to spend, and the
        accrual is what it has actually spent.
        """
        reserved, accrued = (
            (self.tokens, self.accrued_tokens)
            if axis is AdmissionAxis.TOKENS
            else (self.cost_microusd, self.accrued_cost_microusd)
        )
        if reserved is None:
            return None
        return max(reserved, accrued or 0)


class AdmissionAxis(StrEnum):
    """The ceilings an admission is decided against."""

    CONCURRENCY = "concurrency"
    TOKENS = "tokens"
    COST = "cost_microusd"


class AxisStatus(StrEnum):
    """Where one axis stood when the Run asked to be admitted.

    ``unbounded`` means a Run holds no cap on an axis the governor bounds,
    so no reservation can hold it. ``unavailable`` means the governor has
    no ceiling on that axis, which admits nothing on its own.
    """

    WITHIN = "within"
    BREACHED = "breached"
    UNBOUNDED = "unbounded"
    UNAVAILABLE = "unavailable"


class AxisDecision(RuntimeRecord):
    """One axis's ceiling beside what is in flight and what was asked for."""

    axis: AdmissionAxis
    status: AxisStatus
    ceiling: StrictInt | None
    in_flight: StrictInt | None
    requested: StrictInt | None


class AdmissionDecision(StrEnum):
    """What the governor decided. None of the three moves the Run."""

    ADMITTED = "admitted"
    QUEUED = "queued"
    DENIED = "denied"


#: The axis statuses that keep a Run out.
_REFUSING: Final[frozenset[AxisStatus]] = frozenset({AxisStatus.BREACHED, AxisStatus.UNBOUNDED})


class AdmissionReceipt(RuntimeRecord):
    """Why one Run was admitted, queued or denied, as the run ledger holds it.

    Attributes:
        payload_kind: The discriminator separating a receipt line from the
            other lines of the run ledger.
        run_ref: The Run the decision is about.
        decision: What was decided.
        axes: Every governor axis, as it stood.
        prompt_budget: The prompt budget's class-by-class outcome.
        reservation: What the Run reserves while it is live. Only an
            admitted receipt reserves anything.
        governor_digest: The governor revision the decision was made under.
        decided_at: When the governor decided.
        reason: One sentence an operator reads.
    """

    payload_kind: Literal["admission_receipt"] = "admission_receipt"
    run_ref: RunUrn
    decision: AdmissionDecision
    axes: tuple[AxisDecision, ...]
    prompt_budget: PromptBudgetOutcome
    reservation: RunReservation
    governor_digest: Digest
    decided_at: UtcDatetime
    reason: BoundedText

    @model_validator(mode="after")
    def _decision_follows_the_axes(self) -> Self:
        """Admit exactly when no axis refuses and the prompt budget admits.

        Raises:
            ValueError: The decision disagrees with the axes or the prompt
                budget, or the reservation names another Run.
        """
        refused = any(row.status in _REFUSING for row in self.axes)
        admissible = not refused and self.prompt_budget.admitted
        if admissible != (self.decision is AdmissionDecision.ADMITTED):
            raise ValueError(f"a {self.decision.value} receipt disagrees with its axes")
        if self.reservation.run_ref != self.run_ref:
            raise ValueError("the reservation names another Run")
        return self


def _spend_axis(
    axis: AdmissionAxis,
    ceiling: int | None,
    *,
    request: RunReservation,
    in_flight: Sequence[RunReservation],
) -> AxisDecision:
    """Decide one spend axis: a ceiling is only ever held by bounded Runs."""
    requested = request.held(axis)
    if ceiling is None:
        return AxisDecision(
            axis=axis,
            status=AxisStatus.UNAVAILABLE,
            ceiling=None,
            in_flight=None,
            requested=requested,
        )
    held = [row.held(axis) for row in in_flight]
    bounded = [value for value in held if value is not None]
    total = sum(bounded) if len(bounded) == len(held) else None
    if requested is None or total is None:
        status = AxisStatus.UNBOUNDED
    elif total + requested > ceiling:
        status = AxisStatus.BREACHED
    else:
        status = AxisStatus.WITHIN
    return AxisDecision(
        axis=axis, status=status, ceiling=ceiling, in_flight=total, requested=requested
    )


def _reason(
    decision: AdmissionDecision, axes: Sequence[AxisDecision], prompt: PromptBudgetOutcome
) -> str:
    """Say in one sentence why the governor decided what it did."""
    if decision is AdmissionDecision.ADMITTED:
        return "admitted within every ceiling the governor and the prompt budget bind"
    causes = [
        f"{row.axis.value} unbounded: a Run without a {row.axis.value} cap, here or in "
        f"flight, holds no reservation the ceiling of {row.ceiling} can bound"
        if row.status is AxisStatus.UNBOUNDED
        else f"{row.axis.value} breached: {row.in_flight} in flight plus {row.requested} "
        f"requested exceeds the ceiling of {row.ceiling}"
        for row in axes
        if row.status in _REFUSING
    ]
    if prompt.exhausted:
        names = ", ".join(item.value for item in prompt.exhausted)
        causes.append(f"prompt budget exhausted on {names}")
    return f"{decision.value}: {'; '.join(causes)}"


def decide_admission(
    governor: InFlightGovernor,
    *,
    request: RunReservation,
    in_flight: Sequence[RunReservation],
    prompt_budget: PromptBudgetOutcome,
    decided_at: UtcDatetime,
) -> AdmissionReceipt:
    """Decide whether *request* may start beside the Runs already in flight.

    A breached or unbounded governor axis queues or denies the Run as the
    governor declares. An exhausted prompt budget always denies: waiting
    does not shrink a prompt.

    Args:
        governor: The ceilings in force.
        request: What the Run asks to reserve.
        in_flight: What every other live, admitted Run holds.
        prompt_budget: The prompt budget's outcome for this dispatch.
        decided_at: The decision stamp.

    Returns:
        The receipt, naming every axis and the reason.
    """
    running = len(in_flight)
    concurrency = AxisDecision(
        axis=AdmissionAxis.CONCURRENCY,
        status=(
            AxisStatus.BREACHED if running + 1 > governor.max_concurrent_runs else AxisStatus.WITHIN
        ),
        ceiling=governor.max_concurrent_runs,
        in_flight=running,
        requested=1,
    )
    axes = (
        concurrency,
        _spend_axis(
            AdmissionAxis.TOKENS,
            governor.max_in_flight_tokens,
            request=request,
            in_flight=in_flight,
        ),
        _spend_axis(
            AdmissionAxis.COST,
            governor.max_in_flight_cost_microusd,
            request=request,
            in_flight=in_flight,
        ),
    )
    if not prompt_budget.admitted:
        decision = AdmissionDecision.DENIED
    elif any(row.status in _REFUSING for row in axes):
        decision = (
            AdmissionDecision.QUEUED if governor.admission == "queue" else AdmissionDecision.DENIED
        )
    else:
        decision = AdmissionDecision.ADMITTED
    return AdmissionReceipt(
        run_ref=request.run_ref,
        decision=decision,
        axes=axes,
        prompt_budget=prompt_budget,
        reservation=request,
        governor_digest=governor.governor_digest,
        decided_at=decided_at,
        reason=_reason(decision, axes, prompt_budget),
    )


#: The shipped governor. Its token ceiling binds a real resource on day
#: one without a rate table; the cost ceiling stays null until priced.
DEFAULT_GOVERNOR: Final[InFlightGovernor] = InFlightGovernor(
    max_concurrent_runs=8, max_in_flight_tokens=4_000_000, admission="queue"
)


class EconomicsPolicy(RuntimeRecord):
    """The validated ``economics`` table: the prompt budget and the governor."""

    prompt_budget: PromptBudgetPolicy = DEFAULT_PROMPT_BUDGET
    governor: InFlightGovernor = DEFAULT_GOVERNOR


def economics_policy_from(merged: Mapping[str, Any]) -> EconomicsPolicy:
    """Validate the ``economics`` table of a merged layered config.

    Args:
        merged: The merged layered config mapping.

    Returns:
        The validated policy; the shipped defaults where a part is absent.

    Raises:
        pydantic.ValidationError: The table, or a part of it, breaks a
            policy rule or names a key no policy declares.
    """
    return EconomicsPolicy.model_validate(merged.get("economics") or {})


__all__ = [
    "DEFAULT_GOVERNOR",
    "AdmissionAxis",
    "AdmissionDecision",
    "AdmissionReceipt",
    "AxisDecision",
    "AxisStatus",
    "EconomicsPolicy",
    "InFlightGovernor",
    "RunReservation",
    "decide_admission",
    "economics_policy_from",
]
