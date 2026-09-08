"""Tests for :mod:`eawf.kernel.release.gate_binding`.

The contract under test is that no gate of a profile can pass by reading
nothing. So the module pins:

1. The authored ``dev1`` table binds each of the eight gate names to
   exactly one evidence source, and the two dependency gates bind the
   two named components of the ``dependencies`` row rather than the
   whole row twice.
2. The loader refuses a profile gate nobody bound -- the case that would
   otherwise ship a vacuous gate.
3. The loader refuses a binding naming a signal or a component that does
   not exist, and distinguishes the two.
4. The loader refuses a proof command whose argv the L0 gate-runner
   policy rejects, and the three authored proofs pass that policy.
5. A proof command only resolves against a pinned 40-hex source SHA, so
   a proof run against an unpinned tree is unrepresentable.
6. The projection the sweep consumes
   (``release_readiness.GATE_SIGNAL_BINDINGS``) is derived from this
   table rather than authored a second time.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.release.gate_binding import (
    GateBinding,
    GateBindingError,
    GateBindingRejection,
    GateEvidenceKind,
    ProofCommand,
    load_gate_bindings,
    parse_gate_bindings,
    profile_gates,
    resolved_proof_commands,
)
from eawf.kernel.release.signals import ReleaseSignalName
from eawf.kernel.spec.release import ReleaseGateProfile
from eawf.kernel.spec.release_config import ReleaseGateName
from eawf.workflow.release.train import DEV1_GATE_BINDINGS_YAML, gate_bindings_for
from eawf.workflow.verify.release_readiness import GATE_SIGNAL_BINDINGS
from tests._release_helpers import SOURCE_SHA, dev1_binding_rows


def _table(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap *rows* in the top-level ``bindings`` key the loader reads."""
    return {"bindings": rows}


def _row_for(gate: ReleaseGateName) -> dict[str, Any]:
    """Return the authored binding row for *gate*."""
    for row in dev1_binding_rows():
        if row["gate"] == gate.value:
            return row
    raise AssertionError(f"no authored row for {gate.value!r}")


# --- the authored dev1 table ----------------------------------------


def test_dev1_binds_every_profile_gate_exactly_once() -> None:
    bindings = gate_bindings_for(ReleaseGateProfile.DEV1)
    assert tuple(bindings) == profile_gates(ReleaseGateProfile.DEV1)
    assert len(bindings) == 8


def test_dev1_binds_five_gates_to_the_readiness_sweep() -> None:
    bindings = gate_bindings_for(ReleaseGateProfile.DEV1)
    by_ref = {gate: binding.evidence_ref for gate, binding in bindings.items()}
    assert by_ref[ReleaseGateName.VERSION_CONSISTENCY] == "version_consistency"
    assert by_ref[ReleaseGateName.CHANGELOG_ENTRY] == "changelog"
    assert by_ref[ReleaseGateName.ARTIFACT_REPRODUCIBILITY] == "artifacts"
    assert by_ref[ReleaseGateName.DEPENDENCY_INVENTORY] == "dependencies.inventory"
    assert by_ref[ReleaseGateName.SECURITY_REVIEW] == "dependencies.vulnerability"


def test_dev1_dependency_gates_share_one_row_through_two_components() -> None:
    bindings = gate_bindings_for(ReleaseGateProfile.DEV1)
    inventory = bindings[ReleaseGateName.DEPENDENCY_INVENTORY]
    vulnerability = bindings[ReleaseGateName.SECURITY_REVIEW]
    assert inventory.kind is GateEvidenceKind.SIGNAL_COMPONENT
    assert vulnerability.kind is GateEvidenceKind.SIGNAL_COMPONENT
    assert inventory.component == "inventory"
    assert vulnerability.component == "vulnerability"
    assert inventory.required_signal is ReleaseSignalName.DEPENDENCIES
    assert vulnerability.required_signal is ReleaseSignalName.DEPENDENCIES


@pytest.mark.parametrize(
    "gate",
    [
        ReleaseGateName.EPOCH1_STABILIZATION,
        ReleaseGateName.TELEMETRY_PRODUCER,
        ReleaseGateName.FRONT_DOOR_JOURNEY,
    ],
)
def test_dev1_proof_gates_bind_a_proof_command_and_no_row(gate: ReleaseGateName) -> None:
    binding = gate_bindings_for(ReleaseGateProfile.DEV1)[gate]
    assert binding.kind is GateEvidenceKind.PROOF_COMMAND
    assert binding.proof is not None
    assert binding.required_signal is None
    assert binding.evidence_ref.startswith("proof:")


def test_dev1_binds_exactly_three_proof_commands() -> None:
    bindings = gate_bindings_for(ReleaseGateProfile.DEV1)
    proofs = resolved_proof_commands(bindings, source_sha=SOURCE_SHA)
    assert [proof.command_id for proof in proofs] == [
        "epoch1_stabilization_suite",
        "telemetry_turn_cost_record",
        "front_door_install_smoke",
    ]
    assert all(proof.source_sha == SOURCE_SHA for proof in proofs)


def test_readiness_projection_is_derived_from_the_authored_table() -> None:
    bindings = gate_bindings_for(ReleaseGateProfile.DEV1)
    assert {
        gate: binding.required_signal for gate, binding in bindings.items()
    } == GATE_SIGNAL_BINDINGS
    assert set(GATE_SIGNAL_BINDINGS) == set(ReleaseGateName)


# --- an unbound profile gate ----------------------------------------


def test_load_rejects_a_profile_gate_with_no_binding() -> None:
    rows = [row for row in dev1_binding_rows() if row["gate"] != "security_review"]
    with pytest.raises(GateBindingError) as excinfo:
        load_gate_bindings(_table(rows), profile=ReleaseGateProfile.DEV1)
    assert excinfo.value.code is GateBindingRejection.UNBOUND_PROFILE_GATE
    assert "security_review" in str(excinfo.value)


def test_load_rejects_an_empty_binding_list() -> None:
    with pytest.raises(GateBindingError) as excinfo:
        load_gate_bindings(_table([]), profile=ReleaseGateProfile.DEV1)
    assert excinfo.value.code is GateBindingRejection.UNBOUND_PROFILE_GATE


def test_load_rejects_a_duplicate_gate_binding() -> None:
    rows = dev1_binding_rows()
    rows.append(_row_for(ReleaseGateName.CHANGELOG_ENTRY))
    with pytest.raises(GateBindingError) as excinfo:
        load_gate_bindings(_table(rows), profile=ReleaseGateProfile.DEV1)
    assert excinfo.value.code is GateBindingRejection.DUPLICATE_BINDING


def test_load_rejects_an_undeclared_profile() -> None:
    with pytest.raises(GateBindingError) as excinfo:
        load_gate_bindings(DEV1_GATE_BINDINGS_YAML, profile=ReleaseGateProfile.STABLE)
    assert excinfo.value.code is GateBindingRejection.UNDECLARED_PROFILE


def test_profile_gates_rejects_an_undeclared_profile() -> None:
    with pytest.raises(GateBindingError) as excinfo:
        profile_gates(ReleaseGateProfile.DEV2)
    assert excinfo.value.code is GateBindingRejection.UNDECLARED_PROFILE


# --- unknown evidence ------------------------------------------------


def test_load_rejects_a_binding_naming_an_unknown_signal() -> None:
    rows = dev1_binding_rows()
    rows[0]["signal"] = "reproducibility"
    with pytest.raises(GateBindingError) as excinfo:
        load_gate_bindings(_table(rows), profile=ReleaseGateProfile.DEV1)
    assert excinfo.value.code is GateBindingRejection.UNKNOWN_SIGNAL
    assert "reproducibility" in str(excinfo.value)


def test_load_rejects_a_binding_naming_an_unknown_component() -> None:
    rows = dev1_binding_rows()
    for row in rows:
        if row["gate"] == "security_review":
            row["component"] = "licences"
    with pytest.raises(GateBindingError) as excinfo:
        load_gate_bindings(_table(rows), profile=ReleaseGateProfile.DEV1)
    assert excinfo.value.code is GateBindingRejection.UNKNOWN_COMPONENT
    assert "dependencies.licences" in str(excinfo.value)


def test_load_rejects_a_component_on_a_signal_that_declares_none() -> None:
    rows = dev1_binding_rows()
    rows[0]["kind"] = "signal_component"
    rows[0]["component"] = "inventory"
    with pytest.raises(GateBindingError) as excinfo:
        load_gate_bindings(_table(rows), profile=ReleaseGateProfile.DEV1)
    assert excinfo.value.code is GateBindingRejection.UNKNOWN_COMPONENT


def test_load_rejects_a_gate_the_profile_does_not_admit() -> None:
    rows = dev1_binding_rows()
    rows[0]["gate"] = "migration"
    with pytest.raises(GateBindingError) as excinfo:
        load_gate_bindings(_table(rows), profile=ReleaseGateProfile.DEV1)
    assert excinfo.value.code is GateBindingRejection.SCHEMA_INVALID


# --- the L0 argv policy ----------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["bash", "-c", "pytest"],
        ["curl", "https://example.invalid"],
        ["uv", "run", "pytest", "tests", "-q", ";", "rm"],
        ["/usr/bin/pytest"],
        ["uv", "run", "pytest", "tests/*"],
    ],
)
def test_load_rejects_a_proof_argv_the_l0_policy_refuses(argv: list[str]) -> None:
    rows = dev1_binding_rows()
    for row in rows:
        if row["gate"] == "epoch1_stabilization":
            row["proof"]["argv"] = argv
    with pytest.raises(GateBindingError) as excinfo:
        load_gate_bindings(_table(rows), profile=ReleaseGateProfile.DEV1)
    assert excinfo.value.code is GateBindingRejection.ARGV_REJECTED


def test_load_rejects_an_empty_proof_argv() -> None:
    rows = dev1_binding_rows()
    for row in rows:
        if row["gate"] == "telemetry_producer":
            row["proof"]["argv"] = []
    with pytest.raises(GateBindingError) as excinfo:
        load_gate_bindings(_table(rows), profile=ReleaseGateProfile.DEV1)
    assert excinfo.value.code is GateBindingRejection.SCHEMA_INVALID


def test_load_rejects_a_nonpositive_proof_timeout() -> None:
    rows = dev1_binding_rows()
    for row in rows:
        if row["gate"] == "front_door_journey":
            row["proof"]["timeout_seconds"] = 0
    with pytest.raises(GateBindingError) as excinfo:
        load_gate_bindings(_table(rows), profile=ReleaseGateProfile.DEV1)
    assert excinfo.value.code is GateBindingRejection.SCHEMA_INVALID


# --- binding shape ---------------------------------------------------


def test_binding_rejects_a_signal_kind_carrying_a_proof() -> None:
    with pytest.raises(ValidationError):
        GateBinding.model_validate(
            {
                "gate": "changelog_entry",
                "kind": "signal",
                "signal": "changelog",
                "proof": {
                    "command_id": "x",
                    "argv": ["uv", "run", "pytest"],
                    "timeout_seconds": 1,
                },
            }
        )


def test_binding_rejects_a_proof_kind_carrying_no_proof() -> None:
    with pytest.raises(ValidationError):
        GateBinding.model_validate({"gate": "front_door_journey", "kind": "proof_command"})


def test_binding_rejects_a_component_kind_without_a_component() -> None:
    with pytest.raises(ValidationError):
        GateBinding.model_validate(
            {"gate": "security_review", "kind": "signal_component", "signal": "dependencies"}
        )


def test_binding_rejects_an_unknown_field() -> None:
    with pytest.raises(ValidationError):
        GateBinding.model_validate(
            {
                "gate": "changelog_entry",
                "kind": "signal",
                "signal": "changelog",
                "owner": "nobody",
            }
        )


def test_binding_is_frozen() -> None:
    binding = gate_bindings_for(ReleaseGateProfile.DEV1)[ReleaseGateName.CHANGELOG_ENTRY]
    with pytest.raises(ValidationError):
        binding.gate = ReleaseGateName.SECURITY_REVIEW


# --- pinning a proof to the source SHA -------------------------------


def test_resolve_proof_pins_the_command_to_the_source_sha() -> None:
    binding = gate_bindings_for(ReleaseGateProfile.DEV1)[ReleaseGateName.FRONT_DOOR_JOURNEY]
    resolved = binding.resolve_proof(SOURCE_SHA)
    assert resolved.source_sha == SOURCE_SHA
    assert resolved.command_id == "front_door_install_smoke"
    assert resolved.argv == ("uvx", "eawf", "--version")


@pytest.mark.parametrize("sha", ["", "a" * 39, "a" * 41, "A" * 40, "g" * 40, "HEAD"])
def test_resolve_proof_rejects_a_revision_that_is_not_a_sha(sha: str) -> None:
    binding = gate_bindings_for(ReleaseGateProfile.DEV1)[ReleaseGateName.TELEMETRY_PRODUCER]
    with pytest.raises(ValueError, match="40-hex"):
        binding.resolve_proof(sha)


def test_resolve_proof_rejects_a_non_string_revision() -> None:
    binding = gate_bindings_for(ReleaseGateProfile.DEV1)[ReleaseGateName.TELEMETRY_PRODUCER]
    with pytest.raises(TypeError, match="source_sha must be str"):
        binding.resolve_proof(None)  # type: ignore[arg-type]


def test_resolve_proof_rejects_a_gate_bound_to_a_row() -> None:
    binding = gate_bindings_for(ReleaseGateProfile.DEV1)[ReleaseGateName.CHANGELOG_ENTRY]
    with pytest.raises(ValueError, match="not a proof command"):
        binding.resolve_proof(SOURCE_SHA)


def test_resolved_proof_commands_is_empty_for_a_row_only_table() -> None:
    rows = [row for row in dev1_binding_rows() if row["kind"] != "proof_command"]
    bindings = {binding.gate: binding for binding in parse_gate_bindings(_table(rows))}
    assert resolved_proof_commands(bindings, source_sha=SOURCE_SHA) == ()


# --- parse-level refusals --------------------------------------------


def test_parse_rejects_invalid_yaml() -> None:
    with pytest.raises(GateBindingError) as excinfo:
        parse_gate_bindings("bindings: [\n  - gate: :\n")
    assert excinfo.value.code is GateBindingRejection.SCHEMA_INVALID


def test_parse_rejects_a_payload_that_is_not_a_mapping() -> None:
    with pytest.raises(GateBindingError) as excinfo:
        parse_gate_bindings("- gate: changelog_entry\n")
    assert excinfo.value.code is GateBindingRejection.SCHEMA_INVALID


def test_parse_rejects_a_missing_bindings_key() -> None:
    with pytest.raises(GateBindingError) as excinfo:
        parse_gate_bindings({"gates": []})
    assert excinfo.value.code is GateBindingRejection.SCHEMA_INVALID


def test_parse_rejects_a_binding_row_that_is_not_a_mapping() -> None:
    with pytest.raises(GateBindingError) as excinfo:
        parse_gate_bindings(_table(["changelog_entry"]))  # type: ignore[list-item]
    assert excinfo.value.code is GateBindingRejection.SCHEMA_INVALID


def test_parse_accepts_a_single_row_table() -> None:
    bindings = parse_gate_bindings(_table([_row_for(ReleaseGateName.CHANGELOG_ENTRY)]))
    assert len(bindings) == 1
    assert bindings[0].gate is ReleaseGateName.CHANGELOG_ENTRY


def test_proof_command_rejects_a_command_id_that_is_not_a_slug() -> None:
    with pytest.raises(ValidationError):
        ProofCommand.model_validate(
            {"command_id": "Front Door", "argv": ["uvx", "eawf"], "timeout_seconds": 1}
        )
