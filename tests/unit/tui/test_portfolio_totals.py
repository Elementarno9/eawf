"""Unit tests for ``portfolio_totals`` -- the workspace summary-row reducer.

The workspace table appends one totals row under the per-repo rows: the
portfolio reducer sums every repo row's active-phase wave counts and EU
pair into one :class:`PortfolioTotals`, rendered under the table. The
reducer is pure (rows in, totals out), so the tests stay unit-level (no
Pilot / app mount). Repo codes are abstract placeholders (``ABC`` /
``DEF`` / ``GHI``), never real-looking project names.
"""

from __future__ import annotations

import pytest

from eawf.surfaces.tui.widgets.workspace_table import (
    PortfolioTotals,
    RepoRow,
    portfolio_totals,
)


def _row(
    code: str,
    *,
    phase_done: int,
    phase_total: int,
    eu_consumed: float,
    eu_total: float,
) -> RepoRow:
    """Build a minimal :class:`RepoRow` carrying only the reducer inputs."""
    return RepoRow(
        code=code,
        path=f"/abs/path/{code.lower()}",
        phase_id=None,
        phase_done=phase_done,
        phase_total=phase_total,
        eu_consumed=eu_consumed,
        eu_total=eu_total,
        age="—",
    )


# --------------------------------------------------------------------------
# Boundary: empty + single repo
# --------------------------------------------------------------------------


def test_portfolio_totals_empty_is_zero() -> None:
    """No repos folds to a zero-valued totals (``repo_count == 0``)."""
    totals = portfolio_totals([])
    assert totals == PortfolioTotals(
        repo_count=0,
        wave_done=0,
        wave_total=0,
        eu_consumed=pytest.approx(0.0),
        eu_total=pytest.approx(0.0),
    )


def test_portfolio_totals_single_repo_passes_through() -> None:
    """One repo's counts are the totals verbatim (``repo_count == 1``)."""
    totals = portfolio_totals(
        [_row("ABC", phase_done=3, phase_total=6, eu_consumed=1.5, eu_total=4.0)]
    )
    assert totals.repo_count == 1
    assert totals.wave_done == 3
    assert totals.wave_total == 6
    assert totals.eu_consumed == pytest.approx(1.5)
    assert totals.eu_total == pytest.approx(4.0)


# --------------------------------------------------------------------------
# Sum correctness across multiple repos
# --------------------------------------------------------------------------


def test_portfolio_totals_sums_wave_counts_and_eu() -> None:
    """Three repos: wave counts + EU sum element-wise into the totals row."""
    rows = [
        _row("ABC", phase_done=3, phase_total=6, eu_consumed=1.5, eu_total=4.0),
        _row("DEF", phase_done=2, phase_total=5, eu_consumed=0.5, eu_total=2.0),
        _row("GHI", phase_done=0, phase_total=4, eu_consumed=0.0, eu_total=1.0),
    ]
    totals = portfolio_totals(rows)
    assert totals.repo_count == 3
    assert totals.wave_done == 5
    assert totals.wave_total == 15
    assert totals.eu_consumed == pytest.approx(2.0)
    assert totals.eu_total == pytest.approx(7.0)


def test_portfolio_totals_all_zero_repos_stays_zero() -> None:
    """Repos that report no waves / EU keep the totals at zero (honest empty)."""
    rows = [
        _row("ABC", phase_done=0, phase_total=0, eu_consumed=0.0, eu_total=0.0),
        _row("DEF", phase_done=0, phase_total=0, eu_consumed=0.0, eu_total=0.0),
    ]
    totals = portfolio_totals(rows)
    assert totals.repo_count == 2
    assert totals.wave_done == 0
    assert totals.wave_total == 0
    assert totals.eu_consumed == pytest.approx(0.0)
    assert totals.eu_total == pytest.approx(0.0)


def test_portfolio_totals_fractional_eu_accumulates() -> None:
    """Fractional EU (the calibrated bucket values) accumulates without drift."""
    rows = [
        _row("ABC", phase_done=1, phase_total=1, eu_consumed=0.25, eu_total=0.25),
        _row("DEF", phase_done=1, phase_total=1, eu_consumed=0.5, eu_total=0.5),
        _row("GHI", phase_done=1, phase_total=1, eu_consumed=3.5, eu_total=3.5),
    ]
    totals = portfolio_totals(rows)
    assert totals.eu_consumed == pytest.approx(4.25)
    assert totals.eu_total == pytest.approx(4.25)
