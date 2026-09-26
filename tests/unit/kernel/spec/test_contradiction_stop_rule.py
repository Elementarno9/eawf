"""Tests for :class:`eawf.kernel.spec.saturation.ContradictionStopRule`.

Pins the split between the contradiction stop rule and the
``no_contradiction`` gate of :class:`~eawf.kernel.spec.saturation.SaturationReport`:
the two are evaluated and reported separately over the same Claim ledger,
and firing one never sets the other.

1. Gate-fire proof: a ledger with one live REFUTED claim fires the stop
   rule; the SaturationReport blocks on its own ``no_contradiction`` gate,
   reached independently over the same ledger.
2. The reverse: a ledger with no REFUTED claim but an unresolved open
   question does not fire the stop rule, while the SaturationReport still
   blocks -- on a different gate.
3. Boundary: an empty ledger does not fire; a SUPERSEDED claim never fires
   (a single status field cannot be both SUPERSEDED and REFUTED); multiple
   live REFUTED claims all name as offenders.
4. Purity: same ledger -> equal rule; the ledger is not mutated.
5. Wired into the round loop: a round reporting a fired stop rule halts
   with :attr:`RoundHaltReason.CONTRADICTION`, not a saturation reason, and
   the round's saturation verdict is still independently readable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eawf.kernel.spec.round_loop import RoundHaltReason, RoundOutcome, run_round_loop
from eawf.kernel.spec.saturation import (
    DEFAULT_NOVELTY_WINDOW,
    ContradictionStopRule,
    SaturationReport,
)
from eawf.kernel.state.enums import ClaimStatus, OpenQuestionStatus
from eawf.kernel.state.models import Claim, OpenQuestion

_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_SCOPE = "urn:eawf:v1:campaign:OWNER/RES-42"

# Older than the default novelty window so a claim built with it never trips
# the novelty-decay gate, letting each test below isolate a single gate.
_OLD = _NOW - DEFAULT_NOVELTY_WINDOW - timedelta(hours=1)


def _claim(
    *,
    claim_id: str,
    status: ClaimStatus = ClaimStatus.SUPPORTED,
    answers_question_id: str | None = None,
    superseded_by: str | None = None,
) -> Claim:
    """Return a Claim on minimal valid defaults (old, evidence-backed, terminal-ok)."""
    return Claim(
        id=claim_id,
        scope_id=_SCOPE,
        title="State the claim",
        status=status,
        evidence_refs=["src/eawf/kernel/spec/saturation.py"],
        answers_question_id=answers_question_id,
        created_at=_OLD,
        superseded_by=superseded_by,
    )


def _question(*, question_id: str, status: OpenQuestionStatus) -> OpenQuestion:
    """Return an OpenQuestion on minimal valid defaults."""
    return OpenQuestion(
        id=question_id,
        scope_id=_SCOPE,
        title="Frame the question",
        status=status,
        created_at=_NOW,
        resolved_at=_NOW if status is OpenQuestionStatus.ANSWERED else None,
    )


# Gate-fire proof: split, not conflated -----------------------------------


def test_one_contradiction_fires_stop_rule_and_gate_blocks_independently() -> None:
    claims = [_claim(claim_id="C1", status=ClaimStatus.REFUTED)]

    rule = ContradictionStopRule.evaluate(claims)
    report = SaturationReport.reduce(claims, [], now=_NOW)

    assert rule.fired is True
    assert rule.offenders == ("C1",)
    # The gate blocks on its own no_contradiction logic, read from the same
    # ledger -- never because the stop rule fired.
    assert report.blocking_gates() == ("no_contradiction",)


def test_only_saturation_fails_stop_rule_does_not_fire() -> None:
    claims = [_claim(claim_id="C1")]
    questions = [_question(question_id="Q1", status=OpenQuestionStatus.OPEN)]

    rule = ContradictionStopRule.evaluate(claims)
    report = SaturationReport.reduce(claims, questions, now=_NOW)

    assert rule.fired is False
    assert rule.offenders == ()
    assert report.saturated is False
    assert report.blocking_gates() == ("no_open_question",)


# Boundary cases ------------------------------------------------------------


def test_empty_ledger_does_not_fire() -> None:
    rule = ContradictionStopRule.evaluate([])

    assert rule.fired is False
    assert rule.offenders == ()


def test_superseded_claim_never_fires() -> None:
    claims = [_claim(claim_id="C1", status=ClaimStatus.SUPERSEDED, superseded_by="C2")]

    rule = ContradictionStopRule.evaluate(claims)

    assert rule.fired is False


def test_open_claim_does_not_fire() -> None:
    rule = ContradictionStopRule.evaluate([_claim(claim_id="C1", status=ClaimStatus.OPEN)])

    assert rule.fired is False


def test_multiple_live_refuted_claims_all_named() -> None:
    claims = [
        _claim(claim_id="C1", status=ClaimStatus.REFUTED),
        _claim(claim_id="C2", status=ClaimStatus.SUPPORTED),
        _claim(claim_id="C3", status=ClaimStatus.REFUTED),
    ]

    rule = ContradictionStopRule.evaluate(claims)

    assert rule.fired is True
    assert rule.offenders == ("C1", "C3")


# Purity ----------------------------------------------------------------


def test_evaluate_is_pure_same_ledger_same_rule() -> None:
    claims = [_claim(claim_id="C1", status=ClaimStatus.REFUTED)]

    first = ContradictionStopRule.evaluate(claims)
    second = ContradictionStopRule.evaluate(claims)

    assert first == second


def test_evaluate_does_not_mutate_input_ledger() -> None:
    claims = [_claim(claim_id="C1", status=ClaimStatus.REFUTED)]

    ContradictionStopRule.evaluate(claims)

    assert len(claims) == 1


# Wired into the round loop ------------------------------------------------


def test_round_loop_halts_on_contradiction_with_independent_saturation_verdict() -> None:
    """A fired stop rule halts the loop with its own reason, not a saturation one.

    Drives :func:`run_round_loop` with a round_runner that reduces the same
    live-REFUTED-claim ledger through both reducers -- proving the loop
    reports :attr:`RoundHaltReason.CONTRADICTION` (never SATURATED or
    ROUND_BUDGET) while the terminal :class:`SaturationReport` is still
    readable on its own merits, computed by its own gate logic rather than
    silenced or overwritten by the stop rule firing.
    """
    claims = [_claim(claim_id="C1", status=ClaimStatus.REFUTED)]

    def _runner(round_number: int) -> RoundOutcome:
        return RoundOutcome(
            saturation=SaturationReport.reduce(claims, [], now=_NOW),
            contradiction_stop=ContradictionStopRule.evaluate(claims),
        )

    result = run_round_loop(_runner, round_budget=5)

    assert result.halt_reason is RoundHaltReason.CONTRADICTION
    assert result.contradiction_fired is True
    assert result.rounds_run == 1
    # The saturation verdict stands on its own: it blocks on its own
    # no_contradiction gate logic, not because the stop rule fired, and it
    # is neither vacuously passing nor left unset.
    assert result.final_saturation.saturated is False
    assert result.final_saturation.blocking_gates() == ("no_contradiction",)
