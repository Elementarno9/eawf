"""IncidentPayload — payload model for StoreKind.INCIDENT records."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from eawf.state.enums import IncidentSeverity


class TimelineEntry(BaseModel):
    """A single entry in an incident timeline."""

    model_config = ConfigDict(extra="forbid")

    at: datetime
    entry: str


class IncidentPayload(BaseModel):
    """Payload for an incident store record."""

    model_config = ConfigDict(extra="forbid")

    severity: IncidentSeverity
    timeline: list[TimelineEntry]
    root_cause: str | None = None
    corrective_action_ids: list[str] = Field(default_factory=list)
