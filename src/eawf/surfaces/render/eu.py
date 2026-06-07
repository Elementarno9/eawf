"""Effort-unit (EU) render helpers backed by a shared accruing accumulator.

The EU render surface tracks two running totals for a scope: ``elapsed``
EU (effort already consumed) and ``projected`` EU (the remaining estimate).
Rather than re-summing a contribution list on every render, the totals
accrue into a shared :class:`EUAccrual` accumulator: each
:meth:`EUAccrual.accrue` adds one wave's contribution to the running totals
in constant time, and the render functions read those totals directly.

The accumulator is the single source of the elapsed / projected pair, so
the burn ratio, the projected total, and the rendered summary line are all
derived from one accrued state -- no parallel re-walk of the contribution
set that could drift from the running totals.

The render functions here are pure string formatters over the accumulator;
they carry no colour and no widget state. The block-eighths bar
(:mod:`~eawf.surfaces.render.bars`) consumes the burn ratio when a caller
wants a glyph fill alongside the numeric summary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: Rendered when an accumulator has accrued no projection -- a scope with no
#: estimated EU has no burn to show, so the summary surfaces this sentinel
#: rather than a fabricated 0 EU / 0 % line.
EMPTY_STATE: str = "-- no EU"


@dataclass
class EUAccrual:
    """A shared accumulator of elapsed + projected EU running totals.

    Holds the two running totals for a scope. Each :meth:`accrue` adds a
    wave's elapsed and projected contribution to the totals in constant
    time, so a render reads the accrued pair without re-summing the
    contributions. The :attr:`total_eu` and :attr:`burn_ratio` derived
    properties read straight off the running totals.

    Attributes:
        elapsed_eu: Running total of effort units already consumed (>= 0).
        projected_eu: Running total of remaining-estimate effort units
            (>= 0). The total estimate is :attr:`total_eu` -- elapsed plus
            projected.
    """

    elapsed_eu: float = field(default=0.0)
    projected_eu: float = field(default=0.0)

    def accrue(self, *, elapsed_eu: float, projected_eu: float) -> None:
        """Add one contribution to the running elapsed + projected totals.

        Args:
            elapsed_eu: Effort units this contribution has consumed (>= 0).
            projected_eu: Remaining-estimate effort units for this
                contribution (>= 0).

        Raises:
            ValueError: When *elapsed_eu* or *projected_eu* is negative.
        """
        if elapsed_eu < 0.0:
            raise ValueError(f"elapsed_eu must be non-negative: {elapsed_eu!r}")
        if projected_eu < 0.0:
            raise ValueError(f"projected_eu must be non-negative: {projected_eu!r}")
        self.elapsed_eu += elapsed_eu
        self.projected_eu += projected_eu

    @property
    def total_eu(self) -> float:
        """Return the total estimated EU -- elapsed plus projected."""
        return self.elapsed_eu + self.projected_eu

    @property
    def burn_ratio(self) -> float:
        """Return the elapsed-over-total burn ratio in ``[0, 1]``.

        A zero total yields ``0.0`` rather than dividing by zero.
        """
        total = self.total_eu
        if total <= 0.0:
            return 0.0
        return self.elapsed_eu / total


def render_eu_summary(accrual: EUAccrual) -> str:
    """Render the elapsed / total EU summary line from the accrued totals.

    Reads the running totals off *accrual* -- it does not re-sum any
    contribution set -- so the rendered line stays in lock-step with the
    accumulator. A scope that has accrued no projection (zero total)
    surfaces :data:`EMPTY_STATE`.

    Args:
        accrual: The shared accumulator carrying the elapsed + projected
            running totals.

    Returns:
        A line of the form ``3.5/8.0 EU (44%)``, or :data:`EMPTY_STATE`
        when the accrued total is zero.
    """
    total = accrual.total_eu
    if total <= 0.0:
        return EMPTY_STATE
    pct = int(accrual.burn_ratio * 100 + 0.5)
    return f"{accrual.elapsed_eu:.1f}/{total:.1f} EU ({pct}%)"


def accrue_all(contributions: list[tuple[float, float]]) -> EUAccrual:
    """Build an accumulator by accruing each ``(elapsed, projected)`` pair.

    Convenience constructor that folds a contribution list into a single
    :class:`EUAccrual` via repeated :meth:`EUAccrual.accrue`, so a caller
    with a ready list gets the same accrued state a streaming caller builds
    incrementally.

    Args:
        contributions: One ``(elapsed_eu, projected_eu)`` pair per wave.

    Returns:
        An :class:`EUAccrual` with the folded running totals.

    Raises:
        ValueError: When any pair carries a negative component.
    """
    accrual = EUAccrual()
    for elapsed_eu, projected_eu in contributions:
        accrual.accrue(elapsed_eu=elapsed_eu, projected_eu=projected_eu)
    return accrual


__all__ = [
    "EMPTY_STATE",
    "EUAccrual",
    "accrue_all",
    "render_eu_summary",
]
