"""When a budget notice may open: the one policy that owns every notice number.

A notice reports an event that has happened -- an estimate was passed, a
ceiling was reached -- never a prediction that one is coming. The policy
therefore carries no fraction below one on any basis: the estimate's
notify fraction is at least one, and nothing else is declared. A former
progress or warning fraction, under that name or any other, is an unknown
key and fails to load, so a later revision cannot reintroduce an interrupt
at eighty percent by renaming it. The observed fraction stays readable in
the usage gauge; only the interrupt is gone.

Passing an estimate proves elapsed-time arithmetic, not that anything is
wrong: an optimistic estimate and a healthy long Run cross it alike. So an
estimate notice opens only once no progress has been observed for the
grace period as well, and until then the crossing is an activity fact and
nothing more.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Final

from pydantic import Field, StrictInt

from eawf.kernel.economics.prompt_budget import BudgetPolicyId
from eawf.kernel.runtime.provider import RuntimeRecord


class EstimatedTimeNotice(RuntimeRecord):
    """When an elapsed-time estimate crossing becomes a notice.

    Attributes:
        notify_fraction: The elapsed fraction of the estimate at which the
            crossing is a durable fact. At least one: below it the estimate
            has not been passed, and a notice there would be a prediction.
        require_no_progress_for: Seconds with no observed progress before
            the crossing opens a notice.
    """

    notify_fraction: Annotated[Decimal, Field(ge=1)] = Decimal("1.00")
    require_no_progress_for: Annotated[StrictInt, Field(ge=1)] = 600

    def passed(self, *, elapsed_seconds: float, estimate_seconds: float) -> bool:
        """Return whether *elapsed_seconds* reached the notify fraction of the estimate.

        Args:
            elapsed_seconds: Wall seconds since the work started.
            estimate_seconds: The estimate those seconds are measured against.

        Returns:
            ``True`` once the elapsed fraction is at or past the notify
            fraction; never for a non-positive estimate, which states no
            budget to pass.
        """
        if estimate_seconds <= 0:
            return False
        fraction = Decimal(str(elapsed_seconds)) / Decimal(str(estimate_seconds))
        return fraction >= self.notify_fraction

    def grace_met(self, *, last_progress_at: datetime, now: datetime) -> bool:
        """Return whether no progress has been seen for the grace period.

        Args:
            last_progress_at: When progress was last observed.
            now: The reference time.

        Returns:
            ``True`` when at least :attr:`require_no_progress_for` seconds
            separate *last_progress_at* from *now*.
        """
        return now - last_progress_at >= timedelta(seconds=self.require_no_progress_for)


class BudgetNoticePolicy(RuntimeRecord):
    """The validated ``economics.notice_policy`` table.

    Attributes:
        policy_id: The policy's stable identity.
        revision: Bumped on every change.
        estimated_time: When an estimate crossing becomes a notice.
    """

    policy_id: BudgetPolicyId = "NTC-DEFAULT"
    revision: Annotated[StrictInt, Field(ge=1)] = 1
    estimated_time: EstimatedTimeNotice = EstimatedTimeNotice()


#: The shipped notice policy, used where the configuration declares none.
DEFAULT_NOTICE_POLICY: Final[BudgetNoticePolicy] = BudgetNoticePolicy()


__all__ = [
    "DEFAULT_NOTICE_POLICY",
    "BudgetNoticePolicy",
    "EstimatedTimeNotice",
]
