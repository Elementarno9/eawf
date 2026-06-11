"""``jury_calibrated`` close-gate kind (P30-I10 QUAL-3, closes B091).

A close-gate that decides whether the cross-vendor jury has earned
*blocking authority* -- the right to refuse a wave's close -- by reading
the calibration metrics the I09 trust-validation reducer computes. The
contract pins the operator's concern: a jury that has never been scored
against ground truth, or that scores badly, MUST NOT be allowed to block
a close on its own judgment. An un-calibrated or mis-calibrated jury is
exactly the idle / mis-trusted oracle the metric exists to surface, so
it is refused blocking authority and the close degrades to the cheaper
deterministic gate path.

What it reads
-------------

The gate consumes two already-computed inputs -- it never recomputes a
metric and never spawns a juror:

* a :class:`~eawf.observability.eval.jury_validation.JuryValidationReport`
  (the I09 reducer's output) carrying the cohort size (``n``), the Brier
  score (``brier``), and the co-error rate
  (``unanimous_pass_on_known_bad_rate`` -- the fraction of known-bad
  waves the jury unanimously waved through, a false clean);
* the Oracle-Determinism-Ratio (``determinism_ratio``) computed by
  :func:`eawf.observability.metrics.odr.oracle_determinism_ratio` over
  the scope's required criteria.

Authority contract
------------------

Blocking authority is granted ONLY when every threshold clears:

* **determinism-ratio** ``>= min_determinism_ratio`` -- the scope's
  required criteria lean on reproducible falsifiers, not bare judgment;
* **scored-row-count** ``n >= min_scored_rows`` AND the report is
  :attr:`~eawf.observability.eval.jury_validation.JuryValidationStatus.SCORED`
  -- the jury was actually validated against a non-starved cohort (an
  ``INSUFFICIENT`` report leaves every numeric field ``None``, so it can
  never clear a numeric threshold and is refused);
* **Brier** ``<= max_brier`` -- the jury's pass-fraction forecast is
  well-calibrated against ground truth (lower is better);
* **co-error** ``<= max_co_error`` -- the jury rarely unanimously clears
  a known-bad wave (lower is better). A report with no known-bad wave in
  the cohort leaves this rate ``None`` (an undefined denominator, never a
  fabricated zero), which CANNOT clear the threshold -- a jury never
  tested against a known-bad wave has not earned blocking authority.

The gate is a pure function over the typed report; it never mutates
state and never writes a file. The verdict rolls up to a single
``blocking_authorized`` bit plus a one-line ``details`` note naming each
failing metric so the refusal is attributable.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from eawf.observability.eval.jury_validation import JuryValidationReport

logger = logging.getLogger(__name__)

#: The close-gate kind string. Registered into the close-gate registry
#: (:data:`eawf.workflow.audit_dsl.registry.CLOSE_GATE_KINDS`) so the
#: BIND-1 wired-on sweep counts it as a production-reachable kind.
JURY_CALIBRATED_KIND = "jury_calibrated"

#: Default determinism-ratio floor the jury must clear to earn blocking
#: authority. Mirrors :data:`eawf.observability.metrics.odr.DEFAULT_ODR_FLOOR`
#: so a scope leaning mostly on judgment oracles is refused.
DEFAULT_MIN_DETERMINISM_RATIO = 0.80

#: Default minimum scored-row count: the validation cohort must carry at
#: least this many labelled verdicts before the jury's calibration numbers
#: are trusted to gate blocking authority.
DEFAULT_MIN_SCORED_ROWS = 8

#: Default maximum Brier score (lower is better). Above this the jury's
#: pass-fraction forecast is too poorly calibrated to block a close.
DEFAULT_MAX_BRIER = 0.25

#: Default maximum co-error rate (lower is better): the fraction of
#: known-bad waves the jury unanimously cleared. Above this the jury waves
#: through too many false cleans to be trusted with blocking authority.
DEFAULT_MAX_CO_ERROR = 0.10


class JuryCalibrationThresholds(BaseModel):
    """The four thresholds the jury must clear to earn blocking authority.

    ``extra="forbid"`` + ``frozen`` so a drifted threshold key fails at
    construction with :class:`pydantic.ValidationError` rather than
    silently changing the gate.

    Attributes:
        min_determinism_ratio: Floor the Oracle-Determinism-Ratio must
            meet (``[0.0, 1.0]``). A scope below it leans too much on
            judgment oracles for the jury to block.
        min_scored_rows: Floor the validation cohort size (``report.n``)
            must meet (``>= 1``). A starved cohort cannot trust its own
            calibration numbers.
        max_brier: Ceiling the jury's Brier score may reach
            (``[0.0, 1.0]``; lower is better).
        max_co_error: Ceiling the jury's co-error rate -- its unanimous
            false-clean rate on known-bad waves -- may reach
            (``[0.0, 1.0]``; lower is better).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    min_determinism_ratio: float = Field(default=DEFAULT_MIN_DETERMINISM_RATIO, ge=0.0, le=1.0)
    min_scored_rows: int = Field(default=DEFAULT_MIN_SCORED_ROWS, ge=1)
    max_brier: float = Field(default=DEFAULT_MAX_BRIER, ge=0.0, le=1.0)
    max_co_error: float = Field(default=DEFAULT_MAX_CO_ERROR, ge=0.0, le=1.0)


class JuryCalibratedResult(BaseModel):
    """Typed outcome of one jury-calibration authority check.

    ``extra="forbid"`` + ``frozen`` so a malformed result fails at
    construction rather than skewing a downstream rollup.

    Attributes:
        blocking_authorized: ``True`` only when every metric cleared its
            threshold. When ``False`` the cross-vendor jury is refused
            blocking authority and the close degrades to the deterministic
            gate path.
        determinism_ratio: The Oracle-Determinism-Ratio the gate scored.
        scored_rows: The validation cohort size (``report.n``).
        brier: The jury's Brier score, or ``None`` when the cohort refused
            to score.
        co_error: The jury's unanimous-false-clean rate on known-bad
            waves, or ``None`` when the cohort refused to score or carried
            no known-bad wave.
        failing_metrics: The metric names that did NOT clear their
            threshold, in a stable canonical order. Empty when authority
            is granted.
        details: A one-line note suitable for the close-gate record.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    blocking_authorized: bool
    determinism_ratio: float = Field(ge=0.0, le=1.0)
    scored_rows: int = Field(ge=0)
    brier: float | None = Field(default=None, ge=0.0, le=1.0)
    co_error: float | None = Field(default=None, ge=0.0, le=1.0)
    failing_metrics: list[str] = Field(default_factory=list)
    details: str


def check_jury_calibrated(
    report: JuryValidationReport,
    *,
    determinism_ratio: float,
    thresholds: JuryCalibrationThresholds | None = None,
) -> JuryCalibratedResult:
    """Refuse the jury blocking authority unless every calibration metric clears.

    Reads the I09 :class:`JuryValidationReport` plus the scope's
    determinism-ratio and rolls them up: the jury earns blocking authority
    only when the determinism-ratio, the scored-row count, the Brier
    score, and the co-error rate ALL clear their thresholds. A report that
    refused to score (status ``INSUFFICIENT``, every numeric field
    ``None``) can never clear a numeric threshold, so an un-validated jury
    is refused. The gate never mutates state nor writes a file.

    Args:
        report: The I09 reducer's validation report. Read-only.
        determinism_ratio: The Oracle-Determinism-Ratio over the scope's
            required criteria (``[0.0, 1.0]``), from
            :func:`eawf.observability.metrics.odr.oracle_determinism_ratio`.
        thresholds: The authority thresholds. Defaults to a
            :class:`JuryCalibrationThresholds` built from the module
            defaults.

    Returns:
        A :class:`JuryCalibratedResult` whose ``blocking_authorized`` is
        ``True`` only when no metric is failing.

    Raises:
        ValueError: When *determinism_ratio* is outside ``[0.0, 1.0]``.
    """
    from eawf.observability.eval.jury_validation import JuryValidationStatus

    if not 0.0 <= determinism_ratio <= 1.0:
        raise ValueError(f"determinism_ratio out of range: {determinism_ratio!r}")
    thr = thresholds if thresholds is not None else JuryCalibrationThresholds()

    failing: list[str] = []

    if determinism_ratio < thr.min_determinism_ratio:
        failing.append("determinism_ratio")

    scored = report.status is JuryValidationStatus.SCORED
    if not scored or report.n < thr.min_scored_rows:
        failing.append("scored_rows")

    # A ``None`` Brier / co-error (the refuse-to-score or no-known-bad
    # surface) cannot clear a ceiling -- an unmeasured metric is treated as
    # a failure, never a fabricated pass.
    if report.brier is None or report.brier > thr.max_brier:
        failing.append("brier")

    co_error = report.unanimous_pass_on_known_bad_rate
    if co_error is None or co_error > thr.max_co_error:
        failing.append("co_error")

    authorized = not failing
    if authorized:
        details = (
            f"jury earns blocking authority "
            f"(determinism_ratio={determinism_ratio:.4f} scored_rows={report.n} "
            f"brier={report.brier} co_error={co_error})"
        )
        logger.debug(
            f"check_jury_calibrated ok scored_rows={report.n} "
            f"determinism_ratio={determinism_ratio:.4f}"
        )
    else:
        details = (
            f"jury refused blocking authority -- {len(failing)} metric(s) below "
            f"threshold: {', '.join(failing)} "
            f"(determinism_ratio={determinism_ratio:.4f} scored_rows={report.n} "
            f"brier={report.brier} co_error={co_error})"
        )
        logger.info(
            f"check_jury_calibrated refuse failing={failing} "
            f"scored_rows={report.n} determinism_ratio={determinism_ratio:.4f}"
        )

    return JuryCalibratedResult(
        blocking_authorized=authorized,
        determinism_ratio=determinism_ratio,
        scored_rows=report.n,
        brier=report.brier,
        co_error=co_error,
        failing_metrics=failing,
        details=details,
    )


__all__ = [
    "DEFAULT_MAX_BRIER",
    "DEFAULT_MAX_CO_ERROR",
    "DEFAULT_MIN_DETERMINISM_RATIO",
    "DEFAULT_MIN_SCORED_ROWS",
    "JURY_CALIBRATED_KIND",
    "JuryCalibratedResult",
    "JuryCalibrationThresholds",
    "check_jury_calibrated",
]
