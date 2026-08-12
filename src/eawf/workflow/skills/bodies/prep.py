"""``/prep`` skill body.

Per ``docs/architecture/envelope.md``:

    /prep body: { iter_id, objective, non_goals, dag: [{task_id, deps,
                  file_scope, commands, evidence, risk}], waves:
                  [{wave_id, tasks, worktree_policy, estimate_eu}],
                  acceptance: {checks, baselines}, approval_required:
                  bool }
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.workflow.skills.bodies.user_question import UserQuestion


class PrepDagTask(BaseModel):
    """One DAG task in a prep plan.

    Extended in P28-W18 with ``agent_role`` / ``effort_bucket`` /
    ``estimate_eu`` so the prep body carries the same three planning
    signals the canonical ``plan_view`` renderer surfaces in
    ``roadmap show --md`` and the TUI roadmap tree. The three fields
    default to ``None`` / ``0.0`` so a wave that has not yet been
    tagged still projects cleanly into the DAG.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    deps: list[str] = Field(default_factory=list)
    file_scope: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    risk: str
    agent_role: str | None = None
    effort_bucket: str | None = None
    estimate_eu: float = 0.0


class PrepWave(BaseModel):
    """One wave grouping in a prep plan."""

    model_config = ConfigDict(extra="forbid")

    wave_id: str
    tasks: list[str] = Field(default_factory=list)
    worktree_policy: str
    estimate_eu: float


class PrepAcceptance(BaseModel):
    """Acceptance block: checks and baselines."""

    model_config = ConfigDict(extra="forbid")

    checks: list[str] = Field(default_factory=list)
    baselines: list[str] = Field(default_factory=list)


class PrepBody(BaseModel):
    """Body for ``/prep``.

    The optional ``plan_text`` field carries the markdown surface a
    Claude-runtime ``EnterPlanMode`` (or a Codex text-prompt) renders to
    the operator — sourced from the canonical
    :func:`eawf.surfaces.render.plan_view.render_markdown` so the
    plan-mode body, ``eawf roadmap show --md``, and the TUI roadmap
    tree all draw from the same projection.

    The ``no_op`` and ``blocked`` flags mark the two lifecycle stub paths
    that exempt the planning-DAG invariant: ``no_op=True`` is the
    already-active idempotent case (nothing to plan) and ``blocked=True``
    is the closed-phase stub (the operator must reopen first). On every
    other path the body is a real plan, and :meth:`_planning_dag_reconciles`
    requires a non-empty, internally consistent DAG.
    """

    model_config = ConfigDict(extra="forbid")

    iter_id: str
    objective: str
    non_goals: list[str] = Field(default_factory=list)
    dag: list[PrepDagTask] = Field(default_factory=list)
    waves: list[PrepWave] = Field(default_factory=list)
    acceptance: PrepAcceptance | None = None
    approval_required: bool = False
    no_op: bool = False
    blocked: bool = False
    user_question: UserQuestion | None = None
    plan_text: str | None = None

    @model_validator(mode="after")
    def _planning_dag_reconciles(self) -> Self:
        """Require a non-empty, internally consistent DAG on the planning path.

        Mechanizes the ``/prep`` DAG-render rule: a body that claims to plan
        an iter (``no_op`` and ``blocked`` both ``False``) MUST carry a
        non-empty ``dag``, every wave-referenced task MUST reconcile to a
        ``dag`` task, and every task dep MUST reference an existing task. The
        two lifecycle stub paths (``no_op=True`` idempotent already-active,
        ``blocked=True`` closed-phase) keep ``dag`` optional — the conditional
        exemption.

        Returns:
            The validated body (Pydantic ``mode="after"`` contract).

        Raises:
            ValueError: The planning path has an empty ``dag``, a wave that
                references a task id absent from the ``dag``, or a task whose
                dep references a non-existent task id. Pydantic wraps each
                into a :class:`pydantic.ValidationError`.
        """
        if self.no_op or self.blocked:
            return self
        if not self.dag:
            raise ValueError("planning prep body requires a non-empty dag")
        task_ids = {task.task_id for task in self.dag}
        for wave in self.waves:
            for task_id in wave.tasks:
                if task_id not in task_ids:
                    raise ValueError(
                        f"wave {wave.wave_id!r} references task {task_id!r} not in the dag"
                    )
        for task in self.dag:
            for dep in task.deps:
                if dep not in task_ids:
                    raise ValueError(f"task {task.task_id!r} has dangling dep {dep!r}")
        return self


__all__ = ["PrepAcceptance", "PrepBody", "PrepDagTask", "PrepWave"]
