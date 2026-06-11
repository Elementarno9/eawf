"""Staged advisory-to-block authority-gate tests (P30-I09-W04, TRUST-4).

The keystone :func:`~eawf.observability.eval.jury_validation.jury_block_authority`
decides whether a cross-vendor jury has EARNED the right to BLOCK a close
(rather than merely log an advisory veto). These tests pin success criterion C1:

- C1: ``jury_block_authority`` returns ``BLOCKING`` ONLY when all four trust
  conditions hold -- cohort cleared the labelled-wave floor, the Wilson / Beta
  lower bound on the known-bad catch rate clears its floor, the false-clean
  (unanimous-pass-on-known-bad) rate is below ceiling, and no juror is
  length-preferring. It DENIES authority (``ADVISORY``) when the data is thin,
  the blind-spot metric is hot, or the panel is length-preferring.

Plus the config-leaf boundary + the default-advisory honest-negative surface.
"""

from __future__ import annotations

import pytest

from eawf.observability.eval.jury_validation import (
    BlockAuthority,
    JuryAuthorityConfig,
    JuryValidationReport,
    JuryValidationStatus,
    ProbeStatus,
    VerbosityBiasReport,
    jury_block_authority,
)


def _scored_report(
    *,
    n: int = 40,
    known_bad_n: int = 20,
    false_clean_rate: float = 0.0,
) -> JuryValidationReport:
    """Build a SCORED validation report with the given cohort shape.

    A *false_clean_rate* of ``0.0`` over *known_bad_n* known-bad waves means the
    jury caught every known-bad wave -- the all-clear shape that, when the cohort
    is large enough, earns blocking authority.
    """
    return JuryValidationReport(
        n=n,
        status=JuryValidationStatus.SCORED,
        fleiss_kappa=0.95,
        brier=0.05,
        ece=0.05,
        unanimous_pass_on_known_bad_rate=false_clean_rate,
        known_bad_n=known_bad_n,
    )


def _scored_verbosity(*, flagged: tuple[str, ...] = ()) -> VerbosityBiasReport:
    """Build a SCORED verbosity report flagging *flagged* jurors length-preferring."""
    return VerbosityBiasReport(
        n=40,
        status=ProbeStatus.SCORED,
        jurors=(),
        flagged_juror_ids=flagged,
    )


# --- C1: all four conditions hold -> BLOCKING -----------------------------


def test_authority_blocking_when_all_four_conditions_hold() -> None:
    """C1: a large, high-catch, low-blind-spot, unbiased jury earns BLOCKING.

    Cohort cleared the floor (n=40 >= 20), every known-bad wave was caught
    (false-clean 0.0, so the Wilson LB on the catch rate clears 0.80), the
    blind-spot rate is below ceiling, and no juror is length-preferring -- the
    only shape that returns blocking authority.
    """
    report = _scored_report(n=40, known_bad_n=20, false_clean_rate=0.0)
    verbosity = _scored_verbosity()

    assert jury_block_authority(report, verbosity) is BlockAuthority.BLOCKING


# --- C1: each single condition failing -> ADVISORY ------------------------


def test_authority_advisory_when_cohort_thin() -> None:
    """C1: a cohort under min_labeled_waves denies authority (data thin).

    Every other condition would pass, but the cohort holds only 10 labelled
    waves below the default 20-wave floor, so the jury has not been validated on
    enough ground truth to trust its veto.
    """
    report = _scored_report(n=10, known_bad_n=10, false_clean_rate=0.0)
    verbosity = _scored_verbosity()

    assert jury_block_authority(report, verbosity) is BlockAuthority.ADVISORY


def test_authority_advisory_when_report_insufficient() -> None:
    """C1: an INSUFFICIENT validation report denies authority (no scored data).

    A report that refused to score (status INSUFFICIENT, every metric None) can
    never grant blocking -- the absence of evidence is held advisory.
    """
    report = JuryValidationReport(
        n=5,
        status=JuryValidationStatus.INSUFFICIENT,
        known_bad_n=0,
    )
    verbosity = _scored_verbosity()

    assert jury_block_authority(report, verbosity) is BlockAuthority.ADVISORY


def test_authority_advisory_when_no_known_bad_wave() -> None:
    """C1: a cohort with no known-bad wave denies authority (undefined catch rate).

    With zero known-bad waves the catch rate is undefined (an empty
    denominator), so the jury's blind-spot performance is unvalidated and the
    veto is held advisory rather than fabricating a catch rate.
    """
    report = JuryValidationReport(
        n=40,
        status=JuryValidationStatus.SCORED,
        fleiss_kappa=0.95,
        brier=0.05,
        ece=0.05,
        unanimous_pass_on_known_bad_rate=None,
        known_bad_n=0,
    )
    verbosity = _scored_verbosity()

    assert jury_block_authority(report, verbosity) is BlockAuthority.ADVISORY


def test_authority_advisory_when_catch_lb_below_floor() -> None:
    """C1: a catch-rate LB below floor denies authority (thin known-bad subset).

    The cohort cleared the labelled-wave floor and the blind-spot rate would
    pass, but only 4 known-bad waves all caught gives a conservative Wilson LB
    below 0.80 -- a small-but-lucky sample cannot fast-track authority.
    """
    report = _scored_report(n=40, known_bad_n=4, false_clean_rate=0.0)
    verbosity = _scored_verbosity()

    assert jury_block_authority(report, verbosity) is BlockAuthority.ADVISORY


def test_authority_advisory_when_blind_spot_hot() -> None:
    """C1: a false-clean rate at or above ceiling denies authority (hot blind spot).

    A jury that unanimously waves through 15% of known-bad waves
    (false-clean 0.15 >= the 0.10 ceiling) is denied authority even though the
    cohort is large -- a hot blind spot disqualifies the panel.
    """
    report = _scored_report(n=60, known_bad_n=40, false_clean_rate=0.15)
    verbosity = _scored_verbosity()

    assert jury_block_authority(report, verbosity) is BlockAuthority.ADVISORY


def test_authority_advisory_when_panel_length_preferring() -> None:
    """C1: a length-preferring juror denies authority (verbosity-biased panel).

    Every metric clears, but one juror is flagged length-preferring by the
    verbosity probe, so the panel confuses length for quality and its veto is
    held advisory until the bias clears.
    """
    report = _scored_report(n=40, known_bad_n=20, false_clean_rate=0.0)
    verbosity = _scored_verbosity(flagged=("codex",))

    assert jury_block_authority(report, verbosity) is BlockAuthority.ADVISORY


def test_authority_advisory_when_verbosity_probe_unscored() -> None:
    """C1: an INSUFFICIENT verbosity probe denies authority (bias unvalidated).

    The validation report clears every floor, but the verbosity probe refused to
    score (too few observations), so the panel's verbosity bias is unknown and
    the veto is held advisory rather than assuming the panel is unbiased.
    """
    report = _scored_report(n=40, known_bad_n=20, false_clean_rate=0.0)
    verbosity = VerbosityBiasReport(n=1, status=ProbeStatus.INSUFFICIENT)

    assert jury_block_authority(report, verbosity) is BlockAuthority.ADVISORY


# --- config-leaf boundary + override --------------------------------------


def test_authority_config_floor_override_relaxes_floor() -> None:
    """A relaxed config floor lets a smaller cohort earn blocking authority.

    The default min_labeled_waves of 20 would deny a 12-wave cohort, but a
    profile that relaxes the floor to 10 (and the catch LB to 0.50) earns
    blocking on the same cohort -- the floors are config-driven, not hardcoded.
    """
    report = _scored_report(n=12, known_bad_n=12, false_clean_rate=0.0)
    verbosity = _scored_verbosity()
    config = JuryAuthorityConfig(min_labeled_waves=10, known_bad_catch_lb_floor=0.50)

    assert jury_block_authority(report, verbosity, config) is BlockAuthority.BLOCKING


def test_authority_config_rejects_zero_min_labeled_waves() -> None:
    """A zero labelled-wave floor fails validation at the load boundary.

    ``min_labeled_waves`` is ``Field(ge=1)`` -- a zero floor would defeat the
    earned-authority guarantee, so it is rejected as a ValidationError.
    """
    with pytest.raises(ValueError, match="min_labeled_waves"):
        JuryAuthorityConfig(min_labeled_waves=0)


def test_authority_config_rejects_out_of_range_catch_floor() -> None:
    """A catch-rate floor above 1.0 fails validation at the load boundary."""
    with pytest.raises(ValueError, match="known_bad_catch_lb_floor"):
        JuryAuthorityConfig(known_bad_catch_lb_floor=1.5)
