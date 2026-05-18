"""EventPayload — payload model for StoreKind.EVENT records."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class EventPayload(BaseModel):
    """Payload for an event store record.

    The ``actor_principal_id`` field is a v0.3-v0.5 placeholder (per
    c01-foundations §5.3.19 + Q3 2026-05-18 + XB08): rows may carry the
    principal id when known, but ``actor`` stays the load-bearing identity
    string for backward compatibility until the v0.5+ governance phase
    renames callers and back-fills the principal database.
    """

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    event_type: str
    actor: str
    actor_principal_id: str | None = None
    command: str
    args_hash: str
    before_state_version: str | None = None
    after_state_version: str | None = None
    status: str
    message: str
