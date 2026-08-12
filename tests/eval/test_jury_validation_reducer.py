"""Jury-validation reducer tests.

The reducer scores the JURY against the ground-truth cohort
(:class:`~eawf.observability.eval.jury_validation.ValidationCohort` from W01).
These tests pin the two binary success criteria:

- C1: ``validate_jury`` computes Fleiss kappa over the per-juror ballot matrix,
  Brier and ECE against the cohort ground truth, and the
  unanimous-pass-on-known-bad rate; perfect juror agreement -> kappa ``1.0``; a
  unanimously-passed known-bad cohort -> rate ``1.0``;
- C2: a cohort under ``min_validation_n`` -> every metric ``None`` + status
  ``insufficient`` (never fabricated); a wave with NO recorded ballots ->
  ``ValueError`` (a verdict was claimed but no jury ran).

Plus the ECE bucket correctness and the model error paths.
"""

from __future__ import annotations

import pytest

from eawf.kernel.state.enums import AgentReportVerdict, AgentSessionRole
from eawf.observability.eval.jury import JurorBallot
from eawf.observability.eval.jury_validation import (
    JuryValidationConfig,
    JuryValidationReport,
    JuryValidationStatus,
    LabeledVerdict,
    LabelSource,
    ValidationCohort,
    validate_jury,
)
from eawf.observability.eval.reputation import VerdictOutcome, expected_calibration_error

_JURORS = ("claude-code", "codex", "opencode")


def _labeled_verdict(*, wave_id: str, ground_truth: bool) -> LabeledVerdict:
    """Build a silver-tier :class:`LabeledVerdict` for *wave_id*."""
    outcome = VerdictOutcome(
        base_id=wave_id,
        agent_role=AgentSessionRole.AUDITOR,
        runtime="claude",
        verdict=AgentReportVerdict.PASS,
        confidence=0.9,
        held=ground_truth,
        outcome_source="clean" if ground_truth else "reactive",
    )
    return LabeledVerdict(
        outcome=outcome,
        ground_truth=ground_truth,
        label_source=LabelSource.SILVER,
    )


def _ballots(*verdicts: AgentReportVerdict) -> tuple[JurorBallot, ...]:
    """Build one binary :class:`JurorBallot` per verdict, one juror each."""
    return tuple(
        JurorBallot(juror_id=juror, acceptance_style="binary", verdict=verdict)
        for juror, verdict in zip(_JURORS, verdicts, strict=False)
    )


def _cohort(rows: list[LabeledVerdict]) -> ValidationCohort:
    """Wrap *rows* as the silver tier of a cohort (gold empty)."""
    return ValidationCohort(silver=rows, gold=[])


def _all_pass() -> tuple[JurorBallot, ...]:
    """Three jurors unanimously voting PASS."""
    return _ballots(AgentReportVerdict.PASS, AgentReportVerdict.PASS, AgentReportVerdict.PASS)


def _all_fail() -> tuple[JurorBallot, ...]:
    """Three jurors unanimously voting FAIL."""
    return _ballots(AgentReportVerdict.FAIL, AgentReportVerdict.FAIL, AgentReportVerdict.FAIL)


# --- C1: perfect agreement -> kappa 1.0 -----------------------------------


def test_validate_jury_perfect_agreement_kappa_one() -> None:
    """C1: every wave rated unanimously -> Fleiss kappa is exactly 1.0.

    The jury agrees with itself perfectly on every wave (a mix of unanimous
    pass and unanimous fail across waves), so the inter-juror agreement beyond
    chance is maximal.
    """
    rows = [
        _labeled_verdict(wave_id=f"P01-I01-W{i:02d}", ground_truth=i % 2 == 0) for i in range(4)
    ]
    ballots = {
        row.outcome.base_id: (_all_pass() if row.ground_truth else _all_fail()) for row in rows
    }

    report = validate_jury(_cohort(rows), ballots, JuryValidationConfig(min_validation_n=1))

    assert report.status is JuryValidationStatus.SCORED
    assert report.fleiss_kappa == pytest.approx(1.0)


# --- C1: unanimous pass on known-bad -> rate 1.0 ---------------------------


def test_validate_jury_unanimous_pass_on_known_bad_rate_one() -> None:
    """C1: a unanimously-passed known-bad cohort -> rate is exactly 1.0.

    Every wave is known-bad (``ground_truth is False``) yet the jury passed
    each one unanimously -- the jury's worst failure mode (a false clean), so
    the unanimous-pass-on-known-bad rate saturates to 1.0.
    """
    rows = [_labeled_verdict(wave_id=f"P01-I01-W{i:02d}", ground_truth=False) for i in range(3)]
    ballots = {row.outcome.base_id: _all_pass() for row in rows}

    report = validate_jury(_cohort(rows), ballots, JuryValidationConfig(min_validation_n=1))

    assert report.status is JuryValidationStatus.SCORED
    assert report.known_bad_n == 3
    assert report.unanimous_pass_on_known_bad_rate == pytest.approx(1.0)


def test_validate_jury_known_bad_correctly_failed_rate_zero() -> None:
    """A known-bad cohort the jury correctly failed -> unanimous-pass rate 0.0.

    The jury did NOT falsely clean any known-bad wave, so the false-clean rate
    is the floor.
    """
    rows = [_labeled_verdict(wave_id=f"P01-I01-W{i:02d}", ground_truth=False) for i in range(3)]
    ballots = {row.outcome.base_id: _all_fail() for row in rows}

    report = validate_jury(_cohort(rows), ballots, JuryValidationConfig(min_validation_n=1))

    assert report.unanimous_pass_on_known_bad_rate == pytest.approx(0.0)


def test_validate_jury_no_known_bad_rate_is_none() -> None:
    """An all-good cohort has no known-bad denominator -> rate is None, not 0.0.

    The unanimous-pass-on-known-bad rate is undefined with an empty denominator,
    so the reducer refuses to fabricate a zero.
    """
    rows = [_labeled_verdict(wave_id=f"P01-I01-W{i:02d}", ground_truth=True) for i in range(3)]
    ballots = {row.outcome.base_id: _all_pass() for row in rows}

    report = validate_jury(_cohort(rows), ballots, JuryValidationConfig(min_validation_n=1))

    assert report.status is JuryValidationStatus.SCORED
    assert report.known_bad_n == 0
    assert report.unanimous_pass_on_known_bad_rate is None


# --- C1: Brier + ECE against ground truth ----------------------------------


def test_validate_jury_perfect_calibration_brier_and_ece_zero() -> None:
    """A jury that unanimously+correctly votes every wave -> Brier 0, ECE 0.

    Good waves get a unanimous pass (forecast 1.0, outcome 1.0); bad waves get
    a unanimous fail (forecast 0.0, outcome 0.0). The forecast matches the
    ground truth exactly, so both calibration metrics are the floor.
    """
    rows = [
        _labeled_verdict(wave_id=f"P01-I01-W{i:02d}", ground_truth=i % 2 == 0) for i in range(4)
    ]
    ballots = {
        row.outcome.base_id: (_all_pass() if row.ground_truth else _all_fail()) for row in rows
    }

    report = validate_jury(_cohort(rows), ballots, JuryValidationConfig(min_validation_n=1))

    assert report.brier == pytest.approx(0.0)
    assert report.ece == pytest.approx(0.0)


def test_validate_jury_brier_tracks_split_forecast() -> None:
    """A 2-of-3 pass-fraction forecast on a known-bad wave -> Brier (2/3)**2.

    Two jurors pass, one fails -> forecast 2/3 the wave is good; the wave is in
    fact bad (outcome 0.0), so the single-wave Brier is ``(2/3 - 0)**2``.
    """
    row = _labeled_verdict(wave_id="P01-I01-W01", ground_truth=False)
    ballots = {
        row.outcome.base_id: _ballots(
            AgentReportVerdict.PASS, AgentReportVerdict.PASS, AgentReportVerdict.FAIL
        )
    }

    report = validate_jury(_cohort([row]), ballots, JuryValidationConfig(min_validation_n=1))

    assert report.brier == pytest.approx((2.0 / 3.0) ** 2)


def test_expected_calibration_error_bucket_correctness() -> None:
    """ECE bucket correctness: a single populated bucket reads its gap directly.

    Four forecasts of 0.9 (high-confidence) with three positive outcomes ->
    bucket mean forecast 0.9, observed frequency 0.75, so the (single-bucket)
    ECE is exactly ``|0.9 - 0.75| = 0.15``.
    """
    forecasts = [0.9, 0.9, 0.9, 0.9]
    outcomes = [1.0, 1.0, 1.0, 0.0]

    ece = expected_calibration_error(forecasts, outcomes, bins=10)

    assert ece == pytest.approx(0.15)


def test_expected_calibration_error_two_buckets_population_weighted() -> None:
    """ECE is population-weighted across buckets.

    Bucket A (forecast 0.1, three samples, observed 0.0) contributes
    ``(3/4) * |0.1 - 0.0|``; bucket B (forecast 0.9, one sample, observed 1.0)
    contributes ``(1/4) * |0.9 - 1.0|``. The total is their weighted sum.
    """
    forecasts = [0.1, 0.1, 0.1, 0.9]
    outcomes = [0.0, 0.0, 0.0, 1.0]

    ece = expected_calibration_error(forecasts, outcomes, bins=10)

    expected = (3.0 / 4.0) * abs(0.1 - 0.0) + (1.0 / 4.0) * abs(0.9 - 1.0)
    assert ece == pytest.approx(expected)


def test_expected_calibration_error_empty_raises() -> None:
    """An empty forecast set is a hard error -- there is nothing to calibrate."""
    with pytest.raises(ValueError, match="empty forecast set"):
        expected_calibration_error([], [])


def test_expected_calibration_error_length_mismatch_raises() -> None:
    """Mismatched forecast / outcome lengths raise with the offending counts."""
    with pytest.raises(ValueError, match="length mismatch"):
        expected_calibration_error([0.5, 0.5], [1.0])


def test_expected_calibration_error_zero_bins_raises() -> None:
    """A sub-1 bin count is a hard error -- ECE needs at least one bucket."""
    with pytest.raises(ValueError, match="at least 1 bin"):
        expected_calibration_error([0.5], [1.0], bins=0)


# --- C1: gold + silver scored as one labelled set --------------------------


def test_validate_jury_folds_silver_and_gold_into_one_set() -> None:
    """The silver + gold tiers are scored as one labelled set.

    A gold-tier row contributes to ``n`` and the metrics exactly like a silver
    one; the operator ground truth is already baked into each row.
    """
    silver = [_labeled_verdict(wave_id="P01-I01-W01", ground_truth=True)]
    gold_row = _labeled_verdict(wave_id="P01-I01-W02", ground_truth=False)
    gold = [
        LabeledVerdict(
            outcome=gold_row.outcome,
            ground_truth=gold_row.ground_truth,
            label_source=LabelSource.GOLD,
        )
    ]
    cohort = ValidationCohort(silver=silver, gold=gold)
    ballots = {"P01-I01-W01": _all_pass(), "P01-I01-W02": _all_fail()}

    report = validate_jury(cohort, ballots, JuryValidationConfig(min_validation_n=1))

    assert report.n == 2
    assert report.known_bad_n == 1
    assert report.status is JuryValidationStatus.SCORED


# --- C2: under-n -> all None + insufficient ---------------------------------


def test_validate_jury_under_n_all_metrics_none_insufficient() -> None:
    """C2: a cohort under ``min_validation_n`` -> every metric None + insufficient.

    The cohort has one labelled verdict but the floor is twenty, so the reducer
    refuses to score -- every numeric field is ``None`` and the status is the
    honest-negative surface, never a fabricated number.
    """
    row = _labeled_verdict(wave_id="P01-I01-W01", ground_truth=False)
    ballots = {row.outcome.base_id: _all_pass()}

    report = validate_jury(_cohort([row]), ballots)  # default min_validation_n=20

    assert report.status is JuryValidationStatus.INSUFFICIENT
    assert report.n == 1
    assert report.fleiss_kappa is None
    assert report.brier is None
    assert report.ece is None
    assert report.unanimous_pass_on_known_bad_rate is None
    # ``known_bad_n`` is structural metadata, not a scored metric, so it is real.
    assert report.known_bad_n == 1


def test_validate_jury_empty_cohort_insufficient() -> None:
    """An empty cohort is below any positive floor -> insufficient, all None."""
    report = validate_jury(_cohort([]), {})

    assert report.status is JuryValidationStatus.INSUFFICIENT
    assert report.n == 0
    assert report.fleiss_kappa is None
    assert report.brier is None
    assert report.ece is None
    assert report.unanimous_pass_on_known_bad_rate is None


# --- C2: no-ballot wave -> ValueError --------------------------------------


def test_validate_jury_wave_without_ballots_raises() -> None:
    """C2: a labelled wave with NO recorded ballots raises ValueError.

    The verdict asserts a jury reached a decision, but no ballots are on
    record -- a verdict claimed without a jury running, so the reducer raises
    rather than scoring a phantom jury.
    """
    rows = [_labeled_verdict(wave_id=f"P01-I01-W{i:02d}", ground_truth=True) for i in range(2)]
    # W01 has ballots; W00 is missing from the map entirely.
    ballots = {"P01-I01-W01": _all_pass()}

    with pytest.raises(ValueError, match="no recorded jury ballots: 'P01-I01-W00'"):
        validate_jury(_cohort(rows), ballots, JuryValidationConfig(min_validation_n=1))


def test_validate_jury_wave_with_empty_ballot_tuple_raises() -> None:
    """A labelled wave present with an EMPTY ballot tuple also raises.

    An empty ballot tuple is the same phantom-jury defect as an absent key: the
    verdict was claimed but no juror voted.
    """
    row = _labeled_verdict(wave_id="P01-I01-W01", ground_truth=True)
    ballots: dict[str, tuple[JurorBallot, ...]] = {"P01-I01-W01": ()}

    with pytest.raises(ValueError, match="no recorded jury ballots: 'P01-I01-W01'"):
        validate_jury(_cohort([row]), ballots, JuryValidationConfig(min_validation_n=1))


def test_validate_jury_no_ballots_raises_even_under_n() -> None:
    """The phantom-jury check fires even when the cohort is too small to score.

    A no-ballot wave is a hard error regardless of whether the cohort would
    otherwise refuse to score -- the ballots are resolved before the min-N gate.
    """
    row = _labeled_verdict(wave_id="P01-I01-W01", ground_truth=True)

    with pytest.raises(ValueError, match="no recorded jury ballots"):
        validate_jury(_cohort([row]), {})  # default floor=20, but raises first


# --- partial agreement gives a kappa below 1 -------------------------------


def test_validate_jury_split_ballots_kappa_below_one() -> None:
    """Jurors that split on waves -> Fleiss kappa strictly below 1.0.

    With per-wave disagreement the inter-juror agreement no longer saturates,
    so kappa drops under the perfect-agreement ceiling.
    """
    rows = [_labeled_verdict(wave_id=f"P01-I01-W{i:02d}", ground_truth=True) for i in range(3)]
    split = _ballots(AgentReportVerdict.PASS, AgentReportVerdict.PASS, AgentReportVerdict.FAIL)
    ballots = {row.outcome.base_id: split for row in rows}

    report = validate_jury(_cohort(rows), ballots, JuryValidationConfig(min_validation_n=1))

    assert report.fleiss_kappa is not None
    assert report.fleiss_kappa < 1.0


# --- model error paths -----------------------------------------------------


def test_jury_validation_report_rejects_extra_field() -> None:
    """An unexpected JuryValidationReport field fails ``extra='forbid'``."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        JuryValidationReport(
            n=0,
            status=JuryValidationStatus.INSUFFICIENT,
            unexpected="boom",
        )


def test_jury_validation_config_rejects_zero_floor() -> None:
    """A zero ``min_validation_n`` floor fails the ``ge=1`` bound.

    A zero floor would defeat the refuse-to-score guarantee, so it is rejected
    at construction.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        JuryValidationConfig(min_validation_n=0)


def test_jury_validation_config_rejects_extra_field() -> None:
    """An unexpected JuryValidationConfig field fails ``extra='forbid'``."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        JuryValidationConfig(unexpected="boom")
