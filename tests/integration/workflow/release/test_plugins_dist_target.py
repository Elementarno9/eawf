"""``plugins-dist`` joins the declared targets, from ``dev3`` onward.

The Codex tree has been pushed to the publication branch since the first
rung, and no checkpoint ever declared it. ``dev1`` is the record of what
that costs: it reached four targets with a release record open on three,
so the branch could hold a wrong render -- and did -- while the
checkpoint's declared legs all reported home. A target nobody declares
is a target nobody reads back, and a leg nobody reads back cannot stop a
bake.

Declaring it fixes that forward and must not touch anything behind.
``dev1`` and ``dev2`` are published: each baked on the artifact set its
approved manifest froze, and that manifest was frozen from the targets
its configuration declared. A fourth row on those rungs would re-digest
a manifest an approval already bound, which is why the template defers
the row to ``native_canary`` rather than declaring it train-wide.

So the tests come in two halves that pull against each other on purpose.
One half is the new target: present, required, observed through
``git_ref``, and present on every profile after the one it joins at. The
other half is the published past held still: the ``dev2`` document byte
for byte, the three identities the ``dev2`` manifest froze, and that
manifest's digest -- the one ``REL-0.7.0.dev2`` pins and
:func:`assert_manifest_binds` checks -- recomputing unchanged from the
committed evidence.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from eawf.kernel.release.checkpoint_template import render_checkpoint_config
from eawf.kernel.spec.release import ReleaseGateProfile
from eawf.kernel.spec.release_config import (
    ObservationAdapter,
    ReleaseArtifactKind,
    parse_release_config,
)
from eawf.runtime.daemon.methods.release_context import resolve_config
from eawf.workflow.release.observation import FrozenManifest, assert_manifest_binds
from eawf.workflow.release.registry_readers import PLUGINS_DIST_REF
from eawf.workflow.release.train import (
    DEV1_RELEASE_CONFIG_YAML,
    DEV2_RELEASE_CONFIG_YAML,
    DEV3_RELEASE_CONFIG_YAML,
    V07_CONFIG_TEMPLATE,
    V07_TRAIN,
)
from tests._release_helpers import release_record

pytestmark = pytest.mark.integration

#: The rung the target joins at, and the bundle a ``dev3`` record names.
DEV3_VERSION = "0.7.0.dev3"
MEMBERSHIP_REF = "milestone://epoch2/native-canary"

#: The new leg, and the three that were always declared.
PLUGINS_DIST = "plugins-dist"
ESTABLISHED_TARGETS = ("pypi", "npm", "github")

#: sha256 of the rendered ``dev2`` configuration text. The same pin
#: ``test_dev3_config_render`` carries, repeated here because this is the
#: change that could move it: the deferral is the whole reason the
#: fourth target does not reach an already-published rung.
DEV2_CONFIG_DIGEST = (
    "5e3d6dd2f93d7510e57624c6b1280dab43b02cdaca745d45a8ff1e0523651a38"  # pragma: allowlist secret
)

#: The manifest digest ``REL-0.7.0.dev2`` approved and baked on. It is
#: recomputed from the manifest's content, so any change to the targets
#: that rung declares would move it.
DEV2_APPROVED_MANIFEST_DIGEST = "sha256:" + (
    "3ea0e1250281a90d1840583cbd92c2751fc1c99b9d9f1824e69f740ea9c9e148"  # pragma: allowlist secret
)

#: The manifest the ``dev2`` publication froze, as committed.
DEV2_MANIFEST_PATH = (
    Path(__file__).resolve().parents[4]
    / ".ea"
    / "artifacts"
    / "evidence"
    / "2026-09-16-dev2-publication"
    / "frozen-manifest.json"
)

#: Every profile at or after the one the target joins at.
CARRYING_PROFILES = (
    ReleaseGateProfile.NATIVE_CANARY,
    ReleaseGateProfile.PRODUCT_CANARY,
    ReleaseGateProfile.FLAG_DAY,
    ReleaseGateProfile.CROSS_PROVIDER,
    ReleaseGateProfile.STABLE,
)

#: Every profile before it.
PUBLISHED_PROFILES = (ReleaseGateProfile.DEV1, ReleaseGateProfile.DEV2)


def dev2_manifest() -> FrozenManifest:
    """Return the manifest the ``dev2`` approval bound, as committed."""
    return FrozenManifest.model_validate(json.loads(DEV2_MANIFEST_PATH.read_text(encoding="utf-8")))


# --- the target dev3 declares --------------------------------------------


def test_dev3_declares_the_plugins_dist_target() -> None:
    config = parse_release_config(DEV3_RELEASE_CONFIG_YAML)
    assert [target.target_id for target in config.targets] == [
        *ESTABLISHED_TARGETS,
        PLUGINS_DIST,
    ]
    assert config.required_target_ids == (*ESTABLISHED_TARGETS, PLUGINS_DIST)


def test_dev3_observes_the_plugins_dist_target_through_the_git_ref_adapter() -> None:
    config = parse_release_config(DEV3_RELEASE_CONFIG_YAML)
    target = next(row for row in config.targets if row.target_id == PLUGINS_DIST)
    assert target.observe_adapter is ObservationAdapter.GIT_REF
    assert target.artifact_kinds == (ReleaseArtifactKind.CODEX_PLUGIN,)
    assert target.identity == "Elementarno9/eawf"
    assert target.required is True


def test_dev3_plugins_dist_target_names_no_distribution_tag() -> None:
    """A branch has no channel to route a prerelease through."""
    config = parse_release_config(DEV3_RELEASE_CONFIG_YAML)
    target = next(row for row in config.targets if row.target_id == PLUGINS_DIST)
    assert target.prerelease_dist_tag is None
    assert target.stable_dist_tag is None
    assert PLUGINS_DIST_REF == "refs/heads/plugins-dist"


def test_resolve_config_carries_the_fourth_target_to_the_daemon() -> None:
    config = resolve_config(DEV3_VERSION, membership_refs=(MEMBERSHIP_REF,))
    assert config.required_target_ids == (*ESTABLISHED_TARGETS, PLUGINS_DIST)
    assert config.gates.profile is ReleaseGateProfile.NATIVE_CANARY


@pytest.mark.parametrize("profile", CARRYING_PROFILES)
def test_every_profile_from_native_canary_onward_carries_the_target(
    profile: ReleaseGateProfile,
) -> None:
    rows = V07_CONFIG_TEMPLATE.targets_for(profile)
    assert [row["target_id"] for row in rows] == [*ESTABLISHED_TARGETS, PLUGINS_DIST]


# --- the published rungs, held still --------------------------------------


@pytest.mark.parametrize("profile", PUBLISHED_PROFILES)
def test_no_published_profile_gains_the_target(profile: ReleaseGateProfile) -> None:
    rows = V07_CONFIG_TEMPLATE.targets_for(profile)
    assert [row["target_id"] for row in rows] == list(ESTABLISHED_TARGETS)


def test_the_dev2_render_is_unchanged_byte_for_byte() -> None:
    digest = hashlib.sha256(DEV2_RELEASE_CONFIG_YAML.encode("utf-8")).hexdigest()
    assert digest == DEV2_CONFIG_DIGEST


def test_the_dev2_render_still_comes_out_of_the_shared_template() -> None:
    """The deferral is in the template, not a second dev2 document."""
    rendered = render_checkpoint_config(
        rung=V07_TRAIN.checkpoint_for_version("0.7.0.dev2"),
        template=V07_CONFIG_TEMPLATE,
    )
    assert rendered == DEV2_RELEASE_CONFIG_YAML


@pytest.mark.parametrize("config_text", [DEV1_RELEASE_CONFIG_YAML, DEV2_RELEASE_CONFIG_YAML])
def test_a_published_rung_declares_only_its_three_targets(config_text: str) -> None:
    config = parse_release_config(config_text)
    assert [target.target_id for target in config.targets] == list(ESTABLISHED_TARGETS)


# --- the approved manifest that must keep binding -------------------------


def test_the_dev2_approved_manifest_still_recomputes_its_digest() -> None:
    assert dev2_manifest().digest == DEV2_APPROVED_MANIFEST_DIGEST


def test_the_dev2_approved_manifest_freezes_exactly_its_declared_targets() -> None:
    config = parse_release_config(DEV2_RELEASE_CONFIG_YAML)
    configured = {target.target_id for target in config.targets}
    assert set(dev2_manifest().targets) == configured == set(ESTABLISHED_TARGETS)


def test_the_dev2_approved_manifest_still_binds_its_record() -> None:
    record = release_record(
        key="REL-0.7.0.dev2",
        version="0.7.0.dev2",
        manifest_digest=DEV2_APPROVED_MANIFEST_DIGEST,
    )
    assert_manifest_binds(record, dev2_manifest())


def test_the_dev2_manifest_cannot_bind_a_record_pinning_another_digest() -> None:
    record = release_record(
        key="REL-0.7.0.dev2",
        version="0.7.0.dev2",
        manifest_digest=f"sha256:{'0' * 64}",
    )
    with pytest.raises(ValueError, match="is not the digest release"):
        assert_manifest_binds(record, dev2_manifest())
