"""EviBound rung-3 spawned entailment jury (the escalation tail).

This module is the rung-3 layer of the EviBound evidence chain. It sits
directly above the W09 rung-2 in-process NLI scorer
(:mod:`eawf.workflow.evidence.rung2`) and below the rung-4 attested floor
(:mod:`eawf.workflow.evidence.rung4`). The rung ladder, by assurance:

* rung-1 deterministic gate  (:mod:`eawf.workflow.evidence.evibound`)
* rung-2 in-process NLI score (:mod:`eawf.workflow.evidence.rung2`)
* rung-3 spawned jury         -- THIS module
* rung-4 attested render      (:mod:`eawf.workflow.evidence.rung4`)

The escalation tail -- the landmine
-----------------------------------
Rung-3 is the EXPENSIVE rung: it spawns one independent juror per ballot,
each a metered model call. So it fires ONLY when rung-2 is genuinely
uncertain -- a :attr:`~eawf.workflow.evidence.rung2.Rung2Verdict.ESCALATE`
verdict (the ``(REFUTE_THRESHOLD, ENTAIL_THRESHOLD)`` open band). It does
NOT fire when rung-1 fails (a failed deterministic gate is conclusive),
nor when rung-2 is confident (``ENTAILED`` certifies, ``REFUTED``
contradicts). :func:`escalate_to_rung3` enforces that gate: a non-ESCALATE
rung-2 result returns a pass-through outcome with ``convened=False`` and
spawns nothing. Keeping rung-3 off the hot path is the KISS contract --
the common case (a claim rung-1 or rung-2 already resolves) never pays for
the jury.

Independence by construction
----------------------------
Each juror is convened with a prompt built from ONLY the claim + the
evidence (:func:`build_juror_prompt`); no juror sees another juror's
ballot, the rung-2 probability, or any peer channel. That independence is
what makes the minority-veto reducer
(:func:`eawf.observability.eval.jury.aggregate_jury`) meaningful: N
correlated votes are one vote, but N independent votes are a jury. The
per-juror work is injected as a :data:`BallotFn` callback so the convener
spawns no subprocess itself and a test drives it with a recording stub.

Reused seams (no logic rebuilt)
-------------------------------
* ballot reduction -- :func:`eawf.observability.eval.jury.aggregate_jury`
  (the minority-veto reducer over :class:`JurorBallot` rows). Rung-3 does
  NOT reimplement the veto / mean / consensus policy; it convenes the
  jurors and feeds their ballots into the existing reducer unchanged.
* schema validation -- :func:`parse_juror_ballot` validates a spawn's
  JSON-decoded output into a :class:`JurorBallot` and raises
  :class:`pydantic.ValidationError` on a mismatch, so it plugs into the
  bounded re-ask loop
  (:func:`eawf.workflow.dispatch.llm_assist.assist_with_schema`) the same
  way the auditor-body validator does -- a juror that returns a malformed
  ballot is re-asked, then exhausts typed, never silently dropped.
* closed verdict vocabulary -- the jury outcome maps onto the chain-wide
  :class:`~eawf.workflow.evidence.rung4.EviBoundVerdict` via
  :func:`jury_outcome_to_verdict`, so rung-3 reports against the same
  vocabulary every other rung does.

Never silently pass an ambiguous claim
--------------------------------------
A :attr:`~eawf.observability.eval.jury.JuryAggregateOutcome.NEEDS_USER`
aggregate (a split with no veto, or a high-variance graded vote) maps to
:attr:`~eawf.workflow.evidence.rung4.EviBoundVerdict.UNRESOLVED` and sets
:attr:`Rung3Outcome.needs_user`. The convener surfaces that to the
operator rather than letting an unresolvable jury fall through as a pass --
the same refute-first contract rung-2 enforces: the default for the grey
zone is *do not certify*.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from pydantic import TypeAdapter

from eawf.kernel.spec.common import CriterionAcceptanceStyle
from eawf.observability.eval.jury import (
    JurorBallot,
    JuryAggregate,
    JuryAggregateOutcome,
    aggregate_jury,
)
from eawf.workflow.evidence.rung2 import Rung2ClaimResult, Rung2Verdict
from eawf.workflow.evidence.rung4 import EviBoundVerdict

logger = logging.getLogger(__name__)

#: Default number of independent jurors rung-3 convenes for an escalated
#: claim. Three is the smallest odd panel that lets a single dissent show
#: as a minority (one veto sinks the vote under minority-veto, and a 2-1
#: split with no veto surfaces as ``NEEDS_USER``). A caller may widen the
#: panel for a higher-stakes claim.
DEFAULT_JUROR_COUNT: int = 3

#: Forced-schema adapter for a juror ballot. Validating a spawn's
#: JSON-decoded output through this narrows it to a :class:`JurorBallot`: a
#: malformed ballot (missing ``juror_id``, a binary ballot carrying a
#: ``score``, an out-of-range score) fails the model validator and raises
#: :class:`pydantic.ValidationError`, which the bounded re-ask loop
#: classifies as a schema mismatch and re-asks rather than letting escape.
_BALLOT_ADAPTER: TypeAdapter[JurorBallot] = TypeAdapter(JurorBallot)

#: Map the reduced jury outcome onto the chain-wide closed verdict. A
#: ``PASS`` aggregate SUPPORTS the claim (entailment found by the panel); a
#: ``FAIL`` aggregate REFUTES it (a veto or a confident low-score panel); a
#: ``NEEDS_USER`` aggregate is UNRESOLVED -- the panel could not reach a
#: resolvable verdict, so the criterion is blocked on the operator, never
#: silently certified.
_OUTCOME_VERDICT: dict[JuryAggregateOutcome, EviBoundVerdict] = {
    JuryAggregateOutcome.PASS: EviBoundVerdict.SUPPORTED,
    JuryAggregateOutcome.FAIL: EviBoundVerdict.REFUTED,
    JuryAggregateOutcome.NEEDS_USER: EviBoundVerdict.UNRESOLVED,
}

#: A callable that convenes one independent juror for an entailment
#: question and returns that juror's :class:`JurorBallot`. Injected into
#: :func:`convene_entailment_jury` so the convener spawns no subprocess
#: itself: production binds a spawn-then-validate adapter (drive the
#: resolved runtime's ``spawn_session`` through the bounded re-ask loop with
#: :func:`parse_juror_ballot` as the forced-schema validator); a test binds
#: a recording stub returning a canned ballot. The single ``str`` argument
#: is the per-juror prompt (built by :func:`build_juror_prompt`); the
#: callback is invoked once per juror so each vote is independent.
type BallotFn = Callable[[str], Awaitable[JurorBallot]]


class Rung3ConveneError(ValueError):
    """Raised when rung-3 is asked to convene a jury it must not run.

    Two fail-fast cases: a non-positive juror count (a jury needs at least
    one juror to vote), and an attempt to convene over a rung-2 result that
    did not escalate (rung-3 is the escalation tail; running it on a
    confident or rung-1-failed claim would burn a metered spawn for a claim
    already resolved).
    """


def jury_outcome_to_verdict(outcome: JuryAggregateOutcome) -> EviBoundVerdict:
    """Map a reduced :class:`JuryAggregateOutcome` onto an :class:`EviBoundVerdict`.

    The rung-3 jury reports against the same closed chain-wide verdict
    vocabulary every other EviBound rung does. ``PASS`` -> ``SUPPORTED``
    (the panel found entailment); ``FAIL`` -> ``REFUTED`` (a veto or a
    confident low-score panel); ``NEEDS_USER`` -> ``UNRESOLVED`` (an
    unresolvable panel the operator must adjudicate).

    Args:
        outcome: The reduced jury outcome from
            :func:`eawf.observability.eval.jury.aggregate_jury`.

    Returns:
        The chain-wide :class:`EviBoundVerdict` the outcome maps to.
    """
    return _OUTCOME_VERDICT[outcome]


def parse_juror_ballot(raw: object) -> JurorBallot:
    """Validate *raw* as a :class:`JurorBallot`.

    The forced-schema validator a live convener hands the bounded re-ask
    loop (:func:`eawf.workflow.dispatch.llm_assist.assist_with_schema`).
    Validating *raw* through :data:`_BALLOT_ADAPTER` means a spawn that
    returns a malformed ballot (the wrong acceptance-style payload, an
    out-of-range score, a missing ``juror_id``) raises
    :class:`pydantic.ValidationError` -- which the loop catches, classifies
    as a schema mismatch, and re-asks. Raising a ``ValidationError`` rather
    than a plain ``ValueError`` is load-bearing: the loop only catches
    ``json.JSONDecodeError`` + ``ValidationError``, so a plain
    ``ValueError`` would escape the bounded retry uncaught.

    Args:
        raw: The JSON-decoded spawn output.

    Returns:
        The validated :class:`JurorBallot`.

    Raises:
        pydantic.ValidationError: When *raw* is not a valid juror ballot.
    """
    return _BALLOT_ADAPTER.validate_python(raw)


def build_juror_prompt(
    claim: str,
    evidence_text: str,
    *,
    juror_id: str,
    acceptance_style: CriterionAcceptanceStyle = "binary",
) -> str:
    """Return the independent entailment prompt for one juror.

    The prompt carries ONLY the claim + the evidence -- never another
    juror's ballot, the rung-2 probability, or any peer channel. That
    independence is the construction the minority-veto reducer relies on:
    each juror judges the same question in isolation, so N ballots are N
    votes rather than one correlated vote.

    Args:
        claim: The claim text the juror judges (the entailment hypothesis).
        evidence_text: The resolved evidence text the juror judges against
            (the entailment premise).
        juror_id: Stable identifier the juror stamps on its ballot so the
            convener can attribute the vote.
        acceptance_style: ``"binary"`` (the juror returns a pass / fail /
            blocked verdict) or ``"graded"`` (the juror returns a score in
            ``[0, 1]``). Selects which ballot shape the output contract
            asks for.

    Returns:
        The rendered Markdown juror prompt.

    Raises:
        ValueError: When *claim* or *evidence_text* is empty /
            whitespace-only -- there is nothing to judge.
    """
    if not claim.strip():
        raise ValueError("claim must be non-empty")
    if not evidence_text.strip():
        raise ValueError("evidence_text must be non-empty")

    if acceptance_style == "binary":
        contract = (
            "Respond with ONLY a single JSON object that is a juror ballot: "
            f'`juror_id` = {juror_id!r}, `acceptance_style` = "binary", and a '
            "`verdict` of `pass` (the evidence entails the claim), `fail` (the "
            "evidence contradicts the claim), or `blocked` (you cannot tell). "
            "No `score`, no prose, no code fences."
        )
    else:  # "graded"
        contract = (
            "Respond with ONLY a single JSON object that is a juror ballot: "
            f'`juror_id` = {juror_id!r}, `acceptance_style` = "graded", and a '
            "`score` in [0.0, 1.0] for how strongly the evidence entails the "
            "claim (1.0 = fully entails, 0.0 = contradicts). No `verdict`, no "
            "prose, no code fences."
        )
    return (
        f"# Entailment juror: {juror_id}\n"
        "\n"
        "You are an independent juror. You judge ONE question in isolation and\n"
        "have not seen any other juror's vote: does the evidence below entail the\n"
        "claim below? Judge only on the evidence shown -- do not assume facts not\n"
        "present in it.\n"
        "\n"
        "## Claim\n"
        "\n"
        f"{claim}\n"
        "\n"
        "## Evidence\n"
        "\n"
        f"{evidence_text}\n"
        "\n"
        "## Output contract\n"
        "\n"
        f"{contract}"
    )


@dataclass(frozen=True)
class Rung3Outcome:
    """Typed outcome of a rung-3 escalation decision.

    Produced by :func:`convene_entailment_jury` (always a convened jury)
    and by :func:`escalate_to_rung3` (a jury when rung-2 escalated, a
    pass-through otherwise). The :attr:`convened` bit distinguishes the two:
    a pass-through never spawned a juror.

    Attributes:
        convened: ``True`` iff the jury actually ran (one or more jurors
            were convened). ``False`` on the :func:`escalate_to_rung3`
            pass-through where rung-2 was confident / rung-1 failed and no
            juror was spawned.
        verdict: The chain-wide :class:`EviBoundVerdict` rung-3 reports the
            claim under. For a convened jury this is
            :func:`jury_outcome_to_verdict` of the aggregate; for a
            pass-through it carries the rung-2 result mapped onto the
            vocabulary (``ENTAILED`` -> ``SUPPORTED``, ``REFUTED`` ->
            ``REFUTED``).
        needs_user: ``True`` iff the jury aggregate was
            :attr:`~eawf.observability.eval.jury.JuryAggregateOutcome.NEEDS_USER`
            -- an unresolvable panel the operator must adjudicate. The
            convener surfaces this rather than silently passing an
            ambiguous claim. Always ``False`` on a pass-through.
        aggregate: The reduced :class:`JuryAggregate` when a jury ran, else
            ``None``. Carries the per-signal reasons + veto / spread detail
            so a caller can render why the panel landed where it did.
        reasons: One short string per signal that drove the outcome. For a
            convened jury these mirror the aggregate's reasons; for a
            pass-through a single line names why no jury ran.
    """

    convened: bool
    verdict: EviBoundVerdict
    needs_user: bool = False
    aggregate: JuryAggregate | None = None
    reasons: tuple[str, ...] = field(default_factory=tuple)


async def convene_entailment_jury(
    claim: str,
    evidence_text: str,
    *,
    ballot_fn: BallotFn,
    juror_count: int = DEFAULT_JUROR_COUNT,
    acceptance_style: CriterionAcceptanceStyle = "binary",
) -> Rung3Outcome:
    """Convene an independent entailment jury and reduce its ballots.

    Builds one independent prompt per juror (:func:`build_juror_prompt`,
    carrying only the claim + evidence), invokes the injected *ballot_fn*
    once per juror to collect a :class:`JurorBallot`, reduces the ballots
    through the existing minority-veto reducer
    (:func:`eawf.observability.eval.jury.aggregate_jury`), and maps the
    aggregate onto the chain-wide :class:`EviBoundVerdict`
    (:func:`jury_outcome_to_verdict`).

    Each juror votes in isolation (no peer channel), so the panel's
    independence is a construction property, not a runtime assumption. The
    convener spawns no subprocess itself -- *ballot_fn* is the testability
    seam: production binds a spawn-then-validate adapter; a test binds a
    recording stub. A
    :attr:`~eawf.observability.eval.jury.JuryAggregateOutcome.NEEDS_USER`
    aggregate sets :attr:`Rung3Outcome.needs_user` so an unresolvable panel
    surfaces to the operator rather than passing silently.

    Args:
        claim: The claim text the jury judges (the entailment hypothesis).
        evidence_text: The resolved evidence the jury judges against (the
            entailment premise).
        ballot_fn: Injected async callback convening one juror per call and
            returning its :class:`JurorBallot`. Invoked once per juror.
        juror_count: Number of independent jurors to convene. Must be
            ``>= 1`` (a jury needs at least one ballot). Defaults to
            :data:`DEFAULT_JUROR_COUNT`.
        acceptance_style: The ballot shape each juror is asked for --
            ``"binary"`` (verdict) or ``"graded"`` (score). Threaded into
            every juror prompt.

    Returns:
        A :class:`Rung3Outcome` with ``convened=True``, the reduced
        verdict, the ``needs_user`` bit, and the underlying
        :class:`JuryAggregate`.

    Raises:
        Rung3ConveneError: When *juror_count* is less than 1.
        ValueError: When *claim* / *evidence_text* is empty (propagated
            from :func:`build_juror_prompt`).
    """
    if juror_count < 1:
        raise Rung3ConveneError(f"juror_count must be >= 1: {juror_count!r}")

    ballots: list[JurorBallot] = []
    for index in range(juror_count):
        juror_id = f"juror-{index + 1}"
        prompt = build_juror_prompt(
            claim,
            evidence_text,
            juror_id=juror_id,
            acceptance_style=acceptance_style,
        )
        ballot = await ballot_fn(prompt)
        ballots.append(ballot)

    aggregate = aggregate_jury(tuple(ballots))
    verdict = jury_outcome_to_verdict(aggregate.outcome)
    needs_user = aggregate.outcome is JuryAggregateOutcome.NEEDS_USER
    logger.info(
        f"convene_entailment_jury jurors={juror_count} outcome={aggregate.outcome.value} "
        f"verdict={verdict.value} needs_user={needs_user} veto={aggregate.veto_count}"
    )
    return Rung3Outcome(
        convened=True,
        verdict=verdict,
        needs_user=needs_user,
        aggregate=aggregate,
        reasons=aggregate.reasons,
    )


def _passthrough_verdict(rung2_result: Rung2ClaimResult) -> EviBoundVerdict:
    """Map a confident / non-escalating rung-2 result onto the chain verdict.

    Only reached on the :func:`escalate_to_rung3` pass-through, where the
    rung-2 verdict is NOT ``ESCALATE``: ``ENTAILED`` -> ``SUPPORTED`` (the
    in-process scorer certified entailment), ``REFUTED`` -> ``REFUTED`` (a
    confident contradiction). An ``ESCALATE`` verdict never reaches here --
    the caller routes it to the jury instead.
    """
    if rung2_result.verdict is Rung2Verdict.ENTAILED:
        return EviBoundVerdict.SUPPORTED
    return EviBoundVerdict.REFUTED


async def escalate_to_rung3(
    rung2_result: Rung2ClaimResult,
    evidence_text: str,
    *,
    ballot_fn: BallotFn,
    juror_count: int = DEFAULT_JUROR_COUNT,
    acceptance_style: CriterionAcceptanceStyle = "binary",
) -> Rung3Outcome:
    """Convene the rung-3 jury IFF rung-2 escalated, else pass through.

    The escalation gate -- the heart of "rung-3 is the escalation tail". A
    rung-2 result is routed to the jury ONLY when its verdict is
    :attr:`~eawf.workflow.evidence.rung2.Rung2Verdict.ESCALATE` (the
    uncertain band). A confident rung-2 verdict (``ENTAILED`` /
    ``REFUTED``) returns a pass-through :class:`Rung3Outcome` with
    ``convened=False`` and spawns NO juror -- keeping the expensive jury off
    the hot path for any claim rung-2 already resolved.

    A rung-1-failed claim never reaches this function: rung-1 is a separate,
    conclusive gate (:func:`eawf.workflow.evidence.evibound.run_rung1_gate`)
    and a failed deterministic gate is not an entailment question. The
    caller routes only rung-2 results here, so this gate sees only the
    confident-vs-escalate distinction.

    Args:
        rung2_result: The rung-2 scoring result whose verdict decides
            whether the jury convenes.
        evidence_text: The resolved evidence the jury would judge against
            (used only when the jury convenes).
        ballot_fn: Injected async ballot callback, forwarded to
            :func:`convene_entailment_jury` when the jury runs.
        juror_count: Number of jurors to convene on escalation. Forwarded
            to :func:`convene_entailment_jury`.
        acceptance_style: The ballot shape, forwarded to
            :func:`convene_entailment_jury`.

    Returns:
        On ``ESCALATE`` -- the convened :class:`Rung3Outcome` from
        :func:`convene_entailment_jury`. Otherwise -- a pass-through
        :class:`Rung3Outcome` (``convened=False``) carrying the rung-2
        verdict mapped onto the chain vocabulary.
    """
    if rung2_result.verdict is not Rung2Verdict.ESCALATE:
        verdict = _passthrough_verdict(rung2_result)
        reason = (
            f"rung-2 verdict={rung2_result.verdict.value} is conclusive; "
            "rung-3 jury not convened (escalation tail off the hot path)"
        )
        logger.debug(
            f"escalate_to_rung3 convened=False rung2={rung2_result.verdict.value} "
            f"verdict={verdict.value}"
        )
        return Rung3Outcome(
            convened=False,
            verdict=verdict,
            reasons=(reason,),
        )

    logger.debug(
        f"escalate_to_rung3 convened=True rung2={rung2_result.verdict.value} "
        f"probability={rung2_result.probability:.3f}"
    )
    return await convene_entailment_jury(
        rung2_result.claim,
        evidence_text,
        ballot_fn=ballot_fn,
        juror_count=juror_count,
        acceptance_style=acceptance_style,
    )


__all__ = [
    "DEFAULT_JUROR_COUNT",
    "BallotFn",
    "Rung3ConveneError",
    "Rung3Outcome",
    "build_juror_prompt",
    "convene_entailment_jury",
    "escalate_to_rung3",
    "jury_outcome_to_verdict",
    "parse_juror_ballot",
]
