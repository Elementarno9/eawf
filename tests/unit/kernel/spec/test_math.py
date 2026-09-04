"""Tests for the verification-grounded math doc-type.

Covers the typed :class:`~eawf.kernel.spec.math.MathClaim` four-facet
contract (intuition + runnable example gate + regime + citation), the closed
``MathAssurance`` Literal (refute / certify), the
:class:`~eawf.kernel.spec.math.MathExplainer` promotable-iff invariant
(every claim's gate runnable AND its citation resolves), and the
:attr:`~eawf.kernel.state.enums.ArtifactKind.MATH_EXPLAINER` URN route.
"""

from __future__ import annotations

from typing import Any, get_args

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.math import MathAssurance, MathClaim, MathExplainer
from eawf.kernel.state import urn
from eawf.kernel.state.enums import ArtifactKind

# A known-clean argv: an allowlisted wrapper (``uv run``) + tool (``pytest``)
# that passes the L0 argv-policy the GateSpec model-validator enforces.
_CLEAN_ARGV = ["uv", "run", "pytest", "-q"]


def _runnable_gate(gate_id: str = "G1") -> dict[str, Any]:
    """Return a runnable ``command_exit_zero`` GateSpec payload (clean argv)."""
    return {
        "id": gate_id,
        "criterion_id": "C1",
        "kind": "command_exit_zero",
        "args": {"argv": list(_CLEAN_ARGV)},
        "policy": "block",
        "cadence": "every-wave",
    }


def _non_runnable_gate(gate_id: str = "G1") -> dict[str, Any]:
    """Return a non-``command_exit_zero`` GateSpec payload (not runnable).

    A ``regex_match`` gate is a valid GateSpec (it skips the argv check) but
    hosts no runnable command, so :meth:`MathClaim.gate_is_runnable` is False.
    """
    return {
        "id": gate_id,
        "criterion_id": "C1",
        "kind": "regex_match",
        "args": {"pattern": r"^OK$", "input": "OK"},
        "policy": "block",
        "cadence": "every-wave",
    }


def _citation(ref: str = "urn:eawf:v1:artifact:QR/ART-x") -> dict[str, Any]:
    """Return an EvidenceRef payload with the given ``ref``."""
    return {"kind": "artifact", "ref": ref, "summary": "canonical source for the claim"}


def _claim_payload(**overrides: Any) -> dict[str, Any]:
    """Return a minimal-valid MathClaim payload with all four facets present."""
    payload: dict[str, Any] = {
        "statement": "the inverse-payoff hedge delta equals (C_S - C/S)/S",
        "intuition": "the hedge ratio is the spot sensitivity net of the payoff scaling",
        "example_gate": _runnable_gate(),
        "regime": "real positive spot; continuous payoff differentiable in S",
        "assumptions": ["spot S > 0", "payoff C is C1 in S"],
        "citation": _citation(),
        "verifier": "sympy+mpmath",
        "assurance": "refute",
    }
    payload.update(overrides)
    return payload


def _explainer_payload(**overrides: Any) -> dict[str, Any]:
    """Return a minimal-valid MathExplainer payload (one grounded claim)."""
    payload: dict[str, Any] = {
        "title": "Inverse-payoff hedge delta",
        "slug": "inverse-payoff-delta",
        "claims": [_claim_payload()],
    }
    payload.update(overrides)
    return payload


# MathAssurance Literal --------------------------------------------------


def test_math_assurance_has_exactly_two_members() -> None:
    """The closed assurance vocabulary is exactly ``refute`` and ``certify``."""
    assert set(get_args(MathAssurance)) == {"refute", "certify"}


# MathClaim — happy path -------------------------------------------------


def test_math_claim_happy_path_round_trip() -> None:
    """A minimal-valid MathClaim with all four facets round-trips through JSON."""
    claim = MathClaim.model_validate(_claim_payload())
    reloaded = MathClaim.model_validate_json(claim.model_dump_json())
    assert reloaded == claim
    assert reloaded.assurance == "refute"
    assert reloaded.verifier == "sympy+mpmath"


@pytest.mark.parametrize("assurance", ["refute", "certify"])
def test_math_claim_each_assurance_value(assurance: str) -> None:
    """Both members of the closed assurance Literal validate."""
    claim = MathClaim.model_validate(_claim_payload(assurance=assurance))
    assert claim.assurance == assurance


def test_math_claim_assumptions_default_empty() -> None:
    """``assumptions`` defaults to an empty list when the regime headline suffices."""
    payload = _claim_payload()
    del payload["assumptions"]
    claim = MathClaim.model_validate(payload)
    assert claim.assumptions == []


# MathClaim — strict mode + closed Literal errors ------------------------


def test_math_claim_rejects_unknown_key() -> None:
    """extra='forbid' rejects an undeclared key (rule 2)."""
    with pytest.raises(ValidationError) as exc_info:
        MathClaim.model_validate(_claim_payload(bogus_facet=True))
    assert "bogus_facet" in str(exc_info.value)


def test_math_claim_rejects_invalid_assurance() -> None:
    """An out-of-vocabulary assurance is rejected and the bad value appears."""
    with pytest.raises(ValidationError) as exc_info:
        MathClaim.model_validate(_claim_payload(assurance="proven"))
    message = str(exc_info.value)
    assert "proven" in message
    assert "assurance" in message


# MathClaim — 4-facet presence enforced ----------------------------------


@pytest.mark.parametrize(
    "facet",
    ["intuition", "example_gate", "regime", "citation"],
)
def test_math_claim_missing_each_facet_raises(facet: str) -> None:
    """Dropping any of the four facets raises ValidationError naming it.

    The four facets are required fields: (a) intuition, (b) example_gate
    (the runnable example), (c) regime (assumptions/regime-of-validity),
    (d) citation. A claim missing any one cannot be constructed.
    """
    payload = _claim_payload()
    del payload[facet]
    with pytest.raises(ValidationError) as exc_info:
        MathClaim.model_validate(payload)
    assert facet in str(exc_info.value)


def test_math_claim_rejects_empty_intuition() -> None:
    """An empty intuition fails the min_length=1 floor (no citation-as-intuition)."""
    with pytest.raises(ValidationError) as exc_info:
        MathClaim.model_validate(_claim_payload(intuition=""))
    assert "intuition" in str(exc_info.value)


def test_math_claim_rejects_empty_regime() -> None:
    """An empty regime fails the min_length=1 floor."""
    with pytest.raises(ValidationError) as exc_info:
        MathClaim.model_validate(_claim_payload(regime=""))
    assert "regime" in str(exc_info.value)


# MathClaim — grounded predicate (gate runnable + citation resolves) -----


def test_math_claim_grounded_when_gate_runnable_and_citation_resolves() -> None:
    """A claim with a runnable gate + resolving citation is grounded."""
    claim = MathClaim.model_validate(_claim_payload())
    assert claim.gate_is_runnable() is True
    assert claim.citation_resolves() is True
    assert claim.is_grounded() is True


def test_math_claim_not_grounded_when_gate_not_runnable() -> None:
    """A non-command_exit_zero gate is not runnable, so the claim is not grounded."""
    claim = MathClaim.model_validate(_claim_payload(example_gate=_non_runnable_gate()))
    assert claim.gate_is_runnable() is False
    assert claim.is_grounded() is False


def test_math_claim_not_grounded_when_citation_blank() -> None:
    """A whitespace-only citation ref does not resolve, so the claim is not grounded."""
    claim = MathClaim.model_validate(_claim_payload(citation=_citation(ref="   ")))
    assert claim.citation_resolves() is False
    assert claim.is_grounded() is False


# MathExplainer — happy path + promotable-iff ----------------------------


def test_math_explainer_happy_path_round_trip() -> None:
    """A MathExplainer with one grounded claim round-trips and is promotable."""
    explainer = MathExplainer.model_validate(_explainer_payload())
    reloaded = MathExplainer.model_validate_json(explainer.model_dump_json())
    assert reloaded == explainer
    assert reloaded.is_promotable() is True
    assert reloaded.ungrounded_claim_indexes() == []


def test_math_explainer_rejects_empty_claims() -> None:
    """An explainer with no claims fails the min_length=1 bound (nothing to ground)."""
    with pytest.raises(ValidationError) as exc_info:
        MathExplainer.model_validate(_explainer_payload(claims=[]))
    assert "claims" in str(exc_info.value)


def test_math_explainer_rejects_unknown_key() -> None:
    """extra='forbid' rejects an undeclared key on the explainer (rule 2)."""
    with pytest.raises(ValidationError) as exc_info:
        MathExplainer.model_validate(_explainer_payload(bogus=1))
    assert "bogus" in str(exc_info.value)


def test_math_explainer_rejects_over_cap_title() -> None:
    """A title over 72 chars fails the entity-title bound."""
    with pytest.raises(ValidationError) as exc_info:
        MathExplainer.model_validate(_explainer_payload(title="x" * 73))
    assert "title" in str(exc_info.value)


def test_math_explainer_not_promotable_when_a_claim_gate_not_runnable() -> None:
    """An explainer is NOT promotable when any claim's gate is not runnable."""
    grounded = _claim_payload()
    ungrounded = _claim_payload(example_gate=_non_runnable_gate("G2"))
    explainer = MathExplainer.model_validate(_explainer_payload(claims=[grounded, ungrounded]))
    assert explainer.is_promotable() is False
    assert explainer.ungrounded_claim_indexes() == [1]


def test_math_explainer_not_promotable_when_a_citation_blank() -> None:
    """An explainer is NOT promotable when any claim's citation does not resolve."""
    grounded = _claim_payload()
    ungrounded = _claim_payload(citation=_citation(ref=""))
    explainer = MathExplainer.model_validate(_explainer_payload(claims=[grounded, ungrounded]))
    assert explainer.is_promotable() is False
    assert explainer.ungrounded_claim_indexes() == [1]


# ArtifactKind.MATH_EXPLAINER registration + URN route -------------------


def test_artifact_kind_math_explainer_registered() -> None:
    """``MATH_EXPLAINER`` is a registered ArtifactKind member with the wire value."""
    assert ArtifactKind.MATH_EXPLAINER.value == "math_explainer"
    assert ArtifactKind("math_explainer") is ArtifactKind.MATH_EXPLAINER


def test_math_explainer_urn_route_resolves_to_artifact_kind() -> None:
    """The MATH_EXPLAINER URN route maps onto the single ``artifact`` URN kind."""
    assert urn.artifact_kind_urn_kind(ArtifactKind.MATH_EXPLAINER) == "artifact"
    assert urn.ARTIFACT_URN_KIND == "artifact"


def test_math_explainer_urn_round_trips() -> None:
    """A MATH_EXPLAINER artifact URN builds + parses back to identity."""
    kind = urn.artifact_kind_urn_kind(ArtifactKind.MATH_EXPLAINER)
    built = urn.build(kind, owner="QR", id="ART-math-explainer-inverse-delta")
    parsed = urn.parse(built)
    assert parsed.kind == "artifact"
    assert parsed.owner == "QR"
    assert parsed.id == "ART-math-explainer-inverse-delta"
    assert parsed.identity() == built


def test_artifact_kind_urn_kind_rejects_non_member() -> None:
    """A non-ArtifactKind value is refused rather than emitting a malformed URN."""
    with pytest.raises(ValueError, match="not an ArtifactKind"):
        urn.artifact_kind_urn_kind("math_explainer")  # type: ignore[arg-type]


def test_every_artifact_kind_routes_to_artifact_urn_kind() -> None:
    """Every registered ArtifactKind — including new members — has a URN route.

    Pins the ``ArtifactKind`` docstring contract: adding a kind requires a
    documented URN routing rule, so a member added without one fails here.
    """
    for kind in ArtifactKind:
        assert urn.artifact_kind_urn_kind(kind) == "artifact"
