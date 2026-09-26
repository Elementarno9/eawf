"""Tests for the Campaign evidence budget and its status transition matrix.

Pins two contracts on :mod:`eawf.kernel.store.kinds.research_campaign`:

* :class:`ResearchBudget` types one evidence-budget axis as a ``limit`` /
  ``spent`` pair in a shared unit with a derived, never-stored
  ``remainder``. A lone ``limit`` or a lone ``spent`` fails validation
  rather than rendering half a ratio.
* :data:`CAMPAIGN_TRANSITIONS` is a complete Campaign status matrix (every
  :class:`CampaignStatus` carries an explicit, possibly empty, target set)
  and :func:`validate_campaign_transition` refuses any edge outside it.
  :func:`complete_campaign` drives the matrix's convergence edge. That a
  converging campaign leaves a scope Milestone alone is proven on the live
  ``run_campaign`` path in the daemon's budget tests.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.research_campaign import ResearchProfileBlock, StagedCampaign
from eawf.kernel.state.enums import CampaignStatus
from eawf.kernel.store.kinds.research_campaign import (
    CAMPAIGN_TRANSITIONS,
    CampaignTombstone,
    IllegalCampaignTransitionError,
    ResearchBudget,
    ResearchCampaignPayload,
    charge_evidence_budget,
    complete_campaign,
    exhausted_budget_axis,
    open_evidence_budget,
    validate_campaign_transition,
)

pytestmark = pytest.mark.unit


def _campaign(
    status: CampaignStatus = CampaignStatus.ACTIVE, **overrides: Any
) -> ResearchCampaignPayload:
    """Build a minimal valid :class:`ResearchCampaignPayload`."""
    fields: dict[str, Any] = {
        "campaign_id": "RC-001",
        "config": ResearchProfileBlock(),
        "campaign": StagedCampaign(topic="market structure"),
        "status": status,
    }
    fields.update(overrides)
    return ResearchCampaignPayload(**fields)


# ---------------------------------------------------------------------------
# ResearchBudget -- typed limit/spent axis
# ---------------------------------------------------------------------------


def test_research_budget_unset_axis_has_no_remainder() -> None:
    """Boundary: an axis with neither side set is valid and has no remainder."""
    axis = ResearchBudget(unit="rounds")
    assert axis.limit is None
    assert axis.spent is None
    assert axis.remainder is None


def test_research_budget_zero_pair_derives_zero_remainder() -> None:
    """Boundary: a paired zero limit/spend is valid with a zero remainder."""
    axis = ResearchBudget(unit="rounds", limit=0, spent=0)
    assert axis.remainder == 0


def test_research_budget_derives_remainder_from_limit_and_spent() -> None:
    axis = ResearchBudget(unit="usd", limit=10.0, spent=4.0)
    assert axis.remainder == pytest.approx(6.0)


def test_research_budget_spent_past_limit_derives_negative_remainder() -> None:
    """An over-budget axis is representable -- the remainder just goes negative."""
    axis = ResearchBudget(unit="usd", limit=10.0, spent=12.0)
    assert axis.remainder == pytest.approx(-2.0)


def test_research_budget_rejects_a_limit_with_no_spent_counterpart() -> None:
    with pytest.raises(ValidationError, match="has a limit with no counterpart"):
        ResearchBudget(unit="rounds", limit=5.0)


def test_research_budget_rejects_a_spent_with_no_limit_counterpart() -> None:
    with pytest.raises(ValidationError, match="has a spent with no counterpart"):
        ResearchBudget(unit="rounds", spent=5.0)


def test_research_budget_rejects_a_negative_limit() -> None:
    with pytest.raises(ValidationError):
        ResearchBudget(unit="rounds", limit=-1.0, spent=0.0)


def test_research_budget_rejects_a_non_numeric_limit() -> None:
    with pytest.raises(ValidationError):
        ResearchBudget(unit="rounds", limit="a lot", spent=0.0)  # type: ignore[arg-type]


def test_research_budget_rejects_an_empty_unit() -> None:
    with pytest.raises(ValidationError):
        ResearchBudget(unit="", limit=1.0, spent=0.0)


def test_research_budget_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        ResearchBudget.model_validate({"unit": "rounds", "limit": 1.0, "spent": 0.0, "cap": 1.0})


def test_campaign_payload_evidence_budget_defaults_empty() -> None:
    """A campaign staged before any axis is configured carries an empty budget."""
    campaign = _campaign()
    assert campaign.evidence_budget == {}


def test_campaign_payload_carries_typed_evidence_budget_axes() -> None:
    campaign = _campaign(
        evidence_budget={"rounds": ResearchBudget(unit="rounds", limit=5, spent=2)}
    )
    axis = campaign.evidence_budget["rounds"]
    assert axis.remainder == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# open / charge / exhausted -- the budget a run spends
# ---------------------------------------------------------------------------


def test_open_evidence_budget_empty_limits_is_unbudgeted() -> None:
    assert open_evidence_budget({}) == {}


def test_open_evidence_budget_sets_unit_and_zero_spend() -> None:
    budget = open_evidence_budget({"usd": 4.0})
    assert budget == {"usd": ResearchBudget(unit="usd", limit=4.0, spent=0.0)}


def test_open_evidence_budget_rejects_unknown_axis() -> None:
    with pytest.raises(ValueError, match="unknown evidence budget axis"):
        open_evidence_budget({"tokens": 1.0})


def test_open_evidence_budget_rejects_negative_limit() -> None:
    with pytest.raises(ValidationError):
        open_evidence_budget({"rounds": -1.0})


def test_charge_evidence_budget_adds_spend_to_configured_axes_only() -> None:
    budget = open_evidence_budget({"rounds": 3.0})
    charged = charge_evidence_budget(budget, {"rounds": 1.0, "usd": 9.0})
    assert charged == {"rounds": ResearchBudget(unit="rounds", limit=3.0, spent=1.0)}
    assert budget["rounds"].spent == 0.0


def test_exhausted_budget_axis_allows_a_round_that_lands_on_the_limit() -> None:
    budget = {"rounds": ResearchBudget(unit="rounds", limit=2.0, spent=1.0)}
    assert exhausted_budget_axis(budget, {"rounds": 1.0}) is None


def test_exhausted_budget_axis_names_the_axis_one_past_the_limit() -> None:
    budget = {"rounds": ResearchBudget(unit="rounds", limit=2.0, spent=2.0)}
    assert exhausted_budget_axis(budget, {"rounds": 1.0}) == "rounds"


def test_exhausted_budget_axis_no_headroom_exhausts_even_at_zero_projection() -> None:
    budget = {"usd": ResearchBudget(unit="usd", limit=1.0, spent=1.0)}
    assert exhausted_budget_axis(budget, {"usd": 0.0}) == "usd"


def test_exhausted_budget_axis_ignores_an_unconfigured_axis() -> None:
    assert exhausted_budget_axis({"usd": ResearchBudget(unit="usd")}, {"usd": 5.0}) is None


# ---------------------------------------------------------------------------
# CAMPAIGN_TRANSITIONS -- the complete Campaign status matrix
# ---------------------------------------------------------------------------


def test_campaign_transitions_matrix_is_complete_over_every_status() -> None:
    """Every CampaignStatus carries an explicit (possibly empty) entry."""
    assert set(CAMPAIGN_TRANSITIONS) == set(CampaignStatus)


def test_campaign_transitions_terminal_statuses_allow_no_edge() -> None:
    assert CAMPAIGN_TRANSITIONS[CampaignStatus.CONVERGED] == frozenset()
    assert CAMPAIGN_TRANSITIONS[CampaignStatus.CANCELLED] == frozenset()


def test_validate_campaign_transition_allows_active_to_converged() -> None:
    validate_campaign_transition(CampaignStatus.ACTIVE, CampaignStatus.CONVERGED)


def test_validate_campaign_transition_allows_active_to_cancelled() -> None:
    validate_campaign_transition(CampaignStatus.ACTIVE, CampaignStatus.CANCELLED)


def test_validate_campaign_transition_refuses_an_illegal_edge() -> None:
    """Gate-fire proof: an edge outside the matrix raises the typed refusal."""
    with pytest.raises(IllegalCampaignTransitionError, match="illegal campaign transition"):
        validate_campaign_transition(CampaignStatus.CONVERGED, CampaignStatus.ACTIVE)


def test_validate_campaign_transition_refuses_a_terminal_self_loop() -> None:
    with pytest.raises(IllegalCampaignTransitionError):
        validate_campaign_transition(CampaignStatus.CANCELLED, CampaignStatus.CANCELLED)


def test_validate_campaign_transition_refuses_cancelled_to_converged() -> None:
    with pytest.raises(IllegalCampaignTransitionError):
        validate_campaign_transition(CampaignStatus.CANCELLED, CampaignStatus.CONVERGED)


# ---------------------------------------------------------------------------
# complete_campaign -- the convergence edge, kept apart from Milestone
# ---------------------------------------------------------------------------


def test_complete_campaign_converges_an_active_campaign() -> None:
    completed = complete_campaign(_campaign(status=CampaignStatus.ACTIVE))
    assert completed.status is CampaignStatus.CONVERGED
    assert completed.campaign_id == "RC-001"


def test_complete_campaign_refuses_an_already_converged_campaign() -> None:
    with pytest.raises(IllegalCampaignTransitionError):
        complete_campaign(_campaign(status=CampaignStatus.CONVERGED))


def test_complete_campaign_refuses_a_cancelled_campaign() -> None:
    tombstone = CampaignTombstone(cancelled_at="2026-06-07T00:00:00+00:00")
    cancelled = _campaign(status=CampaignStatus.CANCELLED, tombstone=tombstone)
    with pytest.raises(IllegalCampaignTransitionError):
        complete_campaign(cancelled)
