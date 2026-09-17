"""Authoring floor and execution-contract compile over the planning-owned criterion types."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, get_args

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.common import (
    CriterionEvidenceKind,
    CriterionSpec,
    DeferredDeliverable,
    GatePolicy,
    GateSpec,
    ObserveVerb,
    OracleTier,
    ProofLocus,
    QualityDimension,
    ResponseClause,
    SourceUnit,
)
from eawf.workflow.audit_dsl.models import CheckKind
from eawf.workflow.delivery import criteria as criteria_module
from eawf.workflow.delivery.criteria import (
    MAX_CRITERIA_PER_SCOPE,
    AuthoringRule,
    CriteriaAuthoring,
    CriteriaAuthoringError,
    ExecutionContract,
    compile_execution_contracts,
    gate_kind_oracle_tier,
    validate_criteria_authoring,
)

SCOPE = "P99-I01-W01"
ARGV = ["uv", "run", "pytest", "tests/unit/sample/test_loader.py", "-q"]
SIGNAL = "pytest over the loader suite exits zero with one case per rule"


def _response(
    observe: ObserveVerb = ObserveVerb.EXITS,
    *,
    locus: ProofLocus = ProofLocus.PYTEST,
    quantifier: Literal["single", "forall"] = "single",
    gate_ref: str | None = "command_exit_zero",
    jury_reason: str | None = None,
) -> ResponseClause:
    return ResponseClause(
        observe=observe,
        object="zero from the loader suite",
        locus=locus,
        quantifier=quantifier,
        gate_ref=gate_ref,
        jury_reason=jury_reason,
    )


def _criterion(
    criterion_id: str = "CR-01",
    *,
    text: str = "the loader returns the parsed configuration",
    evidence_kind: CriterionEvidenceKind = "deterministic",
    gate_ids: tuple[str, ...] = ("G-01",),
    response: ResponseClause | None = None,
    oracle_tier: OracleTier | None = None,
) -> CriterionSpec:
    return CriterionSpec(
        id=criterion_id,
        text=text,
        kind="behavioral",
        acceptance_style="binary",
        evidence_kind=evidence_kind,
        gate_ids=list(gate_ids),
        quality_dimension=QualityDimension.FUNCTIONAL_SUITABILITY,
        measurable_signal=SIGNAL,
        response=response,
        oracle_tier=oracle_tier,
    )


def _command_gate(
    gate_id: str = "G-01",
    criterion_id: str = "CR-01",
    *,
    argv: list[str] | None = None,
    policy: GatePolicy = "block",
) -> GateSpec:
    return GateSpec(
        id=gate_id,
        criterion_id=criterion_id,
        kind="command_exit_zero",
        args={"argv": list(argv or ARGV)},
        policy=policy,
        cadence="every-wave",
    )


def _gate(gate_id: str, criterion_id: str, kind: str) -> GateSpec:
    return GateSpec(
        id=gate_id,
        criterion_id=criterion_id,
        kind=kind,
        args={"path": "src/sample/loader.py"},
        policy="block",
        cadence="every-wave",
    )


def _authoring(
    criteria: list[CriterionSpec],
    gates: list[GateSpec],
    **extra: Any,
) -> CriteriaAuthoring:
    return CriteriaAuthoring(scope_id=SCOPE, criteria=tuple(criteria), gates=tuple(gates), **extra)


def _clean() -> CriteriaAuthoring:
    return _authoring([_criterion(response=_response())], [_command_gate()])


def _numbered_set(count: int) -> CriteriaAuthoring:
    criteria = [_criterion(f"CR-{n:02d}", gate_ids=(f"G-{n:02d}",)) for n in range(1, count + 1)]
    gates = [_command_gate(f"G-{n:02d}", f"CR-{n:02d}") for n in range(1, count + 1)]
    return _authoring(criteria, gates)


def _rules(authoring: CriteriaAuthoring) -> list[AuthoringRule]:
    return [finding.rule for finding in validate_criteria_authoring(authoring)]


# One builder per authoring rule; each yields a set that breaks exactly that rule.
_REJECTION_CASES: dict[AuthoringRule, Callable[[], CriteriaAuthoring]] = {
    AuthoringRule.UNRESOLVED_GATE: lambda: _authoring(
        [_criterion(gate_ids=("G-01", "G-09"))], [_command_gate()]
    ),
    AuthoringRule.ORPHAN_GATE: lambda: _authoring(
        [_criterion()], [_command_gate(), _command_gate("G-02", "CR-01")]
    ),
    AuthoringRule.UNGATED_CRITERION: lambda: _authoring([_criterion(gate_ids=())], []),
    AuthoringRule.UNKNOWN_ORACLE: lambda: _authoring(
        [_criterion()], [_gate("G-01", "CR-01", "made_up_kind")]
    ),
    AuthoringRule.UNCOMPILED_GATE: _clean,
    AuthoringRule.DETERMINISTIC_JURY_ROUTE: lambda: _authoring(
        [
            _criterion(
                response=_response(
                    ObserveVerb.JUDGED,
                    locus=ProofLocus.JURY,
                    gate_ref=None,
                    jury_reason="readability has no deterministic oracle",
                )
            )
        ],
        [_command_gate()],
    ),
    AuthoringRule.QUANTIFIER_MISMATCH: lambda: _authoring(
        [
            _criterion(
                response=_response(
                    ObserveVerb.HOLDS_FOR_ALL, locus=ProofLocus.HYPOTHESIS, gate_ref=None
                )
            )
        ],
        [_command_gate()],
    ),
    AuthoringRule.LOCUS_MISMATCH: lambda: _authoring(
        [_criterion(response=_response(quantifier="forall", gate_ref=None))],
        [_command_gate()],
    ),
    AuthoringRule.SCOPE_MISMATCH: lambda: _authoring(
        [_criterion(text="every configuration file exists on disk")],
        [_gate("G-01", "CR-01", "file_exists")],
    ),
    AuthoringRule.UNCOVERED_PROMISE: lambda: _authoring(
        [_criterion(response=_response())],
        [_command_gate()],
        promises=(
            SourceUnit(span_id="U-000", quote="Load the configuration", char_offset=0),
            SourceUnit(span_id="U-001", quote="Reject unknown keys", char_offset=24),
            SourceUnit(span_id="U-002", quote="Emit a load metric", char_offset=45),
        ),
        promise_coverage={"U-000": ("CR-01",)},
        deferrals=(
            DeferredDeliverable(
                span_id="U-002",
                reason="metrics land with the telemetry rework",
                target="backlog",
            ),
        ),
    ),
    AuthoringRule.COMPLEXITY_CEILING: lambda: _numbered_set(MAX_CRITERIA_PER_SCOPE + 1),
}


def test_rejection_cases_cover_every_authoring_rule() -> None:
    assert set(_REJECTION_CASES) == set(AuthoringRule)


@pytest.mark.parametrize("rule", list(_REJECTION_CASES), ids=lambda rule: rule.value)
def test_validate_criteria_authoring_rejects_each_rule(
    rule: AuthoringRule,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if rule is AuthoringRule.UNCOMPILED_GATE:
        # A validated gate always compiles today; the rule guards drift between
        # the tier table and the runnable check kinds, simulated at the seam.
        monkeypatch.setattr(criteria_module, "compile_gate", lambda gate, *, criterion: None)
    assert _rules(_REJECTION_CASES[rule]()) == [rule]


def test_validate_criteria_authoring_accepts_clean_set() -> None:
    assert validate_criteria_authoring(_clean()) == ()


@pytest.mark.parametrize(
    "authoring",
    [
        pytest.param(
            _authoring([_criterion()], [_command_gate("G-01", "CR-09")]), id="unknown-owner"
        ),
        pytest.param(
            _authoring(
                [_criterion(), _criterion("CR-02", gate_ids=("G-01",))],
                [_command_gate("G-01", "CR-01"), _command_gate("G-02", "CR-02")],
            ),
            id="owner-omits-gate",
        ),
    ],
)
def test_validate_criteria_authoring_rejects_orphan_gate_variants(
    authoring: CriteriaAuthoring,
) -> None:
    assert AuthoringRule.ORPHAN_GATE in _rules(authoring)


@pytest.mark.parametrize(
    ("criterion", "gates"),
    [
        pytest.param(
            _criterion(evidence_kind="attested"), [_command_gate()], id="attested-with-gate"
        ),
        pytest.param(_criterion(evidence_kind="jury", gate_ids=()), [], id="jury-without-gate"),
        pytest.param(_criterion(), [_command_gate(policy="advisory")], id="advisory-only"),
    ],
)
def test_validate_criteria_authoring_rejects_ungated_variants(
    criterion: CriterionSpec,
    gates: list[GateSpec],
) -> None:
    assert _rules(_authoring([criterion], gates)) == [AuthoringRule.UNGATED_CRITERION]


def test_validate_criteria_authoring_accepts_attested_criterion_without_gate() -> None:
    authoring = _authoring(
        [
            _criterion(response=_response()),
            _criterion("CR-02", evidence_kind="attested", gate_ids=()),
        ],
        [_command_gate()],
    )
    assert validate_criteria_authoring(authoring) == ()


@pytest.mark.parametrize(
    "criterion",
    [
        pytest.param(_criterion(response=_response(gate_ref="file_exists")), id="unbound-kind"),
        pytest.param(
            _criterion(response=_response(), oracle_tier=OracleTier.T1_STATIC),
            id="authored-tier",
        ),
    ],
)
def test_validate_criteria_authoring_rejects_unknown_oracle_on_criterion(
    criterion: CriterionSpec,
) -> None:
    assert _rules(_authoring([criterion], [_command_gate()])) == [AuthoringRule.UNKNOWN_ORACLE]


def test_validate_criteria_authoring_accepts_authored_tier_equal_to_mapping() -> None:
    criterion = _criterion(response=_response(), oracle_tier=OracleTier.T4_CONTRACT)
    assert validate_criteria_authoring(_authoring([criterion], [_command_gate()])) == ()


@pytest.mark.parametrize(
    ("criterion", "expected"),
    [
        pytest.param(
            _criterion(
                response=_response(
                    ObserveVerb.JUDGED,
                    locus=ProofLocus.HUMAN,
                    gate_ref=None,
                    jury_reason="operator signs off on the wording",
                )
            ),
            [AuthoringRule.DETERMINISTIC_JURY_ROUTE],
            id="human-approval",
        ),
        pytest.param(
            _criterion(response=_response(), oracle_tier=OracleTier.T7_JURY),
            [AuthoringRule.UNKNOWN_ORACLE, AuthoringRule.DETERMINISTIC_JURY_ROUTE],
            id="authored-jury-tier",
        ),
    ],
)
def test_validate_criteria_authoring_rejects_deterministic_judgment_route(
    criterion: CriterionSpec,
    expected: list[AuthoringRule],
) -> None:
    assert _rules(_authoring([criterion], [_command_gate()])) == expected


def test_validate_criteria_authoring_accepts_jury_criterion_at_jury_tier() -> None:
    criterion = _criterion(
        evidence_kind="jury",
        response=_response(
            ObserveVerb.JUDGED,
            locus=ProofLocus.JURY,
            gate_ref=None,
            jury_reason="readability has no deterministic oracle",
        ),
    )
    assert validate_criteria_authoring(_authoring([criterion], [_command_gate()])) == ()


def test_validate_criteria_authoring_rejects_forall_on_single_site_gates() -> None:
    criterion = _criterion(
        text="the parser keeps its invariant for generated input",
        response=_response(
            ObserveVerb.HOLDS_FOR_ALL,
            locus=ProofLocus.HYPOTHESIS,
            quantifier="forall",
            gate_ref=None,
        ),
    )
    authoring = _authoring([criterion], [_gate("G-01", "CR-01", "file_exists")])
    assert _rules(authoring) == [AuthoringRule.QUANTIFIER_MISMATCH]


@pytest.mark.parametrize(
    ("response", "evidence_kind"),
    [
        pytest.param(
            _response(locus=ProofLocus.HUMAN), "deterministic", id="machine-verb-at-human"
        ),
        pytest.param(
            _response(
                ObserveVerb.JUDGED,
                gate_ref=None,
                jury_reason="naming quality has no deterministic oracle",
            ),
            "jury",
            id="judged-at-pytest",
        ),
    ],
)
def test_validate_criteria_authoring_rejects_locus_variants(
    response: ResponseClause,
    evidence_kind: CriterionEvidenceKind,
) -> None:
    row = _criterion(evidence_kind=evidence_kind, response=response)
    assert _rules(_authoring([row], [_command_gate()])) == [AuthoringRule.LOCUS_MISMATCH]


def test_validate_criteria_authoring_accepts_universal_prose_on_set_scanning_gate() -> None:
    criterion = _criterion(text="every configuration file loads without error")
    assert validate_criteria_authoring(_authoring([criterion], [_command_gate()])) == ()


def test_validate_criteria_authoring_scope_rule_is_word_bounded() -> None:
    criterion = _criterion(text="the overall configuration file exists on disk")
    authoring = _authoring([criterion], [_gate("G-01", "CR-01", "file_exists")])
    assert validate_criteria_authoring(authoring) == ()


def test_validate_criteria_authoring_names_the_uncovered_promise() -> None:
    findings = validate_criteria_authoring(_REJECTION_CASES[AuthoringRule.UNCOVERED_PROMISE]())
    assert [finding.subject_id for finding in findings] == ["U-001"]
    assert "Reject unknown keys" in findings[0].message


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        pytest.param(1, [], id="single"),
        pytest.param(MAX_CRITERIA_PER_SCOPE, [], id="at-ceiling"),
        pytest.param(
            MAX_CRITERIA_PER_SCOPE + 1, [AuthoringRule.COMPLEXITY_CEILING], id="over-ceiling"
        ),
    ],
)
def test_validate_criteria_authoring_complexity_ceiling_boundary(
    count: int,
    expected: list[AuthoringRule],
) -> None:
    assert _rules(_numbered_set(count)) == expected


def test_validate_criteria_authoring_honours_configured_ceiling() -> None:
    findings = validate_criteria_authoring(_numbered_set(3), max_criteria=2)
    assert [finding.rule for finding in findings] == [AuthoringRule.COMPLEXITY_CEILING]
    assert findings[0].subject_id == SCOPE


def test_validate_criteria_authoring_rejects_nonpositive_ceiling() -> None:
    with pytest.raises(ValueError, match="max_criteria must be at least 1"):
        validate_criteria_authoring(_clean(), max_criteria=0)


def test_validate_criteria_authoring_reports_every_finding_at_once() -> None:
    authoring = _authoring(
        [_criterion(gate_ids=("G-01", "G-09")), _criterion("CR-02", gate_ids=())],
        [_command_gate()],
    )
    assert _rules(authoring) == [AuthoringRule.UNRESOLVED_GATE, AuthoringRule.UNGATED_CRITERION]


@pytest.mark.parametrize("kind", get_args(CheckKind))
def test_gate_kind_oracle_tier_returns_tier_for_each_gate_kind(kind: str) -> None:
    tier = gate_kind_oracle_tier(kind)
    assert isinstance(tier, OracleTier)
    assert tier <= OracleTier.T5_GOLDEN


def test_gate_kind_oracle_tier_maps_known_kinds() -> None:
    assert gate_kind_oracle_tier("file_exists") is OracleTier.T1_STATIC
    assert gate_kind_oracle_tier("command_exit_zero") is OracleTier.T4_CONTRACT


@pytest.mark.parametrize("kind", ["made_up_kind", ""])
def test_gate_kind_oracle_tier_rejects_unknown_kind(kind: str) -> None:
    with pytest.raises(ValueError):
        gate_kind_oracle_tier(kind)


def _raw_criterion(**extra: Any) -> dict[str, Any]:
    return {
        "id": "CR-01",
        "text": "the loader returns the parsed configuration",
        "kind": "behavioral",
        "acceptance_style": "binary",
        "evidence_kind": "deterministic",
        "gate_ids": ["G-01"],
        "quality_dimension": "functional_suitability",
        "measurable_signal": SIGNAL,
        **extra,
    }


def _raw_gate(**extra: Any) -> dict[str, Any]:
    return {
        "id": "G-01",
        "criterion_id": "CR-01",
        "kind": "command_exit_zero",
        "args": {"argv": ARGV},
        "policy": "block",
        "cadence": "every-wave",
        **extra,
    }


def test_criteria_authoring_loads_planning_rows_unchanged() -> None:
    authoring = CriteriaAuthoring.model_validate(
        {"scope_id": SCOPE, "criteria": [_raw_criterion()], "gates": [_raw_gate()]}
    )
    assert isinstance(authoring.criteria[0], CriterionSpec)
    assert isinstance(authoring.gates[0], GateSpec)


@pytest.mark.parametrize(
    "document",
    [
        pytest.param(
            {"scope_id": SCOPE, "criteria": [_raw_criterion(scope_id="X")]},
            id="criterion-extra-field",
        ),
        pytest.param(
            {"scope_id": SCOPE, "criteria": [_raw_criterion()], "gates": [_raw_gate(tier="T1")]},
            id="gate-extra-field",
        ),
        pytest.param({"scope_id": SCOPE, "criteria": [_raw_criterion()], "extra": 1}, id="top"),
        pytest.param({"scope_id": SCOPE, "criteria": []}, id="no-criteria"),
        pytest.param(
            {"scope_id": SCOPE, "criteria": [_raw_criterion(), _raw_criterion()]},
            id="duplicate-criterion",
        ),
        pytest.param(
            {
                "scope_id": SCOPE,
                "criteria": [_raw_criterion()],
                "gates": [_raw_gate(), _raw_gate()],
            },
            id="duplicate-gate",
        ),
        pytest.param(
            {
                "scope_id": SCOPE,
                "criteria": [_raw_criterion()],
                "promise_coverage": {"U-404": ["CR-01"]},
            },
            id="coverage-unknown-promise",
        ),
        pytest.param(
            {
                "scope_id": SCOPE,
                "criteria": [_raw_criterion()],
                "promises": [{"span_id": "U-000", "quote": "Load", "char_offset": 0}],
                "promise_coverage": {"U-000": ["CR-99"]},
            },
            id="coverage-unknown-criterion",
        ),
        pytest.param(
            {
                "scope_id": SCOPE,
                "criteria": [_raw_criterion()],
                "promises": [{"span_id": "U-000", "quote": "Load", "char_offset": 0}],
                "promise_coverage": {"U-000": []},
            },
            id="coverage-empty",
        ),
        pytest.param(
            {
                "scope_id": SCOPE,
                "criteria": [_raw_criterion()],
                "deferrals": [
                    {
                        "span_id": "U-404",
                        "reason": "filed for the follow-up delivery",
                        "target": "backlog",
                    }
                ],
            },
            id="deferral-unknown-promise",
        ),
        pytest.param({"scope_id": "has space", "criteria": [_raw_criterion()]}, id="bad-scope"),
    ],
)
def test_criteria_authoring_rejects_malformed_document(document: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CriteriaAuthoring.model_validate(document)


def _shared_gate_set() -> CriteriaAuthoring:
    return _authoring(
        [
            _criterion(response=_response()),
            _criterion(
                "CR-02",
                text="the loader rejects unknown keys",
                gate_ids=("G-01", "G-02"),
            ),
            _criterion(
                "CR-03",
                text="the operator accepts the wording",
                evidence_kind="attested",
                gate_ids=(),
            ),
        ],
        [_command_gate("G-02", "CR-02"), _command_gate()],
    )


def test_compile_execution_contracts_builds_one_contract_per_gate() -> None:
    compiled = compile_execution_contracts(_shared_gate_set())
    assert [contract.gate_id for contract in compiled.contracts] == ["G-01", "G-02"]
    shared = compiled.contract("G-01")
    assert shared.criterion_ids == ("CR-01", "CR-02")
    assert shared.oracle_tier is OracleTier.T4_CONTRACT
    assert shared.evidence_kind == "deterministic"
    assert shared.check is not None
    assert shared.check.name == "G-01"
    assert shared.check.args["argv"] == ARGV
    assert compiled.contract("G-02").criterion_ids == ("CR-02",)
    assert [criterion.id for criterion in compiled.criteria] == ["CR-01", "CR-02", "CR-03"]


def test_compile_execution_contracts_leaves_judgment_gate_without_check() -> None:
    authoring = _authoring([_criterion(evidence_kind="jury")], [_command_gate()])
    contract = compile_execution_contracts(authoring).contract("G-01")
    assert contract.evidence_kind == "jury"
    assert contract.check is None


def test_compile_execution_contracts_raises_on_findings() -> None:
    with pytest.raises(CriteriaAuthoringError, match="unresolved_gate CR-01") as caught:
        compile_execution_contracts(_REJECTION_CASES[AuthoringRule.UNRESOLVED_GATE]())
    assert [finding.rule for finding in caught.value.findings] == [AuthoringRule.UNRESOLVED_GATE]


def test_execution_contract_set_contract_rejects_unknown_gate() -> None:
    with pytest.raises(KeyError):
        compile_execution_contracts(_clean()).contract("G-99")


def test_execution_contract_digests_track_criterion_and_gate_separately() -> None:
    base = compile_execution_contracts(_clean()).contract("G-01")
    reworded = compile_execution_contracts(
        _authoring(
            [_criterion(text="the loader returns the merged configuration", response=_response())],
            [_command_gate()],
        )
    ).contract("G-01")
    retargeted = compile_execution_contracts(
        _authoring(
            [_criterion(response=_response())],
            [_command_gate(argv=[*ARGV[:3], "tests/unit/sample/test_merge.py", "-q"])],
        )
    ).contract("G-01")
    assert reworded.criteria_digest() != base.criteria_digest()
    assert reworded.gate_digest() == base.gate_digest()
    assert retargeted.gate_digest() != base.gate_digest()
    assert retargeted.criteria_digest() == base.criteria_digest()
    assert (
        len({base.contract_digest(), reworded.contract_digest(), retargeted.contract_digest()}) == 3
    )


def test_execution_contract_digest_ignores_later_edits_to_authored_rows() -> None:
    authoring = _clean()
    compiled = compile_execution_contracts(authoring)
    before = (compiled.criteria_digest(), compiled.gate_manifest_digest())
    authoring.criteria[0].text = "the loader returns something else entirely"
    authoring.criteria[0].oracle_tier = OracleTier.T4_CONTRACT
    assert (compiled.criteria_digest(), compiled.gate_manifest_digest()) == before


def test_execution_contract_set_digests_cover_attested_criteria() -> None:
    compiled = compile_execution_contracts(_shared_gate_set())
    without_attested = compile_execution_contracts(
        _authoring(
            list(_shared_gate_set().criteria[:2]),
            list(_shared_gate_set().gates),
        )
    )
    assert compiled.criteria_digest() != without_attested.criteria_digest()
    assert compiled.gate_manifest_digest() == without_attested.gate_manifest_digest()


def test_execution_contract_rejects_inconsistent_projection() -> None:
    contract = compile_execution_contracts(_clean()).contract("G-01")
    fields = contract.model_dump()
    fields["gate"] = contract.gate
    fields["criteria"] = contract.criteria
    fields["check"] = contract.check
    variants: list[dict[str, Any]] = [
        {**fields, "oracle_tier": OracleTier.T1_STATIC},
        {**fields, "check": None},
        {**fields, "criteria": (_criterion("CR-02", gate_ids=("G-01",)),)},
        {**fields, "criteria": (contract.criteria[0], _criterion("CR-02", gate_ids=()))},
    ]
    for variant in variants:
        with pytest.raises(ValidationError):
            ExecutionContract(**variant)
