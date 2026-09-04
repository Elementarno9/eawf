"""Tests for :mod:`eawf.workflow.evidence.rung3` (EviBound rung-3 jury).

Covers the spawned entailment-jury escalation tail:

* The escalation gate (:func:`escalate_to_rung3`): the jury convenes ONLY
  on a rung-2 ``ESCALATE`` verdict (the ambiguous band) and is a no-op
  pass-through that spawns nothing on a confident ``ENTAILED`` / ``REFUTED``
  result. A rung-1-fail is conclusive upstream and never reaches rung-3.
* Ballot reduction (:func:`convene_entailment_jury`): the convened ballots
  reduce through :func:`eawf.observability.eval.jury.aggregate_jury`, and a
  ``NEEDS_USER`` aggregate surfaces ``needs_user=True`` mapped to the
  ``UNRESOLVED`` chain verdict (never a silent pass of an ambiguous claim).
* Independence by construction: each juror gets its own prompt carrying
  only the claim + evidence, and the convener invokes the injected ballot
  callback once per juror.
* The outcome -> verdict mapping (:func:`jury_outcome_to_verdict`) and the
  ballot validator (:func:`parse_juror_ballot`).
* Boundary cases (zero claims via empty evidence, single juror, unanimous
  vs split) and error paths (a non-positive juror count, an empty claim, a
  malformed ballot).

The convener spawns no subprocess: every test injects a ``ballot_fn`` stub
that returns canned :class:`JurorBallot` rows, so no real model runs.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import AgentReportVerdict
from eawf.observability.eval.jury import (
    JurorBallot,
    JuryAggregateOutcome,
    aggregate_jury,
)
from eawf.workflow.evidence.rung2 import Rung2ClaimResult, Rung2Verdict
from eawf.workflow.evidence.rung3 import (
    BallotFn,
    Rung3ConveneError,
    Rung3Outcome,
    build_juror_prompt,
    convene_entailment_jury,
    escalate_to_rung3,
    jury_outcome_to_verdict,
    parse_juror_ballot,
)
from eawf.workflow.evidence.rung4 import EviBoundVerdict


def _binary_ballot(juror_id: str, verdict: AgentReportVerdict) -> JurorBallot:
    """Return a binary :class:`JurorBallot` carrying *verdict*."""
    return JurorBallot(juror_id=juror_id, acceptance_style="binary", verdict=verdict)


def _rung2_result(verdict: Rung2Verdict, *, probability: float) -> Rung2ClaimResult:
    """Return a :class:`Rung2ClaimResult` with the given *verdict* / *probability*."""
    return Rung2ClaimResult(
        claim="latency improved after the cache rewrite",
        probability=probability,
        verdict=verdict,
        reason="test fixture",
    )


def _recording_ballot_fn(
    verdicts: list[AgentReportVerdict],
    *,
    seen_prompts: list[str] | None = None,
) -> BallotFn:
    """Build a ``ballot_fn`` that returns one canned binary ballot per call.

    The Nth call returns a binary ballot carrying ``verdicts[N]``; the call
    count must not exceed ``len(verdicts)``. When *seen_prompts* is given,
    every prompt the convener passes is appended to it so a test can assert
    independence (one distinct prompt per juror) without a live spawn.

    Args:
        verdicts: One verdict per juror, in convene order.
        seen_prompts: Optional sink the stub records each prompt into.

    Returns:
        An injectable :data:`BallotFn` stub.
    """
    call_index = {"n": 0}

    async def _fn(prompt: str) -> JurorBallot:
        if seen_prompts is not None:
            seen_prompts.append(prompt)
        index = call_index["n"]
        call_index["n"] += 1
        return _binary_ballot(f"juror-{index + 1}", verdicts[index])

    return _fn


def _exploding_ballot_fn() -> BallotFn:
    """Build a ``ballot_fn`` that fails if ever called.

    Used by the pass-through tests to prove rung-3 spawns NO juror when
    rung-2 was confident: any invocation raises, so a green test is proof
    the callback was never reached.
    """

    async def _fn(prompt: str) -> JurorBallot:
        raise AssertionError(f"ballot_fn must not be called on a non-escalated result: {prompt!r}")

    return _fn


# ---------------------------------------------------------------------------
# escalate_to_rung3 -- the escalation gate (success criterion a)
# ---------------------------------------------------------------------------


def test_escalate_to_rung3_convenes_only_on_ambiguous_rung2() -> None:
    """An ESCALATE rung-2 verdict convenes the jury (convened=True)."""
    rung2 = _rung2_result(Rung2Verdict.ESCALATE, probability=0.5)
    ballot_fn = _recording_ballot_fn(
        [AgentReportVerdict.PASS, AgentReportVerdict.PASS, AgentReportVerdict.PASS]
    )

    outcome = asyncio.run(
        escalate_to_rung3(rung2, "the benchmark shows latency fell", ballot_fn=ballot_fn)
    )

    assert outcome.convened is True
    assert outcome.aggregate is not None
    assert outcome.verdict is EviBoundVerdict.SUPPORTED


def test_escalate_to_rung3_passthrough_on_confident_entailed_spawns_nothing() -> None:
    """A confident ENTAILED rung-2 result is a pass-through; no juror spawns."""
    rung2 = _rung2_result(Rung2Verdict.ENTAILED, probability=0.92)

    outcome = asyncio.run(
        escalate_to_rung3(rung2, "evidence text", ballot_fn=_exploding_ballot_fn())
    )

    assert outcome.convened is False
    assert outcome.needs_user is False
    assert outcome.verdict is EviBoundVerdict.SUPPORTED
    assert outcome.aggregate is None
    assert outcome.reasons  # carries a "not convened" reason


def test_escalate_to_rung3_passthrough_on_confident_refuted_spawns_nothing() -> None:
    """A confident REFUTED rung-2 result is a pass-through; no juror spawns."""
    rung2 = _rung2_result(Rung2Verdict.REFUTED, probability=0.1)

    outcome = asyncio.run(
        escalate_to_rung3(rung2, "evidence text", ballot_fn=_exploding_ballot_fn())
    )

    assert outcome.convened is False
    assert outcome.verdict is EviBoundVerdict.REFUTED
    assert outcome.aggregate is None


def test_escalate_to_rung3_does_not_fire_on_confident_verdicts_parametrized() -> None:
    """Neither confident rung-2 verdict convenes a jury (the off-hot-path KISS)."""
    for verdict, probability in (
        (Rung2Verdict.ENTAILED, 0.95),
        (Rung2Verdict.REFUTED, 0.05),
    ):
        rung2 = _rung2_result(verdict, probability=probability)
        outcome = asyncio.run(
            escalate_to_rung3(rung2, "evidence", ballot_fn=_exploding_ballot_fn())
        )
        assert outcome.convened is False


# ---------------------------------------------------------------------------
# convene_entailment_jury -- reduce via aggregate_jury (success criterion b)
# ---------------------------------------------------------------------------


def test_convene_entailment_jury_unanimous_pass_supports() -> None:
    """A unanimous PASS panel reduces to SUPPORTED with needs_user=False."""
    ballot_fn = _recording_ballot_fn(
        [AgentReportVerdict.PASS, AgentReportVerdict.PASS, AgentReportVerdict.PASS]
    )

    outcome = asyncio.run(
        convene_entailment_jury("claim", "evidence", ballot_fn=ballot_fn, juror_count=3)
    )

    assert outcome.convened is True
    assert outcome.verdict is EviBoundVerdict.SUPPORTED
    assert outcome.needs_user is False
    assert outcome.aggregate is not None
    assert outcome.aggregate.outcome is JuryAggregateOutcome.PASS


def test_convene_entailment_jury_veto_refutes() -> None:
    """A single veto (FAIL) ballot sinks the panel to REFUTED under minority-veto."""
    ballot_fn = _recording_ballot_fn(
        [AgentReportVerdict.PASS, AgentReportVerdict.PASS, AgentReportVerdict.FAIL]
    )

    outcome = asyncio.run(
        convene_entailment_jury("claim", "evidence", ballot_fn=ballot_fn, juror_count=3)
    )

    assert outcome.verdict is EviBoundVerdict.REFUTED
    assert outcome.needs_user is False
    assert outcome.aggregate is not None
    assert outcome.aggregate.veto_count == 1


def test_convene_entailment_jury_split_no_veto_surfaces_needs_user() -> None:
    """A split panel with no veto routes to NEEDS_USER / UNRESOLVED (never a silent pass)."""
    ballot_fn = _recording_ballot_fn(
        [AgentReportVerdict.PASS, AgentReportVerdict.PASS_WITH_FOLLOWUPS]
    )

    outcome = asyncio.run(
        convene_entailment_jury("claim", "evidence", ballot_fn=ballot_fn, juror_count=2)
    )

    assert outcome.needs_user is True
    assert outcome.verdict is EviBoundVerdict.UNRESOLVED
    assert outcome.aggregate is not None
    assert outcome.aggregate.outcome is JuryAggregateOutcome.NEEDS_USER


def test_escalate_to_rung3_ambiguous_then_split_surfaces_needs_user() -> None:
    """End to end: an ambiguous rung-2 result whose jury splits surfaces NEEDS_USER."""
    rung2 = _rung2_result(Rung2Verdict.ESCALATE, probability=0.55)
    ballot_fn = _recording_ballot_fn(
        [AgentReportVerdict.PASS, AgentReportVerdict.PASS_WITH_FOLLOWUPS]
    )

    outcome = asyncio.run(escalate_to_rung3(rung2, "evidence", ballot_fn=ballot_fn, juror_count=2))

    assert outcome.convened is True
    assert outcome.needs_user is True
    assert outcome.verdict is EviBoundVerdict.UNRESOLVED


def test_convene_reduces_through_the_shared_aggregate_jury_reducer() -> None:
    """The rung-3 outcome equals the standalone aggregate_jury reduction of the same ballots.

    Proves rung-3 reuses the minority-veto reducer rather than a parallel
    rule: building the same ballots and reducing them directly yields the
    same outcome the convener attaches.
    """
    verdicts = [AgentReportVerdict.PASS, AgentReportVerdict.FAIL, AgentReportVerdict.PASS]
    ballot_fn = _recording_ballot_fn(verdicts)

    outcome = asyncio.run(
        convene_entailment_jury("claim", "evidence", ballot_fn=ballot_fn, juror_count=3)
    )

    direct = aggregate_jury(
        tuple(_binary_ballot(f"juror-{i + 1}", v) for i, v in enumerate(verdicts))
    )
    assert outcome.aggregate is not None
    assert outcome.aggregate.outcome is direct.outcome
    assert outcome.verdict is jury_outcome_to_verdict(direct.outcome)


# ---------------------------------------------------------------------------
# Independence by construction + one ballot per juror
# ---------------------------------------------------------------------------


def test_convene_entailment_jury_invokes_ballot_fn_once_per_juror() -> None:
    """The convener calls the injected ballot callback exactly juror_count times."""
    seen: list[str] = []
    ballot_fn = _recording_ballot_fn(
        [AgentReportVerdict.PASS, AgentReportVerdict.PASS, AgentReportVerdict.PASS],
        seen_prompts=seen,
    )

    outcome = asyncio.run(
        convene_entailment_jury("claim", "evidence", ballot_fn=ballot_fn, juror_count=3)
    )

    assert outcome.aggregate is not None
    assert outcome.aggregate.ballot_count == 3
    assert len(seen) == 3


def test_convene_entailment_jury_each_juror_prompt_is_independent() -> None:
    """Each juror prompt carries only the claim + evidence and a distinct juror id."""
    seen: list[str] = []
    ballot_fn = _recording_ballot_fn(
        [AgentReportVerdict.PASS, AgentReportVerdict.PASS],
        seen_prompts=seen,
    )

    asyncio.run(
        convene_entailment_jury(
            "the index halves query latency",
            "benchmark p50 dropped from 80ms to 40ms",
            ballot_fn=ballot_fn,
            juror_count=2,
        )
    )

    assert len(seen) == 2
    for prompt in seen:
        assert "the index halves query latency" in prompt
        assert "benchmark p50 dropped from 80ms to 40ms" in prompt
    # Distinct juror ids -> distinct prompts: no juror sees a peer's ballot.
    assert "juror-1" in seen[0]
    assert "juror-2" in seen[1]
    assert seen[0] != seen[1]


# ---------------------------------------------------------------------------
# Boundary cases
# ---------------------------------------------------------------------------


def test_convene_entailment_jury_single_juror_pass() -> None:
    """A one-juror panel (boundary) resolves on that single ballot."""
    ballot_fn = _recording_ballot_fn([AgentReportVerdict.PASS])

    outcome = asyncio.run(
        convene_entailment_jury("claim", "evidence", ballot_fn=ballot_fn, juror_count=1)
    )

    assert outcome.verdict is EviBoundVerdict.SUPPORTED
    assert outcome.aggregate is not None
    assert outcome.aggregate.ballot_count == 1


def test_convene_entailment_jury_single_juror_fail() -> None:
    """A one-juror FAIL panel (boundary) refutes."""
    ballot_fn = _recording_ballot_fn([AgentReportVerdict.FAIL])

    outcome = asyncio.run(
        convene_entailment_jury("claim", "evidence", ballot_fn=ballot_fn, juror_count=1)
    )

    assert outcome.verdict is EviBoundVerdict.REFUTED


def test_convene_entailment_jury_graded_panel_supports() -> None:
    """A graded panel whose mean clears the pass threshold supports the claim."""

    async def _graded_fn(prompt: str) -> JurorBallot:
        return JurorBallot(juror_id="g1", acceptance_style="graded", score=0.9)

    outcome = asyncio.run(
        convene_entailment_jury(
            "claim",
            "evidence",
            ballot_fn=_graded_fn,
            juror_count=2,
            acceptance_style="graded",
        )
    )

    assert outcome.verdict is EviBoundVerdict.SUPPORTED
    assert outcome.aggregate is not None
    assert outcome.aggregate.acceptance_style == "graded"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_convene_entailment_jury_zero_jurors_raises() -> None:
    """A non-positive juror count fails fast (a jury needs at least one ballot)."""
    ballot_fn = _recording_ballot_fn([])

    with pytest.raises(Rung3ConveneError, match="juror_count must be >= 1"):
        asyncio.run(
            convene_entailment_jury("claim", "evidence", ballot_fn=ballot_fn, juror_count=0)
        )


def test_build_juror_prompt_empty_claim_raises() -> None:
    """An empty claim has nothing to judge and fails fast."""
    with pytest.raises(ValueError, match="claim must be non-empty"):
        build_juror_prompt("   ", "evidence", juror_id="juror-1")


def test_build_juror_prompt_empty_evidence_raises() -> None:
    """An empty evidence body has nothing to judge against and fails fast."""
    with pytest.raises(ValueError, match="evidence_text must be non-empty"):
        build_juror_prompt("claim", "", juror_id="juror-1")


def test_parse_juror_ballot_rejects_malformed_ballot() -> None:
    """A malformed ballot raises ValidationError so a re-ask loop can catch + re-ask."""
    # A binary ballot must not carry a score (the JurorBallot coupling validator).
    with pytest.raises(ValidationError):
        parse_juror_ballot({"juror_id": "j1", "acceptance_style": "binary", "score": 0.5})


def test_parse_juror_ballot_accepts_valid_ballot() -> None:
    """A well-formed ballot validates into a JurorBallot."""
    ballot = parse_juror_ballot({"juror_id": "j1", "acceptance_style": "binary", "verdict": "pass"})
    assert isinstance(ballot, JurorBallot)
    assert ballot.verdict is AgentReportVerdict.PASS


def test_jury_outcome_to_verdict_maps_all_outcomes() -> None:
    """Every JuryAggregateOutcome maps onto the expected chain verdict."""
    assert jury_outcome_to_verdict(JuryAggregateOutcome.PASS) is EviBoundVerdict.SUPPORTED
    assert jury_outcome_to_verdict(JuryAggregateOutcome.FAIL) is EviBoundVerdict.REFUTED
    assert jury_outcome_to_verdict(JuryAggregateOutcome.NEEDS_USER) is EviBoundVerdict.UNRESOLVED


def test_rung3_outcome_is_frozen() -> None:
    """Rung3Outcome is an immutable value object (frozen dataclass)."""
    outcome = Rung3Outcome(convened=False, verdict=EviBoundVerdict.SUPPORTED)
    with pytest.raises((AttributeError, TypeError)):
        outcome.convened = True  # type: ignore[misc]
