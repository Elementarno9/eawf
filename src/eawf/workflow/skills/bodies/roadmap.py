"""``/roadmap`` skill body.

Phase 4 W01 freezes the field set. ``/roadmap`` proposes phases/iters
and ranks them; the body holds the candidate list plus the chosen
ordering. W03 fills the implementation per `docs/architecture/workflow.md`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from eawf.workflow.skills.bodies.user_question import UserQuestion


class RoadmapItem(BaseModel):
    """One roadmap candidate item."""

    model_config = ConfigDict(extra="forbid")

    item_id: str
    title: str
    rationale: str
    priority: str
    estimate_eu: float | None = None


class RoadmapBody(BaseModel):
    """Body for ``/roadmap``."""

    model_config = ConfigDict(extra="forbid")

    horizon: str | None = None
    candidates: list[RoadmapItem] = Field(default_factory=list)
    chosen_order: list[str] = Field(default_factory=list)
    user_question: UserQuestion | None = None


__all__ = ["RoadmapBody", "RoadmapItem"]
