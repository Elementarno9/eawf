"""The ``0.7.0.dev4`` rung: a second native canary, then the product canary.

``dev4`` ships the surfaces of the phase that rehearsed this repository's
own epoch-2 cutover without performing it, so the opted-in product canary
cannot be run at its cut. The rung therefore repeats the ``native_canary``
profile ``dev3`` ran under, and the product canary moves one rung up to
``dev5``, which stays unauthored until its producers exist.

These tests pin both halves. The ``dev4`` configuration renders from the
train template with the fifteen native-canary gates and four targets and
resolves against a record's membership refs; ``dev5`` sits between it and
the first release candidate under ``product_canary`` and has no
configuration to resolve.
"""

from __future__ import annotations

import pytest

from eawf.kernel.release.checkpoint_template import render_checkpoint_config
from eawf.kernel.release.gate_binding import profile_gates
from eawf.kernel.spec.release import ReleaseGateProfile, release_key
from eawf.kernel.spec.release_config import parse_release_config
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.release_context import resolve_config
from eawf.workflow.release.train import (
    DEV3_RELEASE_CONFIG_YAML,
    DEV4_RELEASE_CONFIG_YAML,
    V07_CONFIG_TEMPLATE,
    V07_TRAIN,
    checkpoint_config_yaml,
    gate_bindings_for,
)

#: The checkpoint under test.
DEV4_VERSION = "0.7.0.dev4"

#: The product-canary rung inserted after it.
DEV5_VERSION = "0.7.0.dev5"

#: An acceptance bundle a ``dev4`` record could name.
MEMBERSHIP_REF = "milestone://epoch2/native-canary"


def test_dev4_rung_runs_the_native_canary_profile() -> None:
    rung = V07_TRAIN.checkpoint_for_version(DEV4_VERSION)
    assert rung.gate_profile is ReleaseGateProfile.NATIVE_CANARY
    assert rung.authority_epoch == 2
    assert rung.requires_membership


def test_dev5_is_the_product_canary_rung_before_the_first_candidate() -> None:
    keys = [rung.release_key for rung in V07_TRAIN.checkpoints]
    dev4, dev5, rc1 = (release_key(v) for v in (DEV4_VERSION, DEV5_VERSION, "0.7.0rc1"))
    assert keys.index(dev5) == keys.index(dev4) + 1
    assert keys.index(rc1) == keys.index(dev5) + 1
    rung = V07_TRAIN.checkpoint_for_version(DEV5_VERSION)
    assert rung.gate_profile is ReleaseGateProfile.PRODUCT_CANARY
    assert rung.requires_membership


def test_no_other_rung_runs_the_product_canary_profile() -> None:
    product = [
        rung.release_key
        for rung in V07_TRAIN.checkpoints
        if rung.gate_profile is ReleaseGateProfile.PRODUCT_CANARY
    ]
    assert product == [release_key(DEV5_VERSION)]


def test_dev4_config_is_rendered_from_the_train_template() -> None:
    rendered = render_checkpoint_config(
        rung=V07_TRAIN.checkpoint_for_version(DEV4_VERSION),
        template=V07_CONFIG_TEMPLATE,
    )
    assert rendered == DEV4_RELEASE_CONFIG_YAML
    assert checkpoint_config_yaml(DEV4_VERSION) == DEV4_RELEASE_CONFIG_YAML


def test_dev4_config_requires_the_fifteen_native_canary_gates() -> None:
    config = parse_release_config(DEV4_RELEASE_CONFIG_YAML)
    assert config.version == DEV4_VERSION
    assert config.authority_epoch == 2
    assert config.gates.profile is ReleaseGateProfile.NATIVE_CANARY
    assert config.gates.required == profile_gates(ReleaseGateProfile.NATIVE_CANARY)
    assert len(config.gates.required) == 15


def test_dev4_config_declares_the_four_dev3_targets() -> None:
    dev4 = parse_release_config(DEV4_RELEASE_CONFIG_YAML)
    dev3 = parse_release_config(DEV3_RELEASE_CONFIG_YAML)
    assert [t.target_id for t in dev4.targets] == ["pypi", "npm", "github", "plugins-dist"]
    assert dev4.model_dump(exclude={"version"}) == dev3.model_dump(exclude={"version"})


def test_every_dev4_gate_has_a_binding() -> None:
    config = parse_release_config(DEV4_RELEASE_CONFIG_YAML)
    bindings = gate_bindings_for(config.gates.profile)
    assert set(config.gates.required) <= set(bindings)


def test_resolve_config_loads_dev4_with_the_records_membership_refs() -> None:
    config = resolve_config(DEV4_VERSION, membership_refs=(MEMBERSHIP_REF,))
    assert config.membership_refs == (MEMBERSHIP_REF,)
    assert config.version == DEV4_VERSION


def test_resolve_config_refuses_dev4_without_membership_refs() -> None:
    with pytest.raises(DaemonValidationError) as excinfo:
        resolve_config(DEV4_VERSION)
    assert "invalid_membership_cardinality" in str(excinfo.value)


def test_resolve_config_refuses_the_unauthored_dev5_rung() -> None:
    with pytest.raises(DaemonValidationError) as excinfo:
        resolve_config(DEV5_VERSION, membership_refs=(MEMBERSHIP_REF,))
    assert "no release configuration" in str(excinfo.value)
