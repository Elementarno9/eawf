"""The release manifest leaves and the cross-object rules that bind them.

A manifest is only evidence about a checkpoint if every part of it
agrees with every other part. These tests pin the four disagreements
that would otherwise load cleanly and be discovered against a registry:
a target claiming an artifact nobody built, a stored leaf digest that no
longer describes its leaf, a rung whose preflight left no receipt, and a
second approval bound to one proof. The epoch-1 membership rule and the
committed ``0.7.0.dev1`` record are pinned alongside them, so adopting
the leaves cannot quietly move the checkpoint they describe.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from eawf.kernel.release import (
    PlatformClaimSet,
    ReleaseApprovalReceipt,
    ReleaseArtifactEntry,
    ReleaseArtifactInventory,
    ReleaseManifest,
    ReleaseNotes,
)
from eawf.kernel.spec.release import (
    Release,
    ReleaseChannel,
    ReleaseStatus,
    ReleaseTrain,
)
from eawf.kernel.spec.release_config import ReleasePlatformClaim
from eawf.workflow.release.train import V07_TRAIN
from tests._release_helpers import release_record

pytestmark = pytest.mark.unit

VERSION = "0.7.0.dev1"
KEY = f"REL-{VERSION}"
APPROVED_AT = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
PROOF_DIGEST = f"sha256:{'e' * 64}"
RECEIPT_REF = "receipt://ci/dev1/preflight"


def _inventory(**overrides: Any) -> ReleaseArtifactInventory:
    """Return the dev1 artifact inventory with *overrides* applied."""
    payload: dict[str, Any] = {
        "release_key": KEY,
        "version": VERSION,
        "entries": (
            ReleaseArtifactEntry(
                artifact_id="ART-wheel",
                kind="wheel",
                filename=f"eawf-{VERSION}-py3-none-any.whl",
                digest=f"sha256:{'1' * 64}",
                size_bytes=1024,
            ),
            ReleaseArtifactEntry(
                artifact_id="ART-sdist",
                kind="sdist",
                filename=f"eawf-{VERSION}.tar.gz",
                digest=f"sha256:{'2' * 64}",
                size_bytes=2048,
            ),
        ),
    }
    payload.update(overrides)
    return ReleaseArtifactInventory(**payload)


def _notes(**overrides: Any) -> ReleaseNotes:
    """Return the dev1 release notes with *overrides* applied."""
    payload: dict[str, Any] = {
        "release_key": KEY,
        "version": VERSION,
        "title": f"eawf {VERSION}",
        "body": "## Summary\n\nThe first stabilization checkpoint of the v0.7.0 train.\n",
    }
    payload.update(overrides)
    return ReleaseNotes(**payload)


def _approval(**overrides: Any) -> ReleaseApprovalReceipt:
    """Return one dev1 approval receipt with *overrides* applied."""
    payload: dict[str, Any] = {
        "receipt_id": UUID(int=41),
        "release_key": KEY,
        "proof_digest": PROOF_DIGEST,
        "approver": "OP-0001",
        "approved_at": APPROVED_AT,
        "policy_revision": 1,
    }
    payload.update(overrides)
    return ReleaseApprovalReceipt(**payload)


def _manifest(**overrides: Any) -> ReleaseManifest:
    """Return the dev1 manifest with *overrides* applied."""
    inventory = overrides.pop("inventory", None) or _inventory()
    notes = overrides.pop("notes", None) or _notes()
    payload: dict[str, Any] = {
        "release_key": KEY,
        "version": VERSION,
        "inventory": inventory,
        "inventory_digest": inventory.digest,
        "notes": notes,
        "notes_digest": notes.digest,
        "platform_claims": PlatformClaimSet(
            claims=(
                ReleasePlatformClaim(
                    platform_id="linux-x86_64",
                    receipt_ref=RECEIPT_REF,
                    real_host=True,
                ),
            )
        ),
        "target_claims": {"pypi": ("ART-wheel", "ART-sdist")},
    }
    payload.update(overrides)
    return ReleaseManifest(**payload)


def _train_with_receipts() -> ReleaseTrain:
    """Return the v0.7.0 train with a dev1 gate receipt recorded."""
    return ReleaseTrain(
        train_id=V07_TRAIN.train_id,
        target_version=V07_TRAIN.target_version,
        checkpoints=V07_TRAIN.checkpoints,
        gate_receipt_refs={KEY: (RECEIPT_REF,)},
    )


# ---- cross-object resolution -------------------------------------------------


def test_release_manifest_rejects_an_unresolved_artifact_id() -> None:
    """A target cannot publish an artifact the inventory never recorded."""
    with pytest.raises(ValidationError, match="ART-plugin"):
        _manifest(target_claims={"pypi": ("ART-wheel", "ART-plugin")})


def test_release_manifest_rejects_a_target_claiming_nothing() -> None:
    """A target with an empty claim publishes nothing and says so vacuously."""
    with pytest.raises(ValidationError, match="claims no artifact"):
        _manifest(target_claims={"pypi": ()})


def test_release_manifest_rejects_a_repeated_artifact_claim() -> None:
    """One artifact claimed twice by one target is an authoring slip."""
    with pytest.raises(ValidationError, match="same artifact twice"):
        _manifest(target_claims={"pypi": ("ART-wheel", "ART-wheel")})


def test_release_manifest_rejects_an_empty_target_claim_map() -> None:
    """A manifest that claims no target publishes nothing at all."""
    with pytest.raises(ValidationError, match="target_claims"):
        _manifest(target_claims={})


def test_release_manifest_artifacts_for_returns_the_claimed_entries() -> None:
    """The claim resolves to inventory entries in the order it lists them."""
    manifest = _manifest()
    assert [entry.artifact_id for entry in manifest.artifacts_for("pypi")] == [
        "ART-wheel",
        "ART-sdist",
    ]


def test_release_manifest_artifacts_for_raises_on_an_unclaimed_target() -> None:
    """Asking about a target the manifest never claimed is a KeyError."""
    with pytest.raises(KeyError, match="npm"):
        _manifest().artifacts_for("npm")


# ---- leaf digest binding -----------------------------------------------------


def test_release_manifest_rejects_an_inventory_digest_mismatch() -> None:
    """A stored digest that no longer describes its leaf approves nothing."""
    with pytest.raises(ValidationError, match="inventory_digest"):
        _manifest(inventory_digest=f"sha256:{'9' * 64}")


def test_release_manifest_rejects_a_notes_digest_mismatch() -> None:
    """The notes digest is checked against the notes beside it."""
    with pytest.raises(ValidationError, match="notes_digest"):
        _manifest(notes_digest=f"sha256:{'9' * 64}")


def test_release_manifest_digest_ignores_the_approvals() -> None:
    """An approval binds the digest, so it cannot be folded back into it."""
    unapproved = _manifest()
    approved = _manifest(approvals=(_approval(),))
    assert approved.digest == unapproved.digest


def test_release_manifest_digest_follows_a_leaf_change() -> None:
    """Rewriting a leaf changes the digest an approval would bind."""
    other_notes = _notes(title="eawf 0.7.0.dev1 (respin)")
    assert _manifest(notes=other_notes).digest != _manifest().digest


def test_release_manifest_rejects_a_leaf_of_another_release() -> None:
    """Every leaf describes the checkpoint the manifest names, or none does."""
    with pytest.raises(ValidationError, match="another release"):
        _manifest(notes=_notes(release_key="REL-0.7.0.dev2", version="0.7.0.dev2"))


def test_release_manifest_rejects_a_key_that_misspells_its_version() -> None:
    """The key and the version name one checkpoint."""
    with pytest.raises(ValidationError, match=re.escape("must be 'REL-0.7.0.dev1'")):
        _manifest(release_key="REL-0.7.0.dev2")


# ---- approvals ---------------------------------------------------------------


def test_release_manifest_accepts_one_approval_per_proof() -> None:
    """One proof approved once is the shape an approval ledger takes."""
    manifest = _manifest(
        approvals=(
            _approval(),
            _approval(receipt_id=UUID(int=42), proof_digest=f"sha256:{'f' * 64}"),
        )
    )
    assert len(manifest.approvals) == 2


def test_release_manifest_rejects_a_second_approval_on_one_proof_digest() -> None:
    """A proof is approved once; a second receipt hides which one governs."""
    with pytest.raises(ValidationError, match="second approval receipt"):
        _manifest(approvals=(_approval(), _approval(receipt_id=UUID(int=42))))


def test_release_manifest_rejects_a_repeated_approval_receipt_id() -> None:
    """Two receipts sharing an id are one receipt with two contents."""
    with pytest.raises(ValidationError, match="repeats an approval receipt id"):
        _manifest(
            approvals=(_approval(), _approval(proof_digest=f"sha256:{'f' * 64}")),
        )


def test_release_approval_receipt_rejects_an_email_approver() -> None:
    """An address is a contact route that changes, not an identity."""
    with pytest.raises(ValidationError, match="approver"):
        _approval(approver="operator@example.com")


def test_release_approval_receipt_rejects_a_zero_policy_revision() -> None:
    """A policy generation starts at one."""
    with pytest.raises(ValidationError, match="policy_revision"):
        _approval(policy_revision=0)


# ---- checkpoint binding ------------------------------------------------------


def test_release_manifest_binds_a_checkpoint_with_a_gate_receipt() -> None:
    """A rung with recorded preflight evidence binds its manifest."""
    manifest = _manifest()
    release = release_record(manifest_digest=manifest.digest)
    rung = manifest.assert_binds_checkpoint(release=release, train=_train_with_receipts())
    assert rung.release_key == KEY


def test_release_manifest_rejects_a_checkpoint_with_no_gate_receipt() -> None:
    """A rung whose preflight left no receipt has no evidence to approve."""
    manifest = _manifest()
    release = release_record(manifest_digest=manifest.digest)
    with pytest.raises(ValueError, match="records no gate receipt"):
        manifest.assert_binds_checkpoint(release=release, train=V07_TRAIN)


def test_release_manifest_rejects_a_release_pinning_another_digest() -> None:
    """The release pins the manifest it approved, not a re-frozen one."""
    with pytest.raises(ValueError, match="pins manifest digest"):
        _manifest().assert_binds_checkpoint(
            release=release_record(),
            train=_train_with_receipts(),
        )


def test_release_manifest_rejects_a_release_of_another_checkpoint() -> None:
    """A dev1 manifest cannot bind a dev2 record."""
    release = release_record(
        key="REL-0.7.0.dev2",
        version="0.7.0.dev2",
        manifest_digest=_manifest().digest,
    )
    with pytest.raises(ValueError, match="cannot bind release"):
        _manifest().assert_binds_checkpoint(release=release, train=_train_with_receipts())


def test_release_manifest_rejects_a_checkpoint_the_train_does_not_declare() -> None:
    """A rung off the ladder cannot be bound at all."""
    version = "9.9.9"
    inventory = _inventory(release_key=f"REL-{version}", version=version)
    notes = _notes(release_key=f"REL-{version}", version=version)
    manifest = _manifest(
        release_key=f"REL-{version}",
        version=version,
        inventory=inventory,
        notes=notes,
    )
    release = Release(
        uid=UUID(int=51),
        key=f"REL-{version}",
        version=version,
        channel=ReleaseChannel.STABLE,
        authority_epoch=1,
        status=ReleaseStatus.DRAFT,
    )
    with pytest.raises(KeyError, match=re.escape("9.9.9")):
        manifest.assert_binds_checkpoint(release=release, train=_train_with_receipts())


# ---- the epoch-1 membership rule and the committed record --------------------


@pytest.mark.parametrize("version", ["0.7.0.dev1", "0.7.0.dev2"])
def test_release_rejects_membership_refs_on_an_epoch1_checkpoint(version: str) -> None:
    """Epoch-2 acceptance bundles cannot exist at dev1 or dev2."""
    with pytest.raises(ValidationError, match="membership_refs must be empty"):
        release_record(
            key=f"REL-{version}",
            version=version,
            membership_refs=("bundle://milestone/MLS-0030",),
        )


def test_release_record_dev1_still_loads() -> None:
    """Adopting the leaves does not move the committed dev1 record."""
    release = release_record()
    assert release.key == KEY
    assert release.version == VERSION
    assert release.channel is ReleaseChannel.DEV
    assert release.status is ReleaseStatus.CANDIDATE
    assert release.membership_refs == ()


# ---- leaf boundaries ---------------------------------------------------------


def test_release_artifact_inventory_accepts_a_single_entry() -> None:
    """One artifact is the smallest inventory a checkpoint can build."""
    inventory = _inventory(entries=_inventory().entries[:1])
    assert inventory.artifact_ids == frozenset({"ART-wheel"})


def test_release_artifact_inventory_rejects_an_empty_entry_set() -> None:
    """A checkpoint that built nothing has nothing to publish."""
    with pytest.raises(ValidationError, match="entries"):
        _inventory(entries=())


def test_release_artifact_inventory_rejects_a_repeated_artifact_id() -> None:
    """An id addresses one artifact or it addresses none."""
    entry = _inventory().entries[0]
    with pytest.raises(ValidationError, match="repeats an artifact id"):
        _inventory(entries=(entry, entry.model_copy(update={"filename": "other.whl"})))


def test_release_artifact_inventory_rejects_a_repeated_filename() -> None:
    """Two artifacts under one filename make one of them unobservable."""
    entry = _inventory().entries[0]
    with pytest.raises(ValidationError, match="repeats a filename"):
        _inventory(entries=(entry, entry.model_copy(update={"artifact_id": "ART-other"})))


def test_release_artifact_entry_rejects_a_zero_length_artifact() -> None:
    """A zero-byte artifact is a failed build, not a small one."""
    with pytest.raises(ValidationError, match="size_bytes"):
        ReleaseArtifactEntry(
            artifact_id="ART-wheel",
            kind="wheel",
            filename="eawf.whl",
            digest=f"sha256:{'1' * 64}",
            size_bytes=0,
        )


def test_release_artifact_entry_rejects_an_unprefixed_digest() -> None:
    """The digest grammar names its algorithm."""
    with pytest.raises(ValidationError, match="digest"):
        ReleaseArtifactEntry(
            artifact_id="ART-wheel",
            kind="wheel",
            filename="eawf.whl",
            digest="1" * 64,
            size_bytes=1,
        )


def test_release_artifact_entry_rejects_an_untyped_artifact_id() -> None:
    """An id outside the ``ART-`` grammar resolves against nothing."""
    with pytest.raises(ValidationError, match="artifact_id"):
        ReleaseArtifactEntry(
            artifact_id="wheel",
            kind="wheel",
            filename="eawf.whl",
            digest=f"sha256:{'1' * 64}",
            size_bytes=1,
        )


def test_platform_claim_set_accepts_no_claims() -> None:
    """Advertising no platform is an answerable state, not a vacuous pass."""
    assert PlatformClaimSet().claims == ()


def test_platform_claim_set_rejects_a_platform_claimed_twice() -> None:
    """Two rows for one platform leave "is it proven" order-dependent."""
    claim = ReleasePlatformClaim(platform_id="linux-x86_64", receipt_ref=RECEIPT_REF)
    with pytest.raises(ValidationError, match="claimed more than once"):
        PlatformClaimSet(claims=(claim, claim.model_copy(update={"real_host": False})))


def test_platform_claim_set_digest_follows_the_real_host_flag() -> None:
    """An unproven claim digests differently from a proven one."""
    proven = PlatformClaimSet(
        claims=(ReleasePlatformClaim(platform_id="linux-x86_64", receipt_ref=RECEIPT_REF),)
    )
    stubbed = PlatformClaimSet(
        claims=(
            ReleasePlatformClaim(
                platform_id="linux-x86_64",
                receipt_ref=RECEIPT_REF,
                real_host=False,
            ),
        )
    )
    assert proven.digest != stubbed.digest


def test_release_notes_reject_an_empty_body() -> None:
    """Notes with no body describe nothing."""
    with pytest.raises(ValidationError, match="body"):
        _notes(body="")


def test_release_notes_reject_a_key_that_misspells_the_version() -> None:
    """The notes name the checkpoint they describe."""
    with pytest.raises(ValidationError, match=re.escape("must be 'REL-0.7.0.dev1'")):
        _notes(release_key="REL-0.7.0")


def test_release_manifest_rejects_an_unknown_field() -> None:
    """An unknown key is a defect, not a value to absorb."""
    with pytest.raises(ValidationError, match="merge_authorization_ref"):
        _manifest(merge_authorization_ref="pr://1")
