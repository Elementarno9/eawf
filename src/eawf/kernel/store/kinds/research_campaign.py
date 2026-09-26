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

The payload also carries the typed evidence budget (:class:`ResearchBudget`
per axis): its limits are set when the campaign is created
(:func:`open_evidence_budget`), a run charges each executed round against
it (:func:`charge_evidence_budget`), and a run halts before a round
:func:`exhausted_budget_axis` says it cannot afford. The payload also
carries the :data:`CAMPAIGN_TRANSITIONS` status matrix a caller
validates a status edge against via :func:`validate_campaign_transition`.
Completing a campaign (:func:`complete_campaign`) is a pure status flip on
the campaign alone — it takes no Milestone reference, so a Campaign's
evidence gathering can never move a Milestone's delivery status; that
acceptance stays a separate, explicit act.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

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


class ResearchBudget(BaseModel):
    """One evidence-budget axis: a limit and spent pair in a shared unit.

    Both sides render against a single :attr:`unit` (e.g. ``"rounds"`` or
    ``"usd"``) so a route can show spend against limit honestly instead of
    mixing units into a false ratio. :attr:`remainder` is always derived,
    never stored, because spend is observed rather than promised.

    Attributes:
        unit: The shared unit :attr:`limit` and :attr:`spent` are measured
            in. Non-empty.
        limit: The hard ceiling for this axis, or ``None`` when the axis
            is not yet configured.
        spent: The amount observed spent so far, or ``None`` when the axis
            is not yet configured. Present iff :attr:`limit` is present.
    """

    model_config = ConfigDict(extra="forbid")

    unit: str = Field(min_length=1)
    limit: float | None = Field(default=None, ge=0)
    spent: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _limit_and_spent_are_paired(self) -> ResearchBudget:
        """Reject a lone ``limit`` or a lone ``spent`` on the same axis.

        Raises:
            ValueError: when exactly one of :attr:`limit` / :attr:`spent`
                is set — a pair renders honestly only when both sides of
                the axis are observed together.
        """
        has_limit = self.limit is not None
        has_spent = self.spent is not None
        if has_limit != has_spent:
            lone = "limit" if has_limit else "spent"
            raise ValueError(f"budget axis {self.unit!r} has a {lone} with no counterpart")
        return self

    @property
    def remainder(self) -> float | None:
        """Derived headroom (``limit - spent``), or ``None`` when unset."""
        if self.limit is None or self.spent is None:
            return None
        return self.limit - self.spent


#: The evidence-budget axes a campaign run spends, mapped to the unit each
#: axis is measured in. ``rounds`` charges one per executed round; ``usd``
#: charges the researcher spend booked against the campaign that round.
EVIDENCE_BUDGET_UNITS: Final[dict[str, str]] = {"rounds": "rounds", "usd": "usd"}


def open_evidence_budget(limits: Mapping[str, float]) -> dict[str, ResearchBudget]:
    """Build the evidence budget a campaign is created with: each limit, nothing spent.

    Args:
        limits: Per-axis limit keyed by an :data:`EVIDENCE_BUDGET_UNITS` axis
            name. An empty mapping creates an unbudgeted campaign.

    Returns:
        One :class:`ResearchBudget` per axis with ``spent=0``.

    Raises:
        ValueError: when an axis name is not in :data:`EVIDENCE_BUDGET_UNITS`
            (a run could never charge it, so the limit would never bind), or a
            limit is negative.
    """
    unknown = sorted(set(limits) - set(EVIDENCE_BUDGET_UNITS))
    if unknown:
        raise ValueError(
            f"unknown evidence budget axis: {unknown!r}; known: {sorted(EVIDENCE_BUDGET_UNITS)!r}"
        )
    return {
        axis: ResearchBudget(unit=EVIDENCE_BUDGET_UNITS[axis], limit=limit, spent=0.0)
        for axis, limit in limits.items()
    }


def charge_evidence_budget(
    budget: Mapping[str, ResearchBudget], spend: Mapping[str, float]
) -> dict[str, ResearchBudget]:
    """Return *budget* with one round's *spend* added to every configured axis.

    Args:
        budget: The campaign's current per-axis budget.
        spend: The round's spend per axis name; an axis the budget does not
            configure is ignored, and a configured axis absent from *spend*
            is left as it stands.

    Returns:
        A new per-axis budget with the spend applied.
    """
    charged: dict[str, ResearchBudget] = {}
    for axis, pair in budget.items():
        if pair.limit is None or pair.spent is None or axis not in spend:
            charged[axis] = pair
            continue
        charged[axis] = pair.model_copy(update={"spent": pair.spent + spend[axis]})
    return charged


def exhausted_budget_axis(
    budget: Mapping[str, ResearchBudget], next_spend: Mapping[str, float]
) -> str | None:
    """Name the first axis a round spending *next_spend* would push past its limit.

    An axis with no headroom left is exhausted even when the projected spend
    is zero, because a researcher round never costs nothing.

    Args:
        budget: The campaign's current per-axis budget.
        next_spend: The projected spend of the next round per axis name.

    Returns:
        The first exhausted axis name in sorted order, or ``None`` when every
        configured axis can afford the round.
    """
    for axis in sorted(budget):
        remainder = budget[axis].remainder
        if remainder is None:
            continue
        if remainder <= 0 or next_spend.get(axis, 0.0) > remainder:
            return axis
    return None


class IllegalCampaignTransitionError(ValueError):
    """Raised when a Campaign status edge is not in :data:`CAMPAIGN_TRANSITIONS`.

    A caller asking to move a campaign from one :class:`CampaignStatus` to
    another that the matrix does not allow gets this typed refusal instead
    of a silently-accepted illegal write.
    """


#: The complete Campaign status transition matrix: every :class:`CampaignStatus`
#: maps to the set of statuses it may move to next. ``CONVERGED`` and
#: ``CANCELLED`` are terminal — their target sets are empty — so every status
#: carries an explicit entry rather than an implicit gap.
CAMPAIGN_TRANSITIONS: Final[dict[CampaignStatus, frozenset[CampaignStatus]]] = {
    CampaignStatus.ACTIVE: frozenset({CampaignStatus.CONVERGED, CampaignStatus.CANCELLED}),
    CampaignStatus.CONVERGED: frozenset(),
    CampaignStatus.CANCELLED: frozenset(),
}


def validate_campaign_transition(current: CampaignStatus, target: CampaignStatus) -> None:
    """Refuse a Campaign status edge that :data:`CAMPAIGN_TRANSITIONS` disallows.

    Args:
        current: The campaign's status before the edge.
        target: The status the caller wants to move it to.

    Raises:
        IllegalCampaignTransitionError: when *target* is not one of the
            statuses :data:`CAMPAIGN_TRANSITIONS` allows from *current*.
    """
    if target not in CAMPAIGN_TRANSITIONS[current]:
        raise IllegalCampaignTransitionError(
            f"illegal campaign transition: {current.value!r} -> {target.value!r}"
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
        status: The campaign's lifecycle position. Defaults to
            :attr:`~eawf.kernel.state.enums.CampaignStatus.ACTIVE` so every
            existing row stays valid without backfill; cancelling flips it to
            :attr:`~eawf.kernel.state.enums.CampaignStatus.CANCELLED`.
        tombstone: The cancel-time marker, present iff :attr:`status` is
            ``CANCELLED``; ``None`` for an active campaign.
        evidence_budget: Per-axis :class:`ResearchBudget` pairs (keyed by
            axis name, e.g. ``"rounds"``). Empty for a campaign created
            without a budget, which a run never halts on.
    """

    model_config = ConfigDict(extra="forbid")

    campaign_id: str = Field(min_length=1)
    config: ResearchProfileBlock
    campaign: StagedCampaign
    status: CampaignStatus = CampaignStatus.ACTIVE
    tombstone: CampaignTombstone | None = None
    evidence_budget: dict[str, ResearchBudget] = Field(default_factory=dict)

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


def complete_campaign(campaign: ResearchCampaignPayload) -> ResearchCampaignPayload:
    """Transition *campaign* to its terminal ``CONVERGED`` (completed) status.

    Takes only the campaign — no Milestone reference is accepted or
    reachable from this call, so completing a Campaign's evidence gathering
    can never move a Milestone's delivery status; accepting a Milestone
    stays a separate, explicit act.

    Args:
        campaign: The campaign to complete.

    Returns:
        A copy of *campaign* with ``status=CampaignStatus.CONVERGED``.

    Raises:
        IllegalCampaignTransitionError: when *campaign* is not
            :attr:`~eawf.kernel.state.enums.CampaignStatus.ACTIVE` (the only
            status :data:`CAMPAIGN_TRANSITIONS` allows a convergence from).
    """
    validate_campaign_transition(campaign.status, CampaignStatus.CONVERGED)
    return campaign.model_copy(update={"status": CampaignStatus.CONVERGED})
