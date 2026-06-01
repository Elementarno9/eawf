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


class ResearchCampaignPayload(BaseModel):
    """Payload for a research-campaign store record.

    Attributes:
        campaign_id: Stable id for the staged campaign (caller-allocated).
        config: The typed ``research:`` block the campaign was staged
            from — pinned so the record reconstructs how the plan was
            produced.
        campaign: The plan-only :class:`StagedCampaign` the Level-1 runner
            emitted. Its ``spawned`` flag is fixed ``False``.
    """

    model_config = ConfigDict(extra="forbid")

    campaign_id: str = Field(min_length=1)
    config: ResearchProfileBlock
    campaign: StagedCampaign

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
