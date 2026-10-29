"""The ``0.7.0rc3`` rung repeats ``cross_provider`` one rung after ``rc2``.

``rc3`` is the third release candidate, cut on the same evidence bar as
``rc2``: these tests pin its place in the ladder, its rendered
configuration and that it carries exactly the gates, targets and platform
claims ``rc2`` declared, differing only in its version.
"""

from __future__ import annotations

from typing import Final

import pytest

from eawf.kernel.release.checkpoint_template import render_checkpoint_config
from eawf.kernel.release.gate_binding import profile_gates
from eawf.kernel.spec.release import ReleaseGateProfile
from eawf.kernel.spec.release_config import parse_release_config
from eawf.workflow.release.train import (
    CHECKPOINT_CONFIGS,
    RC2_RELEASE_CONFIG_YAML,
    RC3_RELEASE_CONFIG_YAML,
    V07_CONFIG_TEMPLATE,
    V07_TRAIN,
    checkpoint_config_yaml,
)

pytestmark = pytest.mark.integration

RC3_VERSION: Final = "0.7.0rc3"
PROFILE: Final = ReleaseGateProfile.CROSS_PROVIDER


def test_rc3_sits_between_rc2_and_rc4() -> None:
    keys = [rung.release_key for rung in V07_TRAIN.checkpoints]

    assert keys[-4:-1] == ["REL-0.7.0rc2", "REL-0.7.0rc3", "REL-0.7.0rc4"]


def test_rc3_runs_epoch_2_under_cross_provider_with_membership() -> None:
    rung = V07_TRAIN.checkpoint_for_version(RC3_VERSION)

    assert rung.authority_epoch == 2
    assert rung.gate_profile is PROFILE
    assert rung.requires_membership is True


def test_rc3_config_is_rendered_from_the_train_template() -> None:
    rendered = render_checkpoint_config(
        rung=V07_TRAIN.checkpoint_for_version(RC3_VERSION),
        template=V07_CONFIG_TEMPLATE,
    )

    assert rendered == RC3_RELEASE_CONFIG_YAML
    assert CHECKPOINT_CONFIGS[RC3_VERSION] == RC3_RELEASE_CONFIG_YAML
    assert checkpoint_config_yaml(RC3_VERSION) == RC3_RELEASE_CONFIG_YAML


def test_rc3_config_requires_the_twenty_four_cross_provider_gates() -> None:
    config = parse_release_config(RC3_RELEASE_CONFIG_YAML)

    assert config.version == RC3_VERSION
    assert config.authority_epoch == 2
    assert config.gates.profile is PROFILE
    assert config.gates.required == profile_gates(PROFILE)
    assert len(config.gates.required) == 24


def test_rc3_declares_the_rc2_targets_and_platform_claims() -> None:
    rc3 = parse_release_config(RC3_RELEASE_CONFIG_YAML)
    rc2 = parse_release_config(RC2_RELEASE_CONFIG_YAML)

    assert rc3.targets == rc2.targets
    assert rc3.platform_claims == rc2.platform_claims
    assert [c.platform_id for c in rc3.platform_claims] == ["linux-x86_64", "windows-x86_64"]


def test_a_rung_past_rc4_has_no_config() -> None:
    """The off-by-one boundary: no fifth candidate is declared."""
    with pytest.raises(KeyError):
        V07_TRAIN.checkpoint_for_version("0.7.0rc5")
