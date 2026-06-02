"""EviBound text-claim chain: rung-2 scoring into the rung-3 escalation tail.

The rung modules each own ONE rung; this module is the production
*assembler* that runs a text claim down the entailment branch of the
EviBound ladder and chains a rung-2 ``ESCALATE`` verdict into the rung-3
jury. Without it the rung-2 scorer and the rung-3 convener are two halves
that never meet: rung-2 stamps ``ESCALATE`` on the uncertain band and
stops, and :func:`eawf.workflow.evidence.rung3.escalate_to_rung3` sits
unreferenced. :func:`drive_text_claim_chain` is the single seam that
closes the gap.

The chain -- rung-2 then (only on escalate) rung-3
--------------------------------------------------
1. Route the claim (:func:`eawf.workflow.evidence.rung2.route_claim_to_rung`).
   A NUMERIC claim is a rung-1 deterministic assertion, not an entailment
   question, so this driver rejects it fail-fast rather than feeding a
   measured assertion to the NLI scorer. Only a text claim continues.
2. Score the text claim at rung-2
   (:func:`eawf.workflow.evidence.rung2.score_claim`) -> a
   :class:`~eawf.workflow.evidence.rung2.Rung2ClaimResult`.
3. Hand that result to
   :func:`eawf.workflow.evidence.rung3.escalate_to_rung3`. That function
   owns the escalation gate: it convenes the jury ONLY on an ``ESCALATE``
   verdict and returns a pass-through :class:`Rung3Outcome`
   (``convened=False``) on a confident ``ENTAILED`` / ``REFUTED`` result
   -- keeping the expensive jury off the hot path for any claim rung-2
   already resolved.

The injected seams (purity by construction)
-------------------------------------------
This driver spawns nothing itself. The two costs are injected so the
whole chain is unit-testable without a model or a runtime:

* ``scorer`` -- the in-process :class:`~eawf.workflow.evidence.rung2.EntailmentScorer`
  rung-2 scores through. Production binds
  :func:`eawf.workflow.evidence.rung2.load_default_scorer`; a test binds a
  canned scorer that returns a chosen probability so the escalate / pass
  branch is exercised deterministically.
* ``ballot_fn`` -- the per-juror callback forwarded to
  :func:`escalate_to_rung3` and invoked once per juror ONLY when rung-2
  escalates. Production binds the spawn-then-validate adapter; a test binds
  a recording stub returning a canned ballot. The callback is never
  invoked on a confident rung-2 verdict, so a test asserting the
  pass-through path can prove the jury did not run.
"""

from __future__ import annotations

import logging

from eawf.kernel.spec.common import CriterionAcceptanceStyle
from eawf.workflow.evidence.rung2 import (
    ClaimRung,
    EntailmentScorer,
    route_claim_to_rung,
    score_claim,
)
from eawf.workflow.evidence.rung3 import (
    DEFAULT_JUROR_COUNT,
    BallotFn,
    Rung3Outcome,
    escalate_to_rung3,
)

logger = logging.getLogger(__name__)


class NumericClaimError(ValueError):
    """Raised when the text-claim chain is handed a numeric claim.

    The rung-2 / rung-3 entailment branch judges *text* entailment; a
    numeric / comparison assertion (``"coverage >= 0.9"``) is a rung-1
    deterministic-gate question, not an NLI one. Routing it here would feed
    a measured assertion to a prose scorer, so the driver rejects it
    fail-fast rather than silently mis-scoring it.
    """


async def drive_text_claim_chain(
    claim: str,
    evidence_text: str,
    *,
    scorer: EntailmentScorer,
    ballot_fn: BallotFn,
    juror_count: int = DEFAULT_JUROR_COUNT,
    acceptance_style: CriterionAcceptanceStyle = "binary",
) -> Rung3Outcome:
    """Run *claim* down the rung-2 -> rung-3 entailment chain.

    The production assembler for the EviBound entailment branch: it scores
    *claim* against *evidence_text* at rung-2 and chains the result into
    the rung-3 escalation gate. The jury convenes ONLY when rung-2 returns
    :attr:`~eawf.workflow.evidence.rung2.Rung2Verdict.ESCALATE` (the
    uncertain band); a confident ``ENTAILED`` / ``REFUTED`` rung-2 verdict
    returns a pass-through :class:`~eawf.workflow.evidence.rung3.Rung3Outcome`
    (``convened=False``) and spawns no juror.

    The driver spawns nothing itself: *scorer* does the in-process rung-2
    score and *ballot_fn* (forwarded to
    :func:`~eawf.workflow.evidence.rung3.escalate_to_rung3`) convenes a
    juror only on escalation, so the whole chain is unit-testable without a
    model or runtime.

    Args:
        claim: The claim text (the NLI hypothesis). Must be a text claim --
            a numeric / comparison assertion is rejected (it belongs to the
            rung-1 deterministic gate).
        evidence_text: The resolved evidence text (the NLI premise) rung-2
            scores against and the jury would judge against on escalation.
        scorer: The in-process rung-2
            :class:`~eawf.workflow.evidence.rung2.EntailmentScorer` backend.
        ballot_fn: The per-juror async callback forwarded to
            :func:`~eawf.workflow.evidence.rung3.escalate_to_rung3`; invoked
            once per juror ONLY when rung-2 escalates.
        juror_count: Jurors to convene on escalation. Forwarded to
            :func:`~eawf.workflow.evidence.rung3.escalate_to_rung3`.
        acceptance_style: The ballot shape asked of each juror, forwarded
            to :func:`~eawf.workflow.evidence.rung3.escalate_to_rung3`.

    Returns:
        The :class:`~eawf.workflow.evidence.rung3.Rung3Outcome` from the
        chain: a convened jury verdict on rung-2 escalation, else a
        pass-through verdict carrying the confident rung-2 result.

    Raises:
        NumericClaimError: When *claim* routes to the rung-1 deterministic
            gate (a numeric / comparison assertion), not the entailment
            branch.
        ValueError: When *claim* / *evidence_text* is empty / whitespace
            (propagated from
            :func:`~eawf.workflow.evidence.rung2.route_claim_to_rung` /
            :func:`~eawf.workflow.evidence.rung2.score_claim`).
    """
    rung = route_claim_to_rung(claim)
    if rung is not ClaimRung.RUNG1:
        rung2_result = score_claim(claim, evidence_text, scorer=scorer)
        logger.debug(
            f"drive_text_claim_chain rung2_verdict={rung2_result.verdict.value} "
            f"probability={rung2_result.probability:.3f}"
        )
        return await escalate_to_rung3(
            rung2_result,
            evidence_text,
            ballot_fn=ballot_fn,
            juror_count=juror_count,
            acceptance_style=acceptance_style,
        )
    raise NumericClaimError(
        f"numeric claim routes to the rung-1 deterministic gate, not the "
        f"rung-2/rung-3 entailment chain: {claim!r}"
    )


__all__ = [
    "NumericClaimError",
    "drive_text_claim_chain",
]
