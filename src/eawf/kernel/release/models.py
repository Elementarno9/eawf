"""The strict leaf objects a release manifest is assembled from.

A release checkpoint pins four separate facts: which artifacts were
built, which platforms the checkpoint advertises, what the notes say,
and who approved the result. Until now the checkpoint record carried
those as opaque reference strings, so nothing could tell an approved
artifact set from a re-frozen one, and a manifest naming an artifact
that was never built loaded cleanly.

:class:`ReleaseManifest` is the aggregate that closes that gap. It is
the *approval-time* record: one :class:`ReleaseArtifactInventory` names
every built artifact exactly once, each publication target claims a
subset of those artifacts *by id* rather than restating their digests,
and the leaf digests it stores are checked against the leaves it holds.
That is deliberately not the same object as the observation-time frozen
manifest, which restates per-target digests because a read-back compares
against them without holding the inventory.

Two rules carry the weight and both are enforced here rather than by a
caller. A target claim naming an artifact the inventory does not hold is
refused, because the alternative is a publish that discovers the gap
against a registry. And a stored leaf digest that disagrees with the
leaf beside it is refused, because an approval binds the digest, so a
digest that no longer describes its own content approves nothing.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from eawf.kernel.spec.common import _StrictModel
from eawf.kernel.spec.release import (
    NormalizedVersionStr,
    Release,
    ReleaseCheckpoint,
    ReleaseKeyStr,
    ReleaseTrain,
    Sha256DigestStr,
    TargetIdStr,
    validate_release_against_train,
)
from eawf.kernel.spec.release_config import ReleaseArtifactKind, ReleasePlatformClaim
from eawf.kernel.state.epoch2.base import PrincipalKey
from eawf.kernel.state.types import UtcDatetime

#: Stable id of one built artifact inside a release inventory. A claim
#: references this rather than a filename because a filename carries the
#: version and would therefore change between checkpoints of one train.
ArtifactIdStr = Annotated[str, Field(pattern=r"^ART-[a-z0-9][a-z0-9-]{0,47}$")]


def _canonical_digest(payload: Mapping[str, Any]) -> str:
    """Return the ``sha256:`` digest of *payload* in canonical form.

    Digests are recomputed from content rather than stored beside it, so
    the ordering has to be a property of the payload and not of the
    author's typing order. Sorting keys and dropping insignificant
    whitespace is what makes two spellings of one record digest alike.

    Args:
        payload: The JSON-serialisable content to digest.

    Returns:
        The ``sha256:``-prefixed hex digest.
    """
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(body.encode('utf-8')).hexdigest()}"


class ReleaseArtifactEntry(_StrictModel):
    """One built artifact, at the digest it was built with.

    Attributes:
        artifact_id: Stable id the target claims reference.
        kind: Artifact kind, matching a target's ``artifact_kinds``.
        filename: Name the registry is expected to advertise it under.
        digest: ``sha256:``-prefixed digest of the built file.
        size_bytes: Size of the built file; at least one byte, because a
            zero-length artifact is a failed build rather than a small
            one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: ArtifactIdStr
    kind: ReleaseArtifactKind
    filename: Annotated[str, Field(min_length=1, max_length=300)]
    digest: Sha256DigestStr
    size_bytes: Annotated[int, Field(ge=1)]


class ReleaseArtifactInventory(_StrictModel):
    """Every artifact one checkpoint built, listed exactly once.

    The inventory is the single home of an artifact's digest. Targets
    claim entries by id, so a digest is written once and a target cannot
    publish a file the inventory never recorded.

    Attributes:
        schema_version: Record schema tag.
        release_key: ``REL-<version>`` this inventory was built for.
        version: Normalized version the artifacts carry.
        entries: Non-empty artifact set; ids and filenames are unique.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["release-artifact-inventory/v1"] = "release-artifact-inventory/v1"
    release_key: ReleaseKeyStr
    version: NormalizedVersionStr
    entries: Annotated[tuple[ReleaseArtifactEntry, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _entries_are_uniquely_addressable(self) -> ReleaseArtifactInventory:
        """Reject a repeated id or filename, or a key that misspells the version.

        Raises:
            ValueError: Two entries share an artifact id, two entries
                share a filename, or ``release_key`` is not
                ``REL-<version>``.
        """
        if self.release_key != f"REL-{self.version}":
            raise ValueError(
                f"release_key {self.release_key!r} must be 'REL-{self.version}' "
                f"for version {self.version!r}"
            )
        ids = [entry.artifact_id for entry in self.entries]
        if len(set(ids)) != len(ids):
            raise ValueError(f"inventory {self.release_key} repeats an artifact id: {ids}")
        names = [entry.filename for entry in self.entries]
        if len(set(names)) != len(names):
            raise ValueError(f"inventory {self.release_key} repeats a filename: {names}")
        return self

    @property
    def artifact_ids(self) -> frozenset[str]:
        """The ids a target claim may reference."""
        return frozenset(entry.artifact_id for entry in self.entries)

    @property
    def digest(self) -> str:
        """The ``sha256:`` digest of this inventory's content."""
        return _canonical_digest(
            {
                "release_key": self.release_key,
                "version": self.version,
                "entries": sorted(
                    (
                        {
                            "artifact_id": entry.artifact_id,
                            "kind": entry.kind.value,
                            "filename": entry.filename,
                            "digest": entry.digest,
                            "size_bytes": entry.size_bytes,
                        }
                        for entry in self.entries
                    ),
                    key=lambda row: str(row["artifact_id"]),
                ),
            }
        )


class PlatformClaimSet(_StrictModel):
    """The platforms one checkpoint advertises, with their receipts.

    The set exists rather than a bare tuple because two claims for one
    platform would let an unproven receipt sit beside a proven one and
    leave the answer to "is this platform proven" dependent on which row
    the reader found first.

    Attributes:
        schema_version: Record schema tag.
        claims: Platform claims, at most one per platform id. Empty
            means the checkpoint advertises no platform, which is an
            answerable state rather than a vacuous pass.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["platform-claim-set/v1"] = "platform-claim-set/v1"
    claims: tuple[ReleasePlatformClaim, ...] = ()

    @model_validator(mode="after")
    def _platforms_are_claimed_once(self) -> PlatformClaimSet:
        """Reject two claims for the same platform.

        Raises:
            ValueError: A platform id appears more than once.
        """
        platforms = [claim.platform_id for claim in self.claims]
        if len(set(platforms)) != len(platforms):
            raise ValueError(f"platform claimed more than once: {platforms}")
        return self

    @property
    def digest(self) -> str:
        """The ``sha256:`` digest of this claim set's content."""
        return _canonical_digest(
            {
                "claims": sorted(
                    (
                        {
                            "platform_id": claim.platform_id,
                            "receipt_ref": claim.receipt_ref,
                            "real_host": claim.real_host,
                        }
                        for claim in self.claims
                    ),
                    key=lambda row: str(row["platform_id"]),
                ),
            }
        )


class ReleaseNotes(_StrictModel):
    """The rendered notes one checkpoint publishes.

    Attributes:
        schema_version: Record schema tag.
        release_key: ``REL-<version>`` the notes describe.
        version: Normalized version the notes describe.
        title: One-line heading a release page shows.
        body: The rendered markdown body.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["release-notes/v1"] = "release-notes/v1"
    release_key: ReleaseKeyStr
    version: NormalizedVersionStr
    title: Annotated[str, Field(min_length=1, max_length=120)]
    body: Annotated[str, Field(min_length=1, max_length=100_000)]

    @model_validator(mode="after")
    def _key_spells_the_version(self) -> ReleaseNotes:
        """Reject notes whose key does not spell the version they describe.

        Raises:
            ValueError: ``release_key`` is not ``REL-<version>``.
        """
        if self.release_key != f"REL-{self.version}":
            raise ValueError(
                f"release_key {self.release_key!r} must be 'REL-{self.version}' "
                f"for version {self.version!r}"
            )
        return self

    @property
    def digest(self) -> str:
        """The ``sha256:`` digest of these notes' content."""
        return _canonical_digest(
            {
                "release_key": self.release_key,
                "version": self.version,
                "title": self.title,
                "body": self.body,
            }
        )


class ReleaseApprovalReceipt(_StrictModel):
    """One operator approval, bound to the exact proof it approved.

    Attributes:
        schema_version: Record schema tag.
        receipt_id: Stable identity of the receipt.
        release_key: ``REL-<version>`` the approval unblocks.
        proof_digest: Digest of the artifact set the approval binds. An
            approval that did not name a proof would follow the release
            across an input change and approve something else.
        approver: Immutable qualified key of the approving principal.
        approved_at: When the approval was given.
        policy_revision: Policy generation the approval was given under.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["release-approval-receipt/v1"] = "release-approval-receipt/v1"
    receipt_id: UUID
    release_key: ReleaseKeyStr
    proof_digest: Sha256DigestStr
    approver: PrincipalKey
    approved_at: UtcDatetime
    policy_revision: Annotated[int, Field(ge=1)]


class ReleaseManifest(_StrictModel):
    """The approval-time aggregate of one checkpoint's release content.

    :attr:`digest` covers the pinned content only -- the leaf digests,
    the target claims and the version -- and deliberately not
    :attr:`approvals`. An approval binds the manifest digest, so folding
    the approval back into that digest would change the very value the
    approval names.

    Attributes:
        schema_version: Record schema tag.
        release_key: ``REL-<version>`` this manifest pins.
        version: Normalized version every leaf must agree on.
        inventory: The complete built artifact set.
        inventory_digest: Digest of :attr:`inventory`, recomputed and
            checked at load.
        notes: The rendered notes.
        notes_digest: Digest of :attr:`notes`, recomputed and checked at
            load.
        platform_claims: Platforms the checkpoint advertises.
        target_claims: Artifact ids each publication target publishes,
            drawn from :attr:`inventory`.
        approvals: Approval receipts, at most one per proof digest.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["release-manifest/v1"] = "release-manifest/v1"
    release_key: ReleaseKeyStr
    version: NormalizedVersionStr
    inventory: ReleaseArtifactInventory
    inventory_digest: Sha256DigestStr
    notes: ReleaseNotes
    notes_digest: Sha256DigestStr
    platform_claims: PlatformClaimSet = PlatformClaimSet()
    target_claims: Annotated[Mapping[TargetIdStr, tuple[ArtifactIdStr, ...]], Field(min_length=1)]
    approvals: tuple[ReleaseApprovalReceipt, ...] = ()

    @model_validator(mode="after")
    def _leaves_agree_on_one_release(self) -> ReleaseManifest:
        """Reject a leaf that describes a different checkpoint.

        Raises:
            ValueError: The manifest key does not spell its version, or
                the inventory, the notes or an approval receipt carries
                a different release key.
        """
        if self.release_key != f"REL-{self.version}":
            raise ValueError(
                f"release_key {self.release_key!r} must be 'REL-{self.version}' "
                f"for version {self.version!r}"
            )
        mismatched = [
            f"{name}={key!r}"
            for name, key in (
                ("inventory", self.inventory.release_key),
                ("notes", self.notes.release_key),
                *((f"approval[{row.receipt_id}]", row.release_key) for row in self.approvals),
            )
            if key != self.release_key
        ]
        if mismatched:
            raise ValueError(
                f"manifest {self.release_key} holds leaves of another release: {mismatched}"
            )
        return self

    @model_validator(mode="after")
    def _leaf_digests_describe_their_leaves(self) -> ReleaseManifest:
        """Reject a stored digest that no longer describes its leaf.

        Raises:
            ValueError: ``inventory_digest`` or ``notes_digest`` differs
                from the recomputed digest of the leaf beside it.
        """
        for name, stored, actual in (
            ("inventory_digest", self.inventory_digest, self.inventory.digest),
            ("notes_digest", self.notes_digest, self.notes.digest),
        ):
            if stored != actual:
                raise ValueError(
                    f"{name} {stored!r} does not describe the {self.release_key} leaf "
                    f"beside it ({actual!r})"
                )
        return self

    @model_validator(mode="after")
    def _target_claims_resolve_against_the_inventory(self) -> ReleaseManifest:
        """Reject a target claim naming an artifact that was never built.

        Raises:
            ValueError: A target claims no artifact at all, repeats an
                artifact id, or names an id the inventory does not hold.
        """
        known = self.inventory.artifact_ids
        for target_id, claimed in self.target_claims.items():
            if not claimed:
                raise ValueError(f"target {target_id!r} claims no artifact")
            if len(set(claimed)) != len(claimed):
                raise ValueError(f"target {target_id!r} claims the same artifact twice: {claimed}")
            unresolved = sorted(set(claimed) - known)
            if unresolved:
                raise ValueError(
                    f"target {target_id!r} claims artifact ids the {self.release_key} "
                    f"inventory does not hold: {unresolved}"
                )
        return self

    @model_validator(mode="after")
    def _one_approval_per_proof(self) -> ReleaseManifest:
        """Reject a second approval receipt bound to one proof digest.

        Raises:
            ValueError: Two receipts share a ``proof_digest``, or two
                receipts share a ``receipt_id``.
        """
        proofs = [row.proof_digest for row in self.approvals]
        if len(set(proofs)) != len(proofs):
            raise ValueError(
                f"manifest {self.release_key} carries a second approval receipt on one "
                f"proof_digest; a proof is approved once"
            )
        ids = [row.receipt_id for row in self.approvals]
        if len(set(ids)) != len(ids):
            raise ValueError(f"manifest {self.release_key} repeats an approval receipt id")
        return self

    @property
    def digest(self) -> str:
        """The ``sha256:`` digest an approval and a release record bind."""
        return _canonical_digest(
            {
                "release_key": self.release_key,
                "version": self.version,
                "inventory_digest": self.inventory_digest,
                "notes_digest": self.notes_digest,
                "platform_claims_digest": self.platform_claims.digest,
                "target_claims": {
                    target_id: sorted(claimed)
                    for target_id, claimed in sorted(self.target_claims.items())
                },
            }
        )

    def artifacts_for(self, target_id: str) -> tuple[ReleaseArtifactEntry, ...]:
        """Return the inventory entries *target_id* publishes.

        Args:
            target_id: Publication target to resolve.

        Returns:
            The claimed entries, in the order the claim lists them.

        Raises:
            KeyError: This manifest declares no claim for that target.
        """
        try:
            claimed = self.target_claims[target_id]
        except KeyError as exc:
            raise KeyError(
                f"manifest {self.release_key} claims no target {target_id!r}; "
                f"claimed: {sorted(self.target_claims)}"
            ) from exc
        by_id = {entry.artifact_id: entry for entry in self.inventory.entries}
        return tuple(by_id[artifact_id] for artifact_id in claimed)

    def assert_binds_checkpoint(
        self,
        *,
        release: Release,
        train: ReleaseTrain,
    ) -> ReleaseCheckpoint:
        """Return the rung this manifest approves, asserting it binds.

        The manifest alone cannot know which rung it belongs to, so the
        epoch agreement, the membership cardinality and the gate
        receipts are checked here against the records that declare them.
        A checkpoint with no gate receipt has no evidence its preflight
        ran, so approving against it would bind a proof nobody produced.

        Args:
            release: The checkpoint record this manifest was frozen for.
            train: The train that declares the rung.

        Returns:
            The checkpoint declaration the release occupies.

        Raises:
            KeyError: The train declares no rung for the release key.
            ValueError: The manifest was frozen for a different release,
                the release pins a different manifest digest, the
                authority epoch or membership disagrees with the rung,
                or the train records no gate receipt for the rung.
        """
        if release.key != self.release_key:
            raise ValueError(
                f"manifest for {self.release_key!r} cannot bind release {release.key!r}"
            )
        rung = validate_release_against_train(release, train)
        if not train.gate_receipt_refs.get(release.key, ()):
            raise ValueError(
                f"checkpoint {release.key!r} records no gate receipt; its preflight "
                f"evidence is missing"
            )
        if release.manifest_digest is not None and release.manifest_digest != self.digest:
            raise ValueError(
                f"release {release.key!r} pins manifest digest "
                f"{release.manifest_digest!r}, not {self.digest!r}"
            )
        return rung


__all__ = [
    "ArtifactIdStr",
    "PlatformClaimSet",
    "ReleaseApprovalReceipt",
    "ReleaseArtifactEntry",
    "ReleaseArtifactInventory",
    "ReleaseManifest",
    "ReleaseNotes",
]
