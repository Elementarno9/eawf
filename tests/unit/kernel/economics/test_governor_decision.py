"""The in-flight governor's decision, as a pure function of what is in flight."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.economics.governor import (
    DEFAULT_GOVERNOR,
    AdmissionAxis,
    AdmissionDecision,
    AdmissionReceipt,
    AxisStatus,
    EconomicsPolicy,
    InFlightGovernor,
    RunReservation,
    decide_admission,
    economics_policy_from,
)
from eawf.kernel.economics.prompt_budget import (
    DEFAULT_PROMPT_BUDGET,
    BudgetClassId,
    RenderedSize,
    evaluate_prompt_budget,
)
from tests import _provider_helpers as fx

AT: Final = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
FITS: Final = evaluate_prompt_budget(DEFAULT_PROMPT_BUDGET, [])


def run(key: int, **fields: Any) -> RunReservation:
    """Return a reservation of the Run numbered *key*."""
    urn = str(fx.RUN_URN).replace("RUN-00000010", f"RUN-{key:08d}")
    return RunReservation.model_validate({"run_ref": urn, **fields})


def governor(**fields: Any) -> InFlightGovernor:
    """Return a governor with the shipped defaults and *fields* applied."""
    return InFlightGovernor.model_validate({**DEFAULT_GOVERNOR.model_dump(mode="json"), **fields})


def decide(
    ceilings: InFlightGovernor, request: RunReservation, *in_flight: RunReservation
) -> AdmissionReceipt:
    """Decide *request* beside *in_flight* under a fitting prompt budget."""
    return decide_admission(
        ceilings, request=request, in_flight=in_flight, prompt_budget=FITS, decided_at=AT
    )


def axis(receipt: AdmissionReceipt, which: AdmissionAxis) -> AxisStatus:
    """Return the status *receipt* recorded on *which*."""
    return next(row.status for row in receipt.axes if row.axis is which)


# ---- ECON-010: a ceiling reached queues or denies ---------------------------


def test_econ_010_a_run_that_fits_every_ceiling_is_admitted() -> None:
    receipt = decide(governor(), run(1, tokens=1_000))
    assert receipt.decision is AdmissionDecision.ADMITTED
    assert receipt.reservation.tokens == 1_000


def test_econ_010_a_governor_at_exactly_its_run_ceiling_admits_the_last_slot() -> None:
    in_flight = [run(key, tokens=1) for key in range(2, 9)]
    assert decide(governor(), run(1, tokens=1), *in_flight).decision is AdmissionDecision.ADMITTED


def test_econ_010_one_run_past_the_run_ceiling_is_queued() -> None:
    in_flight = [run(key, tokens=1) for key in range(2, 10)]
    receipt = decide(governor(), run(1, tokens=1), *in_flight)
    assert receipt.decision is AdmissionDecision.QUEUED
    assert axis(receipt, AdmissionAxis.CONCURRENCY) is AxisStatus.BREACHED


def test_econ_010_a_deny_governor_denies_rather_than_queues() -> None:
    receipt = decide(governor(admission="deny"), run(1, tokens=4_000_001))
    assert receipt.decision is AdmissionDecision.DENIED
    assert "exceeds the ceiling of 4000000" in receipt.reason


def test_econ_010_tokens_exactly_at_the_ceiling_are_admitted_and_one_over_is_not() -> None:
    at = decide(governor(), run(1, tokens=1_000_000), run(2, tokens=3_000_000))
    over = decide(governor(), run(1, tokens=1_000_001), run(2, tokens=3_000_000))
    assert at.decision is AdmissionDecision.ADMITTED
    assert over.decision is AdmissionDecision.QUEUED


def test_econ_010_an_exhausted_prompt_budget_denies_whatever_the_governor_says() -> None:
    exhausted = evaluate_prompt_budget(
        DEFAULT_PROMPT_BUDGET,
        [RenderedSize(class_id=BudgetClassId.STEERING_ZONE1, bytes=10**6)],
    )
    receipt = decide_admission(
        governor(),
        request=run(1, tokens=1),
        in_flight=(),
        prompt_budget=exhausted,
        decided_at=AT,
    )
    assert receipt.decision is AdmissionDecision.DENIED
    assert "prompt budget exhausted on steering_zone1" in receipt.reason


# ---- ECON-011: a null ceiling is unavailable, not permission ----------------


def test_econ_011_a_null_cost_ceiling_is_recorded_unavailable() -> None:
    receipt = decide(governor(), run(1, tokens=1, cost_microusd=10**12))
    assert axis(receipt, AdmissionAxis.COST) is AxisStatus.UNAVAILABLE
    assert receipt.decision is AdmissionDecision.ADMITTED


def test_econ_011_a_governor_whose_every_spend_ceiling_is_null_fails_validation() -> None:
    with pytest.raises(ValidationError, match="a count ceiling alone binds no spend"):
        governor(max_in_flight_tokens=None)


def test_econ_011_a_priced_ceiling_refuses_a_run_that_carries_no_price_cap() -> None:
    receipt = decide(governor(max_in_flight_cost_microusd=1_000), run(1, tokens=1))
    assert axis(receipt, AdmissionAxis.COST) is AxisStatus.UNBOUNDED
    assert receipt.decision is AdmissionDecision.QUEUED


# ---- ECON-013: the governor only decides ------------------------------------


def test_econ_013_the_decision_is_a_receipt_and_names_why() -> None:
    receipt = decide(governor(), run(1))
    assert receipt.payload_kind == "admission_receipt"
    assert receipt.decision is AdmissionDecision.QUEUED
    assert receipt.reason.startswith("queued: tokens unbounded")
    assert {row.axis for row in receipt.axes} == set(AdmissionAxis)


def test_econ_013_a_receipt_disagreeing_with_its_axes_fails_validation() -> None:
    receipt = decide(governor(), run(1))
    with pytest.raises(ValidationError, match="disagrees with its axes"):
        AdmissionReceipt.model_validate({**receipt.model_dump(mode="json"), "decision": "admitted"})


def test_econ_013_a_receipt_reserving_for_another_run_fails_validation() -> None:
    receipt = decide(governor(), run(1, tokens=1))
    with pytest.raises(ValidationError, match="names another Run"):
        AdmissionReceipt.model_validate(
            {**receipt.model_dump(mode="json"), "reservation": run(2, tokens=1).model_dump()}
        )


# ---- ECON-014: spend caps bind spend, not merely claims ---------------------


def test_econ_014_a_run_with_no_token_cap_cannot_be_admitted() -> None:
    receipt = decide(governor(), run(1))
    assert axis(receipt, AdmissionAxis.TOKENS) is AxisStatus.UNBOUNDED
    assert receipt.decision is AdmissionDecision.QUEUED


def test_econ_014_accrued_spend_past_a_reservation_counts_against_the_ceiling() -> None:
    overran = run(2, tokens=1_000, accrued_tokens=3_999_500)
    receipt = decide(governor(), run(1, tokens=1_000), overran)
    tokens = next(row for row in receipt.axes if row.axis is AdmissionAxis.TOKENS)
    assert tokens.in_flight == 3_999_500
    assert receipt.decision is AdmissionDecision.QUEUED


def test_econ_014_an_unbounded_run_in_flight_blocks_the_spend_axis() -> None:
    receipt = decide(governor(), run(1, tokens=1), run(2))
    assert axis(receipt, AdmissionAxis.TOKENS) is AxisStatus.UNBOUNDED


def test_econ_014_the_shipped_governor_bounds_tokens_on_day_one() -> None:
    assert DEFAULT_GOVERNOR.max_in_flight_tokens == 4_000_000
    assert DEFAULT_GOVERNOR.max_in_flight_cost_microusd is None


# ---- the economics table ----------------------------------------------------


def test_economics_policy_from_an_empty_config_is_the_shipped_default() -> None:
    assert economics_policy_from({}) == EconomicsPolicy()


def test_economics_policy_from_reads_a_declared_governor() -> None:
    merged = {
        "economics": {
            "governor": {"max_concurrent_runs": 2, "max_in_flight_tokens": 5, "admission": "deny"}
        }
    }
    assert economics_policy_from(merged).governor.max_concurrent_runs == 2


def test_economics_policy_from_refuses_an_undeclared_key() -> None:
    with pytest.raises(ValidationError):
        economics_policy_from({"economics": {"governer": {}}})


def test_economics_policy_from_refuses_a_zero_run_ceiling() -> None:
    with pytest.raises(ValidationError):
        economics_policy_from(
            {
                "economics": {
                    "governor": {
                        "max_concurrent_runs": 0,
                        "max_in_flight_tokens": 1,
                        "admission": "queue",
                    }
                }
            }
        )
