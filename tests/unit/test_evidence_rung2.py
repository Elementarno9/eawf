"""Unit tests for :mod:`eawf.workflow.evidence.rung2` (EviBound rung-2).

Covers the in-process NLI escalation rung:

* The refute-first thresholding (:func:`classify_probability` /
  :func:`score_claim`): entail above the floor, refute at/below the
  floor, and the uncertain band collapsing to ESCALATE (never a silent
  pass).
* Numeric -> rung-1 routing (:func:`route_claim_to_rung` /
  :func:`looks_numeric`): a numeric / comparison claim is forced to
  rung-1; a text claim routes to rung-2.
* Batch scoring (:func:`score_claims`): a multi-claim batch and the
  empty-batch boundary.
* The pluggable-scorer seam (:func:`load_default_scorer`): the
  degrade-to-lexical path when the optional model backend is absent, and
  a fake scorer injected through the :class:`EntailmentScorer` Protocol
  so no model is ever downloaded or run.
* The persisted-row shape (:func:`run_rung2_gate`): the three-way verdict
  maps onto the binary EvidenceStatus and the probability rides in
  ``metrics``.

No test imports torch / transformers or downloads a model — the scorer
is always the in-tree lexical default or a hand-rolled fake.
"""

from __future__ import annotations

import pytest

from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.workflow.evidence.rung2 import (
    ENTAIL_THRESHOLD,
    OPTIONAL_MODEL_EXTRA,
    REFUTE_THRESHOLD,
    RUNG2_ESCALATION_NOTE,
    ClaimRung,
    EntailmentScorer,
    LexicalEntailmentScorer,
    Rung2ClaimResult,
    Rung2Config,
    Rung2Verdict,
    classify_probability,
    load_default_scorer,
    looks_numeric,
    route_claim_to_rung,
    run_rung2_gate,
    score_claim,
    score_claims,
    verdict_to_status,
)

_SCOPE = "urn:eawf:v1:wave:owner/P29-I01-W09"


class _FakeScorer:
    """A fake EntailmentScorer that returns canned probabilities in order.

    Injected through the :class:`EntailmentScorer` Protocol so a test can
    drive any verdict without a model download. Records the pairs it was
    handed so a test can assert the premise / hypothesis order.
    """

    def __init__(self, probabilities: list[float]) -> None:
        self.probabilities = probabilities
        self.seen: list[tuple[str, str]] = []

    def score_batch(self, pairs: list[tuple[str, str]]) -> list[float]:
        self.seen.extend(pairs)
        return self.probabilities[: len(pairs)]


# --------------------------------------------------------------------------- #
# classify_probability: the refute-first three-way mapping.
# --------------------------------------------------------------------------- #
def test_classify_probability_above_floor_entails() -> None:
    """A probability at/above ENTAIL_THRESHOLD is the only PASSING verdict."""
    verdict, reason = classify_probability(ENTAIL_THRESHOLD)
    assert verdict is Rung2Verdict.ENTAILED
    assert str(ENTAIL_THRESHOLD) in reason


def test_classify_probability_at_or_below_refute_floor_refutes() -> None:
    """A probability at/below REFUTE_THRESHOLD is a confident contradiction."""
    verdict, _ = classify_probability(REFUTE_THRESHOLD)
    assert verdict is Rung2Verdict.REFUTED
    verdict_low, _ = classify_probability(0.0)
    assert verdict_low is Rung2Verdict.REFUTED


def test_classify_probability_uncertain_band_escalates_not_passes() -> None:
    """The uncertain middle band collapses to ESCALATE, never a silent pass.

    A probability strictly between the refute floor and the entail floor
    must NOT certify (refute-first contract). It escalates to the rung-3
    jury instead.
    """
    midpoint = (REFUTE_THRESHOLD + ENTAIL_THRESHOLD) / 2
    verdict, reason = classify_probability(midpoint)
    assert verdict is Rung2Verdict.ESCALATE
    assert verdict is not Rung2Verdict.ENTAILED
    assert "escalate" in reason.lower()


def test_classify_probability_just_below_entail_floor_escalates() -> None:
    """A probability a hair below the entail floor escalates (off-by-one boundary)."""
    verdict, _ = classify_probability(ENTAIL_THRESHOLD - 0.001)
    assert verdict is Rung2Verdict.ESCALATE


def test_classify_probability_just_above_refute_floor_escalates() -> None:
    """A probability a hair above the refute floor escalates (off-by-one boundary)."""
    verdict, _ = classify_probability(REFUTE_THRESHOLD + 0.001)
    assert verdict is Rung2Verdict.ESCALATE


@pytest.mark.parametrize("bad", [-0.1, 1.1, 2.0, -1.0])
def test_classify_probability_out_of_range_raises(bad: float) -> None:
    """An entailment probability outside [0, 1] raises ValueError."""
    with pytest.raises(ValueError, match="out of range"):
        classify_probability(bad)


# --------------------------------------------------------------------------- #
# verdict_to_status: three-way verdict -> persisted binary status.
# --------------------------------------------------------------------------- #
def test_verdict_to_status_mapping() -> None:
    """ENTAILED->pass, REFUTED->fail, ESCALATE->blocked."""
    assert verdict_to_status(Rung2Verdict.ENTAILED) == "pass"
    assert verdict_to_status(Rung2Verdict.REFUTED) == "fail"
    assert verdict_to_status(Rung2Verdict.ESCALATE) == "blocked"


# --------------------------------------------------------------------------- #
# looks_numeric / route_claim_to_rung: numeric claims forced to rung-1.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "claim",
    [
        "latency dropped 40%",
        "coverage >= 0.9",
        "the speedup is 3.5x",
        "p99 < 200ms",
        "throughput rose by 12 requests",
    ],
)
def test_looks_numeric_detects_measured_assertions(claim: str) -> None:
    """A claim carrying a number / percentage / comparison reads as numeric."""
    assert looks_numeric(claim) is True


@pytest.mark.parametrize(
    "claim",
    [
        "the gate now refutes uncertain claims",
        "evidence text entails the hypothesis",
        "the scorer runs in-process with no network egress",
    ],
)
def test_looks_numeric_text_claims_are_not_numeric(claim: str) -> None:
    """A prose claim with no measured assertion is not numeric."""
    assert looks_numeric(claim) is False


def test_looks_numeric_identifier_with_digit_is_not_numeric() -> None:
    """A digit glued to a leading letter (an identifier) does not read as numeric."""
    assert looks_numeric("the v2 schema validates") is False
    assert looks_numeric("encode as utf8 text") is False


def test_route_claim_numeric_forces_rung1() -> None:
    """A numeric claim is forced to rung-1 (NLI is text-only)."""
    assert route_claim_to_rung("latency dropped 40%") is ClaimRung.RUNG1


def test_route_claim_text_routes_to_rung2() -> None:
    """A text claim routes to rung-2 for in-process entailment scoring."""
    assert route_claim_to_rung("the evidence entails the claim") is ClaimRung.RUNG2


def test_route_claim_empty_raises() -> None:
    """An empty / whitespace-only claim cannot be routed and raises ValueError."""
    with pytest.raises(ValueError, match="non-empty"):
        route_claim_to_rung("   ")


# --------------------------------------------------------------------------- #
# LexicalEntailmentScorer: the zero-dependency default backend.
# --------------------------------------------------------------------------- #
def test_lexical_scorer_full_overlap_scores_high() -> None:
    """A premise containing every claim content token scores 1.0."""
    scorer = LexicalEntailmentScorer()
    [score] = scorer.score_batch([("the gate refutes uncertain claims", "gate refutes claims")])
    assert score == pytest.approx(1.0)


def test_lexical_scorer_no_overlap_scores_zero() -> None:
    """A premise sharing no content token with the claim scores 0.0."""
    scorer = LexicalEntailmentScorer()
    [score] = scorer.score_batch([("completely unrelated premise wording", "orthogonal claim")])
    assert score == pytest.approx(0.0)


def test_lexical_scorer_empty_hypothesis_uses_default() -> None:
    """A contentless hypothesis returns the configured empty-hypothesis score."""
    scorer = LexicalEntailmentScorer()
    [score] = scorer.score_batch([("any premise", "the a of")])
    assert score == pytest.approx(0.0)


def test_lexical_scorer_is_runtime_entailment_scorer() -> None:
    """The lexical default satisfies the EntailmentScorer Protocol."""
    assert isinstance(LexicalEntailmentScorer(), EntailmentScorer)


# --------------------------------------------------------------------------- #
# score_claim / score_claims: refute-first scoring over a fake scorer.
# --------------------------------------------------------------------------- #
def test_score_claim_entails_on_high_probability() -> None:
    """A high entailment probability certifies the claim (ENTAILED)."""
    result = score_claim("c", "evidence", scorer=_FakeScorer([0.95]))
    assert isinstance(result, Rung2ClaimResult)
    assert result.verdict is Rung2Verdict.ENTAILED
    assert result.probability == pytest.approx(0.95)


def test_score_claim_refutes_on_low_probability() -> None:
    """A low entailment probability refutes the claim (REFUTED)."""
    result = score_claim("c", "evidence", scorer=_FakeScorer([0.05]))
    assert result.verdict is Rung2Verdict.REFUTED


def test_score_claim_escalates_on_uncertain_probability() -> None:
    """An uncertain probability escalates (refute-first; no silent pass)."""
    result = score_claim("c", "evidence", scorer=_FakeScorer([0.5]))
    assert result.verdict is Rung2Verdict.ESCALATE


def test_score_claims_passes_evidence_as_premise_claim_as_hypothesis() -> None:
    """The scorer is handed (evidence, claim) — premise first, hypothesis second."""
    fake = _FakeScorer([0.9])
    score_claims([("my claim text", "my evidence text")], scorer=fake)
    assert fake.seen == [("my evidence text", "my claim text")]


def test_score_claims_batch_of_mixed_verdicts() -> None:
    """A batch of claims yields one ordered result per pair with mixed verdicts."""
    fake = _FakeScorer([0.95, 0.5, 0.05])
    pairs = [("a", "ev-a"), ("b", "ev-b"), ("c", "ev-c")]
    results = score_claims(pairs, scorer=fake)
    assert [r.verdict for r in results] == [
        Rung2Verdict.ENTAILED,
        Rung2Verdict.ESCALATE,
        Rung2Verdict.REFUTED,
    ]
    assert [r.claim for r in results] == ["a", "b", "c"]


def test_score_claims_empty_batch_returns_empty_without_calling_scorer() -> None:
    """An empty batch returns an empty list and never calls the scorer (boundary)."""
    fake = _FakeScorer([])
    assert score_claims([], scorer=fake) == []
    assert fake.seen == []


def test_score_claims_empty_claim_raises() -> None:
    """A whitespace-only claim in the batch raises ValueError (error path)."""
    with pytest.raises(ValueError, match="non-empty"):
        score_claims([("ok", "ev"), ("   ", "ev2")], scorer=_FakeScorer([0.9, 0.9]))


def test_score_claims_with_lexical_default_refutes_unsupported_claim() -> None:
    """End-to-end refute-first: lexical scorer refutes a claim its evidence omits.

    The evidence text shares no content token with the claim, so the
    lexical overlap is 0.0 -> REFUTED. This exercises the real default
    backend (no fake), proving the refute-first wiring holds with the
    shipped scorer.
    """
    result = score_claim(
        "the daemon is the sole canonical mutator",
        "an unrelated sentence about colour palettes",
        scorer=LexicalEntailmentScorer(),
    )
    assert result.verdict is Rung2Verdict.REFUTED


# --------------------------------------------------------------------------- #
# run_rung2_gate: persisted EvidenceRecord shape.
# --------------------------------------------------------------------------- #
def test_run_rung2_gate_entailed_certifies_and_carries_probability() -> None:
    """An ENTAILED verdict yields a passing jury-kind record with the probability metric."""
    record = run_rung2_gate("c", "evidence", scope_id=_SCOPE, scorer=_FakeScorer([0.92]))
    assert isinstance(record, EvidenceRecord)
    assert record.status == "pass"
    assert record.evidence_kind == "jury"
    assert record.produced_by == "tool"
    assert record.scope_id == _SCOPE
    assert record.metrics is not None
    assert record.metrics["entailment_probability"] == pytest.approx(0.92)
    assert "entailed" in record.summary


def test_run_rung2_gate_escalate_is_blocked_status() -> None:
    """An ESCALATE verdict persists as a 'blocked' row (the jury owns it)."""
    record = run_rung2_gate("c", "evidence", scope_id=_SCOPE, scorer=_FakeScorer([0.5]))
    assert record.status == "blocked"
    assert "escalate" in record.summary


def test_run_rung2_gate_refuted_is_fail_status() -> None:
    """A REFUTED verdict persists as a 'fail' row."""
    record = run_rung2_gate("c", "evidence", scope_id=_SCOPE, scorer=_FakeScorer([0.1]))
    assert record.status == "fail"


# --------------------------------------------------------------------------- #
# load_default_scorer: the pluggable seam degrades to lexical (no model).
# --------------------------------------------------------------------------- #
def test_load_default_scorer_degrades_to_lexical_when_model_absent() -> None:
    """With no optional model backend installed the factory returns the lexical scorer.

    The optional ``eawf.workflow.evidence.rung2_model`` module is NOT
    shipped this wave, so the guarded lazy import fails and the factory
    degrades gracefully — exactly the no-model contract. This is the
    documented intended path, not a failure.
    """
    scorer = load_default_scorer()
    assert isinstance(scorer, LexicalEntailmentScorer)
    assert isinstance(scorer, EntailmentScorer)


def test_load_default_scorer_returns_a_scorer_protocol() -> None:
    """Whatever the factory returns satisfies the EntailmentScorer Protocol."""
    assert isinstance(load_default_scorer(), EntailmentScorer)


# --------------------------------------------------------------------------- #
# Documented escalation contract: thresholds + <0.7 contingency are surfaced.
# --------------------------------------------------------------------------- #
def test_entail_threshold_is_the_documented_floor() -> None:
    """The entail floor is the documented 0.7 escalation threshold."""
    assert pytest.approx(0.7) == ENTAIL_THRESHOLD
    assert REFUTE_THRESHOLD < ENTAIL_THRESHOLD


def test_escalation_note_documents_the_jury_floor_contingency() -> None:
    """The operator-facing note names the <0.7 move-behind-jury contingency."""
    assert "0.7" in RUNG2_ESCALATION_NOTE
    assert "jury" in RUNG2_ESCALATION_NOTE.lower()
    assert "I03" in RUNG2_ESCALATION_NOTE


def test_rung2_config_snapshot_surfaces_the_contract() -> None:
    """Rung2Config.current() captures the live thresholds + escalation note."""
    cfg = Rung2Config.current()
    assert cfg.entail_threshold == pytest.approx(ENTAIL_THRESHOLD)
    assert cfg.refute_threshold == pytest.approx(REFUTE_THRESHOLD)
    assert cfg.escalation_note == RUNG2_ESCALATION_NOTE
    assert cfg.optional_model_extra == OPTIONAL_MODEL_EXTRA


def test_rung2_config_is_frozen() -> None:
    """Rung2Config is an immutable value object."""
    cfg = Rung2Config.current()
    with pytest.raises((AttributeError, TypeError)):
        cfg.entail_threshold = 0.5  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Public exports.
# --------------------------------------------------------------------------- #
def test_rung2_public_exports() -> None:
    """The evidence package re-exports the rung-2 surface."""
    from eawf.workflow import evidence

    assert evidence.run_rung2_gate is run_rung2_gate
    assert evidence.score_claims is score_claims
    assert evidence.route_claim_to_rung is route_claim_to_rung
    assert evidence.LexicalEntailmentScorer is LexicalEntailmentScorer
    assert evidence.load_default_scorer is load_default_scorer
