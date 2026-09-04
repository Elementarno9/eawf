"""Cost-class attribution tests: totality, exclusion and the no-role raise."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from eawf.kernel.state.enums import AgentSessionRole, WaveStatus
from eawf.kernel.state.models import Wave
from eawf.observability.telemetry.cost_class import (
    ROLE_COST_CLASS,
    CostClass,
    classify_cost_class,
)
from eawf.observability.telemetry.turn_cost import (
    CompletedUnitRun,
    PriceSource,
    TurnCostRecord,
    build_turn_cost_record,
)

pytestmark = pytest.mark.unit

_TS = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
_PRICED = PriceSource(kind="pricing_snapshot", pricing_version="2026.05.17")


def _closed_wave(wave_id: str) -> Wave:
    return Wave(
        id=wave_id,
        iter_id="P31-I01",
        title=f"Completed unit {wave_id}",
        status=WaveStatus.CLOSED,
        opened_at=_TS,
    )


def _run(
    run_id: str,
    *,
    role: AgentSessionRole | None,
    cost_usd: str,
) -> CompletedUnitRun:
    return CompletedUnitRun(
        run_id=run_id,
        wave_id="P31-I01-W01",
        role=role,
        runtime="claude",
        model="claude-opus-4-7",
        wall_clock_ms=1_000,
        input_tokens=10,
        cost_usd=Decimal(cost_usd),
        price_source=_PRICED,
    )


def _build(runs: list[CompletedUnitRun]) -> TurnCostRecord:
    return build_turn_cost_record(
        waves=[_closed_wave("P31-I01-W01")],
        runs=runs,
        fixture_id="turn-cost-v1",
        harness_revision="rev-a1b2c3",
        runtime="claude",
        model="claude-opus-4-7",
    )


def test_role_cost_class_is_total_over_agent_session_role() -> None:
    """Every declared role has an attribution row; none defaults silently."""
    assert set(ROLE_COST_CLASS) == set(AgentSessionRole)


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (AgentSessionRole.AUDITOR, CostClass.VERIFICATION),
        (AgentSessionRole.REVIEWER, CostClass.VERIFICATION),
        (AgentSessionRole.POLISHER, CostClass.VERIFICATION),
        (AgentSessionRole.EXECUTOR, CostClass.EXECUTION),
        (AgentSessionRole.PLANNER, CostClass.EXECUTION),
        (AgentSessionRole.RESEARCHER, CostClass.EXECUTION),
        (AgentSessionRole.DOMAIN_SPECIALIST, CostClass.EXECUTION),
        (AgentSessionRole.OPERATOR, CostClass.UNATTRIBUTED),
    ],
)
def test_classify_cost_class_maps_each_role(role: AgentSessionRole, expected: CostClass) -> None:
    """Attribution is derived from the role, never guessed."""
    assert classify_cost_class(role) is expected


def test_classify_cost_class_none_role_raises_value_error() -> None:
    """A run with no role is a measurement gap, not an execution run."""
    with pytest.raises(ValueError, match="carries no agent role"):
        classify_cost_class(None)


def test_classify_cost_class_bare_string_role_raises_type_error() -> None:
    """A free-string role never classifies."""
    with pytest.raises(TypeError, match="must be an AgentSessionRole"):
        classify_cost_class("executor")  # type: ignore[arg-type]


def test_producer_splits_verification_and_execution_cost() -> None:
    """The two sums stay apart and never fold into one another."""
    record = _build(
        [
            _run("run-exec", role=AgentSessionRole.EXECUTOR, cost_usd="0.80"),
            _run("run-audit", role=AgentSessionRole.AUDITOR, cost_usd="0.20"),
            _run("run-review", role=AgentSessionRole.REVIEWER, cost_usd="0.05"),
        ]
    )

    assert record.execution_cost_usd == Decimal("0.80")
    assert record.verification_cost_usd == Decimal("0.25")


def test_producer_excludes_unattributed_runs_from_both_sums_and_counts_them() -> None:
    """An unattributed run leaves both sums untouched but is visible."""
    record = _build(
        [
            _run("run-exec", role=AgentSessionRole.EXECUTOR, cost_usd="0.80"),
            _run("run-op", role=AgentSessionRole.OPERATOR, cost_usd="7.00"),
        ]
    )

    assert record.execution_cost_usd == Decimal("0.80")
    assert record.verification_cost_usd == Decimal("0")
    assert record.unattributed_run_count == 1
    assert record.token_total == 10


def test_producer_run_with_no_role_raises_instead_of_summing_as_execution() -> None:
    """The unlabelled run must not quietly inflate the execution sum."""
    with pytest.raises(ValueError, match="carries no agent role"):
        _build(
            [
                _run("run-exec", role=AgentSessionRole.EXECUTOR, cost_usd="0.80"),
                _run("run-orphan", role=None, cost_usd="9.00"),
            ]
        )


def test_producer_all_unattributed_yields_zero_cost_but_a_real_unit() -> None:
    """A unit made only of unattributed runs still exists, at zero cost."""
    record = _build([_run("run-op", role=AgentSessionRole.OPERATOR, cost_usd="7.00")])

    assert record.unit_count == 1
    assert record.unattributed_run_count == 1
    assert record.p50_cost_usd == Decimal("0")
    assert record.p50_wall_clock_ms == 1_000
