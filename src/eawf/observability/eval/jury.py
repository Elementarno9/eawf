"""Pure reducer over juror ballots (P29-I01-W06).

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

The reducer is **pure** — no live agent spawn, no I/O. A later live-jury
caller (TRUST-7) convenes real jurors and feeds their ballots in unchanged.
"""

from __future__ import annotations

from enum import StrEnum
from statistics import fmean

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.kernel.spec.common import CriterionAcceptanceStyle
from eawf.kernel.state.enums import AgentReportVerdict

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
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    juror_id: str = Field(min_length=1, max_length=72)
    acceptance_style: CriterionAcceptanceStyle
    verdict: AgentReportVerdict | None = None
    score: float | None = Field(default=None, ge=0.0, le=1.0)

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


def _resolve_graded(
    scores: tuple[float, ...],
) -> tuple[JuryAggregateOutcome, float, float, tuple[str, ...]]:
    """Resolve the graded (mean) side of a vote.

    Returns the outcome, the mean, the spread, and the reasons. A spread above
    :data:`CONSENSUS_SPREAD` signals no consensus regardless of the mean.
    """
    mean = fmean(scores)
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


def aggregate_jury(ballots: tuple[JurorBallot, ...]) -> JuryAggregate:
    """Reduce juror ballots into a single :class:`JuryAggregate`.

    Minority-veto for binary verdicts, mean for graded scores. A genuine
    split with no resolvable aggregate (a binary tie with no veto, or a
    graded mid-band / high-variance vote) routes to
    :attr:`JuryAggregateOutcome.NEEDS_USER`.

    Pure: no live agent spawn, no I/O. A later live-jury caller convenes real
    jurors and passes their ballots in unchanged.

    Args:
        ballots: One or more :class:`JurorBallot` rows. Binary and graded
            ballots may be mixed; the reducer keeps minority-veto dominant.

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
        outcome, mean, spread, reasons = _resolve_graded(scores)
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
    graded_outcome, mean, spread, graded_reasons = _resolve_graded(scores)
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
    "PASS_SCORE_THRESHOLD",
    "JurorBallot",
    "JuryAggregate",
    "JuryAggregateOutcome",
    "aggregate_jury",
]
