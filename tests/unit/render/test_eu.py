"""Unit tests for the accruing EU render helpers (P29-I13-W23).

Pins :class:`~eawf.surfaces.render.eu.EUAccrual` (the shared accumulator)
and the render functions over it: the running-total accrual (no recompute),
the derived total / burn ratio, the summary line, and the error paths.
"""

from __future__ import annotations

import pytest

from eawf.surfaces.render.eu import (
    EMPTY_STATE,
    EUAccrual,
    accrue_all,
    render_eu_summary,
)

# --------------------------------------------------------------------------
# EUAccrual -- the shared accumulator
# --------------------------------------------------------------------------


def test_accrual_empty_totals_are_zero() -> None:
    """A fresh accumulator carries zero elapsed, projected, and total."""
    accrual = EUAccrual()
    assert accrual.elapsed_eu == pytest.approx(0.0)
    assert accrual.projected_eu == pytest.approx(0.0)
    assert accrual.total_eu == pytest.approx(0.0)
    assert accrual.burn_ratio == pytest.approx(0.0)


def test_accrue_accumulates_running_totals() -> None:
    """Each accrue adds to the running totals rather than replacing them."""
    accrual = EUAccrual()
    accrual.accrue(elapsed_eu=1.0, projected_eu=3.0)
    accrual.accrue(elapsed_eu=2.5, projected_eu=1.5)
    assert accrual.elapsed_eu == pytest.approx(3.5)
    assert accrual.projected_eu == pytest.approx(4.5)
    assert accrual.total_eu == pytest.approx(8.0)


def test_burn_ratio_is_elapsed_over_total() -> None:
    """The burn ratio is elapsed / (elapsed + projected)."""
    accrual = EUAccrual()
    accrual.accrue(elapsed_eu=3.5, projected_eu=4.5)
    assert accrual.burn_ratio == pytest.approx(3.5 / 8.0)


def test_burn_ratio_zero_total_is_zero() -> None:
    """A zero total yields a zero burn ratio rather than dividing by zero."""
    assert EUAccrual().burn_ratio == pytest.approx(0.0)


def test_streaming_accrual_matches_folded_list() -> None:
    """Incremental accrual and the list-fold helper reach the same totals.

    The accumulator's whole point is that the running totals never need a
    re-walk of the contribution set; this pins that a streaming caller and
    a list caller land on identical accrued state.
    """
    streamed = EUAccrual()
    contributions = [(1.0, 3.0), (2.5, 1.5), (0.0, 2.0)]
    for elapsed_eu, projected_eu in contributions:
        streamed.accrue(elapsed_eu=elapsed_eu, projected_eu=projected_eu)
    folded = accrue_all(contributions)
    assert streamed.elapsed_eu == pytest.approx(folded.elapsed_eu)
    assert streamed.projected_eu == pytest.approx(folded.projected_eu)
    assert streamed.total_eu == pytest.approx(folded.total_eu)


@pytest.mark.parametrize(
    ("elapsed", "projected"),
    [(-1.0, 0.0), (0.0, -2.0), (-0.5, -0.5)],
)
def test_accrue_negative_component_raises(elapsed: float, projected: float) -> None:
    """A negative elapsed or projected component raises ``ValueError``."""
    accrual = EUAccrual()
    with pytest.raises(ValueError, match="must be non-negative"):
        accrual.accrue(elapsed_eu=elapsed, projected_eu=projected)


# --------------------------------------------------------------------------
# render_eu_summary -- the formatted line over the accrued totals
# --------------------------------------------------------------------------


def test_render_eu_summary_formats_elapsed_over_total() -> None:
    """The summary renders elapsed / total EU with the burn percentage."""
    accrual = EUAccrual()
    accrual.accrue(elapsed_eu=3.5, projected_eu=4.5)
    assert render_eu_summary(accrual) == "3.5/8.0 EU (44%)"


def test_render_eu_summary_empty_is_sentinel() -> None:
    """A zero-total accumulator renders the honest-empty sentinel."""
    assert render_eu_summary(EUAccrual()) == EMPTY_STATE


def test_render_eu_summary_full_burn_is_hundred_percent() -> None:
    """An all-elapsed accumulator renders 100 %."""
    accrual = EUAccrual()
    accrual.accrue(elapsed_eu=4.0, projected_eu=0.0)
    assert render_eu_summary(accrual) == "4.0/4.0 EU (100%)"


def test_accrue_all_empty_list_is_empty_accumulator() -> None:
    """Folding an empty contribution list yields a zero accumulator."""
    accrual = accrue_all([])
    assert accrual.total_eu == pytest.approx(0.0)
    assert render_eu_summary(accrual) == EMPTY_STATE
