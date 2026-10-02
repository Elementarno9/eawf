"""EstimatePayload — payload model for StoreKind.ESTIMATE records."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.enums import Confidence


class EstimatePayload(BaseModel):
    """Payload for an estimate store record.

    ``reference_sample_size`` and ``mapping_revision`` are ``None`` on a record
    written before they existed; see :class:`~eawf.kernel.state.models.EstimateSummary`.
    """

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
    reference_sample_size: int | None = Field(default=None, ge=0)
    mapping_revision: int | None = Field(default=None, ge=1)
    confidence: Confidence
    basis: list[str] = Field(default_factory=list)
    coefficients_profile: str
