"""ResearchCampaignPayload — payload model for StoreKind.RESEARCH_CAMPAIGN records.

A research campaign store record persists the *staged* (plan-only)
output of the Level-1 runner :func:`eawf.kernel.spec.research_campaign.stage_campaign`
so a multi-domain ``/research`` sweep can be reviewed and tracked before
any live spawn. The payload wraps the typed
:class:`~eawf.kernel.spec.research_campaign.StagedCampaign` and pins the
``research:`` :class:`~eawf.kernel.spec.research_campaign.ResearchProfileBlock`
the campaign was staged from, so the record is a self-contained
reconstruction of how the plan was produced.

The wrapped campaign's ``spawned`` flag is a fixed ``False`` — persisting
a campaign record never implies execution. A later wave that adds a
live-spawn result store keeps that outcome in its own kind; this kind is
the plan-only half.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.kernel.spec.research_campaign import (
    MAX_STAGED_DISPATCHES,
    ResearchProfileBlock,
    StagedCampaign,
)
from eawf.kernel.state.enums import CampaignStatus
from eawf.kernel.state.types import UtcDatetime


class CampaignTombstone(BaseModel):
    """Cancel-time marker for a tombstoned (cancelled) campaign record.

    Present iff a campaign's :attr:`ResearchCampaignPayload.status` is
    :attr:`~eawf.kernel.state.enums.CampaignStatus.CANCELLED`. The append-only
    store never deletes a campaign row, so cancelling one stamps this marker
    rather than dropping the record — the cancel time + reason stay traceable.

    Attributes:
        cancelled_at: When the campaign was cancelled (UTC).
        reason: Optional short operator-supplied reason for the cancellation;
            ``None`` when no reason was given.
    """

    model_config = ConfigDict(extra="forbid")

    cancelled_at: UtcDatetime
    reason: str | None = Field(default=None, max_length=280)


class ResearchCampaignPayload(BaseModel):
    """Payload for a research-campaign store record.

    Attributes:
        campaign_id: Stable id for the staged campaign (caller-allocated).
        config: The typed ``research:`` block the campaign was staged
            from — pinned so the record reconstructs how the plan was
            produced.
        campaign: The plan-only :class:`StagedCampaign` the Level-1 runner
            emitted. Its ``spawned`` flag is fixed ``False``.
        status: The campaign's lifecycle position. Defaults to
            :attr:`~eawf.kernel.state.enums.CampaignStatus.ACTIVE` so every
            existing row stays valid without backfill; cancelling flips it to
            :attr:`~eawf.kernel.state.enums.CampaignStatus.CANCELLED`.
        tombstone: The cancel-time marker, present iff :attr:`status` is
            ``CANCELLED``; ``None`` for an active campaign.
    """

    model_config = ConfigDict(extra="forbid")

    campaign_id: str = Field(min_length=1)
    config: ResearchProfileBlock
    campaign: StagedCampaign
    status: CampaignStatus = CampaignStatus.ACTIVE
    tombstone: CampaignTombstone | None = None

    @model_validator(mode="after")
    def _dispatch_count_within_bound(self) -> ResearchCampaignPayload:
        """Reject a persisted campaign with too many staged dispatches.

        Raises:
            ValueError: when the campaign stages more than
                :data:`~eawf.kernel.spec.research_campaign.MAX_STAGED_DISPATCHES`
                dispatches — a persisted record stays scannable.
        """
        count = len(self.campaign.dispatches)
        if count > MAX_STAGED_DISPATCHES:
            raise ValueError(
                f"campaign stages {count} dispatches, exceeds max {MAX_STAGED_DISPATCHES}"
            )
        return self

    @model_validator(mode="after")
    def _tombstone_matches_status(self) -> ResearchCampaignPayload:
        """Reject a status / tombstone mismatch (tombstone present iff cancelled).

        Raises:
            ValueError: when the campaign is ``CANCELLED`` without a tombstone,
                or carries a tombstone while still ``ACTIVE`` — the two fields
                must agree so a cancelled row always records its cancel marker.
        """
        cancelled = self.status is CampaignStatus.CANCELLED
        if cancelled and self.tombstone is None:
            raise ValueError("cancelled campaign requires a tombstone")
        if not cancelled and self.tombstone is not None:
            raise ValueError(f"active campaign must not carry a tombstone: {self.status.value!r}")
        return self
