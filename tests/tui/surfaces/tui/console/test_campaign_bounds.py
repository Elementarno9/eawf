"""The Campaign route's BOUNDS row: each budget axis as spend against its limit.

An axis reads like every other budget line: what was spent, the limit and an estimated
remainder. A spend no reading observed reads ``∅ unmetered``, never zero, and a spend
some reading missed reads as a floor with no remainder, the way the cost ceiling does.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.state.epoch2.campaign import BudgetAxis, ResearchBudget
from eawf.surfaces.tui.console.renderers.budget_lines import axis_line
from eawf.surfaces.tui.console.renderers.campaign import bounds_rows


def _axis(**fields: Any) -> BudgetAxis:
    return BudgetAxis.model_validate({"axis_kind": "tokens", "unit": "tokens", **fields})


def test_an_axis_reads_spent_of_its_limit_with_an_estimated_remainder() -> None:
    line = axis_line(_axis(limit=50_000, spent=12_000))
    assert line == "12,000 of 50,000 tokens · ≈38,000 left"


def test_an_axis_whose_unit_differs_from_its_kind_names_the_kind() -> None:
    line = axis_line(_axis(axis_kind="wall_time", unit="h", limit=6, spent=1))
    assert line == "wall time 1 of 6 h · ≈5 left"


@pytest.mark.parametrize(
    ("spent", "left"),
    [(0, "≈1 left"), (1, "≈0 left"), (2, "≈0 left")],
    ids=["empty", "at-limit", "past-limit"],
)
def test_the_remainder_never_reads_below_zero(spent: int, left: str) -> None:
    line = axis_line(_axis(axis_kind="rounds", unit="rounds", limit=1, spent=spent))
    assert line == f"{spent} of 1 rounds · {left}"


def test_a_spend_no_reading_observed_reads_unmetered_never_zero() -> None:
    line = axis_line(_axis(limit=100, spent=0, spent_quality="unavailable"))
    assert line == "∅ unmetered of 100 tokens"


def test_a_spend_some_reading_missed_reads_as_a_floor_with_no_remainder() -> None:
    line = axis_line(_axis(limit=100, spent=40, spent_quality="unavailable"))
    assert line == "≥40 of 100 tokens · partly unmetered"


def test_a_derived_spend_carries_the_approximate_marker() -> None:
    assert axis_line(_axis(limit=100, spent=40, spent_quality="estimated")).startswith("~40 of")


def test_a_soft_axis_says_so() -> None:
    assert axis_line(_axis(limit=100, spent=40, hard=False)).endswith(" · soft")


def test_every_axis_of_the_budget_is_one_row_under_the_label() -> None:
    budget = ResearchBudget.model_validate(
        {
            "axes": [
                {"axis_kind": "rounds", "limit": 7, "spent": 3, "unit": "rounds"},
                {"axis_kind": "tokens", "limit": 900, "unit": "tokens"},
            ]
        }
    )
    first, second = bounds_rows(budget)
    assert first.split() == ["BOUNDS", "3", "of", "7", "rounds", "·", "≈4", "left"]
    assert second.strip() == "0 of 900 tokens · ≈900 left"
    assert not second.lstrip().startswith("BOUNDS")


def test_a_malformed_axis_is_refused_where_it_enters() -> None:
    with pytest.raises(ValidationError):
        _axis(limit=0)
    with pytest.raises(ValidationError):
        _axis(limit=1, spent=-1)
    with pytest.raises(ValidationError):
        _axis(limit=1, spent_quality="partial")
