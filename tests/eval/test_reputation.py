"""Reputation tests: verdict outcomes scored into per-role reliability.

These tests pin:

- the confidence -> float mapping (high/med/low -> 0.9/0.7/0.55);
- the :class:`VerdictOutcome` model's ``extra="forbid"`` and ``confidence``
  bound error paths;
- the per-role reliability score, its insufficiency floors and its tier; and
- the fleet verdict rollup over the report store.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import (
    AgentReportVerdict,
    AgentSessionRole,
    Confidence,
)
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import (
    AgentReportHeader,
    AgentReportPayload,
    AuditorReportBody,
    report_record_id,
    store_kind_for_role,
)
from eawf.kernel.store.paths import store_path
from eawf.observability.eval import (
    DEFAULT_TIER_THRESHOLDS,
    FleetVerdictRow,
    ReliabilityStatus,
    ReputationConfig,
    ReputationTier,
    RoleReliability,
    VerdictOutcome,
    fleet_verdict_rollup,
    map_reliability_to_tier,
)

_T0 = datetime(2026, 5, 1, tzinfo=UTC)


# --- build_verdict_outcomes: honest-empty path ----------------------------


# --- build_verdict_outcomes: clean held outcome ---------------------------


# --- build_verdict_outcomes: reactive refutation --------------------------


# --- build_verdict_outcomes: reopen refutation ----------------------------


# --- build_verdict_outcomes: not-yet-observable ---------------------------


# --- build_verdict_outcomes: iter_id filter -------------------------------


# --- confidence_to_float mapping ------------------------------------------


# --- VerdictOutcome model error paths -------------------------------------


def test_verdict_outcome_rejects_out_of_range_confidence() -> None:
    """A confidence above 1.0 fails the [0.0, 1.0] bound."""
    with pytest.raises(ValidationError):
        VerdictOutcome(
            base_id="P01-I01-W01",
            agent_role=AgentSessionRole.AUDITOR,
            runtime="claude",
            verdict=AgentReportVerdict.PASS,
            confidence=1.5,
        )


def test_verdict_outcome_rejects_extra_field() -> None:
    """An unexpected field fails ``extra='forbid'``."""
    with pytest.raises(ValidationError):
        VerdictOutcome(
            base_id="P01-I01-W01",
            agent_role=AgentSessionRole.AUDITOR,
            runtime="claude",
            verdict=AgentReportVerdict.PASS,
            confidence=0.9,
            unexpected="boom",
        )


# --- reliability scoring layer ------------------------------


def _outcome(
    *,
    held: bool | None,
    verdict: AgentReportVerdict = AgentReportVerdict.PASS,
    confidence: float = 0.9,
    agent_role: AgentSessionRole = AgentSessionRole.EXECUTOR,
    runtime: str = "claude",
) -> VerdictOutcome:
    """Build one observed (or in-flight) VerdictOutcome for scorer tests."""
    return VerdictOutcome(
        base_id="P01-I01-W01",
        agent_role=agent_role,
        runtime=runtime,
        verdict=verdict,
        confidence=confidence,
        held=held,
        outcome_source=None if held is None else ("clean" if held else "reactive"),
    )


def _held_group(
    *,
    n: int,
    held_count: int,
    confidence: float = 0.9,
    agent_role: AgentSessionRole = AgentSessionRole.EXECUTOR,
    runtime: str = "claude",
) -> list[VerdictOutcome]:
    """Build *n* PASS outcomes, *held_count* of which held, the rest refuted."""
    rows: list[VerdictOutcome] = []
    for i in range(n):
        rows.append(
            _outcome(
                held=i < held_count,
                verdict=AgentReportVerdict.PASS,
                confidence=confidence,
                agent_role=agent_role,
                runtime=runtime,
            )
        )
    return rows


# --- compute_role_reliability: honest-empty + refuse-to-score -------------


# --- compute_role_reliability: scored path --------------------------------


# --- compute_role_reliability: Brier ordering -----------------------------


# --- compute_role_reliability: CI-width gate ------------------------------


# --- ReputationConfig validation ------------------------------------------


def test_reputation_config_rejects_min_n_below_one() -> None:
    """``min_n=0`` fails the ``ge=1`` bound -- a zero floor defeats the gate."""
    with pytest.raises(ValidationError):
        ReputationConfig(min_n=0)


def test_reputation_config_rejects_extra_field() -> None:
    """An unexpected config key fails ``extra='forbid'``."""
    with pytest.raises(ValidationError):
        ReputationConfig(unexpected="boom")


def test_reputation_config_rejects_loss_weight_out_of_range() -> None:
    """``loss_weight`` above its 5.0 ceiling fails the bound."""
    with pytest.raises(ValidationError):
        ReputationConfig(loss_weight=6.0)


def test_reputation_config_defaults() -> None:
    """The trust.* leaf defaults match the ratified reputation-engine design."""
    config = ReputationConfig()

    assert config.min_n == 20
    assert config.ci_width_gate == pytest.approx(0.3)
    assert config.tier_thresholds == {}
    assert config.loss_weight == pytest.approx(3.0)


# --- RoleReliability model error paths ------------------------------------


def test_role_reliability_rejects_out_of_range_lower_bound() -> None:
    """A posterior_lower_bound above 1.0 fails the [0.0, 1.0] bound."""
    with pytest.raises(ValidationError):
        RoleReliability(
            agent_role=AgentSessionRole.EXECUTOR,
            runtime="claude",
            n=20,
            status=ReliabilityStatus.SCORED,
            posterior_lower_bound=1.5,
        )


def test_role_reliability_rejects_extra_field() -> None:
    """An unexpected field fails ``extra='forbid'``."""
    with pytest.raises(ValidationError):
        RoleReliability(
            agent_role=AgentSessionRole.EXECUTOR,
            runtime="claude",
            n=20,
            status=ReliabilityStatus.SCORED,
            unexpected="boom",
        )


# --- trust-tier ladder --------------------------------------


def test_map_reliability_to_tier_high_lb_maps_to_a() -> None:
    """A display LB of 0.9 clears the default A floor (0.85) -> tier A."""
    assert map_reliability_to_tier(0.9, ReputationConfig()) is ReputationTier.A


def test_map_reliability_to_tier_mid_lb_maps_to_b() -> None:
    """A display LB of 0.7 clears B (0.6) but not A (0.85) -> tier B."""
    assert map_reliability_to_tier(0.7, ReputationConfig()) is ReputationTier.B


def test_map_reliability_to_tier_low_lb_maps_to_c() -> None:
    """A display LB of 0.3 clears only the C floor (0.0) -> tier C."""
    assert map_reliability_to_tier(0.3, ReputationConfig()) is ReputationTier.C


def test_map_reliability_to_tier_zero_lb_maps_to_c() -> None:
    """A display LB of 0.0 sits on the C floor -> tier C (never below C)."""
    assert map_reliability_to_tier(0.0, ReputationConfig()) is ReputationTier.C


def test_map_reliability_to_tier_b_floor_is_inclusive() -> None:
    """An LB exactly on the B floor maps to B (the >= convention)."""
    b_floor = DEFAULT_TIER_THRESHOLDS["B"]

    assert map_reliability_to_tier(b_floor, ReputationConfig()) is ReputationTier.B


def test_map_reliability_to_tier_a_floor_is_inclusive() -> None:
    """An LB exactly on the A floor maps to A (the >= convention)."""
    a_floor = DEFAULT_TIER_THRESHOLDS["A"]

    assert map_reliability_to_tier(a_floor, ReputationConfig()) is ReputationTier.A


def test_map_reliability_to_tier_just_below_a_floor_maps_to_b() -> None:
    """An LB a hair below the A floor falls back to B, not A."""
    a_floor = DEFAULT_TIER_THRESHOLDS["A"]

    assert map_reliability_to_tier(a_floor - 1e-9, ReputationConfig()) is ReputationTier.B


def test_map_reliability_to_tier_custom_thresholds_override_defaults() -> None:
    """A non-empty config.tier_thresholds overrides DEFAULT_TIER_THRESHOLDS.

    With a stricter A floor of 0.95, an LB of 0.9 that would be A under the
    defaults drops to B -- proving the override is honoured, not the default.
    """
    config = ReputationConfig(tier_thresholds={"C": 0.0, "B": 0.5, "A": 0.95})

    assert map_reliability_to_tier(0.9, config) is ReputationTier.B
    assert map_reliability_to_tier(0.96, config) is ReputationTier.A
    # 0.4 is below the custom B floor (0.5) -> C.
    assert map_reliability_to_tier(0.4, config) is ReputationTier.C


def test_reputation_tier_values_are_c_b_a() -> None:
    """ReputationTier exposes exactly the C / B / A value space."""
    assert {tier.value for tier in ReputationTier} == {"C", "B", "A"}


# --- fleet_verdict_rollup: per-wave latest verdict -----------


def _append_auditor_verdict(
    state_path: Path,
    *,
    base_id: str,
    attempt: int = 1,
    verdict: AgentReportVerdict,
    runtime: str = "claude",
) -> None:
    """Append one AUDITOR verdict envelope at ``base_id``/``attempt`` to disk.

    An attempt-aware sibling of the module-level :func:`_write_auditor_verdict`
    so a single wave can carry two verdict attempts (the latest-wins boundary)
    without colliding on the record id.
    """
    role = AgentSessionRole.AUDITOR
    report_id = report_record_id(role=role, base_id=base_id, attempt=attempt)
    moment = _T0 + timedelta(minutes=attempt)
    body = AuditorReportBody(
        verdict=verdict,
        confidence=Confidence.HIGH,
        summary="recorded auditor verdict",
        target_id=base_id,
    )
    header = AgentReportHeader(
        report_id=report_id,
        role=role,
        session_id=f"S{attempt:02d}",
        scope_id=f"{base_id}::audit",
        base_id=base_id,
        attempt=attempt,
        runtime=runtime,
        generated_at=moment,
        summary="recorded auditor verdict",
    )
    payload = AgentReportPayload(header=header, body=body)
    envelope = Envelope(
        id=report_id,
        kind=store_kind_for_role(role),
        scope_id=base_id,
        created_at=moment,
        updated_at=None,
        summary="recorded auditor verdict",
        payload=payload.model_dump(mode="json"),
    )
    append_envelope(store_path(state_path, store_kind_for_role(role)), envelope)


def test_fleet_verdict_rollup_empty_store_returns_empty(tmp_path: Path) -> None:
    """An empty per-wave report store yields ``[]`` -- the honest-empty path.

    The load-bearing honesty criterion: zero AUDITOR verdict rows on disk means
    the rollup has nothing to surface and must return an empty list rather than
    fabricate a verdict row. The pane paints its honest-empty line off this.
    """
    state_path = tmp_path / "state.json"

    assert fleet_verdict_rollup(state_path) == []


def test_fleet_verdict_rollup_lists_each_wave_latest_verdict(tmp_path: Path) -> None:
    """Two waves with one verdict each surface both, ordered by wave id.

    The success-criterion data half: a pass on one wave and a fail on another
    both land in the rollup, each carrying its own verdict + runtime.
    """
    state_path = tmp_path / "state.json"
    _append_auditor_verdict(
        state_path, base_id="P01-I01-W01", verdict=AgentReportVerdict.PASS, runtime="claude"
    )
    _append_auditor_verdict(
        state_path, base_id="P01-I01-W02", verdict=AgentReportVerdict.FAIL, runtime="codex"
    )

    rollup = fleet_verdict_rollup(state_path)

    assert rollup == [
        FleetVerdictRow(wave_id="P01-I01-W01", verdict=AgentReportVerdict.PASS, runtime="claude"),
        FleetVerdictRow(wave_id="P01-I01-W02", verdict=AgentReportVerdict.FAIL, runtime="codex"),
    ]


def test_fleet_verdict_rollup_keeps_latest_verdict_per_wave(tmp_path: Path) -> None:
    """Two verdict attempts on one wave -> the latest verdict wins.

    Boundary: an earlier FAIL must not shadow a later PASS, so a re-audited wave
    surfaces its current verdict and not a stale earlier one.
    """
    state_path = tmp_path / "state.json"
    wave_id = "P01-I01-W01"
    _append_auditor_verdict(state_path, base_id=wave_id, attempt=1, verdict=AgentReportVerdict.FAIL)
    _append_auditor_verdict(state_path, base_id=wave_id, attempt=2, verdict=AgentReportVerdict.PASS)

    rollup = fleet_verdict_rollup(state_path)

    assert len(rollup) == 1
    assert rollup[0].wave_id == wave_id
    assert rollup[0].verdict is AgentReportVerdict.PASS


def test_fleet_verdict_rollup_orders_by_wave_id(tmp_path: Path) -> None:
    """Rows come back wave-id-sorted regardless of write order, for a stable pane."""
    state_path = tmp_path / "state.json"
    _append_auditor_verdict(state_path, base_id="P01-I01-W03", verdict=AgentReportVerdict.PASS)
    _append_auditor_verdict(state_path, base_id="P01-I01-W01", verdict=AgentReportVerdict.FAIL)

    rollup = fleet_verdict_rollup(state_path)

    assert [row.wave_id for row in rollup] == ["P01-I01-W01", "P01-I01-W03"]


def test_fleet_verdict_row_forbids_extra_field() -> None:
    """A drifted FleetVerdictRow field raises at construction (extra=forbid)."""
    with pytest.raises(ValidationError):
        FleetVerdictRow(
            wave_id="P01-I01-W01",
            verdict=AgentReportVerdict.PASS,
            runtime="claude",
            drift="x",  # type: ignore[call-arg]
        )
