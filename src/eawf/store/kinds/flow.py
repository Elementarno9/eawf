"""FlowPayload — payload model for StoreKind.FLOW records."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class FlowPayload(BaseModel):
    """Payload for a flow store record."""

    model_config = ConfigDict(extra="forbid")

    goal: str
    policy: dict[str, Any]
    last_safe_checkpoint: str | None = None
    next_action: str | None = None
