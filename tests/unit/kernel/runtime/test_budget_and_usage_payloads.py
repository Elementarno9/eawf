"""The typed budget and usage payloads, and the one way usage adds up."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.runtime.events import (
    EVENT_CONTRACTS,
    SUPPORTED_EVENT_KINDS,
    EventPayloadKind,
    RunEventKind,
    RunEventRecord,
)
from eawf.kernel.runtime.usage import (
    AXIS_UNITS,
    BudgetPayload,
    UsagePayload,
    aggregate_usage,
)
from tests import _provider_helpers as fx

AT: Final = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def digest(char: str) -> str:
    """Return a well-formed digest whose body is one repeated character."""
    return f"sha256:{char * 64}"


def budget(**overrides: Any) -> BudgetPayload:
    """Return an exhausted hard-limit token crossing with *overrides* applied."""
    fields: dict[str, Any] = {
        "phase": "exhausted",
        "axis": "input_tokens",
        "basis": "hard_limit",
        "band": "limit_reached",
        "contract_digest": digest("a"),
        "policy_digest": digest("b"),
        "policy_revision": 1,
        "ceiling_value": 1_000,
        "observed_value": 1_000,
        "unit": "tokens",
        "fraction": 1.0,
        "measurement_quality": "measured",
        "notice_key": digest("c"),
    }
    fields.update(overrides)
    return BudgetPayload.model_validate(fields)


def usage(cumulative: bool, **counters: Any) -> UsagePayload:
    """Return a measured provider-transcript reading."""
    return UsagePayload(
        usage_source="provider_transcript",
        is_cumulative=cumulative,
        measurement_quality="measured",
        **counters,
    )


def event(kind: RunEventKind, payload: Any) -> RunEventRecord:
    """Return one event line carrying *payload*."""
    return RunEventRecord.model_validate(
        {
            "event_ref": "EVT-0000000a",
            "run_ref": str(fx.RUN_URN),
            "run_sequence": 1,
            "event_kind": kind,
            "provenance": "daemon_observed",
            "payload": payload,
            "actor": "OP-0001",
            "recorded_at": AT,
        }
    )


# ---- RUN-024: budget events bind to the typed budget payload ----------------


def test_run_024_both_budget_kinds_bind_the_budget_payload_at_their_phase() -> None:
    assert EVENT_CONTRACTS[RunEventKind.BUDGET_WARNING].payload_kind is EventPayloadKind.BUDGET
    assert EVENT_CONTRACTS[RunEventKind.BUDGET_WARNING].phase == "warning"
    assert EVENT_CONTRACTS[RunEventKind.BUDGET_EXHAUSTED].phase == "exhausted"
    assert {RunEventKind.BUDGET_WARNING, RunEventKind.BUDGET_EXHAUSTED} <= SUPPORTED_EVENT_KINDS


def test_run_024_an_exhausted_event_carries_every_named_field() -> None:
    line = event(RunEventKind.BUDGET_EXHAUSTED, budget().model_dump(mode="json"))
    assert isinstance(line.payload, BudgetPayload)
    assert set(BudgetPayload.model_fields) >= {
        "axis",
        "basis",
        "band",
        "contract_digest",
        "policy_digest",
        "policy_revision",
        "ceiling_value",
        "observed_value",
        "unit",
        "fraction",
        "measurement_quality",
        "last_progress_at",
        "last_heartbeat_at",
        "notice_key",
    }


def test_run_024_an_exhausted_event_on_an_estimate_basis_fails_validation() -> None:
    with pytest.raises(ValidationError, match="an estimate cannot be exhausted"):
        budget(basis="estimate")


def test_run_024_a_warning_on_an_estimate_basis_is_admitted() -> None:
    warning = budget(
        phase="warning", basis="estimate", band="approaching", observed_value=900, fraction=0.9
    )
    assert warning.basis == "estimate"


def test_run_024_an_unavailable_reading_cannot_cross_a_threshold() -> None:
    with pytest.raises(ValidationError, match="unavailable quality cannot cross"):
        budget(measurement_quality="unavailable")


def test_run_024_the_budget_payload_under_the_usage_kind_is_refused() -> None:
    with pytest.raises(ValidationError, match="carries payload 'usage'"):
        event(RunEventKind.USAGE_OBSERVED, budget().model_dump(mode="json"))


def test_run_024_a_warning_payload_under_the_exhausted_kind_is_refused() -> None:
    warning = budget(phase="warning", band="approaching", observed_value=900, fraction=0.9)
    with pytest.raises(ValidationError, match="is the 'exhausted' phase"):
        event(RunEventKind.BUDGET_EXHAUSTED, warning.model_dump(mode="json"))


def test_run_024_a_unit_foreign_to_the_axis_fails_validation() -> None:
    with pytest.raises(ValidationError, match="counted in tokens"):
        budget(unit="bytes")
    assert set(AXIS_UNITS) == {
        "wall_seconds",
        "input_tokens",
        "output_bytes",
        "tool_calls",
        "child_runs",
        "cost_microusd",
    }


def test_run_024_a_fraction_not_derived_from_the_values_fails_validation() -> None:
    with pytest.raises(ValidationError, match="is not observed/ceiling"):
        budget(fraction=0.5)


def test_run_024_an_exhausted_event_under_its_ceiling_fails_validation() -> None:
    with pytest.raises(ValidationError, match="at or over its ceiling"):
        budget(observed_value=999, fraction=0.999)


def test_run_024_a_zero_ceiling_fails_validation() -> None:
    with pytest.raises(ValidationError):
        budget(ceiling_value=0)


# ---- RUN-050: every usage payload declares its source and its mode ----------


@pytest.mark.parametrize("missing", ["usage_source", "is_cumulative"])
def test_run_050_a_usage_payload_without_source_or_mode_fails_validation(missing: str) -> None:
    fields: dict[str, Any] = {
        "input_tokens": 1,
        "usage_source": "counter_sidecar",
        "is_cumulative": False,
        "measurement_quality": "measured",
    }
    del fields[missing]
    with pytest.raises(ValidationError, match=missing):
        UsagePayload.model_validate(fields)


def test_run_050_is_cumulative_is_strict_and_never_coerced() -> None:
    with pytest.raises(ValidationError):
        UsagePayload.model_validate(
            {
                "input_tokens": 1,
                "usage_source": "counter_sidecar",
                "is_cumulative": "yes",
                "measurement_quality": "measured",
            }
        )


def test_run_050_the_usage_kind_carries_the_usage_payload() -> None:
    line = event(RunEventKind.USAGE_OBSERVED, usage(True, input_tokens=5).model_dump(mode="json"))
    assert isinstance(line.payload, UsagePayload)
    assert line.payload.is_cumulative is True


def test_run_050_running_totals_contribute_their_maximum_and_deltas_their_sum() -> None:
    totals = aggregate_usage(
        [
            usage(True, input_tokens=300, output_tokens=10),
            usage(True, input_tokens=600, output_tokens=20),
            usage(False, input_tokens=100),
            usage(False, input_tokens=50, cost_microusd=7, price_source="billed"),
        ]
    )
    assert totals.input_tokens == 600 + 150
    assert totals.output_tokens == 20
    assert totals.cost_microusd == 7
    assert totals.tokens == 770


def test_run_050_summing_running_totals_is_not_what_the_fold_does() -> None:
    readings = [usage(True, input_tokens=value) for value in (100, 200, 300)]
    assert aggregate_usage(readings).input_tokens == 300
    assert aggregate_usage(readings).input_tokens != sum(r.input_tokens or 0 for r in readings)


def test_run_050_no_readings_leave_every_counter_absent_not_zero() -> None:
    totals = aggregate_usage([])
    assert totals.input_tokens is None
    assert totals.tokens is None


def test_run_050_one_delta_reading_is_its_own_total() -> None:
    assert aggregate_usage([usage(False, cache_tokens=4)]).tokens == 4


def test_run_050_an_unavailable_reading_carries_no_counters() -> None:
    with pytest.raises(ValidationError, match="carries no counters"):
        UsagePayload(
            usage_source="eawf_derived",
            is_cumulative=False,
            measurement_quality="unavailable",
            input_tokens=0,
        )
    empty = UsagePayload(
        usage_source="eawf_derived", is_cumulative=False, measurement_quality="unavailable"
    )
    assert aggregate_usage([empty]).tokens is None


def test_run_050_a_measured_reading_with_no_counter_fails_validation() -> None:
    with pytest.raises(ValidationError, match="at least one counter"):
        usage(False)


def test_run_050_coverage_belongs_to_an_estimated_reading_alone() -> None:
    with pytest.raises(ValidationError, match="coverage_fraction"):
        UsagePayload(
            usage_source="eawf_derived",
            is_cumulative=False,
            measurement_quality="estimated",
            input_tokens=1,
        )
    estimated = UsagePayload(
        usage_source="eawf_derived",
        is_cumulative=False,
        measurement_quality="estimated",
        input_tokens=1,
        coverage_fraction=0.5,
    )
    assert estimated.coverage_fraction == pytest.approx(0.5)


# ---- MEAS-077: no enforcement consumer accepts a list-reconstructed price ---


def test_meas_077_a_cost_without_its_price_source_fails_validation() -> None:
    with pytest.raises(ValidationError, match="price_source"):
        usage(False, cost_microusd=5)
    with pytest.raises(ValidationError, match="price_source"):
        usage(False, input_tokens=5, price_source="billed")


def test_meas_077_a_reconstructed_price_offered_to_a_cap_is_refused() -> None:
    """The enforcement fold drops a list price; the display fold still shows it."""
    readings = [
        usage(True, input_tokens=100, cost_microusd=900, price_source="list-reconstructed"),
        usage(False, input_tokens=10, cost_microusd=40, price_source="billed"),
    ]

    enforced = aggregate_usage(readings, for_enforcement=True)
    shown = aggregate_usage(readings)

    assert enforced.cost_microusd == 40
    assert enforced.input_tokens == shown.input_tokens == 110
    assert shown.cost_microusd == 940


def test_meas_077_only_reconstructed_prices_leave_the_enforced_cost_unknown() -> None:
    """Boundary: nothing billed is an unknown cost to a cap, never a zero one."""
    readings = [usage(False, output_tokens=3, cost_microusd=12, price_source="list-reconstructed")]

    assert aggregate_usage(readings, for_enforcement=True).cost_microusd is None
    assert aggregate_usage([], for_enforcement=True).cost_microusd is None


def test_meas_077_a_price_source_outside_the_closed_set_fails_validation() -> None:
    with pytest.raises(ValidationError, match="price_source"):
        usage(False, cost_microusd=5, price_source="unpriced")
