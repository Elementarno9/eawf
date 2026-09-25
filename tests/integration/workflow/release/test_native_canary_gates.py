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

**A missing receipt refuses approval.** An epoch-2 checkpoint is
approved only on a fresh stored receipt per required gate, bound to the
candidate's source and manifest. The gate-fire proof runs
``release.approve`` over a tmp state root: fifteen fresh receipts
approve, and dropping any one of them, binding one to other source, or
letting one expire refuses with the gate named and writes no approval.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml

from eawf.kernel.release.checkpoint_template import with_membership_refs
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
from eawf.kernel.spec.release import Release, ReleaseChannel, ReleaseGateProfile, ReleaseStatus
from eawf.kernel.spec.release_config import ReleaseConfig, ReleaseGateName, load_release_config
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.release import approve
from eawf.workflow.evidence.provider_certification import load_canary_evidence
from eawf.workflow.release.advance import (
    CheckpointGateReceipt,
)
from eawf.workflow.release.preflight import assert_approval_receipts
from eawf.workflow.release.records import read_release_record
from eawf.workflow.release.train import (
    DEV2_GATE_BINDINGS_YAML,
    NATIVE_CANARY_GATE_BINDINGS_YAML,
    V07_TRAIN,
    checkpoint_config_yaml,
    gate_bindings_for,
)
from eawf.workflow.release.train_store import record_checkpoint_receipt
from eawf.workflow.verify.release_readiness import compute_readiness
from tests._release_helpers import MANIFEST_DIGEST, SOURCE_SHA, TREE_SHA, all_passing

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


# --- every gate receipted, and a missing receipt refuses approval ----


REPO_ROOT = Path(__file__).resolve().parents[4]
DEV3_VERSION = "0.7.0.dev3"
DEV3_KEY = f"REL-{DEV3_VERSION}"


def membership_ref() -> str:
    """Return the acceptance bundle the committed canary export records."""
    evidence = load_canary_evidence(REPO_ROOT)
    assert evidence is not None, "no canary evidence export is committed"
    reference: str = evidence.milestones[0].reference
    return reference


def dev3_config(membership_refs: tuple[str, ...]) -> ReleaseConfig:
    """Return the dev3 configuration resolved against *membership_refs*."""
    document = with_membership_refs(
        checkpoint_config_yaml(DEV3_VERSION), membership_refs=membership_refs
    )
    return load_release_config(document, train=V07_TRAIN)


def required_gates() -> tuple[ReleaseGateName, ...]:
    """Return the gates the dev3 configuration requires, in gate order."""
    return tuple(dev3_config((membership_ref(),)).gates.required)


def candidate() -> Release:
    """Return a dev3 candidate pinned to the shared test source and manifest."""
    return Release(
        uid=UUID(int=73),
        key=DEV3_KEY,
        version=DEV3_VERSION,
        channel=ReleaseChannel.DEV,
        authority_epoch=2,
        membership_refs=(membership_ref(),),
        status=ReleaseStatus.CANDIDATE,
        source_sha=SOURCE_SHA,
        source_tree_sha=TREE_SHA,
        manifest_ref="artifact://release/dev3-manifest",
        manifest_digest=MANIFEST_DIGEST,
        revision=1,
    )


def receipt(gate: ReleaseGateName, **overrides: Any) -> CheckpointGateReceipt:
    """Return a receipt for *gate* bound to :func:`candidate`, fresh now."""
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "gate": gate,
        "release_key": DEV3_KEY,
        "source_sha": SOURCE_SHA,
        "manifest_digest": MANIFEST_DIGEST,
        "issued_at": now - timedelta(hours=1),
        "expires_at": now + timedelta(hours=1),
        "receipt_ref": f"checkpoint-receipt://{DEV3_KEY}/{gate.value}/fresh",
    }
    payload.update(overrides)
    return CheckpointGateReceipt.model_validate(payload)


def staged_context(root: Path, receipts: list[CheckpointGateReceipt]) -> MethodContext:
    """Return a context over a tmp state root holding *receipts*."""
    state_dir = root / ".ea"
    state_dir.mkdir()
    state_path = state_dir / "state.json"
    state_path.write_text(json.dumps({}), encoding="utf-8")
    now = datetime.now(UTC)
    for row in receipts:
        record_checkpoint_receipt(state_path, row, recorded_at=now, summary="seed")
    return MethodContext(
        started_at=now.isoformat(),
        pid=4242,
        protocol_version=PROTOCOL_VERSION,
        version="test",
        state_path=state_path,
    )


def approve_params() -> dict[str, Any]:
    """Return approve params over :func:`candidate` and an all-green sweep."""
    readiness = compute_readiness(
        dev3_config((membership_ref(),)), probes=all_passing(), computed_at=datetime.now(UTC)
    )
    return {
        "release": candidate().model_dump(mode="json"),
        "readiness": readiness.model_dump(mode="json"),
        "approval_ref": "receipt://approval/dev3",
    }


def refused_approval(ctx: MethodContext) -> str:
    """Return the refusal of an approval that must not succeed."""
    with pytest.raises(DaemonValidationError) as excinfo:
        asyncio.run(approve(ctx, approve_params()))
    return str(excinfo.value)


def test_dev3_requires_every_native_canary_gate() -> None:
    assert set(required_gates()) == set(profile_gates(PROFILE))
    assert len(required_gates()) == 15


def test_approve_admits_a_dev3_candidate_with_every_gate_receipted(tmp_path: Path) -> None:
    ctx = staged_context(tmp_path, [receipt(gate) for gate in required_gates()])

    result = asyncio.run(approve(ctx, approve_params()))

    assert result["release"]["status"] == ReleaseStatus.APPROVED.value
    stored = read_release_record(Path(str(ctx.state_path)), DEV3_KEY)
    assert stored is not None
    assert stored.status is ReleaseStatus.APPROVED


@pytest.mark.parametrize("dropped", profile_gates(PROFILE), ids=lambda gate: gate.value)
def test_approve_refuses_a_dev3_candidate_missing_one_receipt(
    tmp_path: Path, dropped: ReleaseGateName
) -> None:
    """Gate-fire: removing any single receipt refuses and records nothing."""
    ctx = staged_context(
        tmp_path, [receipt(gate) for gate in required_gates() if gate is not dropped]
    )

    message = refused_approval(ctx)

    assert "prerequisite_receipt_missing" in message
    assert repr(dropped.value) in message
    assert "eawf release receipts 0.7.0.dev3" in message
    assert read_release_record(Path(str(ctx.state_path)), DEV3_KEY) is None


def test_approve_refuses_a_receipt_bound_to_other_source(tmp_path: Path) -> None:
    gates = required_gates()
    rows = [receipt(gate) for gate in gates[1:]]
    rows.append(receipt(gates[0], source_sha="d" * 40))
    ctx = staged_context(tmp_path, rows)

    message = refused_approval(ctx)

    assert "prerequisite_receipt_stale" in message
    assert "source_sha" in message


def test_approve_refuses_an_expired_receipt(tmp_path: Path) -> None:
    gates = required_gates()
    now = datetime.now(UTC)
    rows = [receipt(gate) for gate in gates[:-1]]
    rows.append(
        receipt(gates[-1], issued_at=now - timedelta(days=2), expires_at=now - timedelta(days=1))
    )
    ctx = staged_context(tmp_path, rows)

    message = refused_approval(ctx)

    assert "prerequisite_receipt_stale" in message
    assert "expired" in message


def test_approval_receipts_are_not_asked_of_an_epoch1_rung() -> None:
    """dev2 was approved before receipts existed, so its approval is not re-judged."""
    dev2 = V07_TRAIN.checkpoint_for_version("0.7.0.dev2")
    assert dev2.authority_epoch == 1
    refs = assert_approval_receipts(
        candidate(),
        dev3_config((membership_ref(),)),
        (),
        rung=dev2,
        now=datetime.now(UTC),
        train_id=V07_TRAIN.train_id,
    )
    assert refs == ()


def test_approval_receipts_refuse_a_naive_instant() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        assert_approval_receipts(
            candidate(),
            dev3_config((membership_ref(),)),
            [receipt(gate) for gate in required_gates()],
            rung=V07_TRAIN.checkpoint_for_version(DEV3_VERSION),
            now=datetime(2026, 9, 24, 12, 0),
            train_id=V07_TRAIN.train_id,
        )
