"""Tests for :mod:`eawf.kernel.spec.release_config`.

The authored ``0.7.0.dev1`` checkpoint configuration must load clean,
and every rejection the loader promises must fire with its own typed
code. This module runs one case per listed condition:

* the packet ``dev1`` configuration loads and exposes its three targets,
  its profile and its eight gate names;
* duplicate targets, channel/version disagreement, an undeclared epoch,
  an undeclared gate profile, the default distribution tag on a
  prerelease, an invalid membership cardinality (both directions), a
  missing or unrecognised observation adapter, and a nonpositive timeout
  are each rejected with their own :class:`ReleaseConfigRejection`;
* boundary cases: zero targets, one target, an empty gate list, a
  retry limit at its floor, and a timeout at its off-by-one floor;
* error paths: malformed YAML, a non-mapping payload, an unknown key
  and an unknown gate name.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
import yaml

from eawf.kernel.spec.release import ReleaseChannel, ReleaseGateProfile
from eawf.kernel.spec.release_config import (
    ObservationAdapter,
    ReleaseArtifactKind,
    ReleaseConfigError,
    ReleaseConfigRejection,
    ReleaseGateName,
    gate_names,
    load_release_config,
    parse_release_config,
)
from eawf.workflow.release.train import DEV1_RELEASE_CONFIG_YAML, V07_TRAIN


def _payload(**overrides: Any) -> dict[str, Any]:
    """Return the authored dev1 payload with top-level *overrides* applied."""
    decoded = copy.deepcopy(yaml.safe_load(DEV1_RELEASE_CONFIG_YAML))
    body: dict[str, Any] = decoded["release"]
    body.update(overrides)
    return {"release": body}


def _load(payload: dict[str, Any]) -> Any:
    """Load *payload* against the v0.7.0 train."""
    return load_release_config(payload, train=V07_TRAIN)


def _rejects(payload: dict[str, Any], code: ReleaseConfigRejection) -> ReleaseConfigError:
    """Assert *payload* is rejected with *code* and return the error."""
    with pytest.raises(ReleaseConfigError) as excinfo:
        _load(payload)
    assert excinfo.value.code is code
    return excinfo.value


# ---------------------------------------------------------------------------
# The authored dev1 configuration
# ---------------------------------------------------------------------------


def test_release_config_loader_accepts_the_authored_dev1_checkpoint() -> None:
    config = load_release_config(DEV1_RELEASE_CONFIG_YAML, train=V07_TRAIN)
    assert config.version == "0.7.0.dev1"
    assert config.channel is ReleaseChannel.DEV
    assert config.authority_epoch == 1
    assert config.source_branch == "main"
    assert config.require_signed_tag is True
    assert config.require_clean_tree is True
    assert config.require_ancestor_of_remote is True
    assert config.membership_refs == ()
    assert config.release_key == "REL-0.7.0.dev1"
    assert [target.target_id for target in config.targets] == ["pypi", "npm", "github"]
    assert config.required_target_ids == ("pypi", "npm", "github")
    assert config.gates.profile is ReleaseGateProfile.DEV1
    assert gate_names(config.gates.required) == (
        "version_consistency",
        "changelog_entry",
        "dependency_inventory",
        "artifact_reproducibility",
        "security_review",
        "epoch1_stabilization",
        "telemetry_producer",
        "front_door_journey",
    )


def test_release_config_loader_types_every_dev1_target_row() -> None:
    config = load_release_config(DEV1_RELEASE_CONFIG_YAML, train=V07_TRAIN)
    pypi, npm, github = config.targets
    assert pypi.observe_adapter is ObservationAdapter.PACKAGE_INDEX
    assert pypi.artifact_kinds == (ReleaseArtifactKind.WHEEL, ReleaseArtifactKind.SDIST)
    assert pypi.timeout_seconds == 1800
    assert pypi.retry_limit == 2
    assert npm.observe_adapter is ObservationAdapter.NPM_REGISTRY
    assert npm.prerelease_dist_tag == "next"
    assert npm.stable_dist_tag == "latest"
    assert github.observe_adapter is ObservationAdapter.SOURCE_HOST_RELEASE
    assert ReleaseArtifactKind.PLUGIN_BUNDLE in github.artifact_kinds


def test_release_config_loader_accepts_a_bare_release_block() -> None:
    body = yaml.safe_load(DEV1_RELEASE_CONFIG_YAML)["release"]
    assert parse_release_config(body).version == "0.7.0.dev1"


# ---------------------------------------------------------------------------
# One rejection per listed condition
# ---------------------------------------------------------------------------


def test_release_config_loader_rejects_duplicate_targets() -> None:
    payload = _payload()
    targets = payload["release"]["targets"]
    targets.append(copy.deepcopy(targets[0]))
    error = _rejects(payload, ReleaseConfigRejection.DUPLICATE_TARGET)
    assert "pypi" in str(error)


def test_release_config_loader_rejects_channel_version_disagreement() -> None:
    error = _rejects(
        _payload(channel="stable"), ReleaseConfigRejection.CHANNEL_VERSION_DISAGREEMENT
    )
    assert "expected 'dev'" in str(error)


def test_release_config_loader_rejects_an_undeclared_epoch() -> None:
    error = _rejects(_payload(authority_epoch=2), ReleaseConfigRejection.UNDECLARED_EPOCH)
    assert "train TRAIN-0.7.0 declares 1" in str(error)


def test_release_config_loader_rejects_an_undeclared_gate_profile() -> None:
    payload = _payload()
    payload["release"]["gates"]["profile"] = "stable"
    error = _rejects(payload, ReleaseConfigRejection.UNDECLARED_GATE_PROFILE)
    assert "train TRAIN-0.7.0 declares 'dev1'" in str(error)


def test_release_config_loader_rejects_the_default_tag_on_a_prerelease() -> None:
    payload = _payload()
    payload["release"]["targets"][1]["prerelease_dist_tag"] = "latest"
    error = _rejects(payload, ReleaseConfigRejection.PRERELEASE_ON_DEFAULT_TAG)
    assert "npm" in str(error)


def test_release_config_loader_rejects_membership_at_a_checkpoint_that_forbids_it() -> None:
    payload = _payload(membership_refs=["eawf://bundle/ACB-0001"])
    error = _rejects(payload, ReleaseConfigRejection.INVALID_MEMBERSHIP_CARDINALITY)
    assert "forbids membership_refs" in str(error)


def test_release_config_loader_rejects_absent_membership_where_required() -> None:
    payload = _payload(version="0.7.0.dev3", authority_epoch=2)
    payload["release"]["gates"]["profile"] = "native_canary"
    error = _rejects(payload, ReleaseConfigRejection.INVALID_MEMBERSHIP_CARDINALITY)
    assert "requires non-empty membership_refs" in str(error)


def test_release_config_loader_rejects_a_missing_observation_adapter() -> None:
    payload = _payload()
    del payload["release"]["targets"][0]["observe_adapter"]
    _rejects(payload, ReleaseConfigRejection.MISSING_OBSERVATION_ADAPTER)


def test_release_config_loader_rejects_an_unrecognised_observation_adapter() -> None:
    payload = _payload()
    payload["release"]["targets"][0]["observe_adapter"] = "carrier_pigeon"
    _rejects(payload, ReleaseConfigRejection.MISSING_OBSERVATION_ADAPTER)


@pytest.mark.parametrize("timeout", [0, -1, -1800])
def test_release_config_loader_rejects_nonpositive_timeouts(timeout: int) -> None:
    payload = _payload()
    payload["release"]["targets"][2]["timeout_seconds"] = timeout
    _rejects(payload, ReleaseConfigRejection.NONPOSITIVE_TIMEOUT)


def test_release_config_loader_rejects_a_checkpoint_the_train_never_declared() -> None:
    payload = _payload(version="0.7.0.dev9")
    error = _rejects(payload, ReleaseConfigRejection.UNDECLARED_CHECKPOINT)
    assert "REL-0.7.0.dev9" in str(error)


# ---------------------------------------------------------------------------
# Boundary cases
# ---------------------------------------------------------------------------


def test_release_config_loader_rejects_zero_targets() -> None:
    _rejects(_payload(targets=[]), ReleaseConfigRejection.SCHEMA_INVALID)


def test_release_config_loader_accepts_a_single_target() -> None:
    payload = _payload()
    payload["release"]["targets"] = payload["release"]["targets"][:1]
    config = _load(payload)
    assert config.required_target_ids == ("pypi",)


def test_release_config_loader_accepts_a_timeout_at_its_floor() -> None:
    payload = _payload()
    payload["release"]["targets"][0]["timeout_seconds"] = 1
    payload["release"]["targets"][0]["retry_limit"] = 0
    config = _load(payload)
    assert config.targets[0].timeout_seconds == 1
    assert config.targets[0].retry_limit == 0


def test_release_config_loader_rejects_a_negative_retry_limit() -> None:
    payload = _payload()
    payload["release"]["targets"][0]["retry_limit"] = -1
    _rejects(payload, ReleaseConfigRejection.SCHEMA_INVALID)


def test_release_config_loader_rejects_an_empty_required_gate_list() -> None:
    payload = _payload()
    payload["release"]["gates"]["required"] = []
    _rejects(payload, ReleaseConfigRejection.SCHEMA_INVALID)


def test_release_config_loader_accepts_a_single_required_gate() -> None:
    payload = _payload()
    payload["release"]["gates"]["required"] = ["version_consistency"]
    config = _load(payload)
    assert config.gates.required == (ReleaseGateName.VERSION_CONSISTENCY,)


def test_release_config_loader_rejects_an_empty_artifact_kind_list() -> None:
    payload = _payload()
    payload["release"]["targets"][0]["artifact_kinds"] = []
    _rejects(payload, ReleaseConfigRejection.SCHEMA_INVALID)


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_release_config_loader_rejects_an_unknown_gate_name() -> None:
    payload = _payload()
    payload["release"]["gates"]["required"] = ["version_consistency", "vibes_check"]
    _rejects(payload, ReleaseConfigRejection.SCHEMA_INVALID)


def test_release_config_loader_rejects_an_unknown_key() -> None:
    _rejects(_payload(publish_immediately=True), ReleaseConfigRejection.SCHEMA_INVALID)


def test_release_config_loader_rejects_malformed_yaml() -> None:
    with pytest.raises(ReleaseConfigError) as excinfo:
        load_release_config("release: [unclosed", train=V07_TRAIN)
    assert excinfo.value.code is ReleaseConfigRejection.SCHEMA_INVALID
    assert "not valid YAML" in str(excinfo.value)


@pytest.mark.parametrize("source", ["", "- a\n- b\n", "just a string\n"])
def test_release_config_loader_rejects_a_non_mapping_payload(source: str) -> None:
    with pytest.raises(ReleaseConfigError) as excinfo:
        load_release_config(source, train=V07_TRAIN)
    assert excinfo.value.code is ReleaseConfigRejection.SCHEMA_INVALID


def test_release_config_loader_rejects_a_non_mapping_release_block() -> None:
    with pytest.raises(ReleaseConfigError) as excinfo:
        load_release_config({"release": ["nope"]}, train=V07_TRAIN)
    assert excinfo.value.code is ReleaseConfigRejection.SCHEMA_INVALID
    assert "release block must be a mapping" in str(excinfo.value)


def test_release_config_loader_rejects_a_malformed_version() -> None:
    _rejects(_payload(version="0.7"), ReleaseConfigRejection.SCHEMA_INVALID)


def test_release_config_is_frozen() -> None:
    config = load_release_config(DEV1_RELEASE_CONFIG_YAML, train=V07_TRAIN)
    with pytest.raises(ValueError, match="frozen"):
        config.version = "0.7.0"  # type: ignore[misc]
