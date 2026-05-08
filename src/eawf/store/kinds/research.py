"""ResearchPayload — payload model for StoreKind.RESEARCH records."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ResearchPayload(BaseModel):
    """Payload for a research store record."""

    model_config = ConfigDict(extra="forbid")

    topic: str
    findings: list[str]
    sources: list[str]
