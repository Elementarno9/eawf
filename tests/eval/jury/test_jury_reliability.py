"""Reliability-weighting tests for the jury reducer (P29-I05-W04).

The graded mean of :func:`~eawf.observability.eval.jury.aggregate_jury`
optionally weights each juror by its measured reliability, fed from the
reputation engine (:mod:`eawf.observability.eval.reputation`). These tests pin:

- the honest-negative / behavior-preserving path -- with ``reliability=None`` or
  an all-``INSUFFICIENT`` map the weighted mean equals the plain mean, so the
  reducer is identical to its pre-weighting self (the reputation engine scores
  no role today);
- the weighted mean diverges from the plain mean only when jurors carry
  DIFFERENT SCORED reliabilities, with a higher-LB juror pulling the mean toward
  its score;
- :func:`~eawf.observability.eval.jury.juror_weight` returns the neutral default
  for every unavailable-reliability case and the lower bound for a SCORED row;
- minority-veto stays conservative -- a single credible ``fail`` still vetoes
  regardless of that juror's (low) weight.
"""

from __future__ import annotations

from statistics import fmean

import pytest

from eawf.kernel.state.enums import AgentReportVerdict, AgentSessionRole
from eawf.observability.eval.jury import (
    NEUTRAL_JUROR_WEIGHT,
    JurorBallot,
    JuryAggregateOutcome,
    aggregate_jury,
    juror_weight,
)
from eawf.observability.eval.reputation import (
    ReliabilityStatus,
    RoleReliability,
)

_ROLE = AgentSessionRole.AUDITOR


def _scored(runtime: str, lower_bound: float) -> RoleReliability:
    """Build a SCORED reliability row whose display LB is *lower_bound*."""
    return RoleReliability(
        agent_role=_ROLE,
        runtime=runtime,
        n=42,
        status=ReliabilityStatus.SCORED,
        posterior_lower_bound=lower_bound,
    )


def _insufficient(runtime: str) -> RoleReliability:
    """Build an INSUFFICIENT reliability row (every numeric field ``None``)."""
    return RoleReliability(
        agent_role=_ROLE,
        runtime=runtime,
        n=3,
        status=ReliabilityStatus.INSUFFICIENT,
    )


def _graded(juror_id: str, score: float, runtime: str | None = None) -> JurorBallot:
    """Build a graded ballot, optionally tagged with ``(role, runtime)``."""
    return JurorBallot(
        juror_id=juror_id,
        acceptance_style="graded",
        score=score,
        agent_role=_ROLE if runtime is not None else None,
        runtime=runtime,
    )


# --- juror_weight ---------------------------------------------------------


def test_juror_weight_returns_neutral_for_none_reliability() -> None:
    """No reliability map supplied -> neutral weight."""
    ballot = _graded("j", 0.8, runtime="claude-code")

    assert juror_weight(ballot, None) == NEUTRAL_JUROR_WEIGHT


def test_juror_weight_returns_neutral_for_missing_role_and_runtime() -> None:
    """A ballot with no ``(agent_role, runtime)`` to match on -> neutral."""
    ballot = _graded("j", 0.8, runtime=None)
    reliability = {(_ROLE, "claude-code"): _scored("claude-code", 0.9)}

    assert juror_weight(ballot, reliability) == NEUTRAL_JUROR_WEIGHT


def test_juror_weight_returns_neutral_when_runtime_missing_from_ballot() -> None:
    """A role-only ballot (no runtime) cannot key the map -> neutral."""
    ballot = JurorBallot(
        juror_id="j",
        acceptance_style="graded",
        score=0.8,
        agent_role=_ROLE,
        runtime=None,
    )
    reliability = {(_ROLE, "claude-code"): _scored("claude-code", 0.9)}

    assert juror_weight(ballot, reliability) == NEUTRAL_JUROR_WEIGHT


def test_juror_weight_returns_neutral_for_unmatched_pair() -> None:
    """A SCORED map with no row for the ballot's pair -> neutral."""
    ballot = _graded("j", 0.8, runtime="codex")
    reliability = {(_ROLE, "claude-code"): _scored("claude-code", 0.9)}

    assert juror_weight(ballot, reliability) == NEUTRAL_JUROR_WEIGHT


def test_juror_weight_returns_neutral_for_insufficient_row() -> None:
    """An INSUFFICIENT matching row -> neutral (the honest-negative case)."""
    ballot = _graded("j", 0.8, runtime="claude-code")
    reliability = {(_ROLE, "claude-code"): _insufficient("claude-code")}

    assert juror_weight(ballot, reliability) == NEUTRAL_JUROR_WEIGHT


def test_juror_weight_returns_lower_bound_for_scored_row() -> None:
    """A SCORED matching row -> its conservative display lower bound."""
    ballot = _graded("j", 0.8, runtime="claude-code")
    reliability = {(_ROLE, "claude-code"): _scored("claude-code", 0.73)}

    assert juror_weight(ballot, reliability) == pytest.approx(0.73)


# --- behavior-preserving (honest-negative) --------------------------------


def test_aggregate_jury_graded_none_reliability_equals_plain_mean() -> None:
    """``reliability=None`` yields the same outcome + plain mean as before."""
    scores = (0.9, 0.85, 0.8)
    ballots = tuple(_graded(f"j{i}", s, runtime="claude-code") for i, s in enumerate(scores))

    baseline = aggregate_jury(ballots)
    weighted = aggregate_jury(ballots, reliability=None)

    assert weighted.outcome is JuryAggregateOutcome.PASS
    assert weighted.outcome is baseline.outcome
    assert weighted.mean_score == pytest.approx(fmean(scores))
    assert weighted.mean_score == pytest.approx(baseline.mean_score)


def test_aggregate_jury_graded_all_insufficient_equals_plain_mean() -> None:
    """An all-INSUFFICIENT map weights every juror neutrally -> plain mean.

    This is the honest-negative path that ships today: the reputation engine
    returns ``INSUFFICIENT`` for every role, so the weighted aggregate is
    identical to the unweighted one.
    """
    scores = (0.9, 0.85, 0.8)
    runtimes = ("claude-code", "codex", "opencode")
    ballots = tuple(
        _graded(f"j{i}", s, runtime=r)
        for i, (s, r) in enumerate(zip(scores, runtimes, strict=True))
    )
    reliability = {(_ROLE, r): _insufficient(r) for r in runtimes}

    weighted = aggregate_jury(ballots, reliability=reliability)
    baseline = aggregate_jury(ballots)

    assert weighted.outcome is baseline.outcome
    assert weighted.mean_score == pytest.approx(fmean(scores))
    assert weighted.mean_score == pytest.approx(baseline.mean_score)


def test_aggregate_jury_graded_all_equal_scored_weights_equals_plain_mean() -> None:
    """Boundary: equal SCORED weights collapse to the unweighted mean."""
    scores = (0.9, 0.85, 0.8)
    runtimes = ("claude-code", "codex", "opencode")
    ballots = tuple(
        _graded(f"j{i}", s, runtime=r)
        for i, (s, r) in enumerate(zip(scores, runtimes, strict=True))
    )
    # Every juror carries the SAME lower bound -> a uniform weight vector.
    reliability = {(_ROLE, r): _scored(r, 0.7) for r in runtimes}

    weighted = aggregate_jury(ballots, reliability=reliability)

    assert weighted.mean_score == pytest.approx(fmean(scores))


# --- weighting lights up with divergent SCORED reliabilities --------------


def test_aggregate_jury_graded_weighted_mean_differs_from_plain_mean() -> None:
    """Divergent SCORED weights pull the mean off the plain mean."""
    # High-LB juror scores high; low-LB juror scores low. The weighted mean
    # leans toward the high-LB juror's score, above the plain mean.
    ballots = (
        _graded("trusted", 0.9, runtime="claude-code"),
        _graded("shaky", 0.5, runtime="codex"),
    )
    reliability = {
        (_ROLE, "claude-code"): _scored("claude-code", 0.9),
        (_ROLE, "codex"): _scored("codex", 0.3),
    }

    weighted = aggregate_jury(ballots, reliability=reliability)

    plain_mean = fmean((0.9, 0.5))
    expected = (0.9 * 0.9 + 0.3 * 0.5) / (0.9 + 0.3)
    assert weighted.mean_score == pytest.approx(expected)
    assert weighted.mean_score > plain_mean


def test_aggregate_jury_graded_high_lb_juror_pulls_mean_toward_its_score() -> None:
    """A dominant high-LB juror drags the mean toward its own score."""
    ballots = (
        _graded("dominant", 0.95, runtime="claude-code"),
        _graded("weak1", 0.4, runtime="codex"),
        _graded("weak2", 0.45, runtime="opencode"),
    )
    reliability = {
        (_ROLE, "claude-code"): _scored("claude-code", 0.95),
        (_ROLE, "codex"): _scored("codex", 0.05),
        (_ROLE, "opencode"): _scored("opencode", 0.05),
    }

    weighted = aggregate_jury(ballots, reliability=reliability)

    plain_mean = fmean((0.95, 0.4, 0.45))
    # The high-LB juror dominates the weighted average toward 0.95.
    assert weighted.mean_score is not None
    assert weighted.mean_score > plain_mean
    assert weighted.mean_score > 0.7


def test_aggregate_jury_graded_unmatched_jurors_stay_neutral() -> None:
    """A SCORED row only reweights its matched juror; the rest stay neutral."""
    ballots = (
        _graded("matched", 0.9, runtime="claude-code"),
        _graded("unmatched", 0.6, runtime="codex"),
    )
    # Only the claude-code juror has a SCORED row; codex stays neutral (1.0).
    reliability = {(_ROLE, "claude-code"): _scored("claude-code", 0.5)}

    weighted = aggregate_jury(ballots, reliability=reliability)

    expected = (0.5 * 0.9 + NEUTRAL_JUROR_WEIGHT * 0.6) / (0.5 + NEUTRAL_JUROR_WEIGHT)
    assert weighted.mean_score == pytest.approx(expected)


# --- minority-veto stays conservative regardless of weight ----------------


def test_aggregate_jury_low_weight_juror_still_vetoes() -> None:
    """A single credible ``fail`` vetoes even with a low juror weight.

    Minority-veto is the conservative close-gate signal; one credible refutation
    sinks the vote regardless of that juror's reliability weight.
    """
    ballots = (
        JurorBallot(
            juror_id="vetoer",
            acceptance_style="binary",
            verdict=AgentReportVerdict.FAIL,
            agent_role=_ROLE,
            runtime="codex",
        ),
        JurorBallot(
            juror_id="passer",
            acceptance_style="binary",
            verdict=AgentReportVerdict.PASS,
            agent_role=_ROLE,
            runtime="claude-code",
        ),
    )
    # The vetoing juror has the lowest possible reliability weight.
    reliability = {
        (_ROLE, "codex"): _scored("codex", 0.0),
        (_ROLE, "claude-code"): _scored("claude-code", 0.99),
    }

    weighted = aggregate_jury(ballots, reliability=reliability)

    assert weighted.outcome is JuryAggregateOutcome.FAIL
    assert weighted.veto_count == 1


def test_aggregate_jury_mixed_weighted_graded_keeps_veto_dominant() -> None:
    """In a mixed vote, weighting the graded side never overrides a veto."""
    ballots = (
        JurorBallot(
            juror_id="vetoer",
            acceptance_style="binary",
            verdict=AgentReportVerdict.FAIL,
            agent_role=_ROLE,
            runtime="codex",
        ),
        _graded("grader", 0.95, runtime="claude-code"),
    )
    reliability = {
        (_ROLE, "codex"): _scored("codex", 0.1),
        (_ROLE, "claude-code"): _scored("claude-code", 0.95),
    }

    weighted = aggregate_jury(ballots, reliability=reliability)

    assert weighted.outcome is JuryAggregateOutcome.FAIL
    assert weighted.veto_count == 1
    assert weighted.mean_score is not None


# --- boundary cases -------------------------------------------------------


def test_aggregate_jury_single_graded_juror_weight_is_irrelevant() -> None:
    """Boundary: a lone graded juror's mean equals its own score, weighted or not."""
    ballot = _graded("solo", 0.8, runtime="claude-code")
    reliability = {(_ROLE, "claude-code"): _scored("claude-code", 0.3)}

    weighted = aggregate_jury((ballot,), reliability=reliability)

    assert weighted.mean_score == pytest.approx(0.8)


def test_aggregate_jury_empty_ballots_with_reliability_raises() -> None:
    """Error path: an empty jury still raises even with a reliability map."""
    reliability = {(_ROLE, "claude-code"): _scored("claude-code", 0.9)}

    with pytest.raises(ValueError, match="empty jury"):
        aggregate_jury((), reliability=reliability)


def test_juror_ballot_accepts_optional_role_and_runtime() -> None:
    """The new optional fields default to ``None`` and accept a role/runtime."""
    bare = JurorBallot(juror_id="j", acceptance_style="graded", score=0.5)
    assert bare.agent_role is None
    assert bare.runtime is None

    tagged = JurorBallot(
        juror_id="j",
        acceptance_style="graded",
        score=0.5,
        agent_role=_ROLE,
        runtime="claude-code",
    )
    assert tagged.agent_role is _ROLE
    assert tagged.runtime == "claude-code"
