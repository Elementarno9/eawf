"""``/prep`` skill body.

Per ``docs/architecture/envelope.md``:

    /prep body: { iter_id, objective, non_goals, dag: [{task_id, deps,
                  file_scope, commands, evidence, risk}], waves:
                  [{wave_id, tasks, worktree_policy, estimate_eu}],
                  acceptance: {checks, baselines}, approval_required:
                  bool }
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

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
    tree all draw from the same projection (P28-W18).
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
    user_question: UserQuestion | None = None
    plan_text: str | None = None


__all__ = ["PrepAcceptance", "PrepBody", "PrepDagTask", "PrepWave"]
