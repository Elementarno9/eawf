"""When a budget notice may open: the one policy that owns every notice number.

A notice reports a hard ceiling a running Run has reached, never an
estimate it passed nor a prediction that a ceiling is coming: passing an
estimate proved elapsed-time arithmetic, and every notice it opened was
the same wall-clock reading that discriminated nothing. So one rule
remains, the same on every hard axis: the daemon ends a Run at its token
or wall-clock ceiling and stops its worker, so the notice reports that
stop and opens at once.

A former estimate or warning fraction, under any name, is an unknown key
and fails to load, so a later revision cannot reintroduce an interrupt
below the ceiling by renaming it. The observed fraction stays readable in
the usage gauge; only the interrupt is gone.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any, Final

from pydantic import Field, StrictInt, model_validator

from eawf.kernel.economics.prompt_budget import BudgetPolicyId
from eawf.kernel.runtime.provider import RuntimeRecord

#: The key the estimate threshold was configured under before it was removed.
_REMOVED_NOTIFY_FRACTION: Final = "notify_fraction"


class EstimatedTimeNotice(RuntimeRecord):
    """When a Run that reached a hard ceiling opens a budget notice.

    Attributes:
        require_no_progress_for: Seconds a wave past its estimate must show
            no progress before its crossing meets the grace.
    """

    require_no_progress_for: Annotated[StrictInt, Field(ge=1)] = 600

    @model_validator(mode="before")
    @classmethod
    def _refuse_removed_keys(cls, data: Any) -> Any:
        """Name the removed estimate threshold rather than report a bare unknown key.

        Raises:
            ValueError: The table still declares ``notify_fraction``.
        """
        if isinstance(data, dict) and _REMOVED_NOTIFY_FRACTION in data:
            raise ValueError(
                f"{_REMOVED_NOTIFY_FRACTION} was removed: a budget notice opens only at "
                "a Run's hard ceiling, so delete the key"
            )
        return data

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

    def opens(self, *, running: bool, ceiling_reached: bool) -> bool:
        """Return whether a ceiling reading opens a budget notice: the one notice rule.

        Args:
            running: Whether the scope is a Run in ``RUNNING``. Anything
                else -- a stopped Run, a wave -- never opens a notice.
            ceiling_reached: Whether the reading reached the Run's hard
                ceiling on its axis.

        Returns:
            ``True`` for a running Run at its hard ceiling, on either axis.
        """
        return running and ceiling_reached


class BudgetNoticePolicy(RuntimeRecord):
    """The validated ``economics.notice_policy`` table.

    Attributes:
        policy_id: The policy's stable identity.
        revision: Bumped on every change.
        estimated_time: When a ceiling crossing becomes a notice.
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
