"""Tests: :func:`validate_criterion_gate_refs` computes ``oracle_tier`` server-side.

Binds :func:`eawf.kernel.spec.common.assign_oracle_tier` into criterion
validation so a synced criterion's tier is no longer a vaporware ``None``:

* binding-proof (returns) -- a ``JUDGED`` + ``jury_reason`` + non-human-locus
  criterion run through the validator gets ``oracle_tier == T7_JURY``, and a
  ``command_exit_zero`` ``gate_ref`` criterion gets ``T4_CONTRACT`` per the
  gate-kind tier map;
* negative-path (raises) -- a ``JUDGED`` response with an empty ``jury_reason``
  raises ``ValueError`` at the validation binding point, and an input criterion
  carrying a non-``None`` author-set ``oracle_tier`` is rejected with
  ``ValueError`` (the author never owns the tier).

A malformed ``JUDGED`` clause is planted via :meth:`CriterionSpec.model_construct`
so it bypasses the ``_judged_requires_reason`` model validator and reaches the
``validate_criterion_gate_refs`` binding point under test -- the same
build-the-dict-directly tactic the close-gate oracle suite uses to plant a
forbidden tier on the wire.
"""

from __future__ import annotations

import pytest

from eawf.kernel.spec.common import (
    CriterionSpec,
    ObserveVerb,
    OracleTier,
    ProofLocus,
    QualityDimension,
    ResponseClause,
    validate_criterion_gate_refs,
)


def _criterion(
    *,
    response: ResponseClause,
    cid: str = "CR-01",
) -> CriterionSpec:
    """Build a validating :class:`CriterionSpec` carrying *response*."""
    return CriterionSpec(
        id=cid,
        text="bind assign_oracle_tier into criterion validation",
        kind="contract",
        acceptance_style="binary",
        evidence_kind="attested",
        quality_dimension=QualityDimension.FUNCTIONAL_SUITABILITY,
        measurable_signal="the validator computes the oracle tier server-side",
        response=response,
    )


def test_validate_criterion_gate_refs_judged_jury_computes_t7() -> None:
    """A JUDGED + jury_reason + non-human-locus criterion computes T7_JURY."""
    criterion = _criterion(
        response=ResponseClause(
            observe=ObserveVerb.JUDGED,
            object="the cross-vendor jury affirms the deliverable",
            locus=ProofLocus.JURY,
            jury_reason="no deterministic falsifier exists for this aesthetic claim",
        ),
    )
    assert criterion.oracle_tier is None

    validate_criterion_gate_refs([criterion], [])

    assert criterion.oracle_tier is OracleTier.T7_JURY


def test_validate_criterion_gate_refs_command_exit_zero_computes_t4() -> None:
    """A command_exit_zero gate_ref criterion computes T4_CONTRACT."""
    criterion = _criterion(
        response=ResponseClause(
            observe=ObserveVerb.EXITS,
            object="the gauntlet command exits zero",
            locus=ProofLocus.CLI_EXIT,
            gate_ref="command_exit_zero",
        ),
    )
    assert criterion.oracle_tier is None

    validate_criterion_gate_refs([criterion], [])

    assert criterion.oracle_tier is OracleTier.T4_CONTRACT


def test_validate_criterion_gate_refs_judged_empty_jury_reason_raises() -> None:
    """A JUDGED response with an empty jury_reason raises at the binding point."""
    response = ResponseClause(
        observe=ObserveVerb.JUDGED,
        object="the jury affirms",
        locus=ProofLocus.JURY,
        jury_reason=None,
    )
    # model_construct bypasses _judged_requires_reason so the malformed clause
    # reaches the validator binding under test rather than failing at build.
    criterion = CriterionSpec.model_construct(
        id="CR-01",
        text="judged criterion missing its jury reason",
        kind="contract",
        acceptance_style="binary",
        evidence_kind="attested",
        gate_ids=[],
        required=True,
        waiver_reason=None,
        quality_dimension=QualityDimension.FUNCTIONAL_SUITABILITY,
        measurable_signal="the validator rejects a reasonless judged clause",
        response=response,
        oracle_tier=None,
    )

    with pytest.raises(ValueError, match="jury_reason"):
        validate_criterion_gate_refs([criterion], [])


def test_validate_criterion_gate_refs_author_set_oracle_tier_rejected() -> None:
    """A criterion carrying a non-None author-set oracle_tier is rejected."""
    criterion = _criterion(
        response=ResponseClause(
            observe=ObserveVerb.JUDGED,
            object="the jury affirms",
            locus=ProofLocus.JURY,
            jury_reason="auditable fallthrough",
        ),
    )
    criterion.oracle_tier = OracleTier.T7_JURY

    with pytest.raises(ValueError, match="oracle_tier must not be author-set"):
        validate_criterion_gate_refs([criterion], [])
