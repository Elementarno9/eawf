"""Tests for :mod:`eawf.kernel.spec.release`.

Pins the strict record contract the release train is built on:

1. Version grammar and its three derived views (channel, prerelease,
   SemVer spelling), including the malformed-input error paths.
2. A well-formed :class:`Release` validates and exposes every field.
3. The three identity rejections: a key that does not spell the
   version, a channel that disagrees with it, and membership bundles on
   an epoch-1 development checkpoint.
4. The pin invariants: every status from CANDIDATE onward requires the
   source binding and manifest digest, and every status from APPROVED
   onward requires the approval reference.
5. :class:`ReleaseTrain` ladder coherence -- id, stable target, unique
   rungs, terminal rung, index bounds and receipt keys.
6. :func:`validate_release_against_train` -- the cross-object epoch
   equality and membership-cardinality checks, plus their error paths.
7. Frozen-ness and ``extra="forbid"`` on both records.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.release import (
    Release,
    ReleaseChannel,
    ReleaseCheckpoint,
    ReleaseGateProfile,
    ReleaseInvalidation,
    ReleaseInvalidationCause,
    ReleaseStatus,
    ReleaseTrain,
    channel_for_version,
    is_prerelease,
    normalize_version,
    release_key,
    semver_equivalent,
    validate_release_against_train,
)
from eawf.workflow.release.train import V07_TRAIN

_SHA = "a" * 40
_TREE_SHA = "b" * 40
_DIGEST = f"sha256:{'c' * 64}"


def _release(**overrides: Any) -> Release:
    """Build a DRAFT ``0.7.0.dev1`` record with *overrides* applied."""
    payload: dict[str, Any] = {
        "uid": UUID(int=1),
        "key": "REL-0.7.0.dev1",
        "version": "0.7.0.dev1",
        "channel": ReleaseChannel.DEV,
        "authority_epoch": 1,
    }
    payload.update(overrides)
    return Release(**payload)


def _pinned(**overrides: Any) -> dict[str, Any]:
    """Return the four fields every post-DRAFT status requires."""
    pinned: dict[str, Any] = {
        "source_sha": _SHA,
        "source_tree_sha": _TREE_SHA,
        "manifest_ref": "artifact://release/manifest",
        "manifest_digest": _DIGEST,
    }
    pinned.update(overrides)
    return pinned


def _train(*rungs: ReleaseCheckpoint, **overrides: Any) -> ReleaseTrain:
    """Build a ``TRAIN-9.9.9`` train over *rungs* plus its stable terminus."""
    payload: dict[str, Any] = {
        "train_id": "TRAIN-9.9.9",
        "target_version": "9.9.9",
        "checkpoints": (
            *rungs,
            ReleaseCheckpoint(
                release_key="REL-9.9.9",
                authority_epoch=2,
                gate_profile=ReleaseGateProfile.STABLE,
                requires_membership=True,
            ),
        ),
    }
    payload.update(overrides)
    return ReleaseTrain(**payload)


# ---------------------------------------------------------------------------
# Version grammar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "channel"),
    [
        ("0.7.0.dev1", ReleaseChannel.DEV),
        ("0.7.0.dev10", ReleaseChannel.DEV),
        ("0.7.0rc1", ReleaseChannel.RC),
        ("0.7.0", ReleaseChannel.STABLE),
        ("10.20.30", ReleaseChannel.STABLE),
    ],
)
def test_channel_for_version_maps_every_accepted_form(
    version: str, channel: ReleaseChannel
) -> None:
    assert channel_for_version(version) is channel
    assert normalize_version(version) == version
    assert is_prerelease(version) is (channel is not ReleaseChannel.STABLE)


@pytest.mark.parametrize(
    "version",
    ["", "0.7", "0.7.0.post1", "v0.7.0", "0.7.0-dev.1", "0.7.0.dev", "0.7.0a1"],
)
def test_normalize_version_rejects_non_train_versions(version: str) -> None:
    with pytest.raises(ValueError, match="not a train version"):
        normalize_version(version)


@pytest.mark.parametrize(
    ("version", "semver"),
    [("0.7.0.dev1", "0.7.0-dev.1"), ("0.7.0rc2", "0.7.0-rc.2"), ("0.7.0", "0.7.0")],
)
def test_semver_equivalent_spells_the_npm_form(version: str, semver: str) -> None:
    assert semver_equivalent(version) == semver


def test_semver_equivalent_rejects_a_non_train_version() -> None:
    with pytest.raises(ValueError, match="not a train version"):
        semver_equivalent("0.7.0.post1")


def test_release_key_prefixes_the_normalized_version() -> None:
    assert release_key("0.7.0.dev1") == "REL-0.7.0.dev1"
    with pytest.raises(ValueError, match="not a train version"):
        release_key("nonsense")


# ---------------------------------------------------------------------------
# Release identity
# ---------------------------------------------------------------------------


def test_release_models_accepts_a_well_formed_dev1_record() -> None:
    record = _release()
    assert record.key == "REL-0.7.0.dev1"
    assert record.channel is ReleaseChannel.DEV
    assert record.authority_epoch == 1
    assert record.membership_refs == ()
    assert record.status is ReleaseStatus.DRAFT
    assert record.revision == 0
    assert record.schema_version == "release/v1"


def test_release_rejects_a_key_that_does_not_spell_the_version() -> None:
    with pytest.raises(ValidationError, match=re.escape("must be 'REL-0.7.0.dev1'")):
        _release(key="REL-0.7.0.dev2")


@pytest.mark.parametrize(
    ("version", "channel"),
    [
        ("0.7.0.dev1", ReleaseChannel.STABLE),
        ("0.7.0rc1", ReleaseChannel.DEV),
        ("0.7.0", ReleaseChannel.RC),
    ],
)
def test_release_rejects_channel_version_disagreement(
    version: str, channel: ReleaseChannel
) -> None:
    with pytest.raises(ValidationError, match="disagrees with version"):
        _release(key=release_key(version), version=version, channel=channel)


@pytest.mark.parametrize("version", ["0.7.0.dev1", "0.7.0.dev2"])
def test_release_rejects_membership_on_an_epoch1_dev_checkpoint(version: str) -> None:
    with pytest.raises(ValidationError, match="membership_refs must be empty"):
        _release(
            key=release_key(version),
            version=version,
            membership_refs=("eawf://bundle/ACB-0001",),
        )


def test_release_accepts_membership_from_dev3_onward() -> None:
    record = _release(
        key="REL-0.7.0.dev3",
        version="0.7.0.dev3",
        authority_epoch=2,
        membership_refs=("eawf://bundle/ACB-0001",),
    )
    assert record.membership_refs == ("eawf://bundle/ACB-0001",)


def test_release_rejects_superseding_itself() -> None:
    with pytest.raises(ValidationError, match="cannot supersede itself"):
        _release(supersedes_release_ref="REL-0.7.0.dev1")


def test_release_rejects_a_nonpositive_authority_epoch() -> None:
    with pytest.raises(ValidationError):
        _release(authority_epoch=0)


def test_release_is_frozen_and_forbids_extra_keys() -> None:
    record = _release()
    with pytest.raises(ValidationError):
        record.version = "0.7.0"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        _release(unexpected="x")


# ---------------------------------------------------------------------------
# Pin + approval invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        ReleaseStatus.CANDIDATE,
        ReleaseStatus.PREFLIGHT_FAILED,
        ReleaseStatus.BAKED,
    ],
)
def test_release_requires_the_pin_from_candidate_onward(status: ReleaseStatus) -> None:
    with pytest.raises(ValidationError, match="requires pinned fields"):
        _release(status=status)


def test_release_candidate_with_a_complete_pin_validates() -> None:
    record = _release(status=ReleaseStatus.CANDIDATE, **_pinned())
    assert record.manifest_digest == _DIGEST


def test_release_requires_approval_ref_from_approved_onward() -> None:
    with pytest.raises(ValidationError, match="requires approval_ref"):
        _release(status=ReleaseStatus.APPROVED, **_pinned())


def test_release_rejects_a_malformed_manifest_digest() -> None:
    with pytest.raises(ValidationError):
        _release(status=ReleaseStatus.CANDIDATE, **_pinned(manifest_digest="sha256:zz"))


def test_release_carries_a_typed_invalidation_record() -> None:
    record = _release(
        last_invalidation=ReleaseInvalidation(
            cause=ReleaseInvalidationCause.INPUTS_REPLACED,
            invalidated_at=datetime(2026, 9, 4, tzinfo=UTC),
            detail="the changelog section was rewritten after the pin",
            prior_status=ReleaseStatus.CANDIDATE,
        )
    )
    assert record.last_invalidation is not None
    assert record.last_invalidation.cause is ReleaseInvalidationCause.INPUTS_REPLACED


# ---------------------------------------------------------------------------
# ReleaseTrain ladder
# ---------------------------------------------------------------------------


def _dev_rung(version: str, *, epoch: int = 1) -> ReleaseCheckpoint:
    return ReleaseCheckpoint(
        release_key=release_key(version),
        authority_epoch=epoch,
        gate_profile=ReleaseGateProfile.DEV1,
    )


def test_release_models_accepts_a_minimal_single_rung_train() -> None:
    train = ReleaseTrain(
        train_id="TRAIN-9.9.9",
        target_version="9.9.9",
        checkpoints=(
            ReleaseCheckpoint(
                release_key="REL-9.9.9",
                authority_epoch=1,
                gate_profile=ReleaseGateProfile.STABLE,
            ),
        ),
    )
    assert train.current_checkpoint.release_key == "REL-9.9.9"
    assert train.current_checkpoint.channel is ReleaseChannel.STABLE
    assert train.current_checkpoint.version == "9.9.9"


def test_release_train_rejects_an_empty_ladder() -> None:
    with pytest.raises(ValidationError):
        ReleaseTrain(train_id="TRAIN-9.9.9", target_version="9.9.9", checkpoints=())


def test_release_train_rejects_an_id_that_does_not_spell_the_target() -> None:
    with pytest.raises(ValidationError, match=re.escape("must be TRAIN-9.9.9")):
        _train(_dev_rung("9.9.9.dev1"), train_id="TRAIN-8.8.8")


def test_release_train_rejects_a_prerelease_target() -> None:
    with pytest.raises(ValidationError, match="must be a stable version"):
        ReleaseTrain(
            train_id="TRAIN-9.9.9",
            target_version="9.9.9rc1",
            checkpoints=(_dev_rung("9.9.9.dev1"),),
        )


def test_release_train_rejects_duplicate_rungs() -> None:
    with pytest.raises(ValidationError, match="must be unique"):
        _train(_dev_rung("9.9.9.dev1"), _dev_rung("9.9.9.dev1"))


def test_release_train_rejects_a_ladder_that_does_not_end_at_stable() -> None:
    with pytest.raises(ValidationError, match="ladder must end at"):
        ReleaseTrain(
            train_id="TRAIN-9.9.9",
            target_version="9.9.9",
            checkpoints=(_dev_rung("9.9.9.dev1"),),
        )


def test_release_train_rejects_an_index_off_the_end() -> None:
    with pytest.raises(ValidationError, match="is off a ladder"):
        _train(_dev_rung("9.9.9.dev1"), current_checkpoint_index=2)


def test_release_train_accepts_the_last_index() -> None:
    train = _train(_dev_rung("9.9.9.dev1"), current_checkpoint_index=1)
    assert train.current_checkpoint.release_key == "REL-9.9.9"


def test_release_train_rejects_receipts_for_an_undeclared_rung() -> None:
    with pytest.raises(ValidationError, match="undeclared checkpoints"):
        _train(
            _dev_rung("9.9.9.dev1"),
            gate_receipt_refs={"REL-9.9.9.dev7": ("receipt://x",)},
        )


def test_release_train_checkpoint_lookup_raises_on_an_unknown_key() -> None:
    train = _train(_dev_rung("9.9.9.dev1"))
    assert train.checkpoint_for("REL-9.9.9.dev1").authority_epoch == 1
    with pytest.raises(KeyError, match="declares no checkpoint"):
        train.checkpoint_for("REL-9.9.9.dev4")
    with pytest.raises(ValueError, match="not a train version"):
        train.checkpoint_for_version("nope")


def test_v07_train_declares_the_seven_planned_checkpoints() -> None:
    assert V07_TRAIN.train_id == "TRAIN-0.7.0"
    assert len(V07_TRAIN.checkpoints) == 7
    assert V07_TRAIN.current_checkpoint.release_key == "REL-0.7.0.dev1"
    dev1 = V07_TRAIN.checkpoint_for_version("0.7.0.dev1")
    assert dev1.authority_epoch == 1
    assert dev1.gate_profile is ReleaseGateProfile.DEV1
    assert dev1.requires_membership is False
    assert V07_TRAIN.checkpoints[-1].gate_profile is ReleaseGateProfile.STABLE


# ---------------------------------------------------------------------------
# Cross-object: release against its train rung
# ---------------------------------------------------------------------------


def test_validate_release_against_train_returns_the_declared_rung() -> None:
    rung = validate_release_against_train(_release(), V07_TRAIN)
    assert rung.release_key == "REL-0.7.0.dev1"
    assert rung.authority_epoch == 1


def test_validate_release_against_train_rejects_an_epoch_disagreement() -> None:
    record = _release(authority_epoch=2)
    with pytest.raises(ValueError, match=re.escape("but train TRAIN-0.7.0 declares 1")):
        validate_release_against_train(record, V07_TRAIN)


def test_validate_release_against_train_rejects_an_undeclared_checkpoint() -> None:
    record = _release(key="REL-9.9.9", version="9.9.9", channel=ReleaseChannel.STABLE)
    with pytest.raises(KeyError, match="declares no checkpoint"):
        validate_release_against_train(record, V07_TRAIN)


def test_validate_release_against_train_requires_membership_from_dev3() -> None:
    record = _release(key="REL-0.7.0.dev3", version="0.7.0.dev3", authority_epoch=2)
    with pytest.raises(ValueError, match="requires non-empty membership_refs"):
        validate_release_against_train(record, V07_TRAIN)
