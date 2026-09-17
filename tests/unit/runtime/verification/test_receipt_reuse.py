"""Receipt reuse decisions and the rerun list an audit reads."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.delivery.receipts import (
    ComponentComparison,
    FreshnessComponent,
    ProofFreshnessKey,
    ProofReceipt,
    ReceiptReuseDecision,
    ReceiptReusePlan,
    ReuseDisposition,
    ReuseReason,
    RevisionBinding,
    RevisionRefKind,
    canonical_digest,
)
from eawf.kernel.spec.common import (
    CriterionEvidenceKind,
    CriterionSpec,
    GateSpec,
    QualityDimension,
)
from eawf.kernel.state.enums import GateReceiptResult
from eawf.runtime.verification.receipts import (
    VerificationLeg,
    compute_proof_freshness_key,
    decide_leg_reuse,
    decide_receipt_reuse,
)
from eawf.workflow.delivery.criteria import (
    CriteriaAuthoring,
    ExecutionContract,
    ExecutionContractSet,
    compile_execution_contracts,
)

SCOPE = "P99-I01-W01"
REPOSITORY = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/repository/REP-EAWF"
BATCH = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/batch/BAT-0007"
BOUND_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
HEAD = "1" * 40
OLDER_HEAD = "3" * 40


def _compiled(evidence_kinds: dict[str, CriterionEvidenceKind]) -> ExecutionContractSet:
    criteria = []
    gates = []
    for index, (gate_id, evidence_kind) in enumerate(evidence_kinds.items(), start=1):
        criterion_id = f"CR-{index:02d}"
        criteria.append(
            CriterionSpec(
                id=criterion_id,
                text=f"the loader handles case {index}",
                kind="behavioral",
                acceptance_style="binary",
                evidence_kind=evidence_kind,
                gate_ids=[gate_id],
                quality_dimension=QualityDimension.PERFORMANCE_EFFICIENCY,
                measurable_signal="pytest over the loader suite exits zero",
            )
        )
        gates.append(
            GateSpec(
                id=gate_id,
                criterion_id=criterion_id,
                kind="command_exit_zero",
                args={"argv": ["uv", "run", "pytest", f"tests/unit/sample/test_{index}.py"]},
                policy="block",
                cadence="every-wave",
            )
        )
    authoring = CriteriaAuthoring(scope_id=SCOPE, criteria=tuple(criteria), gates=tuple(gates))
    return compile_execution_contracts(authoring)


def _binding(head_sha: str = HEAD) -> RevisionBinding:
    return RevisionBinding.model_validate(
        {
            "repository_ref": REPOSITORY,
            "ref_kind": RevisionRefKind.INTEGRATION,
            "head_sha": head_sha,
            "tree_sha": "2" * 40,
            "parent_sha": None,
            "batch_ref": BATCH,
            "integration_generation": 1,
            "manifest_digest": canonical_digest("manifest"),
            "criteria_digest": canonical_digest("criteria"),
            "policy_digest": canonical_digest("binding-policy"),
            "environment_digest": None,
            "bound_at": BOUND_AT,
        }
    )


def _legs(
    compiled: ExecutionContractSet,
    *,
    head_sha: str = HEAD,
) -> list[VerificationLeg]:
    return [
        VerificationLeg(contract=contract, expected=_key_for(contract, head_sha=head_sha))
        for contract in compiled.contracts
    ]


def _key_for(contract: ExecutionContract, *, head_sha: str = HEAD) -> ProofFreshnessKey:
    return compute_proof_freshness_key(
        contract,
        revision_binding=_binding(head_sha),
        selector_digest=canonical_digest("selector"),
        policy_digest=canonical_digest("policy"),
        runner_digest=canonical_digest("runner"),
        environment_digest=canonical_digest("environment"),
    )


def _receipt(
    leg: VerificationLeg,
    receipt_id: str,
    *,
    key: ProofFreshnessKey | None = None,
    result: GateReceiptResult = GateReceiptResult.PASS,
    ended_at: datetime = BOUND_AT,
    **overrides: Any,
) -> ProofReceipt:
    freshness = key or leg.expected
    fields: dict[str, Any] = {
        "id": receipt_id,
        "scope_id": leg.contract.scope_id,
        "gate_id": leg.contract.gate_id,
        "criterion_ids": leg.contract.criterion_ids,
        "evidence_kind": leg.contract.evidence_kind,
        "freshness": freshness,
        "freshness_key": freshness.digest(),
        "result": result,
        "exit_status": 0 if result is GateReceiptResult.PASS else 1,
        "started_at": ended_at - timedelta(seconds=5),
        "ended_at": ended_at,
    }
    fields.update(overrides)
    return ProofReceipt(**fields)


def _single_leg() -> VerificationLeg:
    return _legs(_compiled({"G-01": "deterministic"}))[0]


def test_decide_receipt_reuse_reuses_fresh_deterministic_receipt() -> None:
    leg = _single_leg()
    plan = decide_receipt_reuse([leg], [_receipt(leg, "RCP-0001")])
    assert plan.reruns == ()
    (decision,) = plan.reused
    assert decision.gate_id == "G-01"
    assert decision.receipt_id == "RCP-0001"
    assert decision.reason is ReuseReason.FRESH
    assert decision.disposition is ReuseDisposition.REUSE
    assert decision.criterion_ids == ("CR-01",)
    assert decision.expected_freshness_key == leg.expected.digest()
    assert [item.component for item in decision.comparisons] == list(FreshnessComponent)
    assert decision.changed_components == ()


def test_decide_receipt_reuse_names_each_rerun_with_reason() -> None:
    legs = _legs(
        _compiled(
            {
                "G-01": "deterministic",
                "G-02": "deterministic",
                "G-03": "deterministic",
                "G-04": "deterministic",
            }
        )
    )
    fresh, missing, stale, failed = legs
    receipts = [
        _receipt(fresh, "RCP-0001"),
        _receipt(stale, "RCP-0003", key=_key_for(stale.contract, head_sha=OLDER_HEAD)),
        _receipt(failed, "RCP-0004", result=GateReceiptResult.FAIL),
    ]
    plan = decide_receipt_reuse(legs, receipts)
    assert [item.gate_id for item in plan.reused] == ["G-01"]
    assert [(item.gate_id, item.reason, item.receipt_id) for item in plan.reruns] == [
        ("G-02", ReuseReason.MISSING, None),
        ("G-03", ReuseReason.STALE, "RCP-0003"),
        ("G-04", ReuseReason.NOT_PASSED, "RCP-0004"),
    ]
    assert plan.reruns[0].comparisons == ()
    assert plan.reruns[1].changed_components == (FreshnessComponent.CODE,)
    assert plan.reruns[2].changed_components == ()
    assert missing.contract.gate_id == "G-02"
    assert plan.unavailable == ()


@pytest.mark.parametrize(
    "result",
    [
        GateReceiptResult.FAIL,
        GateReceiptResult.BLOCKED,
        GateReceiptResult.ERROR,
        GateReceiptResult.TIMEOUT,
        GateReceiptResult.CANCELLED,
    ],
)
def test_decide_leg_reuse_reruns_every_non_passing_result(result: GateReceiptResult) -> None:
    leg = _single_leg()
    decision = decide_leg_reuse(leg, [_receipt(leg, "RCP-0001", result=result)])
    assert decision.reason is ReuseReason.NOT_PASSED
    assert decision.disposition is ReuseDisposition.RERUN


def test_decide_receipt_reuse_with_no_legs_returns_empty_plan() -> None:
    plan = decide_receipt_reuse([], [])
    assert plan.decisions == ()
    assert (plan.reused, plan.reruns, plan.unavailable) == ((), (), ())


def test_decide_leg_reuse_reports_missing_when_no_receipt_exists() -> None:
    decision = decide_leg_reuse(_single_leg(), [])
    assert (decision.reason, decision.disposition, decision.receipt_id) == (
        ReuseReason.MISSING,
        ReuseDisposition.RERUN,
        None,
    )


def test_decide_leg_reuse_prefers_exact_key_over_newer_stale_receipt() -> None:
    leg = _single_leg()
    exact = _receipt(leg, "RCP-0001", ended_at=BOUND_AT)
    newer_stale = _receipt(
        leg,
        "RCP-0002",
        key=_key_for(leg.contract, head_sha=OLDER_HEAD),
        ended_at=BOUND_AT + timedelta(hours=1),
    )
    decision = decide_leg_reuse(leg, [newer_stale, exact])
    assert (decision.reason, decision.receipt_id) == (ReuseReason.FRESH, "RCP-0001")


def test_decide_leg_reuse_takes_latest_exact_receipt() -> None:
    leg = _single_leg()
    older_pass = _receipt(leg, "RCP-0001", ended_at=BOUND_AT)
    newer_fail = _receipt(
        leg, "RCP-0002", result=GateReceiptResult.FAIL, ended_at=BOUND_AT + timedelta(hours=1)
    )
    decision = decide_leg_reuse(leg, [older_pass, newer_fail])
    assert (decision.reason, decision.receipt_id) == (ReuseReason.NOT_PASSED, "RCP-0002")


def test_decide_leg_reuse_ignores_superseded_receipt() -> None:
    leg = _single_leg()
    superseded = _receipt(leg, "RCP-0001")
    successor = _receipt(
        leg,
        "RCP-0002",
        result=GateReceiptResult.FAIL,
        ended_at=BOUND_AT - timedelta(hours=1),
        supersedes_id="RCP-0001",
    )
    decision = decide_leg_reuse(leg, [superseded, successor])
    assert (decision.reason, decision.receipt_id) == (ReuseReason.NOT_PASSED, "RCP-0002")


def test_decide_leg_reuse_ignores_receipts_of_other_gates_and_scopes() -> None:
    leg = _single_leg()
    receipts = [
        _receipt(leg, "RCP-0001", gate_id="G-99"),
        _receipt(leg, "RCP-0002", scope_id="P99-I01-W02"),
    ]
    assert decide_leg_reuse(leg, receipts).reason is ReuseReason.MISSING


@pytest.mark.parametrize(
    ("age", "reason"),
    [
        pytest.param(timedelta(0), ReuseReason.FRESH, id="just-run"),
        pytest.param(timedelta(hours=24), ReuseReason.FRESH, id="at-limit"),
        pytest.param(timedelta(hours=24, seconds=1), ReuseReason.EXPIRED, id="past-limit"),
    ],
)
def test_decide_leg_reuse_applies_age_limit(age: timedelta, reason: ReuseReason) -> None:
    leg = _single_leg()
    decision = decide_leg_reuse(
        leg,
        [_receipt(leg, "RCP-0001")],
        now=BOUND_AT + age,
        max_age=timedelta(hours=24),
    )
    assert decision.reason is reason


def test_decide_leg_reuse_reports_stale_before_expired() -> None:
    leg = _single_leg()
    receipt = _receipt(leg, "RCP-0001", key=_key_for(leg.contract, head_sha=OLDER_HEAD))
    decision = decide_leg_reuse(
        leg, [receipt], now=BOUND_AT + timedelta(days=30), max_age=timedelta(hours=1)
    )
    assert decision.reason is ReuseReason.STALE


def test_decide_leg_reuse_marks_judgment_leg_unavailable() -> None:
    compiled = _compiled({"G-01": "deterministic", "G-02": "jury"})
    deterministic, jury = _legs(compiled)
    plan = decide_receipt_reuse(
        [deterministic, jury],
        [_receipt(deterministic, "RCP-0001"), _receipt(jury, "RCP-0002")],
    )
    (decision,) = plan.unavailable
    assert decision.gate_id == "G-02"
    assert decision.reason is ReuseReason.NOT_DETERMINISTIC
    assert decision.receipt_id is None
    assert [item.gate_id for item in plan.reused] == ["G-01"]
    assert plan.reruns == ()


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"now": BOUND_AT}, id="now-only"),
        pytest.param({"max_age": timedelta(hours=1)}, id="max-age-only"),
    ],
)
def test_decide_leg_reuse_rejects_half_age_policy(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="must be given together"):
        decide_leg_reuse(_single_leg(), [], **kwargs)


def test_decide_leg_reuse_rejects_naive_now() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        decide_leg_reuse(
            _single_leg(), [], now=datetime(2026, 9, 1, 12, 0), max_age=timedelta(hours=1)
        )


def test_decide_receipt_reuse_rejects_repeated_leg() -> None:
    leg = _single_leg()
    with pytest.raises(ValueError, match="decided more than once"):
        decide_receipt_reuse([leg, leg], [])


def test_verification_leg_rejects_key_from_other_contract() -> None:
    first, second = _legs(_compiled({"G-01": "deterministic", "G-02": "deterministic"}))
    with pytest.raises(ValidationError, match="key built for another contract"):
        VerificationLeg(contract=first.contract, expected=second.expected)


def _comparisons(*, changed: FreshnessComponent | None = None) -> tuple[ComponentComparison, ...]:
    same = canonical_digest("same")
    return tuple(
        ComponentComparison(
            component=component,
            expected_digest=same,
            recorded_digest=canonical_digest("moved") if component is changed else same,
        )
        for component in FreshnessComponent
    )


def _decision_fields(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "scope_id": SCOPE,
        "gate_id": "G-01",
        "criterion_ids": ("CR-01",),
        "receipt_id": "RCP-0001",
        "expected_freshness_key": "a" * 64,
        "comparisons": _comparisons(),
        "disposition": ReuseDisposition.REUSE,
        "reason": ReuseReason.FRESH,
    }
    fields.update(overrides)
    return fields


def test_receipt_reuse_decision_accepts_consistent_stale_row() -> None:
    decision = ReceiptReuseDecision(
        **_decision_fields(
            comparisons=_comparisons(changed=FreshnessComponent.POLICY),
            disposition=ReuseDisposition.RERUN,
            reason=ReuseReason.STALE,
        )
    )
    assert decision.changed_components == (FreshnessComponent.POLICY,)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        pytest.param(
            {"disposition": ReuseDisposition.RERUN}, "implies disposition reuse", id="disposition"
        ),
        pytest.param(
            {
                "receipt_id": None,
                "comparisons": (),
                "disposition": ReuseDisposition.RERUN,
                "reason": ReuseReason.STALE,
            },
            "requires a candidate receipt",
            id="stale-without-receipt",
        ),
        pytest.param(
            {
                "receipt_id": None,
                "disposition": ReuseDisposition.RERUN,
                "reason": ReuseReason.MISSING,
            },
            "comparisons require a candidate receipt",
            id="comparisons-without-receipt",
        ),
        pytest.param(
            {"disposition": ReuseDisposition.RERUN, "reason": ReuseReason.MISSING},
            "cannot name a candidate receipt",
            id="missing-with-receipt",
        ),
        pytest.param(
            {"comparisons": _comparisons()[:-1]}, "every component once", id="partial-comparison"
        ),
        pytest.param(
            {"disposition": ReuseDisposition.RERUN, "reason": ReuseReason.STALE},
            "disagrees with changed components",
            id="stale-without-change",
        ),
        pytest.param(
            {"comparisons": _comparisons(changed=FreshnessComponent.CODE)},
            "disagrees with changed components",
            id="fresh-with-change",
        ),
        pytest.param({"criterion_ids": ()}, "at least 1", id="no-criterion"),
    ],
)
def test_receipt_reuse_decision_rejects_inconsistent_row(
    overrides: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        ReceiptReuseDecision(**_decision_fields(**overrides))


def test_receipt_reuse_plan_partitions_by_disposition() -> None:
    reuse = ReceiptReuseDecision(**_decision_fields())
    rerun = ReceiptReuseDecision(
        **_decision_fields(
            gate_id="G-02",
            receipt_id=None,
            comparisons=(),
            disposition=ReuseDisposition.RERUN,
            reason=ReuseReason.MISSING,
        )
    )
    unavailable = ReceiptReuseDecision(
        **_decision_fields(
            gate_id="G-03",
            receipt_id=None,
            comparisons=(),
            disposition=ReuseDisposition.UNAVAILABLE,
            reason=ReuseReason.NOT_DETERMINISTIC,
        )
    )
    plan = ReceiptReusePlan(decisions=(reuse, rerun, unavailable))
    assert (plan.reused, plan.reruns, plan.unavailable) == ((reuse,), (rerun,), (unavailable,))
