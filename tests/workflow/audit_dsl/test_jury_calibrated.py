"""Tests for the ``jury_calibrated`` close-gate kind (P30-I10-W03, B091).

The wave's binary success criterion pinned by these tests: ``jury_calibrated``
refuses the cross-vendor jury blocking authority UNLESS the determinism-ratio,
scored-row-count, Brier, and co-error all clear their thresholds, reading the
metrics the I09 trust-validation reducer computes (a
:class:`~eawf.observability.eval.jury_validation.JuryValidationReport`).

Boundary + error paths covered:

* all four metrics clear -- authority GRANTED (the binding-proof);
* an ``INSUFFICIENT`` report (never validated) -- REFUSED (the load-bearing
  negative: an un-calibrated jury never blocks);
* each single metric below threshold in isolation -- REFUSED, naming only that
  metric;
* a report with no known-bad wave (co-error ``None``) -- REFUSED (an undefined
  denominator never fabricates a pass);
* the exact threshold boundary -- a metric AT its threshold clears;
* a determinism-ratio outside ``[0.0, 1.0]`` -- ``ValueError``;
* the registered-kind + wiring contract.
"""

from __future__ import annotations

import pytest

from eawf.observability.eval.jury_validation import (
    JuryValidationReport,
    JuryValidationStatus,
)
from eawf.workflow.audit_dsl.kinds.jury_calibrated import (
    DEFAULT_MAX_BRIER,
    DEFAULT_MAX_CO_ERROR,
    DEFAULT_MIN_DETERMINISM_RATIO,
    DEFAULT_MIN_SCORED_ROWS,
    JURY_CALIBRATED_KIND,
    JuryCalibratedResult,
    JuryCalibrationThresholds,
    check_jury_calibrated,
)
from eawf.workflow.audit_dsl.registry import (
    CHECK_REGISTRY,
    CLOSE_GATE_KINDS,
    registered_audit_dsl_kinds,
)
from eawf.workflow.verify.readiness import wired_audit_dsl_kinds


def _scored_report(
    *,
    n: int = 12,
    brier: float | None = 0.10,
    co_error: float | None = 0.02,
    known_bad_n: int = 5,
) -> JuryValidationReport:
    """A SCORED report whose metrics clear the defaults unless overridden."""
    return JuryValidationReport(
        n=n,
        status=JuryValidationStatus.SCORED,
        fleiss_kappa=0.80,
        brier=brier,
        ece=0.05,
        unanimous_pass_on_known_bad_rate=co_error,
        known_bad_n=known_bad_n,
    )


def test_all_metrics_clear_grants_authority() -> None:
    # The binding-proof: a well-calibrated jury over a non-starved cohort with a
    # deterministic-leaning scope earns blocking authority.
    result = check_jury_calibrated(_scored_report(), determinism_ratio=0.90)
    assert isinstance(result, JuryCalibratedResult)
    assert result.blocking_authorized is True
    assert result.failing_metrics == []
    assert "earns blocking authority" in result.details


def test_insufficient_report_refuses_authority() -> None:
    # The load-bearing negative: a never-validated jury (INSUFFICIENT, every
    # numeric field None) can never clear a numeric threshold, so it is refused.
    report = JuryValidationReport(n=2, status=JuryValidationStatus.INSUFFICIENT)
    result = check_jury_calibrated(report, determinism_ratio=0.95)
    assert result.blocking_authorized is False
    # scored_rows + brier + co_error all fail (determinism cleared at 0.95).
    assert result.failing_metrics == ["scored_rows", "brier", "co_error"]
    assert "refused blocking authority" in result.details


def test_low_determinism_ratio_refuses() -> None:
    # A scope leaning on judgment oracles (sub-floor ODR) refuses, naming only
    # the determinism metric.
    result = check_jury_calibrated(_scored_report(), determinism_ratio=0.50)
    assert result.blocking_authorized is False
    assert result.failing_metrics == ["determinism_ratio"]


def test_starved_cohort_refuses() -> None:
    # A scored cohort below the scored-row floor refuses on scored_rows alone.
    result = check_jury_calibrated(
        _scored_report(n=DEFAULT_MIN_SCORED_ROWS - 1),
        determinism_ratio=0.90,
    )
    assert result.blocking_authorized is False
    assert result.failing_metrics == ["scored_rows"]


def test_high_brier_refuses() -> None:
    # A poorly-calibrated forecast (Brier above ceiling) refuses on brier alone.
    result = check_jury_calibrated(
        _scored_report(brier=DEFAULT_MAX_BRIER + 0.10),
        determinism_ratio=0.90,
    )
    assert result.blocking_authorized is False
    assert result.failing_metrics == ["brier"]


def test_high_co_error_refuses() -> None:
    # A jury that unanimously clears too many known-bad waves (co-error above
    # ceiling) refuses on co_error alone.
    result = check_jury_calibrated(
        _scored_report(co_error=DEFAULT_MAX_CO_ERROR + 0.05),
        determinism_ratio=0.90,
    )
    assert result.blocking_authorized is False
    assert result.failing_metrics == ["co_error"]


def test_no_known_bad_co_error_none_refuses() -> None:
    # A cohort with no known-bad wave leaves co-error None (undefined
    # denominator). An unmeasured metric is a refusal, never a fabricated pass.
    result = check_jury_calibrated(
        _scored_report(co_error=None, known_bad_n=0),
        determinism_ratio=0.90,
    )
    assert result.blocking_authorized is False
    assert result.failing_metrics == ["co_error"]
    assert result.co_error is None


def test_metrics_at_threshold_boundary_clear() -> None:
    # Off-by-one boundary: a metric AT its threshold clears (>= / <= contract).
    report = _scored_report(
        n=DEFAULT_MIN_SCORED_ROWS,
        brier=DEFAULT_MAX_BRIER,
        co_error=DEFAULT_MAX_CO_ERROR,
    )
    result = check_jury_calibrated(report, determinism_ratio=DEFAULT_MIN_DETERMINISM_RATIO)
    assert result.blocking_authorized is True
    assert result.failing_metrics == []


def test_multiple_metrics_below_threshold_named_together() -> None:
    # Several failing metrics are reported together in canonical order.
    report = _scored_report(brier=0.90, co_error=0.90)
    result = check_jury_calibrated(report, determinism_ratio=0.10)
    assert result.blocking_authorized is False
    assert result.failing_metrics == ["determinism_ratio", "brier", "co_error"]


def test_custom_thresholds_are_honoured() -> None:
    # A tighter threshold set refuses a report the defaults would have passed.
    strict = JuryCalibrationThresholds(
        min_determinism_ratio=0.99,
        min_scored_rows=50,
        max_brier=0.01,
        max_co_error=0.0,
    )
    result = check_jury_calibrated(_scored_report(), determinism_ratio=0.90, thresholds=strict)
    assert result.blocking_authorized is False
    assert result.failing_metrics == ["determinism_ratio", "scored_rows", "brier", "co_error"]


def test_result_carries_scored_metric_values() -> None:
    # The result echoes the metric values it scored so the verdict is auditable.
    report = _scored_report(n=20, brier=0.08, co_error=0.01)
    result = check_jury_calibrated(report, determinism_ratio=0.88)
    assert result.determinism_ratio == pytest.approx(0.88)
    assert result.scored_rows == 20
    assert result.brier == pytest.approx(0.08)
    assert result.co_error == pytest.approx(0.01)


def test_determinism_ratio_below_zero_raises() -> None:
    # Error path: a determinism-ratio outside [0.0, 1.0] is a contract breach.
    with pytest.raises(ValueError, match="determinism_ratio out of range"):
        check_jury_calibrated(_scored_report(), determinism_ratio=-0.1)


def test_determinism_ratio_above_one_raises() -> None:
    with pytest.raises(ValueError, match="determinism_ratio out of range"):
        check_jury_calibrated(_scored_report(), determinism_ratio=1.5)


def test_thresholds_reject_out_of_range_at_construction() -> None:
    # The threshold model is strict: an out-of-range ceiling fails validation.
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        JuryCalibrationThresholds(max_brier=1.5)


def test_thresholds_forbid_extra_keys() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        JuryCalibrationThresholds(unknown_key=1)  # type: ignore[call-arg]


def test_kind_constant_is_stable() -> None:
    # The registered kind string is the stable id the registry + wired-on sweep
    # key on.
    assert JURY_CALIBRATED_KIND == "jury_calibrated"


def test_jury_calibrated_is_a_registered_close_gate_kind() -> None:
    # Registered (so the wired-on sweep counts it) but NOT a file-set runner
    # kind, so it stays out of CHECK_REGISTRY.
    assert JURY_CALIBRATED_KIND in CLOSE_GATE_KINDS
    assert JURY_CALIBRATED_KIND in registered_audit_dsl_kinds()
    assert JURY_CALIBRATED_KIND not in CHECK_REGISTRY


def test_jury_calibrated_is_wired_not_idle() -> None:
    # The new kind has a production binding (CLOSE_GATE_KINDS membership), so the
    # BIND-1 wired-on sweep sees it as wired rather than registered-but-idle.
    assert JURY_CALIBRATED_KIND in wired_audit_dsl_kinds()
    assert not (registered_audit_dsl_kinds() - wired_audit_dsl_kinds())
