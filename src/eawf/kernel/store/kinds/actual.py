"""ActualPayload — payload model for StoreKind.ACTUAL records."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from eawf.kernel.state.enums import ActualStatus


class ActualSegment(BaseModel):
    """A contiguous work segment within an actual record."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    started_at: datetime
    ended_at: datetime
    eu: float
    active_minutes: float
    idle_excluded_minutes: float
    external_wait_minutes: float
    agent_runtime_minutes: float
    status: ActualStatus


class ActualPayload(BaseModel):
    """Payload for an actual store record."""

    model_config = ConfigDict(extra="forbid")

    segments: list[ActualSegment]
    elapsed_eu: float
    attention_eu: float | None = None
    agent_runtime_eu: float | None = None
    ratio_actual_over_estimate: float | None = None
    inside_pessimistic: bool | None = None
    calibration_eligible: bool = False
    outcome: str
    idle_policy: str
