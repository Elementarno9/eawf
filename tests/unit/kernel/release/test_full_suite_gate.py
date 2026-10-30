"""Tests for the ``full_suite`` gate the stable profile runs.

The stable profile settles its suite gate under the name ``full_suite``.
The dev1 to rc4 rungs keep ``epoch1_stabilization``, because their
stored receipts are bound to that name and must keep loading.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.release.gate_binding import GateEvidenceKind, profile_gates
from eawf.kernel.spec.release import ReleaseGateProfile
from eawf.kernel.spec.release_config import ReleaseGateName
from eawf.workflow.release.advance import CheckpointGateReceipt
from eawf.workflow.release.train import gate_bindings_for
from eawf.workflow.release.train_store import read_checkpoint_receipts
from tests._release_helpers import SOURCE_SHA
from tests.conftest import REPO_ROOT

#: The committed receipt store of this repository.
_RECEIPTS = REPO_ROOT / ".ea" / "store" / "release_checkpoint_receipt.jsonl"

#: The profiles the dev1 to rc4 rungs run under.
_HISTORICAL_PROFILES = (
    ReleaseGateProfile.DEV1,
    ReleaseGateProfile.DEV2,
    ReleaseGateProfile.NATIVE_CANARY,
    ReleaseGateProfile.PRODUCT_CANARY,
    ReleaseGateProfile.FLAG_DAY,
    ReleaseGateProfile.CROSS_PROVIDER,
)


#: The rc4 gates stable leaves behind: the ``migration`` row reads the
#: committed rehearsal of a cutover this release no longer ships.
_EPOCH1_ONLY_GATES = frozenset({ReleaseGateName.MIGRATION})


def _release_keys_with_stabilization_receipts(path: Path) -> list[str]:
    """Return each release key whose stored receipts name ``epoch1_stabilization``."""
    keys: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        payload = json.loads(line)["payload"]
        if payload["gate"] == "epoch1_stabilization" and payload["release_key"] not in keys:
            keys.append(payload["release_key"])
    return keys


def test_stable_profile_declares_full_suite() -> None:
    assert ReleaseGateName.FULL_SUITE in profile_gates(ReleaseGateProfile.STABLE)
    assert ReleaseGateName.EPOCH1_STABILIZATION not in profile_gates(ReleaseGateProfile.STABLE)


def test_stable_keeps_every_rc4_gate_but_the_epoch1_ones() -> None:
    rc4 = set(profile_gates(ReleaseGateProfile.CROSS_PROVIDER))
    expected = (rc4 - {ReleaseGateName.EPOCH1_STABILIZATION} - _EPOCH1_ONLY_GATES) | {
        ReleaseGateName.FULL_SUITE
    }

    assert expected <= set(profile_gates(ReleaseGateProfile.STABLE))


def test_stable_drops_the_cutover_rehearsal_row() -> None:
    assert _EPOCH1_ONLY_GATES.isdisjoint(profile_gates(ReleaseGateProfile.STABLE))


def test_stable_settles_the_suite_where_rc4_did() -> None:
    rc4 = profile_gates(ReleaseGateProfile.CROSS_PROVIDER)
    stable = profile_gates(ReleaseGateProfile.STABLE)

    assert stable.index(ReleaseGateName.FULL_SUITE) == rc4.index(
        ReleaseGateName.EPOCH1_STABILIZATION
    )


def test_every_stable_gate_has_a_binding() -> None:
    bindings = gate_bindings_for(ReleaseGateProfile.STABLE)

    assert tuple(bindings) == profile_gates(ReleaseGateProfile.STABLE)


def test_stable_binds_each_shared_gate_as_rc4_does() -> None:
    stable = gate_bindings_for(ReleaseGateProfile.STABLE)
    rc4 = gate_bindings_for(ReleaseGateProfile.CROSS_PROVIDER)
    shared = set(stable) & set(rc4)

    # Everything but the renamed suite gate is carried over.
    assert shared == set(stable) - {ReleaseGateName.FULL_SUITE}
    for gate in shared:
        assert stable[gate] == rc4[gate]


def test_stable_full_suite_runs_the_rc4_suite_command() -> None:
    suite = gate_bindings_for(ReleaseGateProfile.STABLE)[ReleaseGateName.FULL_SUITE]
    rc4 = gate_bindings_for(ReleaseGateProfile.CROSS_PROVIDER)[ReleaseGateName.EPOCH1_STABILIZATION]

    assert suite.proof is not None
    assert rc4.proof is not None
    assert suite.proof.argv == rc4.proof.argv
    assert suite.proof.timeout_seconds == rc4.proof.timeout_seconds


def test_stable_full_suite_runs_the_whole_suite() -> None:
    binding = gate_bindings_for(ReleaseGateProfile.STABLE)[ReleaseGateName.FULL_SUITE]
    assert binding.kind is GateEvidenceKind.PROOF_COMMAND
    assert binding.required_signal is None

    resolved = binding.resolve_proof(SOURCE_SHA)

    # ``just test-all`` is the whole-tree run CI's twice-green job uses.
    assert resolved.argv == ("just", "test-all")
    assert resolved.command_id == "full_suite"
    assert resolved.timeout_seconds == 3600
    assert resolved.source_sha == SOURCE_SHA


@pytest.mark.parametrize("profile", _HISTORICAL_PROFILES)
def test_historical_profiles_keep_epoch1_stabilization(profile: ReleaseGateProfile) -> None:
    gates = profile_gates(profile)
    assert ReleaseGateName.EPOCH1_STABILIZATION in gates
    assert ReleaseGateName.FULL_SUITE not in gates


def test_every_committed_epoch1_stabilization_receipt_loads() -> None:
    keys = _release_keys_with_stabilization_receipts(_RECEIPTS)
    # dev2, dev3, dev5 and rc4 settled the gate under its old name.
    assert len(keys) >= 4

    for key in keys:
        receipts = read_checkpoint_receipts(REPO_ROOT / ".ea" / "state.json", key)
        assert ReleaseGateName.EPOCH1_STABILIZATION in {receipt.gate for receipt in receipts}


def test_a_receipt_naming_an_unknown_gate_is_refused() -> None:
    line = next(
        line
        for line in _RECEIPTS.read_text(encoding="utf-8").splitlines()
        if '"gate":"epoch1_stabilization"' in line
    )
    payload = json.loads(line)["payload"]
    payload["gate"] = "epoch1_stabilisation"

    with pytest.raises(ValidationError, match="gate"):
        CheckpointGateReceipt.model_validate(payload)


def test_full_suite_is_not_an_alias_of_the_old_name() -> None:
    assert ReleaseGateName("full_suite") is not ReleaseGateName("epoch1_stabilization")
