"""The stable candidate freezes its platform claims beside its artifacts.

The record pins one digest, so a platform claim the digest does not
cover could be dropped or re-pointed after the pin and nothing would
notice. These tests freeze the 0.7.0 stable configuration, pin it, and
show both halves of the pinned manifest -- artifacts and platforms --
move the digest; and that the prerelease rungs already pinned under the
first schema keep reproducing their digests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from pydantic import ValidationError

from eawf.kernel.release import PlatformClaimSet
from eawf.kernel.spec.release import Release, ReleaseChannel, ReleaseStatus, release_key
from eawf.kernel.spec.release_config import (
    ReleaseConfig,
    ReleasePlatformClaim,
    load_release_config,
)
from eawf.workflow.release.candidate import freeze_manifest, pin_candidate
from eawf.workflow.release.observation import FrozenManifest
from eawf.workflow.release.train import (
    STABLE_RELEASE_CONFIG_YAML,
    V07_TARGET_VERSION,
    V07_TRAIN,
    checkpoint_config_yaml,
)

STABLE = V07_TARGET_VERSION
MEMBERSHIP_REF = "milestone://epoch2/stable"
SOURCE_SHA = "1" * 40
SOURCE_TREE_SHA = "2" * 40

#: Filenames each stable leg reports, keyed by target id.
REPORTED: dict[str, tuple[str, ...]] = {
    "pypi": (f"eawf-{STABLE}-py3-none-any.whl", f"eawf-{STABLE}.tar.gz"),
    "npm": (f"elementarno-eawf-{STABLE}.tgz",),
    "github": ("RELEASE_NOTES.md", "SHA256SUMS", f"eawf-plugin-{STABLE}.tar.gz"),
    "plugins-dist": ("refs/heads/plugins-dist",),
}

#: Digest of :func:`v1_payload` as the first schema computed it before
#: platform claims existed; a v1 manifest must keep this byte for byte.
V1_DIGEST_HEX = (
    "ccb41d983a188021a14e33b19deac621f24790ded3bdcb3d7e4e71481dd3a606"  # pragma: allowlist secret
)
V1_DIGEST = f"sha256:{V1_DIGEST_HEX}"


def train_config(source: str) -> ReleaseConfig:
    """Return the checkpoint configuration *source*, loaded against the train.

    The rungs from ``dev3`` on require membership bundles the record
    supplies at cut time, so one is added here.
    """
    body = yaml.safe_load(source)
    body["release"]["membership_refs"] = [MEMBERSHIP_REF]
    return load_release_config(body, train=V07_TRAIN)


def stable_config() -> ReleaseConfig:
    """Return the 0.7.0 stable checkpoint configuration."""
    return train_config(STABLE_RELEASE_CONFIG_YAML)


def stable_draft() -> Release:
    """Return the stable DRAFT record a pin starts from."""
    return Release(
        uid=UUID(int=316),
        key=release_key(STABLE),
        version=STABLE,
        channel=ReleaseChannel.STABLE,
        authority_epoch=2,
        membership_refs=(MEMBERSHIP_REF,),
        status=ReleaseStatus.DRAFT,
    )


def write_receipts(directory: Path, *, version: str = STABLE, fill: str = "a") -> Path:
    """Write one publication receipt per leg into *directory*.

    Args:
        directory: Where the receipt files land.
        version: Version every receipt reports and every filename spells.
        fill: Hex digit every artifact digest repeats, so a caller can
            change the artifacts without changing their names.

    Returns:
        *directory*, created if needed.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for target_id, names in REPORTED.items():
        payload = {
            "target_id": target_id,
            "version": version,
            "job_conclusion": "success",
            "run_id": "1",
            "artifact_digests": dict.fromkeys(
                (name.replace(STABLE, version) for name in names), f"sha256:{fill * 64}"
            ),
        }
        (directory / f"publication-receipt-{target_id}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
    return directory


def with_claims(config: ReleaseConfig, claims: tuple[ReleasePlatformClaim, ...]) -> ReleaseConfig:
    """Return *config* advertising *claims* instead of its own."""
    return config.model_copy(update={"platform_claims": claims})


def v1_payload() -> dict[str, Any]:
    """Return a one-target manifest payload under the first schema."""
    return {
        "schema_version": "frozen-manifest/v1",
        "release_key": "REL-0.7.0",
        "version": "0.7.0",
        "targets": {
            "pypi": {
                "identity": "eawf",
                "artifacts": [
                    {
                        "kind": "wheel",
                        "filename": "eawf-0.7.0-py3-none-any.whl",
                        "digest": f"sha256:{'a' * 64}",
                    }
                ],
            }
        },
    }


def test_stable_freeze_carries_the_configured_platform_claims(tmp_path: Path) -> None:
    """The stable manifest is v2 and holds exactly the configured claims."""
    config = stable_config()

    manifest = freeze_manifest(config, receipts_dir=write_receipts(tmp_path / "r"))

    assert manifest.schema_version == "frozen-manifest/v2"
    assert manifest.platform_claims.claims == config.platform_claims
    assert {claim.platform_id for claim in manifest.platform_claims.claims} == {
        "linux-x86_64",
        "windows-x86_64",
    }


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda claims: claims[:1], id="claim-dropped"),
        pytest.param(lambda claims: (), id="all-claims-dropped"),
        pytest.param(
            lambda claims: (
                claims[0],
                claims[1].model_copy(update={"receipt_ref": "ci://eawf/elsewhere"}),
            ),
            id="receipt-repointed",
        ),
        pytest.param(
            lambda claims: (claims[0], claims[1].model_copy(update={"real_host": False})),
            id="real-host-withdrawn",
        ),
    ],
)
def test_stable_candidate_pin_detects_a_platform_claim_change(tmp_path: Path, mutate: Any) -> None:
    """Changing the platform claims after the pin no longer matches the pinned digest."""
    config = stable_config()
    receipts = write_receipts(tmp_path / "r")
    pinned = pin_candidate(
        stable_draft(),
        config,
        freeze_manifest(config, receipts_dir=receipts),
        source_sha=SOURCE_SHA,
        source_tree_sha=SOURCE_TREE_SHA,
        manifest_ref="artifact://release/REL-0.7.0/frozen-manifest.json",
    )
    changed = with_claims(config, mutate(config.platform_claims))

    refrozen = freeze_manifest(changed, receipts_dir=receipts)

    assert pinned.status is ReleaseStatus.CANDIDATE
    assert refrozen.digest != pinned.manifest_digest


def test_stable_candidate_pin_detects_an_artifact_change(tmp_path: Path) -> None:
    """The artifact half stays pinned beside the platform half."""
    config = stable_config()
    pinned = pin_candidate(
        stable_draft(),
        config,
        freeze_manifest(config, receipts_dir=write_receipts(tmp_path / "a")),
        source_sha=SOURCE_SHA,
        source_tree_sha=SOURCE_TREE_SHA,
        manifest_ref="artifact://release/REL-0.7.0/frozen-manifest.json",
    )

    refrozen = freeze_manifest(config, receipts_dir=write_receipts(tmp_path / "b", fill="b"))

    assert refrozen.digest != pinned.manifest_digest


def test_stable_refreeze_of_unchanged_inputs_reproduces_the_pin(tmp_path: Path) -> None:
    """Same receipts and same claims give the same digest, so the check is not noise."""
    config = stable_config()
    receipts = write_receipts(tmp_path / "r")

    first = freeze_manifest(config, receipts_dir=receipts)
    second = freeze_manifest(config, receipts_dir=receipts)

    assert first.digest == second.digest


def test_a_pinned_prerelease_rung_still_freezes_under_v1(tmp_path: Path) -> None:
    """Rungs pinned before platform claims were frozen keep reproducing their pins."""
    config = train_config(checkpoint_config_yaml("0.7.0rc4"))

    manifest = freeze_manifest(
        config, receipts_dir=write_receipts(tmp_path / "rc4", version="0.7.0rc4")
    )

    assert config.platform_claims
    assert manifest.schema_version == "frozen-manifest/v1"
    assert manifest.platform_claims.claims == ()


def test_v1_digest_is_unchanged() -> None:
    """A v1 manifest digests exactly as it did before v2 existed."""
    assert FrozenManifest.model_validate(v1_payload()).digest == V1_DIGEST


def test_v1_manifest_refuses_platform_claims() -> None:
    """A v1 digest cannot cover claims, so a v1 row carrying them is refused."""
    payload = v1_payload()
    payload["platform_claims"] = {
        "claims": [{"platform_id": "linux-x86_64", "receipt_ref": "ci://x", "real_host": True}]
    }

    with pytest.raises(ValidationError, match="frozen-manifest/v1"):
        FrozenManifest.model_validate(payload)


def test_v2_round_trips_through_json(tmp_path: Path) -> None:
    """A stored v2 manifest reloads to the same content and digest."""
    manifest = freeze_manifest(stable_config(), receipts_dir=write_receipts(tmp_path / "r"))

    reloaded = FrozenManifest.model_validate(json.loads(manifest.model_dump_json()))

    assert reloaded == manifest
    assert reloaded.digest == manifest.digest


def test_v2_with_an_empty_claim_set_still_digests_its_claims() -> None:
    """Advertising no platform is an answer the digest pins, not a v1 alias."""
    payload = v1_payload() | {"schema_version": "frozen-manifest/v2"}

    empty = FrozenManifest.model_validate(payload)
    claimed = FrozenManifest.model_validate(
        payload
        | {
            "platform_claims": {
                "claims": [
                    {"platform_id": "linux-x86_64", "receipt_ref": "ci://x", "real_host": True}
                ]
            }
        }
    )

    assert empty.platform_claims == PlatformClaimSet()
    assert empty.digest != V1_DIGEST
    assert empty.digest != claimed.digest


def test_v2_refuses_a_platform_claimed_twice() -> None:
    """One platform with two receipts would leave its proof ambiguous."""
    row = {"platform_id": "linux-x86_64", "receipt_ref": "ci://x", "real_host": True}
    payload = v1_payload() | {
        "schema_version": "frozen-manifest/v2",
        "platform_claims": {"claims": [row, row | {"receipt_ref": "ci://y"}]},
    }

    with pytest.raises(ValidationError, match="more than once"):
        FrozenManifest.model_validate(payload)


def test_an_unknown_schema_version_is_refused() -> None:
    """Only the two declared schemas load."""
    with pytest.raises(ValidationError, match="schema_version"):
        FrozenManifest.model_validate(v1_payload() | {"schema_version": "frozen-manifest/v3"})
