"""``/dispatch`` skill body — the coordination report of one Batch pass.

The coordinator reports what it computed before it moved anything: the
concurrency plan, every Run it addressed with the outcome that came
back, the frontier still standing, and every condition it stopped on. A
report that names no stop condition and an empty frontier is the only
shape that means the Batch is drained.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.workflow.skills.bodies.user_question import UserQuestion

#: The closed terminal outcomes of one ``/dispatch`` invocation.
DispatchOutcome = Literal[
    "candidate_ready",
    "frontier_empty",
    "needs_operator",
    "budget_exhausted",
    "blocked",
    "cancelled",
]

#: What one addressed Run answered with.
DispatchRunOutcome = Literal["dispatched", "retried", "refused"]


class ConcurrencyPlan(BaseModel):
    """What the coordinator decided to fan out and what it had to serialize.

    Attributes:
        parallel: Task references dispatched side by side.
        sequential: Task references the plan forced into an order.
        constraint: Why the sequential arm is sequential, in one phrase.
            Empty when nothing was serialized.
    """

    model_config = ConfigDict(extra="forbid")

    parallel: list[str] = Field(default_factory=list)
    sequential: list[str] = Field(default_factory=list)
    constraint: str = ""


class DispatchedRun(BaseModel):
    """One Run the coordinator addressed.

    Attributes:
        task_ref: The Task the Run was opened for.
        run_ref: The Run that was addressed.
        method: The JSON-RPC method the coordinator called for it.
        outcome: Whether the Run was dispatched, retried, or refused.
        refusal_code: The stable code a refusal carried, or ``None``.
    """

    model_config = ConfigDict(extra="forbid")

    task_ref: str
    run_ref: str
    method: str
    outcome: DispatchRunOutcome
    refusal_code: str | None = None


class DispatchBody(BaseModel):
    """Body for ``/dispatch`` — the coordination report.

    Attributes:
        kind: The output-schema discriminator.
        batch_ref: The Batch this pass coordinated.
        source_cursor: The projection cursor the Batch was read at, or
            ``None`` when no read model was bound.
        plan: The concurrency plan computed before anything was dispatched.
        dispatched: Every Run addressed, in the order it was addressed.
        frontier: Task references still ready and not yet dispatched.
        stopped_on: Every condition that ended the pass, as stable codes.
        outcome: The terminal outcome.
        reason: One sentence an operator reads.
        user_question: Required when the terminal outcome is
            ``needs_operator``; ``None`` otherwise.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["coordination_report"] = "coordination_report"
    batch_ref: str
    source_cursor: int | None = None
    plan: ConcurrencyPlan = Field(default_factory=ConcurrencyPlan)
    dispatched: list[DispatchedRun] = Field(default_factory=list)
    frontier: list[str] = Field(default_factory=list)
    stopped_on: list[str] = Field(default_factory=list)
    outcome: DispatchOutcome
    reason: str
    user_question: UserQuestion | None = None


__all__ = [
    "ConcurrencyPlan",
    "DispatchBody",
    "DispatchOutcome",
    "DispatchRunOutcome",
    "DispatchedRun",
]
