"""The orchestration contract, and the concurrency plan a Task graph implies.

Operators kept typing the same three instructions at every dispatch: the
coordinator only orchestrates, independent work fans out while dependent
work waits its turn, and some work runs with nothing beside it. Each of
the three has a home that is not prose. The first is a role separation,
stated here once as :data:`ORCHESTRATION_CONTRACT` and carried by every
dispatching surface as its resolved default. The second is already
encoded by the Task dependency graph and the Tasks' write claims, so
:func:`derive_concurrency_plan` reads it off the graph instead of taking
a parallelism number from whoever dispatches. The third is the Task's
own ``exclusive`` flag, which the lease scheduler enforces; the plan
only shows where it forces a Task onto a stage of its own.

The derivation is a pure function: no lock, no file, no document. It
does not cap a stage at the in-flight governor's Run ceiling, because
the governor is already the one limiter -- a stage wider than the
ceiling is admitted in part and queued for the rest.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from eawf.kernel.state.epoch2.run import write_path_covers


class OrchestrationContract(BaseModel):
    """The division of labour every dispatching surface states as its default.

    Attributes:
        coordinator: What the coordinating Run does: it proposes and
            dispatches, and nothing else.
        coordinator_write_scope: What it may write: nothing. A coordinator
            runs under a non-task scope, and only a task-scoped Run holds
            a write set.
        execution: Where the work happens: in child or sibling Runs, each
            under its own Task scope.
        concurrency: Where the parallelism comes from: the Task graph,
            never a number typed at dispatch time.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    coordinator: Literal["proposes_and_dispatches"] = "proposes_and_dispatches"
    coordinator_write_scope: Literal["none"] = "none"
    execution: Literal["child_or_sibling_runs_under_own_task_scope"] = (
        "child_or_sibling_runs_under_own_task_scope"
    )
    concurrency: Literal["derived_from_task_graph"] = "derived_from_task_graph"


#: The one resolved orchestration contract.
ORCHESTRATION_CONTRACT: Final = OrchestrationContract()

#: The contract as one sentence, for a surface that renders prose.
ORCHESTRATION_STATEMENT: Final = (
    "The coordinating Run proposes and dispatches and holds no write scope; every Task "
    "executes in its own child or sibling Run under its own Task scope, and which Tasks "
    "run side by side is derived from the Task dependency graph and write claims, never "
    "from a parallelism number typed at dispatch."
)


@dataclass(frozen=True, slots=True, kw_only=True)
class GraphTask:
    """One unfinished Task as the concurrency plan sees it.

    Attributes:
        ref: The Task's key or reference.
        depends_on: The Tasks it must not start ahead of. An edge to a
            Task outside the planned set names work that is no longer
            pending, so it orders nothing.
        write_claims: The repository paths its Runs may write.
        exclusive: Whether it runs alone.
    """

    ref: str
    depends_on: tuple[str, ...] = ()
    write_claims: tuple[str, ...] = ()
    exclusive: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class ConcurrencyPlan:
    """Which Tasks fan out together, which wait, and why each one waits.

    Attributes:
        stages: The Tasks of each stage, in order. The Tasks of one stage
            run side by side; a stage starts once the one before it is
            done.
        reasons: Why each Task that does not run in the first stage, or
            runs on a stage of its own, was placed where it is. A Task
            with no entry was placed only by its dependencies being done.
    """

    stages: tuple[tuple[str, ...], ...]
    reasons: dict[str, tuple[str, ...]]

    @property
    def fan_out(self) -> tuple[str, ...]:
        """Return the Tasks that run side by side now: the first stage."""
        return self.stages[0] if self.stages else ()

    @property
    def sequential(self) -> tuple[str, ...]:
        """Return the Tasks forced to wait for an earlier stage."""
        return tuple(ref for stage in self.stages[1:] for ref in stage)


def _topological(tasks: Sequence[GraphTask]) -> tuple[GraphTask, ...]:
    """Return *tasks* ordered so every Task follows the Tasks it depends on.

    Ties keep the input order, so one graph always yields one plan.

    Raises:
        ValueError: The in-set dependency edges form a cycle.
    """
    refs = {task.ref for task in tasks}
    placed: set[str] = set()
    ordered: list[GraphTask] = []
    pending = list(tasks)
    while pending:
        ready = [
            task
            for task in pending
            if all(dep in placed or dep not in refs for dep in task.depends_on)
        ]
        if not ready:
            cyclic = ", ".join(sorted(task.ref for task in pending))
            raise ValueError(f"these Tasks depend on each other in a cycle: {cyclic}")
        ordered.extend(ready)
        placed.update(task.ref for task in ready)
        pending = [task for task in pending if task.ref not in placed]
    return tuple(ordered)


def _conflict(task: GraphTask, beside: Sequence[GraphTask]) -> str | None:
    """Return why *task* cannot run beside the Tasks of one stage, if it cannot."""
    for other in beside:
        if task.exclusive:
            return f"runs alone, so it cannot run beside {other.ref}"
        if other.exclusive:
            return f"{other.ref} runs alone"
        shared = sorted(
            {
                claim
                for claim in task.write_claims
                for held in other.write_claims
                if write_path_covers(claim, held) or write_path_covers(held, claim)
            }
        )
        if shared:
            return f"writes {', '.join(shared)}, which {other.ref} also writes"
    return None


def derive_concurrency_plan(tasks: Sequence[GraphTask]) -> ConcurrencyPlan:
    """Derive the concurrency plan the graph of *tasks* implies.

    Each Task is placed on the earliest stage after every Task it depends
    on, then moved later while it conflicts with a Task already placed
    there: two Tasks with overlapping write claims never share a stage,
    and an exclusive Task shares one with nothing.

    Args:
        tasks: The unfinished Tasks to plan, in the order ties keep.

    Returns:
        The staged plan with the reason for every forced wait.

    Raises:
        ValueError: Two Tasks share a reference, or the dependency edges
            form a cycle.
    """
    refs = [task.ref for task in tasks]
    if len(set(refs)) != len(refs):
        raise ValueError("a concurrency plan names each Task once")
    stage_of: dict[str, int] = {}
    members: dict[int, list[GraphTask]] = {}
    reasons: dict[str, list[str]] = {}
    for task in _topological(tasks):
        placed_deps = [dep for dep in task.depends_on if dep in stage_of]
        stage = 1 + max((stage_of[dep] for dep in placed_deps), default=0)
        if placed_deps:
            last = max(placed_deps, key=lambda dep: stage_of[dep])
            reasons.setdefault(task.ref, []).append(f"depends on {last}")
        while (why := _conflict(task, members.get(stage, ()))) is not None:
            reasons.setdefault(task.ref, []).append(why)
            stage += 1
        stage_of[task.ref] = stage
        members.setdefault(stage, []).append(task)
    stages = tuple(tuple(task.ref for task in members[index]) for index in sorted(members))
    return ConcurrencyPlan(stages=stages, reasons={ref: tuple(why) for ref, why in reasons.items()})


__all__ = [
    "ORCHESTRATION_CONTRACT",
    "ORCHESTRATION_STATEMENT",
    "ConcurrencyPlan",
    "GraphTask",
    "OrchestrationContract",
    "derive_concurrency_plan",
]
