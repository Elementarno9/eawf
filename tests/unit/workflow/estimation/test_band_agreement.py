"""The elapsed publisher and the stale advisory band a wave the same way."""

from __future__ import annotations

import pytest

from eawf.runtime.daemon.methods.state_events import _wave_elapsed_band
from eawf.workflow.estimation.buckets import BUCKET_EU, EU_MINUTES
from eawf.workflow.estimation.thresholds import classify_band

#: Elapsed minutes that land exactly on a band edge for each effort bucket. The
#: publisher quantizes elapsed to whole minutes, so these are hit rather than
#: skipped past, and every one is exactly representable as a double.
EDGE_CASES: list[tuple[float, float]] = [
    (budget * fraction, budget)
    for budget in (eu * EU_MINUTES for eu in BUCKET_EU.values())
    for fraction in (0.80, 1.00)
]


@pytest.mark.parametrize(
    ("elapsed", "budget"),
    [pytest.param(e, b, id=f"{e:g}of{b:g}") for e, b in EDGE_CASES],
)
def test_the_two_readers_agree_on_a_band_edge(elapsed: float, budget: float) -> None:
    """Two panes must not disagree about the same wave.

    Both once owned a copy of this banding under opposite comparison operators, so a
    wave sitting exactly on an edge read one way in the digest and another in the
    advisory.
    """
    assert _wave_elapsed_band(elapsed, budget) == classify_band(elapsed / budget)


def test_an_absent_budget_bands_as_ok() -> None:
    """No budget is not an over-budget signal."""
    assert _wave_elapsed_band(99.0, None) == "ok"
    assert _wave_elapsed_band(99.0, 0.0) == "ok"
