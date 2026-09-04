"""Tests for :mod:`eawf.workflow.evidence.chain` (rung-2 -> rung-3 assembler).

Pins the production assembler that closes the gap between the rung-2 NLI
scorer and the rung-3 escalation tail. The point of these tests is the
WIRING the audit found missing: a rung-2 ``ESCALATE`` verdict now reaches
:func:`escalate_to_rung3` through a real production caller, not just the
unit test of the convener in isolation.

Covers:

* The chain fires the jury on a rung-2 ESCALATE (an uncertain entailment
  probability) -- the escalated verdict is produced and the convener ran
  (``convened=True``).
* The chain is a pass-through that spawns NO juror when rung-2 is
  confident (``ENTAILED`` / ``REFUTED``) -- the injected ballot callback is
  never invoked.
* The safety gate: a numeric / comparison claim is rejected fail-fast
  (:class:`NumericClaimError`) because it belongs to the rung-1
  deterministic gate, not the entailment branch.
* Boundary + error paths: an empty claim / evidence propagates the rung-2
  ``ValueError``; a single-juror panel.

The scorer is a canned stub returning a chosen probability so the
escalate / pass branch is exercised deterministically, and the ballot
callback is a recording / exploding stub so no real model or subprocess
runs.
"""

from __future__ import annotations

import asyncio

import pytest

from eawf.kernel.state.enums import AgentReportVerdict
from eawf.observability.eval.jury import JurorBallot
from eawf.workflow.evidence.chain import NumericClaimError, drive_text_claim_chain
from eawf.workflow.evidence.rung2 import (
    ENTAIL_THRESHOLD,
    REFUTE_THRESHOLD,
    EntailmentScorer,
)
from eawf.workflow.evidence.rung3 import BallotFn, Rung3Outcome
from eawf.workflow.evidence.rung4 import EviBoundVerdict


class _CannedScorer:
    """An :class:`EntailmentScorer` returning a fixed probability per pair.

    Drives the rung-2 verdict deterministically: a probability in the
    uncertain band escalates, one above :data:`ENTAIL_THRESHOLD` certifies,
    one at / below :data:`REFUTE_THRESHOLD` refutes. Records the pairs it
    saw so a test can assert the premise/hypothesis order the chain hands
    rung-2.

    Attributes:
        probability: The fixed entailment probability returned for every
            pair.
        seen: The ``(premise, hypothesis)`` pairs the scorer was asked to
            score, in call order.
    """

    def __init__(self, probability: float) -> None:
        self.probability = probability
        self.seen: list[tuple[str, str]] = []

    def score_batch(self, pairs: list[tuple[str, str]]) -> list[float]:
        """Return :attr:`probability` for every pair, recording the inputs."""
        self.seen.extend(pairs)
        return [self.probability for _ in pairs]


def _recording_ballot_fn(
    verdicts: list[AgentReportVerdict],
    *,
    seen_prompts: list[str] | None = None,
) -> BallotFn:
    """Build a ``ballot_fn`` returning one canned binary ballot per call."""
    call_index = {"n": 0}

    async def _fn(prompt: str) -> JurorBallot:
        if seen_prompts is not None:
            seen_prompts.append(prompt)
        index = call_index["n"]
        call_index["n"] += 1
        return JurorBallot(
            juror_id=f"juror-{index + 1}",
            acceptance_style="binary",
            verdict=verdicts[index],
        )

    return _fn


def _exploding_ballot_fn() -> BallotFn:
    """Build a ``ballot_fn`` that fails if ever called.

    Proves the chain spawns NO juror on a confident rung-2 verdict: any
    invocation raises, so a green pass-through test is proof the callback
    was never reached.
    """

    async def _fn(prompt: str) -> JurorBallot:
        raise AssertionError(
            f"ballot_fn must not be called on a confident rung-2 result: {prompt!r}"
        )

    return _fn


# Pick band representatives off the live thresholds so the test tracks the
# refute-first contract rather than a hard-coded magic number.
_ESCALATE_P = (REFUTE_THRESHOLD + ENTAIL_THRESHOLD) / 2
_ENTAIL_P = ENTAIL_THRESHOLD + (1.0 - ENTAIL_THRESHOLD) / 2
_REFUTE_P = REFUTE_THRESHOLD / 2


# ---------------------------------------------------------------------------
# The escalation chain fires rung-3 (success criterion a)
# ---------------------------------------------------------------------------


def test_drive_text_claim_chain_escalate_reaches_rung3_jury() -> None:
    """A rung-2 ESCALATE drives the chain into the rung-3 jury (convened)."""
    scorer: EntailmentScorer = _CannedScorer(_ESCALATE_P)
    seen_prompts: list[str] = []
    ballot_fn = _recording_ballot_fn(
        [AgentReportVerdict.PASS, AgentReportVerdict.PASS, AgentReportVerdict.PASS],
        seen_prompts=seen_prompts,
    )

    outcome = asyncio.run(
        drive_text_claim_chain(
            "the cache rewrite reduced tail latency",
            "the benchmark shows the p99 latency fell after the rewrite",
            scorer=scorer,
            ballot_fn=ballot_fn,
        )
    )

    assert isinstance(outcome, Rung3Outcome)
    assert outcome.convened is True
    assert outcome.aggregate is not None
    assert outcome.verdict is EviBoundVerdict.SUPPORTED
    # One independent prompt per juror -- the convener actually ran.
    assert len(seen_prompts) == 3


def test_drive_text_claim_chain_escalate_unresolvable_panel_needs_user() -> None:
    """An escalated claim whose panel splits surfaces needs_user (no silent pass)."""
    scorer: EntailmentScorer = _CannedScorer(_ESCALATE_P)
    # A split with NO veto (PASS mixed with PASS_WITH_FOLLOWUPS) has no
    # resolvable aggregate, so it routes to NEEDS_USER -> UNRESOLVED. A
    # FAIL ballot would be a veto and sink to REFUTED instead.
    ballot_fn = _recording_ballot_fn(
        [AgentReportVerdict.PASS, AgentReportVerdict.PASS_WITH_FOLLOWUPS]
    )

    outcome = asyncio.run(
        drive_text_claim_chain(
            "the rewrite improved throughput",
            "the report is inconclusive about throughput",
            scorer=scorer,
            ballot_fn=ballot_fn,
            juror_count=2,
        )
    )

    assert outcome.convened is True
    assert outcome.needs_user is True
    assert outcome.verdict is EviBoundVerdict.UNRESOLVED


# ---------------------------------------------------------------------------
# The confident pass-through spawns nothing
# ---------------------------------------------------------------------------


def test_drive_text_claim_chain_confident_entailed_spawns_no_juror() -> None:
    """A confident ENTAILED rung-2 score is a pass-through; no juror spawns."""
    scorer: EntailmentScorer = _CannedScorer(_ENTAIL_P)

    outcome = asyncio.run(
        drive_text_claim_chain(
            "the suite passed",
            "the suite passed all checks",
            scorer=scorer,
            ballot_fn=_exploding_ballot_fn(),
        )
    )

    assert outcome.convened is False
    assert outcome.verdict is EviBoundVerdict.SUPPORTED
    assert outcome.aggregate is None


def test_drive_text_claim_chain_confident_refuted_spawns_no_juror() -> None:
    """A confident REFUTED rung-2 score is a pass-through; no juror spawns."""
    scorer: EntailmentScorer = _CannedScorer(_REFUTE_P)

    outcome = asyncio.run(
        drive_text_claim_chain(
            "the deploy succeeded cleanly",
            "completely unrelated narrative about gardening tools",
            scorer=scorer,
            ballot_fn=_exploding_ballot_fn(),
        )
    )

    assert outcome.convened is False
    assert outcome.verdict is EviBoundVerdict.REFUTED
    assert outcome.aggregate is None


def test_drive_text_claim_chain_scorer_sees_evidence_as_premise() -> None:
    """The chain hands rung-2 ``(premise=evidence, hypothesis=claim)``."""
    scorer = _CannedScorer(_ENTAIL_P)

    asyncio.run(
        drive_text_claim_chain(
            "the claim text",
            "the evidence text",
            scorer=scorer,
            ballot_fn=_exploding_ballot_fn(),
        )
    )

    assert scorer.seen == [("the evidence text", "the claim text")]


# ---------------------------------------------------------------------------
# Safety gate + error paths
# ---------------------------------------------------------------------------


def test_drive_text_claim_chain_rejects_numeric_claim() -> None:
    """A numeric claim is rejected fail-fast (belongs to the rung-1 gate)."""
    scorer: EntailmentScorer = _CannedScorer(_ESCALATE_P)

    with pytest.raises(NumericClaimError, match="rung-1 deterministic gate"):
        asyncio.run(
            drive_text_claim_chain(
                "coverage rose to 92%",
                "the coverage report shows 92 percent",
                scorer=scorer,
                ballot_fn=_exploding_ballot_fn(),
            )
        )


def test_drive_text_claim_chain_empty_claim_raises_value_error() -> None:
    """An empty / whitespace claim propagates the routing ValueError."""
    scorer: EntailmentScorer = _CannedScorer(_ESCALATE_P)

    with pytest.raises(ValueError, match="claim must be non-empty"):
        asyncio.run(
            drive_text_claim_chain(
                "   ",
                "some evidence",
                scorer=scorer,
                ballot_fn=_exploding_ballot_fn(),
            )
        )


def test_drive_text_claim_chain_single_juror_boundary() -> None:
    """A single-juror escalated panel still convenes and resolves."""
    scorer: EntailmentScorer = _CannedScorer(_ESCALATE_P)
    ballot_fn = _recording_ballot_fn([AgentReportVerdict.PASS])

    outcome = asyncio.run(
        drive_text_claim_chain(
            "the migration preserved the rows",
            "the audit confirms every row survived the migration",
            scorer=scorer,
            ballot_fn=ballot_fn,
            juror_count=1,
        )
    )

    assert outcome.convened is True
    assert outcome.verdict is EviBoundVerdict.SUPPORTED
