"""Tests for ``calibration_set_from_report`` (P30-I09-W07).

The helper binds a jury-validation report's real Brier + ECE into the TUI
:class:`~eawf.surfaces.tui.modals.calibration_drill.CalibrationSet` the
calibration drill surfaces. Two halves are pinned:

- a SCORED report yields a real :class:`CalibrationSet` whose Brier / ECE /
  sample count are exactly the report's numbers -- the drill never fabricates a
  score;
- an INSUFFICIENT report (every numeric field ``None``) yields ``None`` so the
  drill renders its honest-empty notice rather than reading a number out of a
  starved cohort.
"""

from __future__ import annotations

import pytest

from eawf.observability.eval.jury_validation import (
    JuryValidationReport,
    JuryValidationStatus,
)
from eawf.surfaces.tui.modals.calibration_drill import (
    CalibrationSet,
    calibration_set_from_report,
)


def test_calibration_set_from_scored_report_binds_real_metrics() -> None:
    """A scored report binds a CalibrationSet carrying its Brier / ECE / n."""
    report = JuryValidationReport(
        n=24,
        status=JuryValidationStatus.SCORED,
        fleiss_kappa=0.875,
        brier=0.125,
        ece=0.0625,
        unanimous_pass_on_known_bad_rate=0.0,
        known_bad_n=6,
    )

    calibration = calibration_set_from_report(report)

    assert isinstance(calibration, CalibrationSet)
    assert calibration.brier_score == pytest.approx(0.125)
    assert calibration.ece == pytest.approx(0.0625)
    assert calibration.sample_count == 24


def test_calibration_set_from_insufficient_report_is_none() -> None:
    """An insufficient report (metrics None) yields None -- honest-empty drill."""
    report = JuryValidationReport(
        n=3,
        status=JuryValidationStatus.INSUFFICIENT,
        known_bad_n=1,
    )

    assert calibration_set_from_report(report) is None


def test_calibration_set_from_empty_cohort_report_is_none() -> None:
    """A zero-n insufficient report (the live v0.5 path) yields None."""
    report = JuryValidationReport(n=0, status=JuryValidationStatus.INSUFFICIENT)

    assert calibration_set_from_report(report) is None
