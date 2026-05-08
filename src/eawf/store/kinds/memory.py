"""MemoryPayload — payload model for StoreKind.MEMORY records."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from eawf.state.enums import Confidence


class MemoryPayload(BaseModel):
    """Payload for a memory store record."""

    model_config = ConfigDict(extra="forbid")

    body: str
    confidence: Confidence
    review_due: datetime | None = None
