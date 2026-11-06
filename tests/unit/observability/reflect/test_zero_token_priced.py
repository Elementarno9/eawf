"""A Run that reported zero tokens and no cost spent nothing a total could miss.

Unpriced means reported tokens without a cost: a Run whose every reported counter is
zero raises no ``unpriced_spend`` issue and is not counted unpriced, while a Run with a
token and no cost still is.
"""

from __future__ import annotations

import pytest

from eawf.kernel.state.epoch2.run import Run
from eawf.observability.reflect.issues import IssueKind, highlight_issues
from eawf.observability.reflect.runs import RunReading
from eawf.observability.reflect.totals import RunSpend, scope_totals
from tests.contract.surfaces.cli._reflect_tree import run_row


def _reading(key: str) -> RunReading:
    return RunReading(run=Run.model_validate(run_row(key)), events=(), canonical_sequence=1)


def _unpriced_issue_keys(readings: list[RunReading], spends: list[RunSpend]) -> list[str]:
    return [
        str(issue.evidence_rows[0])
        for issue in highlight_issues(readings, spends)
        if issue.kind is IssueKind.UNPRICED_SPEND
    ]


_ZERO_SPENDS = [
    RunSpend(input_tokens=0, output_tokens=0, cache_tokens=0),
    RunSpend(input_tokens=0),
    RunSpend(output_tokens=0, cache_tokens=None),
]


@pytest.mark.parametrize("spend", _ZERO_SPENDS)
def test_a_zero_token_run_without_cost_raises_no_unpriced_issue(spend: RunSpend) -> None:
    assert _unpriced_issue_keys([_reading("RUN-00000001")], [spend]) == []


@pytest.mark.parametrize("spend", _ZERO_SPENDS)
def test_a_zero_token_run_without_cost_is_not_counted_unpriced(spend: RunSpend) -> None:
    (row,) = scope_totals([_reading("RUN-00000001")], [spend])

    assert (row.priced_runs, row.unpriced_runs) == (0, 0)


@pytest.mark.parametrize(
    "spend",
    [
        RunSpend(input_tokens=1),
        RunSpend(input_tokens=0, output_tokens=0, cache_tokens=1),
        RunSpend(output_tokens=5),
    ],
)
def test_a_run_with_tokens_and_no_cost_is_still_unpriced(spend: RunSpend) -> None:
    """Off by one: a single token is enough to make the missing cost unpriced spend."""
    reading = _reading("RUN-00000001")

    (row,) = scope_totals([reading], [spend])

    assert row.unpriced_runs == 1
    assert _unpriced_issue_keys([reading], [spend]) == ["RUN-00000001"]


def test_zero_token_and_token_bearing_runs_mixed() -> None:
    readings = [_reading("RUN-00000001"), _reading("RUN-00000002"), _reading("RUN-00000003")]
    spends = [
        RunSpend(input_tokens=0, output_tokens=0, cache_tokens=0),
        RunSpend(input_tokens=3, output_tokens=4),
        RunSpend(cost_microusd=0, input_tokens=0),
    ]

    (row,) = scope_totals(readings, spends)

    assert (row.priced_runs, row.unpriced_runs) == (1, 1)
    assert _unpriced_issue_keys(readings, spends) == ["RUN-00000002"]


def test_no_runs_raise_no_issue() -> None:
    """Boundary: an empty listing."""
    assert highlight_issues([], []) == ()


def test_issues_refuse_a_spend_list_of_another_length() -> None:
    """Error path: readings and spends are zipped strictly."""
    with pytest.raises(ValueError, match="zip"):
        highlight_issues([_reading("RUN-00000001")], [])
