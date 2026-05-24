"""DecisionPayload — payload model for StoreKind.DECISION records."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DecisionPayload(BaseModel):
    """Payload for a decision store record."""

    model_config = ConfigDict(extra="forbid")

    summary: str
    rationale: str
    alternatives: list[str] = Field(default_factory=list)
    supersedes: str | None = None
