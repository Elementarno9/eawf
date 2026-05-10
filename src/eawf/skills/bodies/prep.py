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

from eawf.skills.bodies.user_question import UserQuestion


class PrepDagTask(BaseModel):
    """One DAG task in a prep plan."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    deps: list[str] = Field(default_factory=list)
    file_scope: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    risk: str


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
    """Body for ``/prep``."""

    model_config = ConfigDict(extra="forbid")

    iter_id: str
    objective: str
    non_goals: list[str] = Field(default_factory=list)
    dag: list[PrepDagTask] = Field(default_factory=list)
    waves: list[PrepWave] = Field(default_factory=list)
    acceptance: PrepAcceptance | None = None
    approval_required: bool = False
    user_question: UserQuestion | None = None


__all__ = ["PrepAcceptance", "PrepBody", "PrepDagTask", "PrepWave"]
