"""EstimatePayload — payload model for StoreKind.ESTIMATE records."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from eawf.state.enums import Confidence


class EstimatePayload(BaseModel):
    """Payload for an estimate store record."""

    model_config = ConfigDict(extra="forbid")

    scope_type: str
    source: str
    grain: str
    expected_eu: float
    pessimistic_eu: float
    expected_minutes: float
    pessimistic_minutes: float
    display: str
    display_category: str
    reference_class: str | None = None
    confidence: Confidence
    basis: list[str] = Field(default_factory=list)
    coefficients_profile: str
