"""The wave time-budget projection.

:func:`wave_budget_minutes` is the one wave-id -> pessimistic-minutes
projection (estimate row first, else the measured p90 of the one effort
constant), so every elapsed-time reader shares one budget semantics
instead of re-deriving it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eawf.kernel.state.models import State


def wave_budget_minutes(state: State, wave_id: str) -> float:
    """Return a wave's pessimistic time budget in minutes.

    Prefers an explicit estimate row's ``pessimistic_minutes`` and falls
    back to the declared upper bound of the mapping in force, whatever size
    label the wave carries.

    Args:
        state: Loaded typed state snapshot (read-only).
        wave_id: The wave whose time budget is wanted.

    Returns:
        The pessimistic budget in minutes (``> 0``).
    """
    estimates = state.estimates or {}
    estimate = estimates.get(wave_id)
    if estimate is not None and estimate.pessimistic_minutes > 0:
        return estimate.pessimistic_minutes
    from eawf.workflow.estimation.mapping import CURRENT_EFFORT_MAPPING

    return CURRENT_EFFORT_MAPPING.pessimistic_minutes


__all__ = ["wave_budget_minutes"]
