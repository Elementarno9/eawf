"""The fourth candidate retains the existing cross-provider release bar."""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.kernel.release.checkpoint_template import render_checkpoint_config
from eawf.kernel.release.gate_binding import profile_gates
from eawf.kernel.spec.release import ReleaseGateProfile
from eawf.kernel.spec.release_config import parse_release_config
from eawf.workflow.evidence.provider_certification import load_canary_evidence
from eawf.workflow.release.train import (
    CHECKPOINT_CONFIGS,
    RC3_RELEASE_CONFIG_YAML,
    RC4_RELEASE_CONFIG_YAML,
    V07_CONFIG_TEMPLATE,
    V07_TRAIN,
    checkpoint_config_yaml,
)
from tests.integration.workflow.release.test_dev3_canary_rehearsal_record import WalkRecord

pytestmark = pytest.mark.integration


def test_rc4_follows_rc3_before_stable() -> None:
    keys = [rung.release_key for rung in V07_TRAIN.checkpoints]

    assert keys[-3:] == ["REL-0.7.0rc3", "REL-0.7.0rc4", "REL-0.7.0"]


def test_rc4_keeps_epoch_profile_membership_and_twenty_four_gates() -> None:
    rung = V07_TRAIN.checkpoint_for_version("0.7.0rc4")
    config = parse_release_config(RC4_RELEASE_CONFIG_YAML)

    assert rung.authority_epoch == 2
    assert rung.requires_membership is True
    assert rung.gate_profile is ReleaseGateProfile.CROSS_PROVIDER
    assert config.gates.required == profile_gates(rung.gate_profile)
    assert len(config.gates.required) == 24


def test_rc4_uses_the_train_renderer_and_its_own_lookup() -> None:
    rendered = render_checkpoint_config(
        rung=V07_TRAIN.checkpoint_for_version("0.7.0rc4"), template=V07_CONFIG_TEMPLATE
    )

    assert rendered == RC4_RELEASE_CONFIG_YAML
    assert CHECKPOINT_CONFIGS["0.7.0rc4"] == rendered
    assert checkpoint_config_yaml("0.7.0rc4") == rendered


def test_rc4_preserves_rc3_targets_and_platform_claims() -> None:
    rc3 = parse_release_config(RC3_RELEASE_CONFIG_YAML)
    rc4 = parse_release_config(RC4_RELEASE_CONFIG_YAML)

    assert rc4.targets == rc3.targets
    assert len(rc4.targets) == 4
    assert rc4.platform_claims == rc3.platform_claims
    assert [claim.platform_id for claim in rc4.platform_claims] == [
        "linux-x86_64",
        "windows-x86_64",
    ]


def test_fifth_candidate_remains_unknown() -> None:
    with pytest.raises(KeyError, match="declares no checkpoint"):
        V07_TRAIN.checkpoint_for_version("0.7.0rc5")
    with pytest.raises(KeyError):
        checkpoint_config_yaml("0.7.0rc5")


def test_rc4_membership_matches_its_fresh_native_acceptance_walk() -> None:
    root = Path(__file__).resolve().parents[4]
    fixture = root / (
        "tests/fixtures/artifacts/2026-10-29-rc4-native-linux/canary-acceptance-walk.json"
    )
    record = WalkRecord.model_validate_json(fixture.read_bytes())
    evidence = load_canary_evidence(root, "REL-0.7.0rc4")

    assert evidence is not None
    (membership,) = evidence.milestones
    assert membership.reference == record.milestone.reference
    assert membership.bundle_digest == record.acceptance_bundle.digest()
    assert membership.status.value == "COMPLETED"
    assert record.provider_processes_started == 0
    assert [(claim.runtime_id, claim.os_classes) for claim in evidence.advertised] == [
        ("claude-code", ("linux",))
    ]
