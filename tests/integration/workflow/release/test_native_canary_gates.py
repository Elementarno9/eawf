"""The fifteen ``native_canary`` gates, and what the three new ones read.

``native_canary`` is the first epoch-2 rung, so it is the first that can
prove anything about a real runtime tuple running in a disposable
repository. These tests pin three claims about its gate set.

**It is a superset, in order.** The profile is the twelve ``dev2`` gates
in ``dev2``'s own order, then ``provider``, ``membership`` and
``canary_isolation``. A checkpoint that reordered or dropped a gate its
predecessor passed could not claim the train stabilizes monotonically,
so the prefix relation is asserted rather than inferred from two literal
lists.

**Every new name resolves once.** ``provider`` and ``membership`` read a
whole readiness row each; ``canary_isolation`` has no row and is settled
by running an argv at the pinned source revision. A gate that reads
nothing passes vacuously, which is the failure the binding table exists
to prevent, so the unbound and out-of-profile refusals are exercised
here too.

**The proof command is pinned.** ``canary_isolation`` resolves only
against a 40-hex source SHA, so a rehearsal run against whatever the
working tree happens to hold is unrepresentable.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from eawf.kernel.release.gate_binding import (
    DEV1_GATES,
    DEV2_ADDED_GATES,
    NATIVE_CANARY_ADDED_GATES,
    PROFILE_GATES,
    GateBindingError,
    GateBindingRejection,
    GateEvidenceKind,
    load_gate_bindings,
    profile_gates,
    resolved_proof_commands,
)
from eawf.kernel.release.signals import ReleaseSignalName
from eawf.kernel.spec.release import ReleaseGateProfile
from eawf.kernel.spec.release_config import ReleaseGateName
from eawf.workflow.release.train import (
    DEV2_GATE_BINDINGS_YAML,
    NATIVE_CANARY_GATE_BINDINGS_YAML,
    gate_bindings_for,
)
from tests._release_helpers import SOURCE_SHA

#: The profile under test.
PROFILE = ReleaseGateProfile.NATIVE_CANARY

#: The test file the ``canary_isolation`` proof command runs. It lands
#: with the rehearsal wave, so the gate is red until then; naming it here
#: is what makes that debt visible rather than silent.
ISOLATION_TEST_PATH = "tests/integration/workflow/release/test_dev3_canary_isolation.py"


def _rows() -> list[dict[str, Any]]:
    """Return the authored ``native_canary`` binding rows, decoded."""
    decoded = yaml.safe_load(NATIVE_CANARY_GATE_BINDINGS_YAML)
    rows: list[dict[str, Any]] = list(decoded["bindings"])
    return rows


def _without(gate: ReleaseGateName) -> dict[str, Any]:
    """Return the authored table with *gate*'s row removed."""
    return {"bindings": [row for row in _rows() if row["gate"] != gate.value]}


# --- the gate vocabulary ---------------------------------------------


def test_release_gate_name_admits_the_three_native_canary_gates() -> None:
    assert ReleaseGateName.PROVIDER.value == "provider"
    assert ReleaseGateName.MEMBERSHIP.value == "membership"
    assert ReleaseGateName.CANARY_ISOLATION.value == "canary_isolation"


def test_native_canary_added_gates_are_the_three_new_names() -> None:
    assert NATIVE_CANARY_ADDED_GATES == (
        ReleaseGateName.PROVIDER,
        ReleaseGateName.MEMBERSHIP,
        ReleaseGateName.CANARY_ISOLATION,
    )


# --- the profile -----------------------------------------------------


def test_native_canary_is_the_dev2_twelve_then_the_three_new_gates() -> None:
    assert profile_gates(PROFILE) == (
        *DEV1_GATES,
        *DEV2_ADDED_GATES,
        *NATIVE_CANARY_ADDED_GATES,
    )


def test_native_canary_keeps_the_dev2_gates_in_dev2_order() -> None:
    dev2 = PROFILE_GATES[ReleaseGateProfile.DEV2]
    assert profile_gates(PROFILE)[: len(dev2)] == dev2


def test_native_canary_admits_fifteen_gates() -> None:
    assert len(profile_gates(PROFILE)) == 15
    assert len(set(profile_gates(PROFILE))) == 15


# --- what the three new gates read -----------------------------------


@pytest.mark.parametrize(
    ("gate", "signal"),
    [
        (ReleaseGateName.PROVIDER, ReleaseSignalName.PROVIDER),
        (ReleaseGateName.MEMBERSHIP, ReleaseSignalName.MEMBERSHIP),
    ],
)
def test_native_canary_binds_a_signal_gate_to_its_whole_row(
    gate: ReleaseGateName, signal: ReleaseSignalName
) -> None:
    binding = gate_bindings_for(PROFILE)[gate]
    assert binding.kind is GateEvidenceKind.SIGNAL
    assert binding.signal is signal
    assert binding.component is None
    assert binding.evidence_ref == signal.value
    assert binding.required_signal is signal


def test_native_canary_binds_canary_isolation_to_a_proof_command() -> None:
    binding = gate_bindings_for(PROFILE)[ReleaseGateName.CANARY_ISOLATION]
    assert binding.kind is GateEvidenceKind.PROOF_COMMAND
    assert binding.proof is not None
    assert binding.proof.command_id == "canary_leaves_production_untouched"
    assert binding.evidence_ref == "proof:canary_leaves_production_untouched"
    assert binding.required_signal is None


def test_canary_isolation_runs_the_production_root_isolation_test() -> None:
    binding = gate_bindings_for(PROFILE)[ReleaseGateName.CANARY_ISOLATION]
    assert binding.proof is not None
    assert binding.proof.argv == ("uv", "run", "pytest", ISOLATION_TEST_PATH, "-q")
    assert binding.proof.timeout_seconds == 1800


def test_canary_isolation_resolves_only_against_a_pinned_source_sha() -> None:
    binding = gate_bindings_for(PROFILE)[ReleaseGateName.CANARY_ISOLATION]
    resolved = binding.resolve_proof(SOURCE_SHA)
    assert resolved.source_sha == SOURCE_SHA
    assert resolved.command_id == "canary_leaves_production_untouched"


def test_canary_isolation_refuses_a_source_sha_that_is_not_a_string() -> None:
    binding = gate_bindings_for(PROFILE)[ReleaseGateName.CANARY_ISOLATION]
    with pytest.raises(TypeError, match="source_sha must be str"):
        binding.resolve_proof(None)  # type: ignore[arg-type]


@pytest.mark.parametrize("sha", ["", "a" * 39, "a" * 41])
def test_canary_isolation_refuses_a_source_sha_of_the_wrong_length(sha: str) -> None:
    binding = gate_bindings_for(PROFILE)[ReleaseGateName.CANARY_ISOLATION]
    with pytest.raises(ValueError, match="40-hex"):
        binding.resolve_proof(sha)


def test_native_canary_proof_commands_extend_the_dev2_four() -> None:
    proofs = resolved_proof_commands(gate_bindings_for(PROFILE), source_sha=SOURCE_SHA)
    assert [proof.command_id for proof in proofs] == [
        "epoch1_stabilization_suite",
        "telemetry_turn_cost_record",
        "front_door_install_smoke",
        "hosted_close_runs_the_gates",
        "epoch2_strictness_census",
        "canary_leaves_production_untouched",
    ]


# --- the loader over the authored table ------------------------------


def test_gate_bindings_for_loads_every_native_canary_gate_exactly_once() -> None:
    bindings = gate_bindings_for(PROFILE)
    assert tuple(bindings) == profile_gates(PROFILE)
    assert len(bindings) == 15


def test_native_canary_table_extends_the_dev2_table_rather_than_restating_it() -> None:
    assert NATIVE_CANARY_GATE_BINDINGS_YAML.startswith(DEV2_GATE_BINDINGS_YAML)


@pytest.mark.parametrize("gate", NATIVE_CANARY_ADDED_GATES)
def test_load_rejects_the_table_with_one_new_gate_unbound(gate: ReleaseGateName) -> None:
    with pytest.raises(GateBindingError) as excinfo:
        load_gate_bindings(_without(gate), profile=PROFILE)
    assert excinfo.value.code is GateBindingRejection.UNBOUND_PROFILE_GATE
    assert gate.value in str(excinfo.value)


def test_load_rejects_the_dev2_table_under_native_canary() -> None:
    """The three additions are unbound there, so the twelve-row table is refused."""
    with pytest.raises(GateBindingError) as excinfo:
        load_gate_bindings(DEV2_GATE_BINDINGS_YAML, profile=PROFILE)
    assert excinfo.value.code is GateBindingRejection.UNBOUND_PROFILE_GATE
    for gate in NATIVE_CANARY_ADDED_GATES:
        assert gate.value in str(excinfo.value)


def test_load_rejects_the_native_canary_table_under_dev2() -> None:
    with pytest.raises(GateBindingError) as excinfo:
        load_gate_bindings(NATIVE_CANARY_GATE_BINDINGS_YAML, profile=ReleaseGateProfile.DEV2)
    assert excinfo.value.code is GateBindingRejection.UNBOUND_GATE_OUTSIDE_PROFILE


def test_load_rejects_an_empty_table_under_native_canary() -> None:
    with pytest.raises(GateBindingError) as excinfo:
        load_gate_bindings({"bindings": []}, profile=PROFILE)
    assert excinfo.value.code is GateBindingRejection.UNBOUND_PROFILE_GATE


def test_load_rejects_a_duplicated_membership_binding() -> None:
    rows = _rows()
    rows.append(next(row for row in rows if row["gate"] == "membership"))
    with pytest.raises(GateBindingError) as excinfo:
        load_gate_bindings({"bindings": rows}, profile=PROFILE)
    assert excinfo.value.code is GateBindingRejection.DUPLICATE_BINDING


def test_load_rejects_a_provider_binding_naming_a_signal_that_does_not_exist() -> None:
    rows = _rows()
    for row in rows:
        if row["gate"] == "provider":
            row["signal"] = "runtimes"
    with pytest.raises(GateBindingError) as excinfo:
        load_gate_bindings({"bindings": rows}, profile=PROFILE)
    assert excinfo.value.code is GateBindingRejection.UNKNOWN_SIGNAL


def test_load_rejects_a_canary_isolation_row_carrying_a_signal_as_well() -> None:
    """A proof command that also names a row declares two evidence sources."""
    rows = _rows()
    for row in rows:
        if row["gate"] == "canary_isolation":
            row["signal"] = "provider"
    with pytest.raises(GateBindingError) as excinfo:
        load_gate_bindings({"bindings": rows}, profile=PROFILE)
    assert excinfo.value.code is GateBindingRejection.SCHEMA_INVALID
