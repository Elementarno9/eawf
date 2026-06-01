"""Fixture-driven tests for the ``aggregate_jury`` reducer (P29-I01-W06).

The reducer is validated against committed ballot fixtures under
``fixtures/`` — no live agent spawn. Each fixture is a self-describing
ballot set plus the expected aggregate outcome, mirroring the recorded-wave
corpus convention. Boundary cases (empty, single, unanimous, tie/split,
mixed binary+graded) and the error-path coupling validator are exercised
directly so a later live-jury caller can consume the reducer unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.observability.eval.jury import (
    CONSENSUS_SPREAD,
    FAIL_SCORE_THRESHOLD,
    PASS_SCORE_THRESHOLD,
    JurorBallot,
    JuryAggregateOutcome,
    aggregate_jury,
)

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


def _load_fixture(name: str) -> dict[str, object]:
    raw = json.loads((_FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _ballots_from_fixture(fixture: dict[str, object]) -> tuple[JurorBallot, ...]:
    rows = fixture["ballots"]
    assert isinstance(rows, list)
    return tuple(JurorBallot.model_validate(row) for row in rows)


def _all_fixture_names() -> tuple[str, ...]:
    return tuple(sorted(path.stem for path in _FIXTURE_DIR.glob("*.json")))


@pytest.mark.parametrize("fixture_name", _all_fixture_names())
def test_aggregate_jury_matches_fixture_outcome(fixture_name: str) -> None:
    """Every committed fixture reduces to its declared outcome + veto count."""
    fixture = _load_fixture(fixture_name)
    ballots = _ballots_from_fixture(fixture)

    result = aggregate_jury(ballots)

    assert result.outcome.value == fixture["expected_outcome"]
    assert result.veto_count == fixture["expected_veto_count"]
    assert result.ballot_count == len(ballots)


def test_aggregate_jury_binary_unanimous_pass_has_no_reasons() -> None:
    """A clean unanimous binary pass carries an empty reasons tuple."""
    ballots = _ballots_from_fixture(_load_fixture("binary-unanimous-pass"))

    result = aggregate_jury(ballots)

    assert result.outcome is JuryAggregateOutcome.PASS
    assert result.acceptance_style == "binary"
    assert result.reasons == ()
    assert result.mean_score is None


def test_aggregate_jury_minority_veto_reports_veto_count() -> None:
    """A single veto among passes fails the vote and names the veto count."""
    ballots = _ballots_from_fixture(_load_fixture("binary-minority-veto"))

    result = aggregate_jury(ballots)

    assert result.outcome is JuryAggregateOutcome.FAIL
    assert result.veto_count == 1
    assert any("minority-veto" in reason for reason in result.reasons)


def test_aggregate_jury_blocked_counts_as_veto() -> None:
    """A ``blocked`` ballot is a veto, not a mere non-pass."""
    ballots = _ballots_from_fixture(_load_fixture("binary-blocked-veto"))

    result = aggregate_jury(ballots)

    assert result.outcome is JuryAggregateOutcome.FAIL
    assert result.veto_count == 1


def test_aggregate_jury_binary_split_no_veto_needs_user() -> None:
    """A pass / pass-with-followups split with no veto routes to needs_user."""
    ballots = _ballots_from_fixture(_load_fixture("binary-split-no-veto"))

    result = aggregate_jury(ballots)

    assert result.outcome is JuryAggregateOutcome.NEEDS_USER
    assert result.veto_count == 0


def test_aggregate_jury_graded_consensus_pass_reports_mean() -> None:
    """A high, tight graded vote passes on the mean and reports it."""
    ballots = _ballots_from_fixture(_load_fixture("graded-consensus-pass"))

    result = aggregate_jury(ballots)

    assert result.outcome is JuryAggregateOutcome.PASS
    assert result.acceptance_style == "graded"
    assert result.mean_score is not None
    assert result.mean_score == pytest.approx((0.9 + 0.85 + 0.8) / 3.0)
    assert result.mean_score >= PASS_SCORE_THRESHOLD


def test_aggregate_jury_graded_consensus_fail_reports_mean() -> None:
    """A low, tight graded vote fails on the mean."""
    ballots = _ballots_from_fixture(_load_fixture("graded-consensus-fail"))

    result = aggregate_jury(ballots)

    assert result.outcome is JuryAggregateOutcome.FAIL
    assert result.mean_score is not None
    assert result.mean_score <= FAIL_SCORE_THRESHOLD


def test_aggregate_jury_graded_midband_needs_user() -> None:
    """A graded mean in the indeterminate band signals no consensus."""
    ballots = _ballots_from_fixture(_load_fixture("graded-midband-split"))

    result = aggregate_jury(ballots)

    assert result.outcome is JuryAggregateOutcome.NEEDS_USER
    assert result.mean_score is not None
    assert FAIL_SCORE_THRESHOLD < result.mean_score < PASS_SCORE_THRESHOLD


def test_aggregate_jury_graded_high_variance_needs_user() -> None:
    """A wide graded spread routes to needs_user even when the mean clears."""
    ballots = _ballots_from_fixture(_load_fixture("graded-high-variance"))

    result = aggregate_jury(ballots)

    assert result.outcome is JuryAggregateOutcome.NEEDS_USER
    assert result.score_spread is not None
    assert result.score_spread > CONSENSUS_SPREAD
    # Mean alone would have cleared the pass threshold; the spread overrides.
    assert result.mean_score is not None
    assert result.mean_score >= PASS_SCORE_THRESHOLD


def test_aggregate_jury_mixed_veto_dominates_high_graded_mean() -> None:
    """A binary veto sinks a mixed vote despite a high graded mean."""
    ballots = _ballots_from_fixture(_load_fixture("mixed-veto-dominates"))

    result = aggregate_jury(ballots)

    assert result.outcome is JuryAggregateOutcome.FAIL
    assert result.acceptance_style is None
    assert result.veto_count == 1
    assert result.mean_score is not None
    assert result.mean_score >= PASS_SCORE_THRESHOLD


def test_aggregate_jury_mixed_both_pass() -> None:
    """A mixed vote passes only when both the binary and graded sides clear."""
    ballots = _ballots_from_fixture(_load_fixture("mixed-both-pass"))

    result = aggregate_jury(ballots)

    assert result.outcome is JuryAggregateOutcome.PASS
    assert result.acceptance_style is None
    assert result.veto_count == 0


def test_aggregate_jury_single_binary_pass() -> None:
    """Boundary: a jury of one passing ballot resolves to pass."""
    ballots = _ballots_from_fixture(_load_fixture("single-binary-pass"))

    result = aggregate_jury(ballots)

    assert result.outcome is JuryAggregateOutcome.PASS
    assert result.ballot_count == 1


def test_aggregate_jury_single_binary_veto() -> None:
    """Boundary: a jury of one veto ballot fails."""
    ballots = _ballots_from_fixture(_load_fixture("single-binary-veto"))

    result = aggregate_jury(ballots)

    assert result.outcome is JuryAggregateOutcome.FAIL
    assert result.ballot_count == 1
    assert result.veto_count == 1


def test_aggregate_jury_single_graded_pass() -> None:
    """Boundary: a single graded ballot at the threshold passes; spread is 0."""
    ballots = (JurorBallot(juror_id="solo", acceptance_style="graded", score=PASS_SCORE_THRESHOLD),)

    result = aggregate_jury(ballots)

    assert result.outcome is JuryAggregateOutcome.PASS
    assert result.score_spread == pytest.approx(0.0)


def test_aggregate_jury_empty_ballots_raises() -> None:
    """Error path: an empty jury cannot resolve and raises ValueError."""
    with pytest.raises(ValueError, match="empty jury"):
        aggregate_jury(())


def test_juror_ballot_binary_requires_verdict() -> None:
    """Error path: a binary ballot without a verdict fails validation."""
    with pytest.raises(ValidationError, match="binary ballot requires verdict"):
        JurorBallot(juror_id="j", acceptance_style="binary")


def test_juror_ballot_binary_rejects_score() -> None:
    """Error path: a binary ballot carrying a score fails validation."""
    with pytest.raises(ValidationError, match="binary ballot must not carry score"):
        JurorBallot(juror_id="j", acceptance_style="binary", verdict="pass", score=0.9)


def test_juror_ballot_graded_requires_score() -> None:
    """Error path: a graded ballot without a score fails validation."""
    with pytest.raises(ValidationError, match="graded ballot requires score"):
        JurorBallot(juror_id="j", acceptance_style="graded")


def test_juror_ballot_graded_rejects_verdict() -> None:
    """Error path: a graded ballot carrying a verdict fails validation."""
    with pytest.raises(ValidationError, match="graded ballot must not carry verdict"):
        JurorBallot(juror_id="j", acceptance_style="graded", score=0.5, verdict="pass")


def test_juror_ballot_score_out_of_range_raises() -> None:
    """Error path: a graded score above 1.0 fails the field bound."""
    with pytest.raises(ValidationError):
        JurorBallot(juror_id="j", acceptance_style="graded", score=1.5)


def test_juror_ballot_rejects_extra_keys() -> None:
    """Error path: an unknown ballot key fails the extra='forbid' guard."""
    with pytest.raises(ValidationError):
        JurorBallot.model_validate(
            {"juror_id": "j", "acceptance_style": "binary", "verdict": "pass", "weight": 2.0}
        )
