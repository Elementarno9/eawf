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
    EscapeFinding,
    EscapeStage,
    OdrAdvisory,
    escape_rate,
    iter_odr_advisory,
    odr_below_floor,
    oracle_determinism_ratio,
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
