"""TrackPolicy: where a Track's domain variation lives.

A Track has two stored states and no more. Everything a domain might
otherwise express as an extra state -- how much work may run at once,
which Milestone kinds are admitted, how outcomes are measured, how the
Track presents -- is a policy field here, so adding a variant never adds
an edge to a transition table and never invalidates a proof bound to one.

The two work-in-progress limits differ in kind, not just in value, and
the difference is the reason they are separate fields with separate
predicates.

``active_batches_per_repo`` is mechanical. Canonical integration is
serialised per repository and every Batch binds an exact head, so a
second active Batch in one repository continuously invalidates the first
one's exact-head audit and review. :meth:`TrackPolicy.admits_batch_activation`
therefore answers admit-or-refuse.

``active_milestones`` is advisory. Nothing breaks when a Track carries
more active Milestones; the limit bounds split attention and keeps a
roadmap readable. :meth:`TrackPolicy.exceeds_milestone_advisory` therefore
answers a watchlist question and never refuses anything.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StrictInt, field_validator, model_validator

from eawf.kernel.state.epoch2.base import (
    Epoch2Model,
    MetricId,
    SlugStr,
    StrictNonNegativeInt,
    StrictPositiveInt,
    TokenStr,
)
from eawf.kernel.state.epoch2.milestone import MilestoneKind
from eawf.kernel.state.epoch2.values import OwnerPrincipal


class WipPolicy(Epoch2Model):
    """The two work-in-progress limits a Track declares.

    Exactly these two fields. A third limit would have to say which of the
    two kinds it is -- refusing or advising -- and the answer belongs in
    the type rather than in a comment beside a shared integer.
    """

    active_milestones: StrictNonNegativeInt
    active_batches_per_repo: Annotated[StrictInt, Field(ge=1)]


class MetricSpec(Epoch2Model):
    """One outcome metric a Track measures.

    A metric with no declared unit or locus cannot be compared across two
    readings, so both are required rather than defaulted.
    """

    metric_id: MetricId
    unit: TokenStr
    measurement_locus: TokenStr


class TrackPresentationDefaults(Epoch2Model):
    """UI-only defaults. Never mutation authority.

    A renderer reads these; no transition guard does. Keeping them in a
    named sub-model rather than loose on the policy is what makes that
    boundary checkable.
    """

    default_view: TokenStr
    color_token: TokenStr


class CampaignTemplateRef(Epoch2Model):
    """A campaign template the Track's policy admits."""

    template_id: SlugStr


class PromotionRule(Epoch2Model):
    """One rule turning Track evidence into an outcome or a projection.

    ``produces`` is a closed pair because a rule that could produce a
    Track status would be a third lifecycle authority beside the two
    stored states and the transition table.
    """

    rule_id: SlugStr
    produces: Literal["outcome", "projection"]


def _reject_duplicate_ids(ids: tuple[str, ...], *, field: str) -> None:
    """Raise when *ids* repeats an identifier.

    Args:
        ids: The identifiers to check.
        field: The field name to name in the rejection message.

    Raises:
        ValueError: An identifier appears twice.
    """
    if len(set(ids)) != len(ids):
        raise ValueError(f"{field} repeats an identifier: {ids}")


class TrackPolicy(Epoch2Model):
    """The strict policy document a Track carries.

    ``revision`` is incremented on any policy edit and is what a proof
    binds, so a policy change invalidates the proofs taken under the
    previous revision rather than silently re-grading them.
    """

    revision: StrictPositiveInt
    wip: WipPolicy
    ownership_principal: OwnerPrincipal
    permitted_milestone_kinds: frozenset[MilestoneKind]
    campaign_templates: tuple[CampaignTemplateRef, ...] = ()
    outcome_metrics: tuple[MetricSpec, ...] = ()
    promotion_rules: tuple[PromotionRule, ...] = ()
    integration_priority: Annotated[StrictInt, Field(ge=0, le=100)]
    presentation: TrackPresentationDefaults

    @field_validator("permitted_milestone_kinds")
    @classmethod
    def _admits_at_least_one_kind(cls, value: frozenset[MilestoneKind]) -> frozenset[MilestoneKind]:
        """Require the policy to admit at least one Milestone kind.

        Raises:
            ValueError: The set is empty, which would admit no Milestone
                at all while still reading as a configured Track.
        """
        if not value:
            raise ValueError("permitted_milestone_kinds must admit at least one kind")
        return value

    @model_validator(mode="after")
    def _leaf_ids_are_unique(self) -> Self:
        """Require unique metric, template and rule identifiers.

        Raises:
            ValueError: Two leaves share an identifier, which would make a
                reference to it ambiguous.
        """
        _reject_duplicate_ids(
            tuple(metric.metric_id for metric in self.outcome_metrics),
            field="outcome_metrics",
        )
        _reject_duplicate_ids(
            tuple(template.template_id for template in self.campaign_templates),
            field="campaign_templates",
        )
        _reject_duplicate_ids(
            tuple(rule.rule_id for rule in self.promotion_rules),
            field="promotion_rules",
        )
        return self

    def admits_batch_activation(self, *, active_batches_in_repo: int) -> bool:
        """Whether one more Batch may activate in a repository.

        This is the hard limit: a caller that gets ``False`` refuses the
        activation rather than recording an exception.

        Args:
            active_batches_in_repo: How many Batches of this Track are
                already ACTIVE in the target repository.

        Returns:
            ``True`` when the repository has room for one more.
        """
        return active_batches_in_repo < self.wip.active_batches_per_repo

    def exceeds_milestone_advisory(self, *, active_milestones: int) -> bool:
        """Whether the Track carries more active Milestones than advised.

        This is the advisory limit: a caller that gets ``True`` raises a
        watchlist signal and proceeds. Nothing refuses on this answer.

        Args:
            active_milestones: How many Milestones of this Track are
                currently ACTIVE.

        Returns:
            ``True`` when the advisory bound is exceeded.
        """
        return active_milestones > self.wip.active_milestones


__all__ = [
    "CampaignTemplateRef",
    "MetricSpec",
    "PromotionRule",
    "TrackPolicy",
    "TrackPresentationDefaults",
    "WipPolicy",
]
