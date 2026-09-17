"""Proof freshness keys and stale-receipt rejection."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.delivery.receipts import (
    FRESHNESS_KEY_COMPONENTS,
    REVISION_BINDING_COMPONENTS,
    ExternalInputDigest,
    FreshnessComponent,
    ProofFreshnessKey,
    ProofReceipt,
    RevisionBinding,
    RevisionRefKind,
    canonical_digest,
)
from eawf.kernel.spec.common import (
    CriterionSpec,
    GateSpec,
    QualityDimension,
)
from eawf.kernel.state.enums import GateReceiptResult
from eawf.runtime.verification.receipts import (
    StaleReceiptError,
    compare_freshness,
    compute_proof_freshness_key,
    require_fresh_receipt,
)
from eawf.workflow.delivery.criteria import (
    CriteriaAuthoring,
    ExecutionContract,
    compile_execution_contracts,
)

REPOSITORY = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/repository/REP-EAWF"
BATCH = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/batch/BAT-0007"
OTHER_BATCH = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/batch/BAT-0008"
FOREIGN_BATCH = "eawf://WSP-MAIN/PRJ-EAWF/REP-OTHER/batch/BAT-0007"
ARGV = ["uv", "run", "pytest", "tests/unit/sample/test_loader.py", "-q"]
BOUND_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
HEAD = "1" * 40
OLDER_HEAD = "3" * 40


def _digest(label: str) -> str:
    return canonical_digest(label)


def _contract(
    *,
    text: str = "the loader returns the parsed configuration",
    argv: list[str] | None = None,
) -> ExecutionContract:
    criterion = CriterionSpec(
        id="CR-01",
        text=text,
        kind="behavioral",
        acceptance_style="binary",
        evidence_kind="deterministic",
        gate_ids=["G-01"],
        quality_dimension=QualityDimension.RELIABILITY,
        measurable_signal="pytest over the loader suite exits zero",
    )
    gate = GateSpec(
        id="G-01",
        criterion_id="CR-01",
        kind="command_exit_zero",
        args={"argv": list(argv or ARGV)},
        policy="block",
        cadence="every-wave",
    )
    authoring = CriteriaAuthoring(scope_id="P99-I01-W01", criteria=(criterion,), gates=(gate,))
    return compile_execution_contracts(authoring).contract("G-01")


def _binding(**overrides: Any) -> RevisionBinding:
    fields: dict[str, Any] = {
        "repository_ref": REPOSITORY,
        "ref_kind": RevisionRefKind.INTEGRATION,
        "head_sha": HEAD,
        "tree_sha": "2" * 40,
        "parent_sha": "4" * 40,
        "batch_ref": BATCH,
        "integration_generation": 3,
        "manifest_digest": _digest("manifest"),
        "criteria_digest": _digest("criteria"),
        "policy_digest": _digest("binding-policy"),
        "environment_digest": None,
        "bound_at": BOUND_AT,
    }
    fields.update(overrides)
    return RevisionBinding(**fields)


def _key(
    contract: ExecutionContract | None = None,
    *,
    binding: RevisionBinding | None = None,
    **overrides: Any,
) -> ProofFreshnessKey:
    facts: dict[str, Any] = {
        "selector_digest": _digest("selector"),
        "policy_digest": _digest("policy"),
        "runner_digest": _digest("runner"),
        "environment_digest": _digest("environment"),
        "external_inputs": (),
    }
    facts.update(overrides)
    return compute_proof_freshness_key(
        contract or _contract(),
        revision_binding=binding or _binding(),
        **facts,
    )


def _receipt(key: ProofFreshnessKey, **overrides: Any) -> ProofReceipt:
    fields: dict[str, Any] = {
        "id": "RCP-0001",
        "scope_id": "P99-I01-W01",
        "gate_id": "G-01",
        "criterion_ids": ("CR-01",),
        "evidence_kind": "deterministic",
        "freshness": key,
        "freshness_key": key.digest(),
        "result": GateReceiptResult.PASS,
        "exit_status": 0,
        "started_at": BOUND_AT,
        "ended_at": BOUND_AT + timedelta(seconds=5),
    }
    fields.update(overrides)
    return ProofReceipt(**fields)


def _changed(recorded: ProofFreshnessKey, expected: ProofFreshnessKey) -> set[FreshnessComponent]:
    return {item.component for item in compare_freshness(recorded, expected) if not item.matches}


# One variant per freshness input; each changes exactly that input.
_INPUT_CHANGES: dict[FreshnessComponent, Callable[[], ProofFreshnessKey]] = {
    FreshnessComponent.CODE: lambda: _key(binding=_binding(head_sha=OLDER_HEAD)),
    FreshnessComponent.CRITERION: lambda: _key(
        _contract(text="the loader returns the merged configuration")
    ),
    FreshnessComponent.GATE: lambda: _key(
        _contract(argv=[*ARGV[:3], "tests/unit/sample/test_merge.py", "-q"])
    ),
    FreshnessComponent.SELECTOR: lambda: _key(selector_digest=_digest("selector-2")),
    FreshnessComponent.POLICY: lambda: _key(policy_digest=_digest("policy-2")),
    FreshnessComponent.RUNNER: lambda: _key(runner_digest=_digest("runner-2")),
    FreshnessComponent.ENVIRONMENT: lambda: _key(environment_digest=_digest("environment-2")),
    FreshnessComponent.EXTERNAL_INPUT: lambda: _key(
        external_inputs=(ExternalInputDigest(name="package-index", digest=_digest("index")),)
    ),
}


def test_input_changes_cover_every_freshness_component() -> None:
    assert set(_INPUT_CHANGES) == set(FreshnessComponent)


@pytest.mark.parametrize("component", list(_INPUT_CHANGES), ids=lambda item: item.value)
def test_compute_proof_freshness_key_digest_changes_per_input(
    component: FreshnessComponent,
) -> None:
    base = _key()
    changed = _INPUT_CHANGES[component]()
    assert changed.digest() != base.digest()
    assert _changed(base, changed) == {component}


def test_compute_proof_freshness_key_is_stable_for_equal_inputs() -> None:
    assert _key().digest() == _key().digest()
    assert len(_key().digest()) == 64


def test_compute_proof_freshness_key_changes_with_external_input_content() -> None:
    first = _key(external_inputs=(ExternalInputDigest(name="index", digest=_digest("a")),))
    second = _key(external_inputs=(ExternalInputDigest(name="index", digest=_digest("b")),))
    assert _changed(first, second) == {FreshnessComponent.EXTERNAL_INPUT}


def test_compute_proof_freshness_key_ignores_external_input_order() -> None:
    alpha = ExternalInputDigest(name="alpha", digest=_digest("alpha"))
    beta = ExternalInputDigest(name="beta", digest=_digest("beta"))
    assert (
        _key(external_inputs=(alpha, beta)).digest() == _key(external_inputs=(beta, alpha)).digest()
    )


def _binding_variants() -> list[tuple[str, Any]]:
    return [
        ("repository_ref", "eawf://WSP-MAIN/PRJ-EAWF/REP-OTHER/repository/REP-OTHER"),
        ("ref_kind", RevisionRefKind.CANDIDATE),
        ("head_sha", OLDER_HEAD),
        ("tree_sha", "5" * 40),
        ("parent_sha", None),
        ("batch_ref", OTHER_BATCH),
        ("integration_generation", 4),
        ("manifest_digest", _digest("manifest-2")),
        ("criteria_digest", _digest("criteria-2")),
        ("policy_digest", _digest("binding-policy-2")),
        ("environment_digest", _digest("environment")),
        ("bound_at", BOUND_AT + timedelta(hours=1)),
    ]


def test_binding_variants_cover_every_binding_field() -> None:
    assert {name for name, _ in _binding_variants()} == set(RevisionBinding.model_fields)


@pytest.mark.parametrize(
    ("field", "value"), _binding_variants(), ids=[name for name, _ in _binding_variants()]
)
def test_proof_freshness_key_keys_every_binding_field(field: str, value: Any) -> None:
    overrides = {field: value}
    if field == "repository_ref":
        overrides["batch_ref"] = "eawf://WSP-MAIN/PRJ-EAWF/REP-OTHER/batch/BAT-0007"
    base = _key()
    variant = _key(binding=_binding(**overrides))
    expected = REVISION_BINDING_COMPONENTS[field]
    if expected is None:
        assert variant.digest() == base.digest()
    else:
        assert _changed(base, variant) == {expected}


def test_component_tables_cover_every_model_field() -> None:
    assert set(REVISION_BINDING_COMPONENTS) == set(RevisionBinding.model_fields)
    assert set(FRESHNESS_KEY_COMPONENTS) | {"revision_binding"} == set(
        ProofFreshnessKey.model_fields
    )
    assert set(FRESHNESS_KEY_COMPONENTS.values()) | {FreshnessComponent.CODE} == set(
        FreshnessComponent
    )


def test_proof_freshness_key_rejects_duplicate_external_input() -> None:
    item = ExternalInputDigest(name="index", digest=_digest("index"))
    with pytest.raises(ValidationError, match="external_inputs repeats index"):
        _key(external_inputs=(item, item))


def test_proof_freshness_key_rejects_environment_disagreement() -> None:
    binding = _binding(environment_digest=_digest("elsewhere"))
    with pytest.raises(ValidationError, match="environment_digest differs"):
        _key(binding=binding)


def test_proof_freshness_key_accepts_matching_bound_environment() -> None:
    binding = _binding(environment_digest=_digest("environment"))
    assert _key(binding=binding).revision_binding.environment_digest == _digest("environment")


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"selector_digest": "abc"}, id="unprefixed"),
        pytest.param({"runner_digest": "sha256:" + "A" * 64}, id="uppercase"),
        pytest.param({"policy_digest": ""}, id="empty"),
    ],
)
def test_compute_proof_freshness_key_rejects_malformed_digest(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _key(**overrides)


def test_require_fresh_receipt_returns_receipt_for_exact_key() -> None:
    receipt = _receipt(_key())
    assert require_fresh_receipt(receipt, _key()) is receipt


def test_require_fresh_receipt_accepts_reobserved_head() -> None:
    receipt = _receipt(_key())
    reobserved = _key(binding=_binding(bound_at=BOUND_AT + timedelta(days=1)))
    assert require_fresh_receipt(receipt, reobserved) is receipt


def test_require_fresh_receipt_rejects_receipt_bound_to_older_head() -> None:
    receipt = _receipt(_key(binding=_binding(head_sha=OLDER_HEAD)))
    with pytest.raises(StaleReceiptError, match=r"RCP-0001.*stale: changed code") as caught:
        require_fresh_receipt(receipt, _key())
    assert caught.value.receipt_id == "RCP-0001"
    assert caught.value.changed == (FreshnessComponent.CODE,)


def test_require_fresh_receipt_names_every_changed_input() -> None:
    receipt = _receipt(_key(binding=_binding(head_sha=OLDER_HEAD), runner_digest=_digest("old")))
    with pytest.raises(StaleReceiptError) as caught:
        require_fresh_receipt(receipt, _key())
    assert caught.value.changed == (FreshnessComponent.CODE, FreshnessComponent.RUNNER)


def test_stale_receipt_error_is_a_value_error() -> None:
    assert issubclass(StaleReceiptError, ValueError)


def test_proof_receipt_rejects_mismatched_freshness_key() -> None:
    with pytest.raises(ValidationError, match="does not match its inputs"):
        _receipt(_key(), freshness_key=_key(binding=_binding(head_sha=OLDER_HEAD)).digest())


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        pytest.param(
            {"ended_at": BOUND_AT - timedelta(seconds=1)}, "cannot precede", id="reversed-time"
        ),
        pytest.param({"criterion_ids": ("CR-01", "CR-01")}, "repeats", id="repeated-criterion"),
        pytest.param({"criterion_ids": ()}, "at least 1", id="no-criterion"),
        pytest.param({"supersedes_id": "RCP-0001"}, "supersede itself", id="self-supersession"),
        pytest.param({"id": "RCP-1"}, "entity_key_invalid", id="bad-key"),
        pytest.param({"freshness_key": "sha256:" + "0" * 64}, "pattern", id="prefixed-key"),
        pytest.param({"evidence_kind": "vibes"}, "evidence_kind", id="bad-evidence-kind"),
        pytest.param({"unknown": 1}, "Extra inputs", id="extra-field"),
    ],
)
def test_proof_receipt_rejects_malformed_record(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _receipt(_key(), **overrides)


def test_proof_receipt_is_immutable() -> None:
    receipt = _receipt(_key())
    with pytest.raises(ValidationError):
        receipt.result = GateReceiptResult.FAIL


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        pytest.param({"batch_ref": FOREIGN_BATCH}, "not under repository", id="foreign-batch"),
        pytest.param({"parent_sha": HEAD}, "cannot equal head_sha", id="self-parent"),
        pytest.param({"integration_generation": 0}, "greater than 0", id="zero-generation"),
        pytest.param({"head_sha": "abc"}, "pattern", id="short-sha"),
        pytest.param({"bound_at": datetime(2026, 9, 1, 12, 0)}, "timezone-aware", id="naive-time"),
        pytest.param(
            {"repository_ref": BATCH, "batch_ref": BATCH}, "identity_kind_mismatch", id="kind"
        ),
    ],
)
def test_revision_binding_rejects_malformed_binding(
    overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _binding(**overrides)


@pytest.mark.parametrize("field", ["parent_sha", "environment_digest"])
def test_revision_binding_requires_nullable_fields_explicitly(field: str) -> None:
    fields = _binding().model_dump()
    del fields[field]
    with pytest.raises(ValidationError, match="Field required"):
        RevisionBinding(**fields)
