"""The ``0.7.0rc1`` rung renders under ``flag_day`` with every gate bound.

``rc1`` is the first release candidate and the first rung whose package
refuses epoch-1 state, so its profile adds one gate to the twenty-two
``product_canary`` gates: the proof that a plain epoch-1 tree takes no
mutating verb. These tests pin the gate set, the rendered configuration
with its four targets, the binding table behind it, and the committed
canary export the membership rung resolves its bundle against.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from eawf import __version__
from eawf.kernel.release.checkpoint_template import render_checkpoint_config
from eawf.kernel.release.gate_binding import (
    FLAG_DAY_ADDED_GATES,
    GateBindingError,
    GateBindingRejection,
    GateEvidenceKind,
    load_gate_bindings,
    profile_gates,
)
from eawf.kernel.spec.release import ReleaseGateProfile
from eawf.kernel.spec.release_config import ReleaseGateName, parse_release_config
from eawf.workflow.evidence.provider_certification import load_canary_evidence
from eawf.workflow.release.admission import checkpoint_release_config
from eawf.workflow.release.train import (
    DEV5_RELEASE_CONFIG_YAML,
    FLAG_DAY_GATE_BINDINGS_YAML,
    RC1_RELEASE_CONFIG_YAML,
    V07_CONFIG_TEMPLATE,
    V07_TRAIN,
    checkpoint_config_yaml,
    gate_bindings_for,
)

pytestmark = pytest.mark.integration

#: This checkout, whose committed export is the real evidence.
REPO_ROOT: Final = Path(__file__).resolve().parents[4]

RC1_VERSION: Final = "0.7.0rc1"
RC1_KEY: Final = f"REL-{RC1_VERSION}"
PROFILE: Final = ReleaseGateProfile.FLAG_DAY

#: The test files the flag-day proof runs.
FLAG_DAY_PROOF_FILES: Final = (
    "tests/integration/surfaces/cli/test_rel_021_epoch1_tree_gate.py",
    "tests/contract/surfaces/cli/test_rel_021_flag_day_census.py",
)


def binding_rows() -> list[dict[str, Any]]:
    """Return the authored flag-day binding rows as mutable dicts."""
    decoded: dict[str, Any] = yaml.safe_load(FLAG_DAY_GATE_BINDINGS_YAML)
    rows: list[dict[str, Any]] = decoded["bindings"]
    return rows


def test_flag_day_is_the_product_canary_gates_plus_the_epoch1_refusal() -> None:
    gates = profile_gates(PROFILE)

    assert gates == (*profile_gates(ReleaseGateProfile.PRODUCT_CANARY), *FLAG_DAY_ADDED_GATES)
    assert FLAG_DAY_ADDED_GATES == (ReleaseGateName.EPOCH1_REFUSAL,)
    assert len(gates) == 23


def test_rc1_config_is_rendered_from_the_train_template() -> None:
    rendered = render_checkpoint_config(
        rung=V07_TRAIN.checkpoint_for_version(RC1_VERSION),
        template=V07_CONFIG_TEMPLATE,
    )

    assert rendered == RC1_RELEASE_CONFIG_YAML
    assert checkpoint_config_yaml(RC1_VERSION) == RC1_RELEASE_CONFIG_YAML


def test_rc1_config_requires_the_twenty_three_flag_day_gates() -> None:
    config = parse_release_config(RC1_RELEASE_CONFIG_YAML)

    assert config.version == RC1_VERSION
    assert config.authority_epoch == 2
    assert config.gates.profile is PROFILE
    assert config.gates.required == profile_gates(PROFILE)


def test_rc1_config_declares_the_four_dev5_targets() -> None:
    rc1 = parse_release_config(RC1_RELEASE_CONFIG_YAML)
    dev5 = parse_release_config(DEV5_RELEASE_CONFIG_YAML)

    assert [t.target_id for t in rc1.targets] == ["pypi", "npm", "github", "plugins-dist"]
    assert rc1.targets == dev5.targets
    assert rc1.platform_claims == dev5.platform_claims


def test_every_rc1_gate_has_a_binding() -> None:
    config = parse_release_config(RC1_RELEASE_CONFIG_YAML)

    assert tuple(gate_bindings_for(config.gates.profile)) == config.gates.required


def test_the_epoch1_refusal_gate_runs_the_flag_day_suites() -> None:
    binding = gate_bindings_for(PROFILE)[ReleaseGateName.EPOCH1_REFUSAL]

    assert binding.kind is GateEvidenceKind.PROOF_COMMAND
    assert binding.proof is not None
    assert binding.proof.argv[:3] == ("uv", "run", "pytest")
    for path in FLAG_DAY_PROOF_FILES:
        assert path in binding.proof.argv
        assert (REPO_ROOT / path).is_file(), path


def test_the_epoch1_refusal_gate_left_unbound_is_refused() -> None:
    """Gate-fire proof: dropping the flag-day binding reds the load."""
    rows = [row for row in binding_rows() if row["gate"] != ReleaseGateName.EPOCH1_REFUSAL.value]

    with pytest.raises(GateBindingError) as caught:
        load_gate_bindings({"bindings": rows}, profile=PROFILE)

    assert caught.value.code is GateBindingRejection.UNBOUND_PROFILE_GATE
    assert ReleaseGateName.EPOCH1_REFUSAL.value in str(caught.value)


def test_the_epoch1_refusal_binding_is_refused_under_product_canary() -> None:
    """The earlier profile does not admit the gate the flag day adds."""
    with pytest.raises(GateBindingError) as caught:
        load_gate_bindings({"bindings": binding_rows()}, profile=ReleaseGateProfile.PRODUCT_CANARY)

    assert caught.value.code is GateBindingRejection.UNBOUND_GATE_OUTSIDE_PROFILE


def test_rc1_resolves_its_membership_off_the_committed_export() -> None:
    evidence = load_canary_evidence(REPO_ROOT, RC1_KEY)
    config = checkpoint_release_config(RC1_VERSION, repo_root=REPO_ROOT)

    assert evidence is not None
    assert evidence.release_key == RC1_KEY
    assert len(config.membership_refs) == 1
    accepted = evidence.milestone_for(config.membership_refs[0])
    assert accepted is not None
    assert accepted.status.value == "COMPLETED"


def test_the_package_version_is_rc1() -> None:
    assert __version__ == RC1_VERSION
