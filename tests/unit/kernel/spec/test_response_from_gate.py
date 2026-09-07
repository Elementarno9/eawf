"""Tests: :func:`response_from_gate` derives a clause from a criterion's gate.

A gated criterion that authored no :class:`ResponseClause` never reaches the
tier compute in :func:`validate_criterion_gate_refs`, so its ``oracle_tier``
stays ``None`` and every determinism metric reads the row as unproven. The
derivation closes that gap from the bound gate alone. Coverage:

* happy path -- a ``command_exit_zero``-gated criterion yields a clause whose
  ``gate_ref`` names that kind, and :func:`assign_oracle_tier` resolves it to
  ``T4_CONTRACT``;
* boundary -- empty ``gate_ids``, ``gate_ids`` that resolve to nothing, and an
  already-authored ``response`` each derive nothing;
* boundary -- two bound gates of different tiers pick the CHEAPER kind, and a
  single-site gate is skipped when the criterion text claims universal scope
  (the pairing :meth:`CriterionSpec._scope_agreement` rejects);
* error path -- a gate kind outside the tier / response maps raises
  ``ValueError``;
* structural -- the response map's key set equals the tier map's, and no value
  uses a verb that would invalidate the derived clause.
"""

from __future__ import annotations

import pytest

from eawf.kernel.spec.common import (
    _GATE_KIND_RESPONSE,
    _GATE_KIND_TIER,
    CriterionSpec,
    GateSpec,
    ObserveVerb,
    OracleTier,
    ProofLocus,
    QualityDimension,
    ResponseClause,
    assign_oracle_tier,
    response_from_gate,
)

#: Criterion prose free of :data:`UNIVERSAL_SCOPE_TOKENS` so the single-witness
#: derivation is not skipped by the scope-agreement guard.
_TEXT = "derive the response clause from the bound gate"

#: Clears the 20-char ``measurable_signal`` floor and becomes the derived
#: clause's ``object``.
_SIGNAL = "the derived clause names the bound gate kind"


def _criterion(
    *,
    gate_ids: list[str],
    response: ResponseClause | None = None,
    text: str = _TEXT,
) -> CriterionSpec:
    """Build a typed criterion binding *gate_ids*."""
    return CriterionSpec(
        id="CR-01",
        text=text,
        kind="contract",
        acceptance_style="binary",
        evidence_kind="deterministic",
        gate_ids=gate_ids,
        quality_dimension=QualityDimension.FUNCTIONAL_SUITABILITY,
        measurable_signal=_SIGNAL,
        response=response,
    )


def _gate(gate_id: str, kind: str) -> GateSpec:
    """Build a gate row of *kind* bound to ``CR-01``."""
    args = {"argv": ["pytest", "-q"]} if kind == "command_exit_zero" else {}
    return GateSpec(
        id=gate_id,
        criterion_id="CR-01",
        kind=kind,
        args=args,
        policy="block",
        cadence="every-wave",
    )


def test_response_from_gate_command_exit_zero_yields_contract_tier() -> None:
    """A command_exit_zero-gated criterion derives a T4_CONTRACT clause."""
    gate = _gate("G-01", "command_exit_zero")
    criterion = _criterion(gate_ids=[gate.id])

    derived = response_from_gate(criterion, [gate])

    assert derived is not None
    assert derived.gate_ref == "command_exit_zero"
    assert derived.quantifier == "single"
    assert derived.object == _SIGNAL
    assert derived.expected is None
    assert derived.jury_reason is None
    assert derived.observe is ObserveVerb.EXITS
    assert derived.locus is ProofLocus.PYTEST
    assert assign_oracle_tier(derived) is OracleTier.T4_CONTRACT


def test_response_from_gate_empty_gate_ids_returns_none() -> None:
    """A criterion binding no gate has nothing to derive from."""
    criterion = _criterion(gate_ids=[])

    assert response_from_gate(criterion, [_gate("G-01", "command_exit_zero")]) is None


def test_response_from_gate_unresolvable_gate_ids_returns_none() -> None:
    """gate_ids that resolve to no gate in the candidate set derive nothing."""
    criterion = _criterion(gate_ids=["G-99"])

    assert response_from_gate(criterion, [_gate("G-01", "command_exit_zero")]) is None


def test_response_from_gate_existing_response_is_not_overwritten() -> None:
    """An authored response clause is left exactly as the author wrote it."""
    authored = ResponseClause(
        observe=ObserveVerb.RETURNS,
        object="the authored clause survives derivation",
        locus=ProofLocus.PYTEST,
    )
    criterion = _criterion(gate_ids=["G-01"], response=authored)

    assert response_from_gate(criterion, [_gate("G-01", "command_exit_zero")]) is None


def test_response_from_gate_picks_the_cheaper_of_two_bound_gates() -> None:
    """Two bound gates of different tiers resolve to the cheaper gate's kind."""
    contract = _gate("G-01", "command_exit_zero")
    static = _gate("G-02", "regex_in_file")
    criterion = _criterion(gate_ids=[contract.id, static.id])

    derived = response_from_gate(criterion, [contract, static])

    assert derived is not None
    assert derived.gate_ref == "regex_in_file"
    assert assign_oracle_tier(derived) is OracleTier.T1_STATIC


def test_response_from_gate_skips_single_site_gate_under_universal_text() -> None:
    """Universal prose over a single-site gate derives nothing, not a bad row."""
    static = _gate("G-01", "regex_in_file")
    criterion = _criterion(
        gate_ids=[static.id],
        text="every rendered row carries the derived response clause",
    )

    assert response_from_gate(criterion, [static]) is None


def test_response_from_gate_unknown_gate_kind_raises() -> None:
    """A gate kind outside the tier / response maps is rejected by name."""
    criterion = _criterion(gate_ids=["G-01"])
    unknown = _gate("G-01", "not_a_registered_kind")

    with pytest.raises(ValueError, match="unknown gate kind"):
        response_from_gate(criterion, [unknown])


def test_response_from_gate_map_covers_every_tiered_gate_kind() -> None:
    """Each tiered gate kind has a derivable clause shape, and no other."""
    assert set(_GATE_KIND_RESPONSE) == set(_GATE_KIND_TIER)


def test_response_from_gate_map_omits_the_unusable_verbs() -> None:
    """HOLDS_FOR_ALL and JUDGED would invalidate a derived single-witness clause."""
    verbs = {observe for observe, _locus in _GATE_KIND_RESPONSE.values()}

    assert ObserveVerb.HOLDS_FOR_ALL not in verbs
    assert ObserveVerb.JUDGED not in verbs
