"""``/differentiate`` skill body.

Phase 4 W01 freezes the field set. ``/differentiate`` compares the
current scope to peer projects and surfaces differentiators; the body
holds the comparison axes and the conclusions. W03 fills the
implementation per `docs/architecture/workflow.md`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from eawf.skills.bodies.user_question import UserQuestion


class DifferentiateAxis(BaseModel):
    """One comparison axis."""

    model_config = ConfigDict(extra="forbid")

    name: str
    current: str
    peers: list[str] = Field(default_factory=list)
    advantage: str | None = None


class DifferentiateBody(BaseModel):
    """Body for ``/differentiate``."""

    model_config = ConfigDict(extra="forbid")

    target_scope: str | None = None
    axes: list[DifferentiateAxis] = Field(default_factory=list)
    conclusions: list[str] = Field(default_factory=list)
    user_question: UserQuestion | None = None


__all__ = ["DifferentiateAxis", "DifferentiateBody"]
