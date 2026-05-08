"""EventPayload — payload model for StoreKind.EVENT records."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class EventPayload(BaseModel):
    """Payload for an event store record."""

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    event_type: str
    actor: str
    command: str
    args_hash: str
    before_state_version: str | None = None
    after_state_version: str | None = None
    status: str
    message: str
