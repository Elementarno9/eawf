"""Verbosity-bias + citation-faithfulness probe tests (P30-I09-W03).

The two probes validate the jury for *bias* rather than *agreement*:

- :func:`~eawf.observability.eval.jury_validation.measure_verbosity_bias`
  correlates each juror's pass signal with the judged artifact's byte-length and
  flags a length-preferring juror above the ceiling;
- :func:`~eawf.observability.eval.jury_validation.measure_faithfulness` scores
  whether each RESOLVING evidence ref actually entails the claim it cites, using
  the in-process lexical entailment scorer (never an LLM), and flags a
  resolving-but-non-entailing ref as unfaithful.

These tests pin the two binary success criteria:

- C1: the verbosity probe flags a juror whose pass-rate rises monotonically with
  artifact length above the ceiling; the faithfulness probe scores a ref that
  resolves on disk but does NOT entail its claim as unfaithful;
- C2: both probes refuse to score under ``min_validation_n``, returning
  None-gated reports (no fabricated correlations / rates).

Plus boundary (constant series, no-resolving ref, single observation) and error
(resolving ref with no evidence text, model ``extra='forbid'``) paths.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.observability.eval.jury_validation import (
    CitedEvidenceRef,
    EvidenceRefFaithfulness,
    FaithfulnessConfig,
    FaithfulnessReport,
    JurorLengthObservation,
    JurorVerbosityBias,
    ProbeStatus,
    VerbosityBiasConfig,
    VerbosityBiasReport,
    measure_faithfulness,
    measure_verbosity_bias,
)

# --- verbosity-bias helpers ------------------------------------------------


def _length_run(
    juror_id: str,
    *,
    lengths: list[int],
    passes: list[bool],
) -> list[JurorLengthObservation]:
    """Build one juror's stream of length observations from aligned lists."""
    return [
        JurorLengthObservation(juror_id=juror_id, artifact_bytes=length, passed=passed)
        for length, passed in zip(lengths, passes, strict=True)
    ]


def _monotone_length_preferring(juror_id: str) -> list[JurorLengthObservation]:
    """A juror whose pass-rate rises monotonically with artifact length.

    Short artifacts are refuted, long ones passed -- the verbosity-biased juror
    that confuses length for quality.
    """
    return _length_run(
        juror_id,
        lengths=[10, 20, 30, 40, 50, 60],
        passes=[False, False, False, True, True, True],
    )


# --- C1: verbosity probe flags a length-preferring juror -------------------


def test_measure_verbosity_bias_flags_length_preferring_juror() -> None:
    """C1: a juror passing longer artifacts above the ceiling is flagged.

    The juror's pass-rate rises monotonically with artifact length, so the
    length/pass rank correlation clears the ceiling and the juror is flagged
    length-preferring (verbosity-biased).
    """
    observations = _monotone_length_preferring("codex")

    report = measure_verbosity_bias(
        observations, VerbosityBiasConfig(min_validation_n=2, verbosity_bias_ceiling=0.5)
    )

    assert report.status is ProbeStatus.SCORED
    assert report.flagged_juror_ids == ("codex",)
    (row,) = report.jurors
    assert row.juror_id == "codex"
    assert row.length_preferring is True
    assert row.length_pass_correlation is not None
    assert row.length_pass_correlation > 0.5


def test_measure_verbosity_bias_does_not_flag_length_neutral_juror() -> None:
    """A juror whose pass signal does not track length is not flagged.

    The pass/refute pattern is interleaved across lengths, so the correlation
    stays below the ceiling -- no verbosity bias, no flag.
    """
    observations = _length_run(
        "claude-code",
        lengths=[10, 20, 30, 40, 50, 60],
        passes=[True, False, True, False, True, False],
    )

    report = measure_verbosity_bias(
        observations, VerbosityBiasConfig(min_validation_n=2, verbosity_bias_ceiling=0.5)
    )

    assert report.status is ProbeStatus.SCORED
    assert report.flagged_juror_ids == ()
    (row,) = report.jurors
    assert row.length_preferring is False


def test_measure_verbosity_bias_scores_each_juror_independently() -> None:
    """Two jurors are scored independently; only the biased one is flagged."""
    biased = _monotone_length_preferring("codex")
    neutral = _length_run(
        "opencode",
        lengths=[10, 20, 30, 40, 50, 60],
        passes=[True, False, True, False, True, False],
    )

    report = measure_verbosity_bias(
        biased + neutral, VerbosityBiasConfig(min_validation_n=2, verbosity_bias_ceiling=0.5)
    )

    assert report.status is ProbeStatus.SCORED
    assert report.flagged_juror_ids == ("codex",)
    assert {row.juror_id for row in report.jurors} == {"codex", "opencode"}


# --- C2: verbosity probe refuses to score under min_validation_n -----------


def test_measure_verbosity_bias_under_n_is_none_gated() -> None:
    """C2: an observation set below the floor -> insufficient, no jurors scored.

    The probe refuses to fabricate a correlation off a starved sample: the whole
    report is ``INSUFFICIENT`` with an empty jurors tuple.
    """
    observations = _monotone_length_preferring("codex")[:3]

    report = measure_verbosity_bias(observations, VerbosityBiasConfig(min_validation_n=20))

    assert report.status is ProbeStatus.INSUFFICIENT
    assert report.n == 3
    assert report.jurors == ()
    assert report.flagged_juror_ids == ()


def test_measure_verbosity_bias_empty_is_insufficient() -> None:
    """An empty observation set is below any positive floor -> insufficient."""
    report = measure_verbosity_bias([])

    assert report.status is ProbeStatus.INSUFFICIENT
    assert report.n == 0
    assert report.jurors == ()


# --- verbosity boundary: undefined correlation never fabricated ------------


def test_measure_verbosity_bias_constant_passes_correlation_none() -> None:
    """A juror that always passes has no rank variance -> correlation is None.

    A constant pass column makes the Spearman correlation undefined; the probe
    returns ``None`` rather than fabricating a zero, and the juror is unflagged.
    """
    observations = _length_run(
        "codex",
        lengths=[10, 20, 30, 40],
        passes=[True, True, True, True],
    )

    report = measure_verbosity_bias(
        observations, VerbosityBiasConfig(min_validation_n=2, verbosity_bias_ceiling=0.5)
    )

    assert report.status is ProbeStatus.SCORED
    (row,) = report.jurors
    assert row.length_pass_correlation is None
    assert row.length_preferring is False
    assert report.flagged_juror_ids == ()


def test_measure_verbosity_bias_single_observation_juror_is_none() -> None:
    """A juror with a single observation has no rank order -> correlation None.

    The probe still clears the total floor (other jurors carry the count), but a
    juror with fewer than two observations cannot be correlated, so its
    correlation stays ``None`` and it is never flagged.
    """
    lonely = [JurorLengthObservation(juror_id="lonely", artifact_bytes=99, passed=True)]
    filler = _monotone_length_preferring("codex")

    report = measure_verbosity_bias(
        lonely + filler, VerbosityBiasConfig(min_validation_n=2, verbosity_bias_ceiling=0.5)
    )

    lonely_row = next(row for row in report.jurors if row.juror_id == "lonely")
    assert lonely_row.n == 1
    assert lonely_row.length_pass_correlation is None
    assert lonely_row.length_preferring is False


# --- faithfulness helpers --------------------------------------------------

_FAITHFUL_CLAIM = "the resolver rejects absolute paths"
_FAITHFUL_EVIDENCE = "the resolver rejects absolute paths and file urls at the boundary"
_UNFAITHFUL_EVIDENCE = "license copyright notice all rights reserved years"


# --- C1: faithfulness probe flags a resolving-but-non-entailing ref --------


def test_measure_faithfulness_flags_resolving_non_entailing_ref() -> None:
    """C1: a ref that resolves on disk but does NOT entail its claim is unfaithful.

    The evidence text resolves (rung-1 pass) but shares no content with the
    claim, so the in-process lexical entailment falls below the floor -- a hollow
    citation flagged unfaithful.
    """
    cited = [
        CitedEvidenceRef(
            ref="docs/license.md",
            claim=_FAITHFUL_CLAIM,
            resolved=True,
            evidence_text=_UNFAITHFUL_EVIDENCE,
        )
    ]

    report = measure_faithfulness(cited, FaithfulnessConfig(min_validation_n=1))

    assert report.status is ProbeStatus.SCORED
    assert report.scored_n == 1
    assert report.unfaithful_n == 1
    assert report.unfaithful_rate == pytest.approx(1.0)
    (row,) = report.refs
    assert row.resolved is True
    assert row.scored is True
    assert row.faithful is False
    assert row.entailment_probability is not None
    assert row.entailment_probability < FaithfulnessConfig().entail_threshold


def test_measure_faithfulness_passes_a_genuinely_entailing_ref() -> None:
    """A resolving ref whose evidence entails its claim is faithful, not flagged."""
    cited = [
        CitedEvidenceRef(
            ref="src/resolve.py",
            claim=_FAITHFUL_CLAIM,
            resolved=True,
            evidence_text=_FAITHFUL_EVIDENCE,
        )
    ]

    report = measure_faithfulness(cited, FaithfulnessConfig(min_validation_n=1))

    assert report.status is ProbeStatus.SCORED
    assert report.scored_n == 1
    assert report.unfaithful_n == 0
    assert report.unfaithful_rate == pytest.approx(0.0)
    (row,) = report.refs
    assert row.faithful is True


def test_measure_faithfulness_unresolved_ref_carried_but_not_scored() -> None:
    """An unresolved ref is counted for ``n`` but never scored for entailment.

    A ref that does not resolve is a separate rung-1 failure, not an
    unfaithfulness, so it is carried unscored and does not enter the unfaithful
    denominator.
    """
    cited = [
        CitedEvidenceRef(ref="missing/path.py", claim=_FAITHFUL_CLAIM, resolved=False),
        CitedEvidenceRef(
            ref="src/resolve.py",
            claim=_FAITHFUL_CLAIM,
            resolved=True,
            evidence_text=_FAITHFUL_EVIDENCE,
        ),
    ]

    report = measure_faithfulness(cited, FaithfulnessConfig(min_validation_n=1))

    assert report.n == 2
    assert report.scored_n == 1
    unresolved_row = next(row for row in report.refs if not row.resolved)
    assert unresolved_row.scored is False
    assert unresolved_row.faithful is None
    assert unresolved_row.entailment_probability is None


# --- C2: faithfulness probe refuses to score under min_validation_n --------


def test_measure_faithfulness_under_n_is_none_gated() -> None:
    """C2: a cited-ref set below the floor -> insufficient, every rate None.

    The probe refuses to fabricate a faithfulness rate off a starved sample: the
    report is ``INSUFFICIENT`` with an empty refs tuple and a ``None`` rate.
    """
    cited = [
        CitedEvidenceRef(
            ref="docs/license.md",
            claim=_FAITHFUL_CLAIM,
            resolved=True,
            evidence_text=_UNFAITHFUL_EVIDENCE,
        )
    ]

    report = measure_faithfulness(cited)  # default min_validation_n=20

    assert report.status is ProbeStatus.INSUFFICIENT
    assert report.n == 1
    assert report.scored_n == 0
    assert report.unfaithful_n == 0
    assert report.unfaithful_rate is None
    assert report.refs == ()


def test_measure_faithfulness_empty_is_insufficient() -> None:
    """An empty cited-ref set is below any positive floor -> insufficient."""
    report = measure_faithfulness([])

    assert report.status is ProbeStatus.INSUFFICIENT
    assert report.n == 0
    assert report.unfaithful_rate is None


# --- faithfulness boundary: no resolving ref -> undefined rate -------------


def test_measure_faithfulness_no_resolving_ref_rate_is_none() -> None:
    """A cohort with no resolving ref has an empty denominator -> rate is None.

    The unfaithful rate is undefined with no scored ref, so the probe returns
    ``None`` rather than a fabricated zero.
    """
    cited = [
        CitedEvidenceRef(ref=f"missing/path-{i}.py", claim=_FAITHFUL_CLAIM, resolved=False)
        for i in range(3)
    ]

    report = measure_faithfulness(cited, FaithfulnessConfig(min_validation_n=1))

    assert report.status is ProbeStatus.SCORED
    assert report.scored_n == 0
    assert report.unfaithful_rate is None


# --- faithfulness error path -----------------------------------------------


def test_measure_faithfulness_resolving_ref_without_evidence_text_raises() -> None:
    """A resolving ref carrying no evidence text is a hard error.

    A resolving ref MUST supply its resolved premise, else the entailment cannot
    be scored -- the probe raises rather than silently skipping it.
    """
    cited = [
        CitedEvidenceRef(ref="src/resolve.py", claim=_FAITHFUL_CLAIM, resolved=True),
    ]

    with pytest.raises(ValueError, match=r"no evidence text: 'src/resolve\.py'"):
        measure_faithfulness(cited, FaithfulnessConfig(min_validation_n=1))


# --- model error paths -----------------------------------------------------


def test_verbosity_bias_report_rejects_extra_field() -> None:
    """An unexpected VerbosityBiasReport field fails ``extra='forbid'``."""
    with pytest.raises(ValidationError):
        VerbosityBiasReport(n=0, status=ProbeStatus.INSUFFICIENT, unexpected="boom")


def test_verbosity_bias_config_rejects_sub_two_floor() -> None:
    """A min_validation_n below 2 fails the ``ge=2`` bound.

    A single observation has no rank order to correlate, so a floor below two
    would admit a juror that cannot be scored.
    """
    with pytest.raises(ValidationError):
        VerbosityBiasConfig(min_validation_n=1)


def test_verbosity_bias_config_rejects_out_of_range_ceiling() -> None:
    """A ceiling outside ``[-1, 1]`` fails the correlation bound."""
    with pytest.raises(ValidationError):
        VerbosityBiasConfig(verbosity_bias_ceiling=1.5)


def test_juror_verbosity_bias_rejects_out_of_range_correlation() -> None:
    """A correlation outside ``[-1, 1]`` fails the bound at construction."""
    with pytest.raises(ValidationError):
        JurorVerbosityBias(juror_id="codex", n=4, length_pass_correlation=2.0)


def test_faithfulness_config_rejects_zero_floor() -> None:
    """A zero ``min_validation_n`` floor fails the ``ge=1`` bound."""
    with pytest.raises(ValidationError):
        FaithfulnessConfig(min_validation_n=0)


def test_faithfulness_report_rejects_extra_field() -> None:
    """An unexpected FaithfulnessReport field fails ``extra='forbid'``."""
    with pytest.raises(ValidationError):
        FaithfulnessReport(n=0, status=ProbeStatus.INSUFFICIENT, unexpected="boom")


def test_evidence_ref_faithfulness_rejects_out_of_range_probability() -> None:
    """An entailment probability outside ``[0, 1]`` fails the bound."""
    with pytest.raises(ValidationError):
        EvidenceRefFaithfulness(
            ref="src/resolve.py",
            claim=_FAITHFUL_CLAIM,
            resolved=True,
            scored=True,
            entailment_probability=1.5,
        )


def test_cited_evidence_ref_rejects_empty_claim() -> None:
    """An empty claim fails the ``min_length=1`` bound at construction."""
    with pytest.raises(ValidationError):
        CitedEvidenceRef(ref="src/resolve.py", claim="", resolved=False)
