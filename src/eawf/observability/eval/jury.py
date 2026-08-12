"""Pure reducer over juror ballots.

The :func:`aggregate_jury` reducer collapses N independent juror ballots
into a single :class:`JuryAggregate`. It is the "gate machinery" that
:data:`eawf.kernel.spec.common.CriterionEvidenceKind` ``"jury"`` defers
the minority-veto policy to: a vote of multiple agent reviewers resolves
through this reducer rather than a parallel ad-hoc rule.

Two acceptance styles flow through the same reducer, mirroring
:data:`~eawf.kernel.spec.common.CriterionAcceptanceStyle`:

- **binary** ballots carry an :class:`~eawf.kernel.state.enums.AgentReportVerdict`.
  The aggregate applies **minority-veto**: a single ``FAIL`` / ``BLOCKED``
  ballot (a veto) sinks the whole vote to :attr:`JuryAggregateOutcome.FAIL`.
  A unanimous ``PASS`` clears to :attr:`JuryAggregateOutcome.PASS`. A split
  with no veto (e.g. ``PASS`` mixed with ``PASS_WITH_FOLLOWUPS``) has no
  resolvable aggregate, so it routes to :attr:`JuryAggregateOutcome.NEEDS_USER`.
- **graded** ballots carry a continuous ``score`` in ``[0.0, 1.0]``. The
  aggregate is the **mean** of the scores, thresholded: a mean at or above
  :data:`PASS_SCORE_THRESHOLD` with a spread inside
  :data:`CONSENSUS_SPREAD` clears to ``PASS``; a mean at or below
  :data:`FAIL_SCORE_THRESHOLD` (same spread bound) sinks to ``FAIL``. A mean
  in the indeterminate mid-band, or a spread wide enough to signal no
  consensus (high variance), routes to ``NEEDS_USER``.

A **mixed** binary+graded ballot set keeps minority-veto as the dominant
signal: any binary veto sinks the vote regardless of the graded mean.
Absent a veto, both the binary side and the graded side must independently
resolve to pass for the aggregate to pass; any indeterminacy on either axis
routes to ``NEEDS_USER``.

**Reliability weighting.** The graded mean optionally weights
each juror by its measured reliability, fed from the reputation engine
(:mod:`eawf.observability.eval.reputation`). When :func:`aggregate_jury`
receives a *reliability* map, the graded outcome uses a weighted mean
``sum(w_i * s_i) / sum(w_i)`` -- a juror with a higher conservative held-rate
lower bound pulls the mean toward its score. A juror whose reliability is
unavailable (no matching row, an ``INSUFFICIENT`` row, or a ballot carrying no
``(agent_role, runtime)`` to match on) falls back to a neutral
:data:`NEUTRAL_JUROR_WEIGHT`. When every weight is neutral -- including the
honest-negative case today, where the reputation engine scores no role -- the
weighted mean is identical to the plain :func:`~statistics.fmean`, so the
weighting is behavior-preserving until SCORED reliability accrues. The
**binary minority-veto is never down-weighted**: a single credible ``fail`` /
``blocked`` ballot still vetoes regardless of that juror's weight, because one
credible refutation is the conservative close-gate signal.

The reducer is **pure** — no live agent spawn, no I/O. A later live-jury
caller (TRUST-7) convenes real jurors and feeds their ballots in unchanged.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from enum import StrEnum
from statistics import fmean

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.kernel.spec.common import CriterionAcceptanceStyle
from eawf.kernel.state.enums import AgentReportVerdict, AgentSessionRole
from eawf.observability.eval.reputation import ReliabilityStatus, RoleReliability

#: Mean at or above this clears a graded vote to ``PASS`` (when the spread
#: is within :data:`CONSENSUS_SPREAD`). Mirrors the ``0.85`` floor the
#: skill-eval scorer uses, relaxed to ``0.7`` so a jury of human-graded
#: ballots is not held to the byte-equality bar a golden-fixture score is.
PASS_SCORE_THRESHOLD: float = 0.7

#: Mean at or below this sinks a graded vote to ``FAIL`` (same spread bound).
#: The open band ``(FAIL_SCORE_THRESHOLD, PASS_SCORE_THRESHOLD)`` is the
#: indeterminate mid-band that routes to ``NEEDS_USER``.
FAIL_SCORE_THRESHOLD: float = 0.4

#: Maximum graded spread (``max - min``) tolerated before the jury is read
#: as having no consensus. A spread above this routes to ``NEEDS_USER`` even
#: when the mean would otherwise clear, because wide disagreement is itself
#: the signal that a human must adjudicate.
CONSENSUS_SPREAD: float = 0.5

#: Binary verdicts that count as a veto under minority-veto. A single veto
#: ballot sinks the whole vote.
_VETO_VERDICTS: frozenset[AgentReportVerdict] = frozenset(
    {AgentReportVerdict.FAIL, AgentReportVerdict.BLOCKED}
)

#: Weight given to a juror whose reliability is unavailable -- no matching
#: :class:`~eawf.observability.eval.reputation.RoleReliability` row, an
#: ``INSUFFICIENT`` row, or a ballot carrying no ``(agent_role, runtime)`` to
#: match on. A uniform neutral weight makes the weighted mean collapse to the
#: plain mean when every juror is neutral (the honest-negative case today), so
#: reliability weighting is behavior-preserving until SCORED rows accrue.
NEUTRAL_JUROR_WEIGHT: float = 1.0


class JuryAggregateOutcome(StrEnum):
    """Outcome of a reduced jury vote.

    Distinct from :class:`~eawf.kernel.state.enums.AgentReportVerdict`: a
    single juror casts an ``AgentReportVerdict`` (or a graded score), but the
    *aggregate* may be unresolvable, which a single verdict cannot express.
    :attr:`NEEDS_USER` reuses the envelope ``needs_user`` status word so a
    downstream caller can route an unresolvable vote straight to the
    operator-pause surface.
    """

    PASS = "pass"
    FAIL = "fail"
    NEEDS_USER = "needs_user"


class JurorBallot(BaseModel):
    """One juror's vote on a single criterion.

    A ballot is either binary (carries :attr:`verdict`, leaves :attr:`score`
    ``None``) or graded (carries :attr:`score`, leaves :attr:`verdict`
    ``None``); :attr:`acceptance_style` declares which and the
    :meth:`_check_style_payload` validator enforces the coupling so a
    malformed ballot fails at construction rather than silently skewing the
    aggregate.

    Attributes:
        juror_id: Stable identifier for the juror. Bounded so it stays
            scannable in a dense ballot dump.
        acceptance_style: ``"binary"`` or ``"graded"`` — reuses
            :data:`~eawf.kernel.spec.common.CriterionAcceptanceStyle` rather
            than defining a parallel vocabulary.
        verdict: The juror's verdict for a binary ballot; ``None`` for a
            graded ballot.
        score: The juror's score in ``[0.0, 1.0]`` for a graded ballot;
            ``None`` for a binary ballot.
        agent_role: Role of the agent that cast the ballot, used to match the
            ballot to its
            :class:`~eawf.observability.eval.reputation.RoleReliability` row for
            reliability weighting. ``None`` (the default) means the ballot is
            not matched to a reliability row and is given the neutral weight, so
            every existing construction site is unaffected.
        runtime: Runtime adapter id that cast the ballot, the second half of the
            ``(agent_role, runtime)`` reliability join key. ``None`` (the
            default) means the ballot is given the neutral weight.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    juror_id: str = Field(min_length=1, max_length=72)
    acceptance_style: CriterionAcceptanceStyle
    verdict: AgentReportVerdict | None = None
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    agent_role: AgentSessionRole | None = None
    runtime: str | None = None

    @model_validator(mode="after")
    def _check_style_payload(self) -> JurorBallot:
        """Enforce the binary↔verdict / graded↔score coupling.

        Raises:
            ValueError: When a binary ballot omits ``verdict`` or carries a
                ``score``, or a graded ballot omits ``score`` or carries a
                ``verdict``.
        """
        if self.acceptance_style == "binary":
            if self.verdict is None:
                raise ValueError("binary ballot requires verdict")
            if self.score is not None:
                raise ValueError("binary ballot must not carry score")
        else:  # "graded"
            if self.score is None:
                raise ValueError("graded ballot requires score")
            if self.verdict is not None:
                raise ValueError("graded ballot must not carry verdict")
        return self


class JuryAggregate(BaseModel):
    """Reduced outcome over a set of :class:`JurorBallot` rows.

    Attributes:
        outcome: The resolved :class:`JuryAggregateOutcome`.
        acceptance_style: ``"binary"`` when every ballot is binary,
            ``"graded"`` when every ballot is graded, ``None`` for a mixed
            ballot set (the reducer still resolves an outcome, but no single
            style describes the input).
        ballot_count: Number of ballots reduced (``>= 1``).
        veto_count: Number of binary veto ballots (``FAIL`` / ``BLOCKED``).
        mean_score: Mean of the graded scores, or ``None`` when no ballot was
            graded.
        score_spread: ``max - min`` of the graded scores, or ``None`` when no
            ballot was graded.
        reasons: One short string per signal that drove the outcome, in the
            order they were evaluated. Empty only for a clean unanimous pass.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: JuryAggregateOutcome
    acceptance_style: CriterionAcceptanceStyle | None
    ballot_count: int = Field(ge=1)
    veto_count: int = Field(ge=0)
    mean_score: float | None = None
    score_spread: float | None = None
    reasons: tuple[str, ...] = ()


def juror_weight(
    ballot: JurorBallot,
    reliability: Mapping[tuple[AgentSessionRole, str], RoleReliability] | None,
) -> float:
    """Return the reliability weight for one juror's ballot.

    The natural juror weight is the conservative DISPLAY held-rate of its
    matching :class:`~eawf.observability.eval.reputation.RoleReliability` --
    :attr:`~eawf.observability.eval.reputation.RoleReliability.posterior_lower_bound`,
    which never overstates a role that got lucky on a tiny sample. The weight is
    that lower bound exactly when the ballot carries an ``(agent_role, runtime)``
    pair, *reliability* holds a row for that pair, the row is
    :attr:`~eawf.observability.eval.reputation.ReliabilityStatus.SCORED`, and its
    lower bound is not ``None``. Every other case falls back to
    :data:`NEUTRAL_JUROR_WEIGHT`:

    - *reliability* is ``None`` (no map supplied);
    - the ballot carries no ``agent_role`` or no ``runtime`` to match on;
    - no row exists for the ballot's ``(agent_role, runtime)`` pair;
    - the matching row is ``INSUFFICIENT`` (the honest-negative surface today --
      the reputation engine scores no role yet) or has a ``None`` lower bound.

    Args:
        ballot: The juror ballot to weight.
        reliability: Map from ``(agent_role, runtime)`` to the role's
            :class:`~eawf.observability.eval.reputation.RoleReliability`, or
            ``None`` to weight every juror neutrally.

    Returns:
        The juror's weight: the SCORED row's ``posterior_lower_bound`` when
        available, else :data:`NEUTRAL_JUROR_WEIGHT`.
    """
    if reliability is None or ballot.agent_role is None or ballot.runtime is None:
        return NEUTRAL_JUROR_WEIGHT
    row = reliability.get((ballot.agent_role, ballot.runtime))
    if row is None or row.status is not ReliabilityStatus.SCORED:
        return NEUTRAL_JUROR_WEIGHT
    if row.posterior_lower_bound is None:
        return NEUTRAL_JUROR_WEIGHT
    return row.posterior_lower_bound


def _resolve_binary(
    verdicts: tuple[AgentReportVerdict, ...],
) -> tuple[JuryAggregateOutcome, tuple[str, ...]]:
    """Resolve the binary (minority-veto) side of a vote.

    Returns the outcome plus the reasons. A single veto dominates; absent a
    veto, a unanimous ``PASS`` clears and any other split is unresolvable.
    """
    vetoes = tuple(v for v in verdicts if v in _VETO_VERDICTS)
    if vetoes:
        return JuryAggregateOutcome.FAIL, (
            f"minority-veto: {len(vetoes)} of {len(verdicts)} binary ballots vetoed",
        )
    if all(v is AgentReportVerdict.PASS for v in verdicts):
        return JuryAggregateOutcome.PASS, ()
    return JuryAggregateOutcome.NEEDS_USER, ("binary split with no veto: no clean consensus",)


def _weighted_mean(scores: tuple[float, ...], weights: tuple[float, ...]) -> float:
    """Return the reliability-weighted mean of *scores*.

    The weighted mean is ``sum(w_i * s_i) / sum(w_i)``. When every weight is
    equal (the neutral-weight case, including the honest-negative path today)
    this is exactly the plain :func:`~statistics.fmean`. A degenerate all-zero
    weight vector cannot down-weight every juror to nothing, so it falls back to
    the unweighted mean rather than dividing by zero.
    """
    total_weight = math.fsum(weights)
    if total_weight <= 0.0:
        return fmean(scores)
    return math.fsum(w * s for w, s in zip(weights, scores, strict=True)) / total_weight


def _resolve_graded(
    scores: tuple[float, ...],
    weights: tuple[float, ...],
) -> tuple[JuryAggregateOutcome, float, float, tuple[str, ...]]:
    """Resolve the graded (mean) side of a vote.

    Returns the outcome, the mean, the spread, and the reasons. The mean is the
    reliability-weighted mean of *scores* (see :func:`_weighted_mean`); with all
    weights neutral it equals the plain :func:`~statistics.fmean`. The spread
    stays the raw ``max - min`` of the scores -- weighting tunes the central
    estimate, but disagreement among jurors is the consensus signal and must not
    be diluted by a juror's weight. A spread above :data:`CONSENSUS_SPREAD`
    signals no consensus regardless of the mean.
    """
    mean = _weighted_mean(scores, weights)
    spread = max(scores) - min(scores)
    if spread > CONSENSUS_SPREAD:
        return (
            JuryAggregateOutcome.NEEDS_USER,
            mean,
            spread,
            (f"graded spread {spread:.3f} exceeds consensus bound {CONSENSUS_SPREAD}",),
        )
    if mean >= PASS_SCORE_THRESHOLD:
        return JuryAggregateOutcome.PASS, mean, spread, ()
    if mean <= FAIL_SCORE_THRESHOLD:
        return (
            JuryAggregateOutcome.FAIL,
            mean,
            spread,
            (f"graded mean {mean:.3f} at or below fail threshold {FAIL_SCORE_THRESHOLD}",),
        )
    return (
        JuryAggregateOutcome.NEEDS_USER,
        mean,
        spread,
        (f"graded mean {mean:.3f} in indeterminate band: no consensus",),
    )


def _combine_mixed(
    binary_outcome: JuryAggregateOutcome,
    graded_outcome: JuryAggregateOutcome,
) -> JuryAggregateOutcome:
    """Combine the two sides of a mixed binary+graded vote.

    Minority-veto dominates: a binary ``FAIL`` sinks the aggregate regardless
    of the graded side. Absent a veto, both sides must independently pass for
    the aggregate to pass; any indeterminacy routes to ``NEEDS_USER``.
    """
    if binary_outcome is JuryAggregateOutcome.FAIL:
        return JuryAggregateOutcome.FAIL
    if binary_outcome is JuryAggregateOutcome.PASS and graded_outcome is JuryAggregateOutcome.PASS:
        return JuryAggregateOutcome.PASS
    if graded_outcome is JuryAggregateOutcome.FAIL:
        return JuryAggregateOutcome.FAIL
    return JuryAggregateOutcome.NEEDS_USER


def aggregate_jury(
    ballots: tuple[JurorBallot, ...],
    reliability: Mapping[tuple[AgentSessionRole, str], RoleReliability] | None = None,
) -> JuryAggregate:
    """Reduce juror ballots into a single :class:`JuryAggregate`.

    Minority-veto for binary verdicts, mean for graded scores. A genuine
    split with no resolvable aggregate (a binary tie with no veto, or a
    graded mid-band / high-variance vote) routes to
    :attr:`JuryAggregateOutcome.NEEDS_USER`.

    When *reliability* is supplied, the graded mean is reliability-weighted:
    each graded ballot is weighted by :func:`juror_weight` (its matching SCORED
    role's conservative held-rate lower bound, else
    :data:`NEUTRAL_JUROR_WEIGHT`), so a more reliable juror pulls the mean toward
    its score. With *reliability* omitted, or when every weight is neutral (the
    honest-negative case today -- the reputation engine scores no role yet), the
    weighted mean is identical to the plain mean, so the reducer is
    behavior-preserving until SCORED reliability accrues. The binary
    minority-veto is **never** down-weighted: a single credible veto still sinks
    the vote regardless of that juror's weight -- one credible refutation is the
    conservative close-gate signal.

    Pure: no live agent spawn, no I/O. A later live-jury caller convenes real
    jurors and passes their ballots in unchanged.

    Args:
        ballots: One or more :class:`JurorBallot` rows. Binary and graded
            ballots may be mixed; the reducer keeps minority-veto dominant.
        reliability: Optional map from ``(agent_role, runtime)`` to the role's
            :class:`~eawf.observability.eval.reputation.RoleReliability`, used to
            weight the graded mean. ``None`` (the default) weights every juror
            neutrally, leaving the graded mean equal to the unweighted mean.

    Returns:
        The reduced :class:`JuryAggregate`.

    Raises:
        ValueError: When *ballots* is empty — a jury needs at least one
            ballot to resolve.
    """
    if not ballots:
        raise ValueError("cannot aggregate an empty jury: at least one ballot required")

    verdicts = tuple(b.verdict for b in ballots if b.verdict is not None)
    scores = tuple(b.score for b in ballots if b.score is not None)
    weights = tuple(juror_weight(b, reliability) for b in ballots if b.score is not None)
    veto_count = sum(1 for v in verdicts if v in _VETO_VERDICTS)

    has_binary = bool(verdicts)
    has_graded = bool(scores)

    if has_binary and not has_graded:
        outcome, reasons = _resolve_binary(verdicts)
        return JuryAggregate(
            outcome=outcome,
            acceptance_style="binary",
            ballot_count=len(ballots),
            veto_count=veto_count,
            reasons=reasons,
        )

    if has_graded and not has_binary:
        outcome, mean, spread, reasons = _resolve_graded(scores, weights)
        return JuryAggregate(
            outcome=outcome,
            acceptance_style="graded",
            ballot_count=len(ballots),
            veto_count=0,
            mean_score=mean,
            score_spread=spread,
            reasons=reasons,
        )

    # Mixed binary + graded ballots.
    binary_outcome, binary_reasons = _resolve_binary(verdicts)
    graded_outcome, mean, spread, graded_reasons = _resolve_graded(scores, weights)
    combined = _combine_mixed(binary_outcome, graded_outcome)
    reasons = ("mixed binary+graded vote", *binary_reasons, *graded_reasons)
    return JuryAggregate(
        outcome=combined,
        acceptance_style=None,
        ballot_count=len(ballots),
        veto_count=veto_count,
        mean_score=mean,
        score_spread=spread,
        reasons=reasons,
    )


__all__ = [
    "CONSENSUS_SPREAD",
    "FAIL_SCORE_THRESHOLD",
    "NEUTRAL_JUROR_WEIGHT",
    "PASS_SCORE_THRESHOLD",
    "JurorBallot",
    "JuryAggregate",
    "JuryAggregateOutcome",
    "aggregate_jury",
    "juror_weight",
]
