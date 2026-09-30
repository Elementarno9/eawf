"""The dispatch queue as the daemon states it: its Runs, its plan, its control and its legs.

The queue is every Run whose lifecycle has not ended. Each carries what the daemon holds
about it while it runs: when it started, the wall-clock budget its capsule sealed, and how
much it says about its own progress. A Run reports no enumeration of its obligations, so
its progress mode is ``opaque`` and the surface renders its elapsed time against its
budget; a verification leg whose command publishes its collection is ``enumerated`` and
carries its completed and total counts, the last unit it finished and its running tallies.

The concurrency plan is derived when it is read and stored nowhere: the slots are the
governor's ceiling, the slots in use are the admitted Runs still live, and a forced
sequential edge is a queued Run's Task waiting on a Task it depends on that has not
completed.

Dispatch control is the one tree-wide switch the operator holds over the scheduler, kept
as append-only facts on the run ledger. ``pause`` holds admission of new Runs until a
``resume``; ``drain`` holds it too, so what is already running finishes and nothing new
starts. Neither stops or cancels a Run that is already claimed.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.ledger import LedgerRecord

#: The ledger-line key prefix a dispatch control fact is filed under.
DISPATCH_CONTROL_KEY_PREFIX: Final = "DSP-"

#: The payload discriminator of a dispatch control fact.
DISPATCH_CONTROL_KIND: Final = "dispatch_control"

#: Who states the dispatch queue.
DISPATCH_QUEUE_PRODUCER: Final = "the daemon dispatch scheduler"

_Ref = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]
_Count = Annotated[int, Field(ge=0)]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DispatchVerb(StrEnum):
    """What an operator may ask of the dispatch scheduler."""

    PAUSE = "pause"
    DRAIN = "drain"
    RESUME = "resume"


class DispatchOutcome(StrEnum):
    """What became of one dispatch control request."""

    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class DispatchControlFact(_Frozen):
    """One dispatch control request as the run ledger records it.

    Attributes:
        payload_kind: The payload discriminator.
        request_ref: The id the request was sent under; the same id sent again answers
            with this fact rather than recording a second.
        verb: What was asked.
        actor: The principal who asked.
        requested_at: When the daemon recorded it.
        outcome: Whether it took effect or was refused.
        reason: Why it was refused; ``None`` for a confirmed request.
    """

    payload_kind: Literal["dispatch_control"] = DISPATCH_CONTROL_KIND
    request_ref: _Ref
    verb: DispatchVerb
    actor: _Ref
    requested_at: UtcDatetime
    outcome: DispatchOutcome
    reason: str | None = None


class DispatchControl(_Frozen):
    """Where the scheduler's control stands, folded from every confirmed request.

    Attributes:
        dispatch_paused: Whether a pause holds admission of new Runs.
        drain_requested: Whether a drain holds admission of new Runs.
        last_request: The newest request, confirmed or refused; ``None`` before any.
    """

    dispatch_paused: bool = False
    drain_requested: bool = False
    last_request: DispatchControlFact | None = None

    @property
    def holding(self) -> DispatchVerb | None:
        """Return the verb holding admission, drain first, or ``None`` when none does."""
        if self.drain_requested:
            return DispatchVerb.DRAIN
        return DispatchVerb.PAUSE if self.dispatch_paused else None


def fold_dispatch_control(facts: tuple[DispatchControlFact, ...]) -> DispatchControl:
    """Return the control the facts leave standing, in the order they were recorded."""
    paused = drained = False
    for fact in facts:
        if fact.outcome is not DispatchOutcome.CONFIRMED:
            continue
        if fact.verb is DispatchVerb.RESUME:
            paused = drained = False
        elif fact.verb is DispatchVerb.PAUSE:
            paused = True
        else:
            drained = True
    return DispatchControl(
        dispatch_paused=paused,
        drain_requested=drained,
        last_request=facts[-1] if facts else None,
    )


def dispatch_control_facts(records: Sequence[LedgerRecord]) -> tuple[DispatchControlFact, ...]:
    """Return every dispatch control fact on the run ledger, in the order recorded."""
    return tuple(
        DispatchControlFact.model_validate(item.payload)
        for item in records
        if item.payload.get("payload_kind") == DISPATCH_CONTROL_KIND
    )


def admission_hold(records: Sequence[LedgerRecord]) -> DispatchVerb | None:
    """Return the control holding admission of a new Run, or ``None`` when none does."""
    return fold_dispatch_control(dispatch_control_facts(records)).holding


class ProgressMode(StrEnum):
    """How much a queued unit says about its own progress."""

    OPAQUE = "opaque"
    ENUMERATED = "enumerated"


class QueuedRun(_Frozen):
    """One Run the queue holds.

    Attributes:
        run_key: The Run.
        task_key: The Task it works, or ``None`` for a Run not scoped to a Task.
        state: Its control-reduced status.
        suspension_reason: Why a suspended Run is suspended; ``None`` otherwise.
        admitted_at: When the governor admitted it; ``None`` before admission.
        started_at: When it started; ``None`` while queued.
        budget_seconds: The wall-clock limit its capsule sealed; ``None`` when unsealed.
        progress_mode: How much it says about its progress; a Run enumerates nothing.
    """

    run_key: _Ref
    task_key: _Ref | None = None
    state: _Ref
    suspension_reason: _Ref | None = None
    admitted_at: UtcDatetime | None = None
    started_at: UtcDatetime | None = None
    budget_seconds: Annotated[int, Field(ge=1)] | None = None
    progress_mode: ProgressMode = ProgressMode.OPAQUE


class VerificationLeg(_Frozen):
    """One verification leg still running, as its progress manifest states it.

    Attributes:
        gate_id: The gate the leg runs.
        criterion_id: The criterion the gate verifies.
        started_at: When the leg started.
        heartbeat_at: When its runner last wrote the manifest.
        budget_seconds: The leg's resolved timeout; ``None`` when none resolved.
        progress_mode: ``enumerated`` when the command published its collection.
        completed: How many obligations ended; ``None`` for an opaque leg.
        total: How many the command collected; ``None`` for an opaque leg.
        last_completed: The last obligation that ended; ``None`` before one did.
        passed: How many ended passing.
        failed: How many ended failing.
        unknown: How many ended with no verdict.
    """

    gate_id: _Ref
    criterion_id: _Ref
    started_at: UtcDatetime
    heartbeat_at: UtcDatetime
    budget_seconds: Annotated[int, Field(ge=1)] | None = None
    progress_mode: ProgressMode
    completed: _Count | None = None
    total: _Count | None = None
    last_completed: Annotated[str, StringConstraints(max_length=4_096)] | None = None
    passed: _Count = 0
    failed: _Count = 0
    unknown: _Count = 0


class QueueEdge(_Frozen):
    """One forced sequential edge: a queued Run's Task waiting on another Task.

    Attributes:
        waits: The Task that waits.
        on: The Task it waits on.
        source: What declares the edge.
    """

    waits: _Ref
    on: _Ref
    source: Literal["depends_on"] = "depends_on"


class DispatchPlan(_Frozen):
    """The concurrency plan, derived from the governor and the dependency graph.

    Attributes:
        slots: The governor's ceiling on live admitted Runs; ``None`` when the tree's
            economics could not be read.
        in_use: How many admitted Runs are live.
        edges: Every forced sequential edge of a queued Run, in Run key order.
    """

    slots: Annotated[int, Field(ge=1)] | None
    in_use: _Count
    edges: tuple[QueueEdge, ...] = ()


class DispatchQueueView(_Frozen):
    """The dispatch queue at one instant.

    Attributes:
        runs: Every Run whose lifecycle has not ended, in key order.
        legs: Every verification leg still running, in gate order.
        plan: The concurrency plan.
        control: Where the scheduler's control stands.
        read_at: When the daemon answered, which a reader ages the answer by.
    """

    runs: tuple[QueuedRun, ...]
    legs: tuple[VerificationLeg, ...] = ()
    plan: DispatchPlan
    control: DispatchControl
    read_at: UtcDatetime

    def run(self, run_key: str) -> QueuedRun | None:
        """Return the queue's entry for Run ``run_key``, or ``None``."""
        return next((entry for entry in self.runs if entry.run_key == run_key), None)

    def forced_sequential(self) -> int:
        """Return how many queued Runs not yet admitted wait on a Task edge."""
        waiting = {edge.waits for edge in self.plan.edges}
        return sum(
            1
            for entry in self.runs
            if entry.state == "QUEUED" and entry.admitted_at is None and entry.task_key in waiting
        )


__all__ = [
    "DISPATCH_CONTROL_KEY_PREFIX",
    "DISPATCH_CONTROL_KIND",
    "DISPATCH_QUEUE_PRODUCER",
    "DispatchControl",
    "DispatchControlFact",
    "DispatchOutcome",
    "DispatchPlan",
    "DispatchQueueView",
    "DispatchVerb",
    "ProgressMode",
    "QueueEdge",
    "QueuedRun",
    "VerificationLeg",
    "admission_hold",
    "dispatch_control_facts",
    "fold_dispatch_control",
]
