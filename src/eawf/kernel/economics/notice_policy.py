"""When a budget notice may open: the one policy that owns every notice number.

A notice reports a hard ceiling a running Run has reached, never an
estimate it passed nor a prediction that a ceiling is coming: passing an
estimate proved elapsed-time arithmetic, and every notice it opened was
the same wall-clock reading that discriminated nothing. So one rule
remains. On the token axis the ceiling's kill ladder has already acted,
and the notice opens at once. On the wall-clock axis a long healthy Run
and a stuck one reach the ceiling alike, so the notice opens only once
the Run has also shown no event for the grace period.

A former estimate or warning fraction, under any name, is an unknown key
and fails to load, so a later revision cannot reintroduce an interrupt
below the ceiling by renaming it. The observed fraction stays readable in
the usage gauge; only the interrupt is gone.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any, Final, Literal

from pydantic import Field, StrictInt, model_validator

from eawf.kernel.economics.prompt_budget import BudgetPolicyId
from eawf.kernel.runtime.provider import RuntimeRecord

#: The key the estimate threshold was configured under before it was removed.
_REMOVED_NOTIFY_FRACTION: Final = "notify_fraction"


class EstimatedTimeNotice(RuntimeRecord):
    """When a Run that reached a hard ceiling opens a budget notice.

    Attributes:
        require_no_progress_for: Seconds a Run must show no event before
            its wall-clock ceiling crossing opens a notice.
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

    def opens(
        self,
        *,
        running: bool,
        axis: Literal["tokens", "wall_seconds"],
        ceiling_reached: bool,
        silent_for: timedelta,
    ) -> bool:
        """Return whether a ceiling reading opens a budget notice: the one notice rule.

        Args:
            running: Whether the scope is a Run in ``RUNNING``. Anything
                else -- a stopped Run, a wave -- never opens a notice.
            axis: The metered resource the ceiling is set on.
            ceiling_reached: Whether the reading reached the Run's hard
                ceiling on *axis*.
            silent_for: How long the Run has shown no event, counted from
                its start when it has shown none. The token axis ignores it.

        Returns:
            ``True`` for a running Run at its hard ceiling: always on the
            token axis, and on the wall-clock axis only once the Run has
            been silent for :attr:`require_no_progress_for` seconds.
        """
        if not (running and ceiling_reached):
            return False
        return axis == "tokens" or silent_for >= timedelta(seconds=self.require_no_progress_for)


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
