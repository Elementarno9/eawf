"""``/blitz`` skill body."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BlitzBody(BaseModel):
    """Body for ``/blitz`` follow-up research chaining."""

    model_config = ConfigDict(extra="forbid")

    depth: int
    depth_cap: int
    residual_unknowns: int
    followup_research_args: dict[str, Any] = Field(default_factory=dict)
    next_actions: list[str] = Field(default_factory=list)


__all__ = ["BlitzBody"]
