"""Tests for :mod:`eawf.kernel.spec.saturation` (P29-I01-W13).

Pins the four-gate loop-until-dry reducer:

1. All four gates pass over a non-empty ledger -> ``saturated`` is True.
2. Each gate fails in isolation -> that gate is the sole blocking gate and
   ``saturated`` is False:
   (a) an OPEN / BLOCKED question fails ``no_open_question``;
   (b) an in-window new claim over the floor fails ``novelty_decay``;
   (c) a live REFUTED claim fails ``no_contradiction``;
   (d) a dangling answer-edge OR an evidence-less live claim fails
       ``integration_closed``.
3. Empty Claim ledger -> ``saturated`` is False even though every gate
   passes trivially (the empty-ledger short-circuit).
4. SUPERSEDED claims are excluded from the live view (cannot contradict or
   dangle).
5. The reducer is pure: same ledgers + ``now`` -> equal report.
6. :meth:`SaturationReport.gate` returns the named result and raises
   :class:`KeyError` on an unknown gate name; :meth:`blocking_gates` lists
   the open gates in gate order.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eawf.kernel.spec.saturation import (
    DEFAULT_NOVELTY_FLOOR,
    DEFAULT_NOVELTY_WINDOW,
    SaturationGateResult,
    SaturationReport,
)
from eawf.kernel.state.enums import ClaimStatus, OpenQuestionStatus
from eawf.kernel.state.models import Claim, OpenQuestion

_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_SCOPE = "urn:eawf:v1:campaign:OWNER/RES-42"

# A timestamp comfortably older than the default novelty window so a claim
# built with it never counts as "new" for the novelty-decay gate.
_OLD = _NOW - DEFAULT_NOVELTY_WINDOW - timedelta(hours=1)


def _claim(
    *,
    claim_id: str,
    status: ClaimStatus = ClaimStatus.SUPPORTED,
    created_at: datetime = _OLD,
    evidence_refs: list[str] | None = None,
    answers_question_id: str | None = None,
    superseded_by: str | None = None,
) -> Claim:
    """Return a Claim on minimal valid defaults (old, evidence-backed, terminal-ok)."""
    return Claim(
        id=claim_id,
        scope_id=_SCOPE,
        title="State the claim",
        status=status,
        evidence_refs=["src/eawf/kernel/spec/saturation.py"]
        if evidence_refs is None
        else evidence_refs,
        answers_question_id=answers_question_id,
        created_at=created_at,
        superseded_by=superseded_by,
    )


def _question(
    *,
    question_id: str,
    status: OpenQuestionStatus = OpenQuestionStatus.ANSWERED,
    answered_by_claim_id: str | None = None,
) -> OpenQuestion:
    """Return an OpenQuestion on minimal valid defaults (terminal by default)."""
    return OpenQuestion(
        id=question_id,
        scope_id=_SCOPE,
        title="Frame the question",
        status=status,
        answered_by_claim_id=answered_by_claim_id,
        created_at=_OLD,
        resolved_at=_NOW if status is OpenQuestionStatus.ANSWERED else None,
    )


def _reduce(
    claims: list[Claim],
    questions: list[OpenQuestion],
) -> SaturationReport:
    """Reduce at the fixed reference instant with default novelty params."""
    return SaturationReport.reduce(claims, questions, now=_NOW)


# All-gates-pass -> saturated --------------------------------------------


def test_reduce_all_gates_pass_is_saturated() -> None:
    claims = [
        _claim(claim_id="C1"),
        _claim(claim_id="C2", answers_question_id="Q1"),
    ]
    questions = [_question(question_id="Q1", answered_by_claim_id="C2")]

    report = _reduce(claims, questions)

    assert report.saturated is True
    assert report.empty_ledger is False
    assert report.live_claim_count == 2
    assert all(g.passed for g in report.gates)
    assert report.blocking_gates() == ()


def test_reduce_gate_order_is_fixed() -> None:
    report = _reduce([_claim(claim_id="C1")], [])

    assert tuple(g.name for g in report.gates) == (
        "no_open_question",
        "novelty_decay",
        "no_contradiction",
        "integration_closed",
    )


# Empty ledger -> not saturated (short-circuit) --------------------------


def test_reduce_empty_ledger_is_not_saturated() -> None:
    report = _reduce([], [])

    assert report.saturated is False
    assert report.empty_ledger is True
    assert report.live_claim_count == 0
    # Every gate passes trivially over empty inputs; the empty-ledger
    # short-circuit (not a failing gate) is what blocks saturation.
    assert all(g.passed for g in report.gates)
    assert report.blocking_gates() == ()


def test_reduce_empty_claims_with_answered_question_still_not_saturated() -> None:
    report = _reduce([], [_question(question_id="Q1")])

    assert report.saturated is False
    assert report.empty_ledger is True


# Gate (a): no open question ---------------------------------------------


def test_reduce_open_question_fails_no_open_question_gate() -> None:
    report = _reduce(
        [_claim(claim_id="C1")],
        [_question(question_id="Q1", status=OpenQuestionStatus.OPEN)],
    )

    assert report.saturated is False
    assert report.blocking_gates() == ("no_open_question",)
    gate = report.gate("no_open_question")
    assert gate.passed is False
    assert gate.offenders == ("Q1",)


def test_reduce_blocked_question_fails_no_open_question_gate() -> None:
    report = _reduce(
        [_claim(claim_id="C1")],
        [_question(question_id="Q1", status=OpenQuestionStatus.BLOCKED)],
    )

    assert report.gate("no_open_question").passed is False
    assert report.gate("no_open_question").offenders == ("Q1",)


def test_reduce_dropped_question_does_not_block() -> None:
    report = _reduce(
        [_claim(claim_id="C1")],
        [_question(question_id="Q1", status=OpenQuestionStatus.DROPPED)],
    )

    assert report.gate("no_open_question").passed is True
    assert report.saturated is True


# Gate (b): novelty decay ------------------------------------------------


def test_reduce_in_window_claim_fails_novelty_gate() -> None:
    # A claim created just inside the window is "new" -> over the zero floor.
    fresh = _NOW - timedelta(hours=1)
    report = _reduce([_claim(claim_id="C1", created_at=fresh)], [])

    assert report.saturated is False
    assert report.blocking_gates() == ("novelty_decay",)
    gate = report.gate("novelty_decay")
    assert gate.passed is False
    assert gate.offenders == ("C1",)


def test_reduce_old_claim_passes_novelty_gate() -> None:
    report = _reduce([_claim(claim_id="C1", created_at=_OLD)], [])

    gate = report.gate("novelty_decay")
    assert gate.passed is True
    assert gate.offenders == ()


def test_reduce_novelty_floor_tolerates_trickle() -> None:
    fresh = _NOW - timedelta(hours=1)
    claims = [
        _claim(claim_id="C1", created_at=fresh),
        _claim(claim_id="C2", created_at=_OLD),
    ]
    # One in-window claim with floor=1 reads as decayed.
    report = SaturationReport.reduce(claims, [], now=_NOW, novelty_floor=1)

    assert report.gate("novelty_decay").passed is True
    assert report.saturated is True


def test_reduce_window_boundary_inclusive() -> None:
    # A claim created exactly at the cutoff (now - window) counts as in-window
    # (the gate uses >= cutoff), so it fails the zero floor.
    at_cutoff = _NOW - DEFAULT_NOVELTY_WINDOW
    report = _reduce([_claim(claim_id="C1", created_at=at_cutoff)], [])

    assert report.gate("novelty_decay").passed is False
    assert report.gate("novelty_decay").offenders == ("C1",)


# Gate (c): no contradiction ---------------------------------------------


def test_reduce_live_refuted_claim_fails_contradiction_gate() -> None:
    report = _reduce([_claim(claim_id="C1", status=ClaimStatus.REFUTED)], [])

    assert report.saturated is False
    assert report.blocking_gates() == ("no_contradiction",)
    gate = report.gate("no_contradiction")
    assert gate.passed is False
    assert gate.offenders == ("C1",)


def test_reduce_superseded_refuted_claim_does_not_contradict() -> None:
    # A SUPERSEDED claim is off the live view even if it was refuted; it
    # cannot keep the contradiction gate open. Pair it with a live claim so
    # the ledger is not empty-only-superseded.
    claims = [
        _claim(claim_id="C0", status=ClaimStatus.SUPERSEDED, superseded_by="C1"),
        _claim(claim_id="C1", status=ClaimStatus.SUPPORTED),
    ]
    report = _reduce(claims, [])

    assert report.live_claim_count == 1
    assert report.gate("no_contradiction").passed is True
    assert report.saturated is True


# Gate (d): integration closed -------------------------------------------


def test_reduce_dangling_answer_edge_fails_integration_gate() -> None:
    # Claim asserts it answers Q1, but Q1 is still OPEN -> dangling edge.
    # (Q1 OPEN also trips gate (a); assert the integration gate specifically.)
    report = _reduce(
        [_claim(claim_id="C1", answers_question_id="Q1")],
        [_question(question_id="Q1", status=OpenQuestionStatus.OPEN)],
    )

    gate = report.gate("integration_closed")
    assert gate.passed is False
    assert gate.offenders == ("C1",)
    assert "integration_closed" in report.blocking_gates()


def test_reduce_evidence_less_live_claim_fails_integration_gate() -> None:
    report = _reduce([_claim(claim_id="C1", evidence_refs=[])], [])

    assert report.saturated is False
    assert report.blocking_gates() == ("integration_closed",)
    gate = report.gate("integration_closed")
    assert gate.passed is False
    assert gate.offenders == ("C1",)


def test_reduce_answer_edge_to_answered_question_closes() -> None:
    report = _reduce(
        [_claim(claim_id="C1", answers_question_id="Q1")],
        [_question(question_id="Q1", answered_by_claim_id="C1")],
    )

    assert report.gate("integration_closed").passed is True
    assert report.saturated is True


def test_reduce_evidence_less_superseded_claim_does_not_dangle() -> None:
    claims = [
        _claim(claim_id="C0", status=ClaimStatus.SUPERSEDED, evidence_refs=[]),
        _claim(claim_id="C1", status=ClaimStatus.SUPPORTED),
    ]
    report = _reduce(claims, [])

    assert report.gate("integration_closed").passed is True
    assert report.saturated is True


# Multiple gates open at once --------------------------------------------


def test_reduce_multiple_open_gates_listed_in_order() -> None:
    fresh = _NOW - timedelta(hours=1)
    claims = [_claim(claim_id="C1", status=ClaimStatus.REFUTED, created_at=fresh)]
    questions = [_question(question_id="Q1", status=OpenQuestionStatus.OPEN)]

    report = _reduce(claims, questions)

    assert report.saturated is False
    # Gate order is preserved in blocking_gates.
    assert report.blocking_gates() == (
        "no_open_question",
        "novelty_decay",
        "no_contradiction",
    )


# Purity ------------------------------------------------------------------


def test_reduce_is_pure_same_inputs_same_report() -> None:
    claims = [_claim(claim_id="C1"), _claim(claim_id="C2", answers_question_id="Q1")]
    questions = [_question(question_id="Q1", answered_by_claim_id="C2")]

    first = _reduce(claims, questions)
    second = _reduce(claims, questions)

    assert first == second


def test_reduce_does_not_mutate_input_ledgers() -> None:
    claims = [_claim(claim_id="C1")]
    questions = [_question(question_id="Q1")]

    _reduce(claims, questions)

    assert len(claims) == 1
    assert len(questions) == 1


# gate() / blocking_gates() helpers --------------------------------------


def test_gate_returns_named_result() -> None:
    report = _reduce([_claim(claim_id="C1")], [])

    result = report.gate("no_contradiction")
    assert isinstance(result, SaturationGateResult)
    assert result.name == "no_contradiction"


def test_gate_unknown_name_raises_keyerror() -> None:
    report = _reduce([_claim(claim_id="C1")], [])

    with pytest.raises(KeyError, match="unknown saturation gate"):
        report.gate("not_a_gate")


def test_default_novelty_constants_exposed() -> None:
    assert DEFAULT_NOVELTY_FLOOR == 0
    assert timedelta(hours=24) == DEFAULT_NOVELTY_WINDOW
