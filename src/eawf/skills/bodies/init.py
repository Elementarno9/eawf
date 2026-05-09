"""``/init`` skill body.

Phase 4 W01 freezes the field set. ``/init`` wraps the install wizard
landed in Phase 3 W05; the body captures the wizard's terminal status
(steps completed, repo paths created, profile choice). W03 fills the
implementation; this schema is intentionally minimal so W03 can extend
additively without revising W01.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from eawf.skills.bodies.user_question import UserQuestion


class InitStep(BaseModel):
    """One step the wizard executed."""

    model_config = ConfigDict(extra="forbid")

    name: str
    status: str
    detail: str | None = None


class InitBody(BaseModel):
    """Body for ``/init`` (Eä install wizard wrapper)."""

    model_config = ConfigDict(extra="forbid")

    project_code: str | None = None
    workspace_root: str | None = None
    profile_ids: list[str] = Field(default_factory=list)
    steps: list[InitStep] = Field(default_factory=list)
    user_question: UserQuestion | None = None


__all__ = ["InitBody", "InitStep"]
