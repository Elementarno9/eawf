"""Setting a Campaign's limits: the budget, the step bounds and the stop it draws.

:func:`relimited` is a pure function of the Campaign as it stands, so each case
builds one record and reads what the new limits make of it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.state.epoch2.campaign import (
    Campaign,
    ResearchBudget,
    budget_stop,
    relimited,
)

NOW: Final = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
SLOT: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
QUESTION: Final = "eawf://WSP-MAIN/PRJ-EAWF/_/question/QST-0001"


def _rounds(limit: int, spent: int = 0) -> dict[str, Any]:
    return {"axes": [{"axis_kind": "rounds", "limit": limit, "spent": spent, "unit": "rounds"}]}


def _campaign(limit: int, spent: int = 0, *, started: int = 0) -> Campaign:
    steps = [
        {
            "ordinal": n,
            "title": f"Survey source {n}",
            "method": "survey",
            "question_ref": QUESTION,
            "bound": _rounds(1),
            **(
                {
                    "state": "running",
                    "run_refs": [f"{SLOT}/run/RUN-0000001{n}"],
                    "started_at": NOW.isoformat(),
                }
                if n <= started
                else {}
            ),
        }
        for n in (1, 2)
    ]
    return Campaign.model_validate(
        {
            "uid": "00000000-0000-4000-8000-000000000001",
            "key": "CAM-0001",
            "origin": {"kind": "native", "mapping_basis": "native", "confidence": "exact"},
            "urn": f"{SLOT}/campaign/CAM-0001",
            "track_ref": f"{SLOT}/track/TRK-RUNTIME",
            "title": "Establish whether replay preserves event order",
            "evidence_budget": _rounds(limit, spent),
            "approved_plan_digest": "sha256:" + "0" * 64,
            "plan_steps": steps,
            "revision": 1,
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
        }
    )


def test_no_limit_leaves_the_campaign_as_it_stands() -> None:
    campaign = _campaign(2)
    assert relimited(campaign, {}, now=NOW) == (
        campaign.evidence_budget,
        campaign.plan_steps,
        None,
    )


def test_a_new_axis_reaches_only_the_pending_steps() -> None:
    budget, steps, _stop = relimited(_campaign(2, started=1), {"tokens": 500}, now=NOW)
    axis = budget.axis("tokens")
    assert axis is not None and (axis.limit, axis.spent, axis.unit) == (500, 0, "tokens")
    assert [[a.axis_kind for a in s.bound.axes] for s in steps] == [
        ["rounds"],
        ["rounds", "tokens"],
    ]


@pytest.mark.parametrize(
    ("limit", "stopped"),
    [(1, True), (2, False), (3, False)],
    ids=["at-spend", "one-past", "two-past"],
)
def test_the_stop_follows_whether_a_hard_axis_reached_its_new_limit(
    limit: int, stopped: bool
) -> None:
    _budget, _steps, stop = relimited(_campaign(2, spent=1), {"rounds": limit}, now=NOW)
    assert (stop is not None) is stopped


def test_a_stop_the_new_limits_still_call_for_keeps_when_it_was_recorded() -> None:
    first = budget_stop(ResearchBudget.model_validate(_rounds(1, 1)), waiting=True, now=NOW)
    assert first is not None
    earlier = _campaign(1, spent=1).model_copy(update={"stop": first})
    later = datetime(2026, 10, 3, tzinfo=UTC)
    _budget, _steps, stop = relimited(earlier, {"rounds": 1}, now=later)
    assert stop == first


def test_nothing_waiting_calls_for_no_stop() -> None:
    exhausted = ResearchBudget.model_validate(_rounds(1, 1))
    assert budget_stop(exhausted, waiting=False, now=NOW) is None
    assert budget_stop(ResearchBudget.model_validate(_rounds(2, 1)), waiting=True, now=NOW) is None


def test_a_limit_the_record_cannot_hold_is_refused() -> None:
    with pytest.raises(ValidationError):
        relimited(_campaign(2), {"rounds": 0}, now=NOW)
