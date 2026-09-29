"""CampaignFinding: what a Campaign learned, promoted into its own record.

Three things in the system are called findings, and only this one is a
Campaign result. A plan-lens finding is the deterministic verdict a lens
returns over one plan body and lives only as long as that check; a review
finding is what a Batch reviewer reported. The three have different
writers, different fields and different lifetimes, so each keeps its own
name and none of them is addressed through a shared finding URN: a field
typed :data:`~eawf.kernel.state.epoch2.urns.CampaignFindingUrn` refuses
any other kind with ``identity_kind_mismatch``, which is what keeps a
citation, a repair scope or an ownership selector unambiguous about
which record it names.

A finding nobody consumed is still a result. Its disposition is ``held``
until a Milestone consumes it, and a held finding stays in the record set
rather than being dropped for want of a consumer.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import StringConstraints, model_validator

from eawf.kernel.state.epoch2.urns import CampaignFindingUrn, MilestoneUrn
from eawf.kernel.state.epoch2.values import Epoch2Record
from eawf.kernel.state.types import UtcDatetime

#: One line saying what was learned. A line break is refused because the
#: statement is the row a promoted-findings view prints, and a second line
#: is where an argument starts hiding inside a result.
FindingStatement = Annotated[
    str,
    StringConstraints(
        strict=True, strip_whitespace=True, min_length=1, max_length=200, pattern=r"^[^\r\n]+$"
    ),
]


class FindingDisposition(StrEnum):
    """Whether a Milestone has consumed a finding yet."""

    HELD = "held"
    CONSUMED = "consumed"


class CampaignFinding(Epoch2Record):
    """A promoted Campaign finding, keyed ``CFN-####``.

    Attributes:
        urn: The finding's own address; any other kind is refused.
        statement: What was learned, in one line.
        disposition: ``held`` until a Milestone consumes it, then ``consumed``.
        consumed_by_milestone_ref: The Milestone that consumed it.
        consumed_at: When that Milestone consumed it.

    Raises:
        pydantic.ValidationError: The URN addresses another kind, or the
            disposition disagrees with the consumption fields.
    """

    urn: CampaignFindingUrn
    statement: FindingStatement
    disposition: FindingDisposition = FindingDisposition.HELD
    consumed_by_milestone_ref: MilestoneUrn | None = None
    consumed_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _disposition_matches_consumption(self) -> Self:
        """Keep the disposition, the consuming Milestone and its time in lockstep.

        Raises:
            ValueError: Only one of the two consumption fields is set, or
                the disposition names the other state.
        """
        consumed = self.consumed_by_milestone_ref is not None
        if consumed != (self.consumed_at is not None):
            raise ValueError("consumed_by_milestone_ref and consumed_at are set together or not")
        if consumed != (self.disposition is FindingDisposition.CONSUMED):
            raise ValueError(
                f"a finding {'consumed by a Milestone' if consumed else 'no Milestone consumed'} "
                f"cannot carry the disposition {self.disposition.value!r}"
            )
        return self


__all__ = ["CampaignFinding", "FindingDisposition", "FindingStatement"]
