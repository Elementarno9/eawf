"""Tests for :func:`assign_oracle_tier` (FS02 total tier function).

Covers the typed criteria CR-1..CR-3 of the FS02 spec:

* CR-1 (returns, contract): ``assign_oracle_tier`` is total over every
  :class:`ObserveVerb` member -- each member either returns an
  :class:`OracleTier` or raises a handled ``ValueError``; never a
  ``KeyError``. Deterministic verbs map to their ``_VERB_TIER`` entry;
  JUDGED routes to T6_APPROVAL (HUMAN locus) or T7_JURY (JURY locus).
* CR-2 (raises, contract): a ``forall`` quantifier with a non-hypothesis
  locus raises ``ValueError`` (substring "hypothesis").
* CR-3 (raises, contract): a JUDGED response with an empty ``jury_reason``
  raises ``ValueError`` (substring "jury_reason").
* error-path: a clause carrying a ``gate_ref`` routes to the
  ``_tier_for_gate_kind`` stub, which raises ``ValueError`` (substring
  "gate kind").
"""

from __future__ import annotations

import pytest

from eawf.kernel.spec.common import (
    _VERB_TIER,
    ObserveVerb,
    OracleTier,
    ProofLocus,
    ResponseClause,
    assign_oracle_tier,
)


def _clause(
    observe: ObserveVerb,
    *,
    locus: ProofLocus = ProofLocus.PYTEST,
    quantifier: str = "single",
    jury_reason: str | None = None,
    gate_ref: str | None = None,
) -> ResponseClause:
    """Build a minimal :class:`ResponseClause` for tier-assignment tests."""
    return ResponseClause(
        observe=observe,
        object="x",
        locus=locus,
        quantifier=quantifier,  # type: ignore[arg-type]
        jury_reason=jury_reason,
        gate_ref=gate_ref,
    )


@pytest.mark.parametrize("verb", list(_VERB_TIER))
def test_assign_oracle_tier_deterministic_verb_maps_to_table(verb: ObserveVerb) -> None:
    """Each deterministic verb returns exactly its ``_VERB_TIER`` entry."""
    assert assign_oracle_tier(_clause(verb)) == _VERB_TIER[verb]


@pytest.mark.parametrize("verb", list(ObserveVerb))
def test_assign_oracle_tier_total_over_every_member(verb: ObserveVerb) -> None:
    """Total: every ObserveVerb member returns or raises ValueError, never KeyError."""
    clause = _clause(verb, jury_reason="auditable" if verb is ObserveVerb.JUDGED else None)
    try:
        result = assign_oracle_tier(clause)
    except ValueError:
        # A handled fallthrough is acceptable; the contract forbids KeyError.
        return
    assert isinstance(result, OracleTier)


def test_assign_oracle_tier_judged_human_locus_returns_t6() -> None:
    """JUDGED with a HUMAN locus escalates to T6_APPROVAL."""
    clause = _clause(ObserveVerb.JUDGED, locus=ProofLocus.HUMAN, jury_reason="operator signs off")
    assert assign_oracle_tier(clause) is OracleTier.T6_APPROVAL


def test_assign_oracle_tier_judged_jury_locus_returns_t7() -> None:
    """JUDGED with a JURY locus escalates to T7_JURY."""
    clause = _clause(ObserveVerb.JUDGED, locus=ProofLocus.JURY, jury_reason="cross-vendor jury")
    assert assign_oracle_tier(clause) is OracleTier.T7_JURY


def test_assign_oracle_tier_forall_non_hypothesis_locus_raises() -> None:
    """A forall quantifier with a non-hypothesis locus raises ValueError."""
    clause = _clause(ObserveVerb.HOLDS_FOR_ALL, locus=ProofLocus.PYTEST, quantifier="forall")
    with pytest.raises(ValueError, match="hypothesis"):
        assign_oracle_tier(clause)


def test_assign_oracle_tier_forall_hypothesis_locus_returns_tier() -> None:
    """A forall quantifier with the hypothesis locus returns its table tier."""
    clause = _clause(ObserveVerb.HOLDS_FOR_ALL, locus=ProofLocus.HYPOTHESIS, quantifier="forall")
    assert assign_oracle_tier(clause) is OracleTier.T4_CONTRACT


def test_assign_oracle_tier_judged_empty_jury_reason_raises() -> None:
    """JUDGED with an empty jury_reason raises ValueError."""
    clause = _clause(ObserveVerb.JUDGED, locus=ProofLocus.JURY, jury_reason=None)
    with pytest.raises(ValueError, match="jury_reason"):
        assign_oracle_tier(clause)


def test_assign_oracle_tier_gate_ref_routes_to_gate_kind_stub() -> None:
    """A clause carrying a gate_ref routes to the gate-kind stub, which raises."""
    clause = _clause(ObserveVerb.RETURNS, gate_ref="GATE-1")
    with pytest.raises(ValueError, match="gate kind"):
        assign_oracle_tier(clause)
