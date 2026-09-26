"""The ``0.7.0.dev5`` rung renders under ``product_canary`` with every gate bound.

``dev5`` is the first rung that runs the native path on this repository
rather than in a disposable canary, so its profile adds the seven
canary-window receipts to the fifteen ``native_canary`` gates. These tests
pin the gate set, the rendered configuration with its four targets, and
the binding table behind it -- including the refusal that fires when a
profile gate is left unbound, which is what keeps a new gate from passing
by reading nothing.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from eawf.kernel.release.checkpoint_template import render_checkpoint_config
from eawf.kernel.release.gate_binding import (
    NATIVE_CANARY_ADDED_GATES,
    PRODUCT_CANARY_POST_MERGE_GATES,
    PRODUCT_CANARY_PRE_MERGE_GATES,
    GateBindingError,
    GateBindingRejection,
    GateEvidenceKind,
    load_gate_bindings,
    profile_gates,
)
from eawf.kernel.spec.release import ReleaseGateProfile
from eawf.kernel.spec.release_config import ReleaseGateName, parse_release_config
from eawf.runtime.daemon.methods.release_context import resolve_config
from eawf.workflow.release.train import (
    DEV4_RELEASE_CONFIG_YAML,
    DEV5_RELEASE_CONFIG_YAML,
    PRODUCT_CANARY_GATE_BINDINGS_YAML,
    V07_CONFIG_TEMPLATE,
    V07_TRAIN,
    checkpoint_config_yaml,
    gate_bindings_for,
)

DEV5_VERSION = "0.7.0.dev5"
PROFILE = ReleaseGateProfile.PRODUCT_CANARY

#: The seven gates the product canary adds, in declaration order.
CANARY_WINDOW_GATES = (
    ReleaseGateName.PLAN_REVISION_APPROVED,
    ReleaseGateName.PARALLEL_DISPATCH,
    ReleaseGateName.EXACT_HEAD_INTEGRATION,
    ReleaseGateName.REAL_DIFF_REVIEW,
    ReleaseGateName.MILESTONE_ACCEPTED,
    ReleaseGateName.MIGRATION_RERUN_IDENTICAL,
    ReleaseGateName.RELEASE_TAGGED_OBSERVED,
)

#: An acceptance bundle a ``dev5`` record could name.
MEMBERSHIP_REF = "milestone://epoch2/product-canary"


def binding_rows() -> list[dict[str, Any]]:
    """Return the authored product-canary binding rows as mutable dicts."""
    decoded: dict[str, Any] = yaml.safe_load(PRODUCT_CANARY_GATE_BINDINGS_YAML)
    rows: list[dict[str, Any]] = decoded["bindings"]
    return rows


def test_product_canary_is_the_native_canary_gates_plus_the_canary_window() -> None:
    gates = profile_gates(PROFILE)

    assert gates == (*profile_gates(ReleaseGateProfile.NATIVE_CANARY), *CANARY_WINDOW_GATES)
    assert len(gates) == 22
    assert gates[-7:] == (*PRODUCT_CANARY_PRE_MERGE_GATES, *PRODUCT_CANARY_POST_MERGE_GATES)
    assert set(NATIVE_CANARY_ADDED_GATES) < set(gates)


def test_release_tagged_observed_is_the_only_post_merge_gate() -> None:
    assert PRODUCT_CANARY_POST_MERGE_GATES == (ReleaseGateName.RELEASE_TAGGED_OBSERVED,)
    assert ReleaseGateName.RELEASE_TAGGED_OBSERVED not in PRODUCT_CANARY_PRE_MERGE_GATES
    assert len(PRODUCT_CANARY_PRE_MERGE_GATES) == 6


def test_dev5_config_is_rendered_from_the_train_template() -> None:
    rendered = render_checkpoint_config(
        rung=V07_TRAIN.checkpoint_for_version(DEV5_VERSION),
        template=V07_CONFIG_TEMPLATE,
    )

    assert rendered == DEV5_RELEASE_CONFIG_YAML
    assert checkpoint_config_yaml(DEV5_VERSION) == DEV5_RELEASE_CONFIG_YAML


def test_dev5_config_requires_the_twenty_two_product_canary_gates() -> None:
    config = parse_release_config(DEV5_RELEASE_CONFIG_YAML)

    assert config.version == DEV5_VERSION
    assert config.authority_epoch == 2
    assert config.gates.profile is PROFILE
    assert config.gates.required == profile_gates(PROFILE)


def test_dev5_config_declares_the_four_dev4_targets() -> None:
    dev5 = parse_release_config(DEV5_RELEASE_CONFIG_YAML)
    dev4 = parse_release_config(DEV4_RELEASE_CONFIG_YAML)

    assert [t.target_id for t in dev5.targets] == ["pypi", "npm", "github", "plugins-dist"]
    assert dev5.targets == dev4.targets
    assert dev5.platform_claims == dev4.platform_claims


def test_every_dev5_gate_has_a_binding() -> None:
    config = parse_release_config(DEV5_RELEASE_CONFIG_YAML)
    bindings = gate_bindings_for(config.gates.profile)

    assert tuple(bindings) == config.gates.required


@pytest.mark.parametrize("gate", CANARY_WINDOW_GATES, ids=lambda gate: gate.value)
def test_each_canary_window_gate_is_settled_by_a_pinned_proof(gate: ReleaseGateName) -> None:
    binding = gate_bindings_for(PROFILE)[gate]

    assert binding.kind is GateEvidenceKind.PROOF_COMMAND
    assert binding.required_signal is None
    assert binding.proof is not None
    assert binding.proof.argv[:3] == ("uv", "run", "pytest")


def test_the_live_cutover_gate_runs_the_committed_record_check() -> None:
    proof = gate_bindings_for(PROFILE)[ReleaseGateName.MIGRATION_RERUN_IDENTICAL].proof

    assert proof is not None
    assert "tests/integration/kernel/migration/test_epoch2_live_cutover_record.py" in proof.argv


def test_the_integration_gate_runs_the_real_daemon_seal_proof() -> None:
    proof = gate_bindings_for(PROFILE)[ReleaseGateName.EXACT_HEAD_INTEGRATION].proof

    assert proof is not None
    assert "tests/integration/runtime/daemon/test_integrate_seal_real_daemon.py" in proof.argv


@pytest.mark.parametrize("gate", CANARY_WINDOW_GATES, ids=lambda gate: gate.value)
def test_a_product_canary_gate_left_unbound_is_refused(gate: ReleaseGateName) -> None:
    """Gate-fire proof: dropping any one canary-window binding reds the load."""
    rows = [row for row in binding_rows() if row["gate"] != gate.value]

    with pytest.raises(GateBindingError) as caught:
        load_gate_bindings({"bindings": rows}, profile=PROFILE)

    assert caught.value.code is GateBindingRejection.UNBOUND_PROFILE_GATE
    assert gate.value in str(caught.value)


def test_a_canary_window_binding_is_refused_under_native_canary() -> None:
    """The earlier profile does not admit the gates the product canary adds."""
    with pytest.raises(GateBindingError) as caught:
        load_gate_bindings({"bindings": binding_rows()}, profile=ReleaseGateProfile.NATIVE_CANARY)

    assert caught.value.code is GateBindingRejection.UNBOUND_GATE_OUTSIDE_PROFILE


def test_resolve_config_loads_dev5_with_the_records_membership_refs() -> None:
    config = resolve_config(DEV5_VERSION, membership_refs=(MEMBERSHIP_REF,))

    assert config.version == DEV5_VERSION
    assert config.membership_refs == (MEMBERSHIP_REF,)
