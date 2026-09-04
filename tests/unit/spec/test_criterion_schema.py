"""Schema tests for the typed :class:`CriterionSpec` (oracle / EARS / quality).

Covers the typed criteria CR-1..CR-4 of the FS01 spec:

* CR-1 (returns, pytest): a fully-populated CriterionSpec carrying a
  ``response`` clause, ``quality_dimension``, and ``measurable_signal``
  validates, plus the ``measurable_signal`` length boundary (20-char floor).
* CR-2 (raises, contract): a JUDGED response clause with no ``jury_reason``
  fails the ``_judged_requires_reason`` validator.
* CR-3 (raises, contract): an unknown extra field fails the inherited
  ``extra="forbid"`` config.
* back-compat: a pre-existing :class:`WaveBehavior` row (OPERABILITY
  dimension) still validates after the QualityDimension relocation.
* CR-4 (holds_for_all, hypothesis): JSON round-trip identity over a
  Hypothesis strategy that builds valid CriterionSpec instances.
* REL-002 (raises, contract): the scope-agreement ceiling -- universal
  prose paired with a single-witness gate fails at ``model_validate``,
  while a widened proof or narrowed prose passes.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from eawf.kernel.spec.common import (
    UNIVERSAL_SCOPE_TOKENS,
    CriterionSpec,
    ObserveVerb,
    OracleTier,
    ProofLocus,
    QualityDimension,
    ResponseClause,
    _claims_universal_scope,
)
from eawf.kernel.spec.wave import QualityDimension as WaveQualityDimension
from eawf.kernel.spec.wave import WaveBehavior

# A measurable_signal string of exactly 20 characters (the min_length floor).
_SIGNAL_20 = "x" * 20
# One short of the floor.
_SIGNAL_19 = "x" * 19


# --------------------------------------------------------------------------- #
# CR-1 — a fully-populated criterion validates.
# --------------------------------------------------------------------------- #
def test_criterion_spec_full_validates() -> None:
    """A criterion with response + quality_dimension + measurable_signal validates."""
    criterion = CriterionSpec(
        id="CR-1",
        text="the loader rejects an unknown key",
        kind="behavioral",
        acceptance_style="binary",
        evidence_kind="deterministic",
        quality_dimension=QualityDimension.FUNCTIONAL_SUITABILITY,
        measurable_signal="the loader raises ValidationError on an extra key",
        response=ResponseClause(
            observe=ObserveVerb.RAISES,
            object="ValidationError",
            locus=ProofLocus.PYTEST,
            expected="extra fields not permitted",
        ),
        oracle_tier=OracleTier.T1_STATIC,
    )
    assert criterion.response is not None
    assert criterion.response.observe is ObserveVerb.RAISES
    assert criterion.quality_dimension is QualityDimension.FUNCTIONAL_SUITABILITY
    assert criterion.oracle_tier is OracleTier.T1_STATIC


def test_criterion_spec_oracle_tier_defaults_none() -> None:
    """oracle_tier is None by default; the runner assigns it later."""
    criterion = CriterionSpec(
        id="CR-1",
        text="default oracle tier",
        kind="behavioral",
        acceptance_style="binary",
        evidence_kind="deterministic",
        quality_dimension=QualityDimension.RELIABILITY,
        measurable_signal="the criterion carries no pre-assigned oracle tier",
    )
    assert criterion.oracle_tier is None
    assert criterion.response is None


# --------------------------------------------------------------------------- #
# CR-1 boundary — measurable_signal length floor.
# --------------------------------------------------------------------------- #
def test_criterion_spec_measurable_signal_min_length_boundary() -> None:
    """A measurable_signal of exactly 20 chars passes the min_length floor."""
    criterion = CriterionSpec(
        id="CR-1",
        text="boundary check",
        kind="behavioral",
        acceptance_style="binary",
        evidence_kind="deterministic",
        quality_dimension=QualityDimension.FUNCTIONAL_SUITABILITY,
        measurable_signal=_SIGNAL_20,
    )
    assert criterion.measurable_signal == _SIGNAL_20


def test_criterion_spec_measurable_signal_below_min_length_raises() -> None:
    """A measurable_signal of 19 chars is one short and raises ValidationError."""
    with pytest.raises(ValidationError, match="at least 20 characters"):
        CriterionSpec(
            id="CR-1",
            text="below floor",
            kind="behavioral",
            acceptance_style="binary",
            evidence_kind="deterministic",
            quality_dimension=QualityDimension.FUNCTIONAL_SUITABILITY,
            measurable_signal=_SIGNAL_19,
        )


# --------------------------------------------------------------------------- #
# CR-2 — JUDGED response demands a jury_reason.
# --------------------------------------------------------------------------- #
def test_criterion_spec_judged_without_reason_raises() -> None:
    """A JUDGED response clause with no jury_reason fails the validator."""
    with pytest.raises(ValidationError, match="jury_reason"):
        CriterionSpec(
            id="CR-2",
            text="judged criterion without a reason",
            kind="judgement",
            acceptance_style="graded",
            evidence_kind="jury",
            quality_dimension=QualityDimension.INTERACTION_CAPABILITY,
            measurable_signal="a jury votes the UX is acceptable on the rubric",
            response=ResponseClause(
                observe=ObserveVerb.JUDGED,
                object="x",
                locus=ProofLocus.JURY,
                jury_reason=None,
            ),
        )


def test_criterion_spec_judged_with_reason_validates() -> None:
    """A JUDGED response clause carrying a jury_reason validates."""
    criterion = CriterionSpec(
        id="CR-2",
        text="judged criterion with a reason",
        kind="judgement",
        acceptance_style="graded",
        evidence_kind="jury",
        quality_dimension=QualityDimension.INTERACTION_CAPABILITY,
        measurable_signal="a jury votes the UX is acceptable on the rubric",
        response=ResponseClause(
            observe=ObserveVerb.JUDGED,
            object="x",
            locus=ProofLocus.JURY,
            jury_reason="no deterministic oracle exists for subjective layout",
        ),
    )
    assert criterion.response is not None
    assert criterion.response.jury_reason


# --------------------------------------------------------------------------- #
# CR-3 — extra="forbid" rejects an unknown field.
# --------------------------------------------------------------------------- #
def test_criterion_spec_extra_field_raises() -> None:
    """An unknown extra field fails the inherited extra='forbid' config."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CriterionSpec(
            id="CR-3",
            text="criterion with a stray key",
            kind="behavioral",
            acceptance_style="binary",
            evidence_kind="deterministic",
            quality_dimension=QualityDimension.SECURITY,
            measurable_signal="the strict model forbids the unknown key",
            bogus_field="not allowed",  # type: ignore[call-arg]
        )


# --------------------------------------------------------------------------- #
# back-compat — WaveBehavior still validates after the relocation.
# --------------------------------------------------------------------------- #
def test_wave_behavior_operability_dimension_still_validates() -> None:
    """A jury-scorable WaveBehavior on OPERABILITY validates post-relocation."""
    behavior = WaveBehavior(
        id="B1",
        text="the cockpit pane refreshes within the latency budget",
        jury_scorable=True,
        quality_dimension=QualityDimension.OPERABILITY,
    )
    assert behavior.quality_dimension is QualityDimension.OPERABILITY


def test_wave_behavior_imports_relocated_dimension() -> None:
    """wave.QualityDimension is the same object relocated into common."""
    assert WaveQualityDimension is QualityDimension


# --------------------------------------------------------------------------- #
# REL-002 — the scope-agreement ceiling.
# --------------------------------------------------------------------------- #
def _scoped_criterion(
    *,
    text: str,
    kind: str = "behavioral",
    gate_ref: str | None = "regex_in_file",
    quantifier: str = "single",
    locus: ProofLocus = ProofLocus.SOURCE,
    with_response: bool = True,
) -> CriterionSpec:
    """Build a criterion whose prose breadth and proof breadth are both dialled."""
    response = (
        ResponseClause(
            observe=ObserveVerb.VALIDATES,
            object="the handler input",
            locus=locus,
            quantifier=quantifier,  # type: ignore[arg-type]
            gate_ref=gate_ref,
        )
        if with_response
        else None
    )
    return CriterionSpec(
        id="CR-SCOPE",
        text=text,
        kind=kind,
        acceptance_style="binary",
        evidence_kind="deterministic",
        gate_ids=["GATE-01"],
        quality_dimension=QualityDimension.FUNCTIONAL_SUITABILITY,
        measurable_signal="the handler rejects an unknown key at the boundary",
        response=response,
    )


def test_criterion_spec_universal_prose_with_single_site_gate_raises() -> None:
    """Universal prose proved by a one-file grep fails at model_validate."""
    with pytest.raises(ValidationError, match="fails scope agreement"):
        _scoped_criterion(text="every handler validates its own input")


def test_criterion_spec_scope_agreement_error_names_the_criterion_id() -> None:
    """The rejection quotes the criterion id so a plan author can find the row."""
    with pytest.raises(ValidationError, match="CR-SCOPE"):
        _scoped_criterion(text="all handlers validates their own input")


@pytest.mark.parametrize("token", list(UNIVERSAL_SCOPE_TOKENS))
def test_criterion_spec_each_universal_token_trips_scope_agreement(token: str) -> None:
    """Every member of the closed universal-token set widens the claim."""
    with pytest.raises(ValidationError, match="fails scope agreement"):
        _scoped_criterion(text=f"the loader {token} rejects an unknown key")


def test_criterion_spec_universal_prose_with_set_scanning_gate_validates() -> None:
    """Widening the proof to a set-scanning gate restores agreement."""
    criterion = _scoped_criterion(
        text="every handler validates its own input",
        gate_ref="criterion_in_diff",
    )
    assert criterion.response is not None
    assert criterion.response.gate_ref == "criterion_in_diff"


def test_criterion_spec_universal_prose_with_forall_clause_validates() -> None:
    """A forall clause at the hypothesis locus is a universal proof, so it agrees."""
    criterion = _scoped_criterion(
        text="every handler validates its own input",
        quantifier="forall",
        locus=ProofLocus.HYPOTHESIS,
    )
    assert criterion.response is not None
    assert criterion.response.quantifier == "forall"


def test_criterion_spec_narrow_prose_with_single_site_gate_validates() -> None:
    """A single-site claim proved by a single-site gate is the agreeing case."""
    criterion = _scoped_criterion(text="the loader validates one unknown key")
    assert criterion.text == "the loader validates one unknown key"


def test_criterion_spec_universal_prose_without_response_validates() -> None:
    """No response clause means no proof breadth to disagree with (empty case)."""
    criterion = _scoped_criterion(
        text="every handler validates its own input",
        with_response=False,
    )
    assert criterion.response is None


def test_criterion_spec_scope_agreement_exempts_grandfathered_kind() -> None:
    """A legacy row carries a synthesised clause, so the ceiling does not apply."""
    criterion = _scoped_criterion(
        text="every handler validates its own input",
        kind="legacy",
    )
    assert criterion.kind == "legacy"


def test_criterion_spec_scope_agreement_exempts_converted_kind() -> None:
    """A converter-built row is machine-authored prose, so it is exempt too."""
    criterion = _scoped_criterion(
        text="every handler validates its own input",
        kind="converted",
    )
    assert criterion.kind == "converted"


@pytest.mark.parametrize("word", ["overall", "anywhere", "alleged", "nevermore"])
def test_claims_universal_scope_ignores_substring_matches(word: str) -> None:
    """Word-boundary anchoring keeps ``overall`` from reading as ``all``."""
    assert _claims_universal_scope(f"the loader validates the {word} case") is False


def test_claims_universal_scope_empty_text() -> None:
    """Empty prose claims nothing (boundary: empty)."""
    assert _claims_universal_scope("") is False


def test_claims_universal_scope_is_case_insensitive() -> None:
    """A sentence-leading ``Every`` still reads as a universal claim."""
    assert _claims_universal_scope("Every handler validates its input") is True


# --------------------------------------------------------------------------- #
# CR-4 — JSON round-trip identity over a strategy of valid criteria.
# --------------------------------------------------------------------------- #
def _valid_id() -> st.SearchStrategy[str]:
    """Generate an IdStr-shaped token (non-empty, no whitespace)."""
    return st.text(
        alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="-_"),
        min_size=1,
        max_size=12,
    )


def _response_clauses() -> st.SearchStrategy[ResponseClause | None]:
    """Generate an optional ResponseClause; JUDGED always carries a reason."""
    non_judged = st.builds(
        ResponseClause,
        observe=st.sampled_from([v for v in ObserveVerb if v is not ObserveVerb.JUDGED]),
        object=st.text(min_size=1, max_size=40),
        locus=st.sampled_from(list(ProofLocus)),
        expected=st.none() | st.text(max_size=40),
        quantifier=st.sampled_from(["single", "forall"]),
        gate_ref=st.none(),
        jury_reason=st.none() | st.text(min_size=1, max_size=40),
    )
    judged = st.builds(
        ResponseClause,
        observe=st.just(ObserveVerb.JUDGED),
        object=st.text(min_size=1, max_size=40),
        locus=st.just(ProofLocus.JURY),
        expected=st.none(),
        quantifier=st.just("single"),
        gate_ref=st.none(),
        jury_reason=st.text(min_size=1, max_size=40),
    )
    return st.none() | non_judged | judged


@settings(max_examples=50, deadline=None)
@given(
    cid=_valid_id(),
    text=st.text(min_size=1, max_size=500),
    kind=st.text(min_size=1, max_size=30),
    acceptance_style=st.sampled_from(["binary", "graded"]),
    evidence_kind=st.sampled_from(["deterministic", "jury", "attested"]),
    required=st.booleans(),
    quality_dimension=st.sampled_from(list(QualityDimension)),
    measurable_signal=st.text(min_size=20, max_size=300),
    response=_response_clauses(),
    oracle_tier=st.none() | st.sampled_from(list(OracleTier)),
)
def test_criterion_spec_json_round_trip_identity(
    cid: str,
    text: str,
    kind: str,
    acceptance_style: str,
    evidence_kind: str,
    required: bool,
    quality_dimension: QualityDimension,
    measurable_signal: str,
    response: ResponseClause | None,
    oracle_tier: OracleTier | None,
) -> None:
    """Property: a valid CriterionSpec survives a JSON model dump + reload unchanged."""
    criterion = CriterionSpec(
        id=cid,
        text=text,
        kind=kind,
        acceptance_style=acceptance_style,  # type: ignore[arg-type]
        evidence_kind=evidence_kind,  # type: ignore[arg-type]
        required=required,
        quality_dimension=quality_dimension,
        measurable_signal=measurable_signal,
        response=response,
        oracle_tier=oracle_tier,
    )
    reloaded = CriterionSpec.model_validate_json(criterion.model_dump_json())
    assert reloaded == criterion
