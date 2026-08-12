"""JuryBallotPayload — payload model for StoreKind.JURY_BALLOT records.

One row per juror per convened cross-vendor jury. The
calibration reader
(:func:`eawf.observability.eval.jury_validation.read_recorded_ballots`)
rebuilds the per-wave :class:`JurorBallot` map from these rows; an
abstention (no verdict) carries the structured ``error`` instead so the
lane failure stays auditable without fabricating a ballot.
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
