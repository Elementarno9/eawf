"""The v0.7.0 release train and its ``dev1`` checkpoint configuration.

This module is *data*, not schema. The seven checkpoints below are this
project's plan for one target version; they bind no other release, no
other project and no other repository. A different train is a different
:class:`~eawf.kernel.spec.release.ReleaseTrain` value built the same
way, which is why the ladder is a field on the record rather than a
constant in the model.

:data:`DEV1_RELEASE_CONFIG_YAML` is the authored checkpoint
configuration for the first rung, kept beside the ladder it belongs to
so the two cannot drift: the loader validates the configuration against
:data:`V07_TRAIN`, and the pair is exercised as one unit.
"""

from __future__ import annotations

import logging
from typing import Final

from eawf.kernel.spec.release import (
    ReleaseCheckpoint,
    ReleaseGateProfile,
    ReleaseTrain,
    release_key,
)

logger = logging.getLogger(__name__)

#: Stable version the v0.7.0 train walks toward.
V07_TARGET_VERSION: Final[str] = "0.7.0"

#: The seven rungs of the v0.7.0 train, in order. Epochs and profiles
#: come from the train table: ``dev1``/``dev2`` stabilize and migrate on
#: epoch-1 authority, ``dev3``/``dev4`` run epoch 2 in canary
#: repositories, and the two release candidates plus stable run epoch 2
#: only. Membership bundles start at ``dev3`` because an epoch-2
#: Milestone acceptance bundle cannot exist before then.
V07_CHECKPOINTS: Final[tuple[ReleaseCheckpoint, ...]] = (
    ReleaseCheckpoint(
        release_key=release_key("0.7.0.dev1"),
        authority_epoch=1,
        gate_profile=ReleaseGateProfile.DEV1,
        requires_membership=False,
    ),
    ReleaseCheckpoint(
        release_key=release_key("0.7.0.dev2"),
        authority_epoch=1,
        gate_profile=ReleaseGateProfile.DEV2,
        requires_membership=False,
    ),
    ReleaseCheckpoint(
        release_key=release_key("0.7.0.dev3"),
        authority_epoch=2,
        gate_profile=ReleaseGateProfile.NATIVE_CANARY,
        requires_membership=True,
    ),
    ReleaseCheckpoint(
        release_key=release_key("0.7.0.dev4"),
        authority_epoch=2,
        gate_profile=ReleaseGateProfile.PRODUCT_CANARY,
        requires_membership=True,
    ),
    ReleaseCheckpoint(
        release_key=release_key("0.7.0rc1"),
        authority_epoch=2,
        gate_profile=ReleaseGateProfile.FLAG_DAY,
        requires_membership=True,
    ),
    ReleaseCheckpoint(
        release_key=release_key("0.7.0rc2"),
        authority_epoch=2,
        gate_profile=ReleaseGateProfile.CROSS_PROVIDER,
        requires_membership=True,
    ),
    ReleaseCheckpoint(
        release_key=release_key(V07_TARGET_VERSION),
        authority_epoch=2,
        gate_profile=ReleaseGateProfile.STABLE,
        requires_membership=True,
    ),
)

#: The v0.7.0 train, opened at its first rung.
V07_TRAIN: Final[ReleaseTrain] = ReleaseTrain(
    train_id=f"TRAIN-{V07_TARGET_VERSION}",
    target_version=V07_TARGET_VERSION,
    checkpoints=V07_CHECKPOINTS,
    current_checkpoint_index=0,
)

#: The authored ``0.7.0.dev1`` checkpoint configuration. ``membership_refs``
#: is omitted rather than written empty because the checkpoint forbids it
#: outright, and the three publication targets each declare the adapter
#: their observation is read back through.
DEV1_RELEASE_CONFIG_YAML: Final[str] = """\
release:
  version: 0.7.0.dev1
  channel: dev
  authority_epoch: 1
  source_branch: main
  require_signed_tag: true
  require_clean_tree: true
  require_ancestor_of_remote: true
  targets:
    - target_id: pypi
      required: true
      artifact_kinds: [wheel, sdist]
      observe_adapter: package_index
      timeout_seconds: 1800
      retry_limit: 2
    - target_id: npm
      required: true
      artifact_kinds: [codex_plugin]
      observe_adapter: npm_registry
      prerelease_dist_tag: next
      stable_dist_tag: latest
      timeout_seconds: 1800
      retry_limit: 2
    - target_id: github
      required: true
      artifact_kinds: [release_notes, checksums, plugin_bundle]
      observe_adapter: source_host_release
      timeout_seconds: 1800
      retry_limit: 2
  gates:
    profile: dev1
    required:
      - version_consistency
      - changelog_entry
      - dependency_inventory
      - artifact_reproducibility
      - security_review
      - epoch1_stabilization
      - telemetry_producer
      - front_door_journey
"""

#: Authored checkpoint configurations by version. Later rungs are
#: generated independently from the train template rather than by
#: overlaying version/channel/epoch onto this one, so each lands with
#: the wave that builds it.
CHECKPOINT_CONFIGS: Final[dict[str, str]] = {
    "0.7.0.dev1": DEV1_RELEASE_CONFIG_YAML,
}


def checkpoint_config_yaml(version: str) -> str:
    """Return the authored configuration text for *version*.

    Args:
        version: Normalized checkpoint version, e.g. ``0.7.0.dev1``.

    Returns:
        The YAML text of that checkpoint's configuration.

    Raises:
        KeyError: When no configuration has been authored for *version*
            yet -- the later rungs land with the waves that build them.
    """
    try:
        return CHECKPOINT_CONFIGS[version]
    except KeyError as exc:
        authored = sorted(CHECKPOINT_CONFIGS)
        raise KeyError(
            f"no release configuration authored for {version!r}; have {authored}"
        ) from exc


__all__ = [
    "CHECKPOINT_CONFIGS",
    "DEV1_RELEASE_CONFIG_YAML",
    "V07_CHECKPOINTS",
    "V07_TARGET_VERSION",
    "V07_TRAIN",
    "checkpoint_config_yaml",
]
