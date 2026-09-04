"""Tests for the Oracle-Determinism-Ratio metric and the escape ledger.

Covers the FS14 contract:

* CR-1 -- :func:`oracle_determinism_ratio` over a known criteria set
  equals the hand-computed fraction, with boundary cases (all
  deterministic -> ``1.0``, all jury -> ``0.0``) and the zero-required
  defined value (``EMPTY_RATIO``, never ``ZeroDivisionError``).
* CR-2 -- a below-floor set surfaces an ADVISORY finding via
  :func:`odr_below_floor`, asserted through ``caplog``.
* The escape-ledger primitive -- :func:`escape_rate` over tagged
  :class:`EscapeFinding` rows, plus the ``caught_at`` validation path.
"""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.common import (
    CriterionSpec,
    OracleTier,
    QualityDimension,
)
from eawf.observability.metrics.odr import (
    DEFAULT_ODR_FLOOR,
    EMPTY_RATIO,
    DriftPulseReport,
    EscapeFinding,
    EscapeStage,
    OdrAdvisory,
    WavePlanRow,
    drift_budget_pulse,
    escape_rate,
    iter_odr_advisory,
    odr_below_floor,
    oracle_determinism_ratio,
    pulse_refuses_dispatch,
)


def _criterion(
    cid: str,
    *,
    tier: OracleTier | None,
    required: bool = True,
) -> CriterionSpec:
    """Build a minimal valid CriterionSpec with a given tier + required bit."""
    return CriterionSpec(
        id=cid,
        text=f"criterion {cid} succeeds and is observable",
        kind="deterministic",
        acceptance_style="binary",
        evidence_kind="deterministic",
        quality_dimension=QualityDimension.FUNCTIONAL_SUITABILITY,
        measurable_signal="a deterministic check produces a bit verdict",
        required=required,
        oracle_tier=tier,
    )


# --- oracle_determinism_ratio: boundary cases -----------------------------


def test_oracle_determinism_ratio_all_deterministic_returns_one() -> None:
    criteria = [
        _criterion("CR-01", tier=OracleTier.T1_STATIC),
        _criterion("CR-02", tier=OracleTier.T2_STRUCTURAL),
        _criterion("CR-03", tier=OracleTier.T3_SNAPSHOT),
        _criterion("CR-04", tier=OracleTier.T4_CONTRACT),
        _criterion("CR-05", tier=OracleTier.T5_GOLDEN),
    ]
    assert oracle_determinism_ratio(criteria) == pytest.approx(1.0)


def test_oracle_determinism_ratio_all_jury_returns_zero() -> None:
    criteria = [
        _criterion("CR-01", tier=OracleTier.T7_JURY),
        _criterion("CR-02", tier=OracleTier.T7_JURY),
        _criterion("CR-03", tier=OracleTier.T7_JURY),
    ]
    assert oracle_determinism_ratio(criteria) == pytest.approx(0.0)


def test_oracle_determinism_ratio_t6_approval_is_not_deterministic() -> None:
    # T6 (approval) is a judgment oracle -- excluded from the numerator.
    criteria = [
        _criterion("CR-01", tier=OracleTier.T5_GOLDEN),
        _criterion("CR-02", tier=OracleTier.T6_APPROVAL),
    ]
    assert oracle_determinism_ratio(criteria) == pytest.approx(0.5)


# --- CR-1: hand-computed mixed fraction -----------------------------------


def test_oracle_determinism_ratio_mixed_set_equals_hand_computed_fraction() -> None:
    # Five required criteria: T1, T4, T5 are deterministic (3); T7 + None
    # are not. Hand-computed ratio = 3 / 5 = 0.6.
    criteria = [
        _criterion("CR-01", tier=OracleTier.T1_STATIC),
        _criterion("CR-02", tier=OracleTier.T4_CONTRACT),
        _criterion("CR-03", tier=OracleTier.T5_GOLDEN),
        _criterion("CR-04", tier=OracleTier.T7_JURY),
        _criterion("CR-05", tier=None),
    ]
    assert oracle_determinism_ratio(criteria) == pytest.approx(3 / 5)


def test_oracle_determinism_ratio_none_tier_excluded_from_numerator() -> None:
    # A None-tier (grandfathered) required criterion is NOT deterministic.
    criteria = [
        _criterion("CR-01", tier=OracleTier.T2_STRUCTURAL),
        _criterion("CR-02", tier=None),
    ]
    assert oracle_determinism_ratio(criteria) == pytest.approx(0.5)


def test_oracle_determinism_ratio_optional_criteria_ignored() -> None:
    # Optional (required=False) criteria enter neither numerator nor
    # denominator: the one required deterministic criterion -> 1.0.
    criteria = [
        _criterion("CR-01", tier=OracleTier.T1_STATIC, required=True),
        _criterion("CR-02", tier=OracleTier.T7_JURY, required=False),
        _criterion("CR-03", tier=None, required=False),
    ]
    assert oracle_determinism_ratio(criteria) == pytest.approx(1.0)


# --- error / boundary: zero denominator -----------------------------------


def test_oracle_determinism_ratio_empty_returns_defined_value() -> None:
    assert oracle_determinism_ratio([]) == pytest.approx(EMPTY_RATIO)
    assert pytest.approx(1.0) == EMPTY_RATIO


def test_oracle_determinism_ratio_no_required_returns_defined_value() -> None:
    # Optional-only set -> zero denominator -> EMPTY_RATIO, not a crash.
    criteria = [
        _criterion("CR-01", tier=OracleTier.T7_JURY, required=False),
        _criterion("CR-02", tier=None, required=False),
    ]
    assert oracle_determinism_ratio(criteria) == pytest.approx(EMPTY_RATIO)


# --- CR-2: advisory finding below floor (caplog) --------------------------


def test_odr_below_floor_emits_advisory_finding(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # ODR = 1 / 3 ~= 0.33, below the 0.80 default floor.
    criteria = [
        _criterion("CR-01", tier=OracleTier.T1_STATIC),
        _criterion("CR-02", tier=OracleTier.T7_JURY),
        _criterion("CR-03", tier=OracleTier.T7_JURY),
    ]
    with caplog.at_level(logging.WARNING, logger="eawf.observability.metrics.odr"):
        below = odr_below_floor(criteria, scope_id="P29-I11-W08")
    assert below is True
    records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "odr_below_floor" in message
    assert "severity=advisory" in message
    assert "scope='P29-I11-W08'" in message


def test_odr_below_floor_at_or_above_floor_is_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    criteria = [
        _criterion("CR-01", tier=OracleTier.T1_STATIC),
        _criterion("CR-02", tier=OracleTier.T4_CONTRACT),
    ]
    with caplog.at_level(logging.WARNING, logger="eawf.observability.metrics.odr"):
        below = odr_below_floor(criteria, floor=DEFAULT_ODR_FLOOR)
    assert below is False
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_odr_below_floor_default_floor_is_eighty_hundredths() -> None:
    assert pytest.approx(0.80) == DEFAULT_ODR_FLOOR


# --- iter_odr_advisory: binding seam --------------------------------------


def test_iter_odr_advisory_below_floor_returns_finding(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # ODR = 1 / 3 ~= 0.33, below the 0.80 default floor.
    criteria = [
        _criterion("CR-01", tier=OracleTier.T1_STATIC),
        _criterion("CR-02", tier=OracleTier.T7_JURY),
        _criterion("CR-03", tier=OracleTier.T7_JURY),
    ]
    with caplog.at_level(logging.WARNING, logger="eawf.observability.metrics.odr"):
        advisory = iter_odr_advisory(criteria, scope_id="P03-I01")
    assert advisory is not None
    assert isinstance(advisory, OdrAdvisory)
    assert advisory.scope_id == "P03-I01"
    assert advisory.odr == pytest.approx(1 / 3)
    assert advisory.floor == pytest.approx(DEFAULT_ODR_FLOOR)
    assert advisory.required == 3
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "odr_below_floor" in warnings[0].getMessage()


def test_iter_odr_advisory_at_or_above_floor_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    criteria = [
        _criterion("CR-01", tier=OracleTier.T1_STATIC),
        _criterion("CR-02", tier=OracleTier.T4_CONTRACT),
    ]
    with caplog.at_level(logging.WARNING, logger="eawf.observability.metrics.odr"):
        advisory = iter_odr_advisory(criteria, scope_id="P03-I01")
    assert advisory is None
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_iter_odr_advisory_empty_criteria_returns_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Sentinel path: zero criteria -> EMPTY_RATIO (1.0) -> no advisory, no log.
    with caplog.at_level(logging.WARNING, logger="eawf.observability.metrics.odr"):
        advisory = iter_odr_advisory([], scope_id="P03-I01")
    assert advisory is None
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_iter_odr_advisory_rejects_extra_keys() -> None:
    with pytest.raises(ValidationError):
        OdrAdvisory(
            scope_id="P03-I01",
            odr=0.5,
            floor=0.8,
            required=2,
            severity="advisory",  # type: ignore[call-arg]
        )


# --- escape ledger --------------------------------------------------------


def test_escape_rate_mixed_findings_returns_expected_fraction() -> None:
    findings = [
        EscapeFinding(finding_id="F-01", caught_at=EscapeStage.CLOSE),
        EscapeFinding(finding_id="F-02", caught_at=EscapeStage.REVIEW),
        EscapeFinding(finding_id="F-03", caught_at=EscapeStage.PRODUCTION),
        EscapeFinding(finding_id="F-04", caught_at=EscapeStage.CLOSE),
    ]
    # Two of four escaped the close gate (review + production) -> 0.5.
    assert escape_rate(findings) == pytest.approx(0.5)


def test_escape_rate_all_caught_at_close_is_zero() -> None:
    findings = [
        EscapeFinding(finding_id="F-01", caught_at=EscapeStage.CLOSE),
        EscapeFinding(finding_id="F-02", caught_at=EscapeStage.CLOSE),
    ]
    assert escape_rate(findings) == pytest.approx(0.0)


def test_escape_rate_empty_ledger_returns_zero() -> None:
    assert escape_rate([]) == pytest.approx(0.0)


def test_escape_finding_unknown_caught_at_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        EscapeFinding(finding_id="F-01", caught_at="post-mortem")  # type: ignore[arg-type]


def test_escape_finding_rejects_extra_keys() -> None:
    with pytest.raises(ValidationError):
        EscapeFinding(
            finding_id="F-01",
            caught_at=EscapeStage.CLOSE,
            severity="advisory",  # type: ignore[call-arg]
        )


# --- drift-budget pulse: WavePlanRow thinness predicate -------------------


def _plan_row(
    wave_id: str,
    *,
    planned: int,
    delivered: int,
    eu: float = 0.0,
) -> WavePlanRow:
    """Build a WavePlanRow pairing planned vs delivered criteria counts."""
    return WavePlanRow(
        wave_id=wave_id,
        planned_criteria=planned,
        delivered_criteria=delivered,
        eu=eu,
    )


def test_wave_plan_row_is_thin_when_delivered_below_planned() -> None:
    assert _plan_row("P01-I01-W01", planned=4, delivered=2).is_thin is True


def test_wave_plan_row_is_not_thin_when_delivered_meets_plan() -> None:
    assert _plan_row("P01-I01-W01", planned=3, delivered=3).is_thin is False


def test_wave_plan_row_over_delivery_is_not_thin() -> None:
    assert _plan_row("P01-I01-W01", planned=2, delivered=5).is_thin is False


def test_wave_plan_row_rejects_negative_counts() -> None:
    with pytest.raises(ValidationError):
        WavePlanRow(wave_id="P01-I01-W01", planned_criteria=-1, delivered_criteria=0)


def test_wave_plan_row_rejects_extra_keys() -> None:
    with pytest.raises(ValidationError):
        WavePlanRow(
            wave_id="P01-I01-W01",
            planned_criteria=2,
            delivered_criteria=2,
            severity="x",  # type: ignore[call-arg]
        )


# --- drift_budget_pulse: budget boundary ----------------------------------


def test_drift_budget_pulse_fewer_than_k_returns_none() -> None:
    # Two clean closes against a K=3 budget -> below boundary -> no pulse.
    rows = [
        _plan_row("P01-I01-W01", planned=2, delivered=2),
        _plan_row("P01-I01-W02", planned=2, delivered=2),
    ]
    assert drift_budget_pulse(rows, budget_waves=3, budget_eu=0.0) is None


def test_drift_budget_pulse_k_clean_closes_reports_no_drift() -> None:
    # Exactly K=3 clean closes -> pulse fires with drift_detected=False.
    rows = [
        _plan_row("P01-I01-W01", planned=2, delivered=2),
        _plan_row("P01-I01-W02", planned=3, delivered=3),
        _plan_row("P01-I01-W03", planned=1, delivered=4),
    ]
    report = drift_budget_pulse(rows, budget_waves=3, budget_eu=0.0)
    assert report is not None
    assert isinstance(report, DriftPulseReport)
    assert report.drift_detected is False
    assert report.thin_wave_ids == []
    assert report.findings == []
    assert report.window_waves == ["P01-I01-W01", "P01-I01-W02", "P01-I01-W03"]
    assert report.budget_waves == 3


def test_drift_budget_pulse_thin_wave_reports_drift() -> None:
    # One wave delivers fewer criteria than its plan row -> drift_detected.
    rows = [
        _plan_row("P01-I01-W01", planned=2, delivered=2),
        _plan_row("P01-I01-W02", planned=4, delivered=1),
        _plan_row("P01-I01-W03", planned=2, delivered=2),
    ]
    report = drift_budget_pulse(rows, budget_waves=3, budget_eu=0.0)
    assert report is not None
    assert report.drift_detected is True
    assert report.thin_wave_ids == ["P01-I01-W02"]
    assert report.findings == ["wave=P01-I01-W02 planned=4 delivered=1"]


def test_drift_budget_pulse_eu_arm_fires_before_wave_count() -> None:
    # Two large waves accumulate D=3.5 EU before the K=5 wave count -> pulse.
    rows = [
        _plan_row("P01-I01-W01", planned=2, delivered=2, eu=2.0),
        _plan_row("P01-I01-W02", planned=2, delivered=2, eu=2.0),
    ]
    report = drift_budget_pulse(rows, budget_waves=5, budget_eu=3.5)
    assert report is not None
    assert report.drift_detected is False


def test_drift_budget_pulse_eu_arm_below_budget_returns_none() -> None:
    rows = [_plan_row("P01-I01-W01", planned=2, delivered=2, eu=1.0)]
    assert drift_budget_pulse(rows, budget_waves=5, budget_eu=3.5) is None


def test_drift_budget_pulse_no_live_budget_arm_raises() -> None:
    rows = [_plan_row("P01-I01-W01", planned=2, delivered=2)]
    with pytest.raises(ValueError, match="no live budget arm"):
        drift_budget_pulse(rows, budget_waves=0, budget_eu=0.0)


# --- pulse_refuses_dispatch: barrier vs optimistic ------------------------


def test_pulse_refuses_dispatch_barrier_drift_refuses() -> None:
    rows = [
        _plan_row("P01-I01-W01", planned=2, delivered=2),
        _plan_row("P01-I01-W02", planned=4, delivered=1),
        _plan_row("P01-I01-W03", planned=2, delivered=2),
    ]
    report = drift_budget_pulse(rows, budget_waves=3, budget_eu=0.0, checkpoint_mode="barrier")
    assert report is not None
    assert report.drift_detected is True
    assert pulse_refuses_dispatch(report) is True


def test_pulse_refuses_dispatch_optimistic_drift_does_not_refuse() -> None:
    # Optimistic mode is advisory: detected drift never refuses dispatch.
    rows = [
        _plan_row("P01-I01-W01", planned=2, delivered=2),
        _plan_row("P01-I01-W02", planned=4, delivered=1),
        _plan_row("P01-I01-W03", planned=2, delivered=2),
    ]
    report = drift_budget_pulse(rows, budget_waves=3, budget_eu=0.0, checkpoint_mode="optimistic")
    assert report is not None
    assert report.drift_detected is True
    assert pulse_refuses_dispatch(report) is False


def test_pulse_refuses_dispatch_barrier_clean_does_not_refuse() -> None:
    # A clean pulse never refuses, even in barrier mode (zero parallelism cost).
    rows = [
        _plan_row("P01-I01-W01", planned=2, delivered=2),
        _plan_row("P01-I01-W02", planned=2, delivered=2),
        _plan_row("P01-I01-W03", planned=2, delivered=2),
    ]
    report = drift_budget_pulse(rows, budget_waves=3, budget_eu=0.0, checkpoint_mode="barrier")
    assert report is not None
    assert report.drift_detected is False
    assert pulse_refuses_dispatch(report) is False


def test_drift_budget_pulse_rejects_extra_keys() -> None:
    with pytest.raises(ValidationError):
        DriftPulseReport(
            drift_detected=False,
            budget_waves=3,
            budget_eu=3.5,
            checkpoint_mode="optimistic",
            severity="x",  # type: ignore[call-arg]
        )
