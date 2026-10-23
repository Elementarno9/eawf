"""JuryBallotPayload — payload model for StoreKind.JURY_BALLOT records.

One row per juror per convened cross-vendor jury, carrying that juror's
ballot; an abstention (no verdict) carries the structured ``error`` instead
so the lane failure stays auditable without fabricating a ballot.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class JuryBallotPayload(BaseModel):
    """Payload for one persisted juror ballot."""

    model_config = ConfigDict(extra="forbid")

    wave_id: str
    runtime: str
    verdict: str | None = None
    error: str | None = None
    cast_at: datetime


__all__ = ["JuryBallotPayload"]
