"""Freeze a checkpoint's manifest from its publication receipts, and pin it.

The release machine has one edge into ``candidate``:
``DRAFT -> CANDIDATE`` under
:attr:`~eawf.workflow.release.lifecycle.ReleaseGuardName.MANIFEST_COMPLETE`.
Nothing drove it. The manifest an approval binds was therefore assembled
by hand -- a throwaway script that read three receipt files, spelled the
three registry identities as literals and called
:func:`~eawf.workflow.release.lifecycle.advance_release` itself -- which
means the one artifact the whole publication path is judged against was
produced by code nobody reviewed and nothing tested.

This module is that script, made a contract. Two pieces:

* :func:`freeze_manifest` turns the receipts a tag's publish jobs left
  behind into a :class:`~eawf.workflow.release.observation.FrozenManifest`.
  Every declared target must have left a receipt naming this checkpoint's
  version, and every declared artifact kind must resolve to a file that
  receipt reported, so a partial download cannot freeze a partial
  manifest that still reads complete.
* :func:`pin_candidate` applies the pin. The ``manifest_complete``
  predicate is *computed* from the configuration and the manifest rather
  than asserted by the caller, which is what makes the guard mean
  something: a manifest missing a required leg reaches the transition
  and is denied ``release_manifest_incomplete``.

The registry identities come from the checkpoint configuration
(:attr:`~eawf.kernel.spec.release_config.ReleaseTargetConfig.identity`).
A target declaring none is refused rather than defaulted: an identity
invented at freeze time would be observed against an external name
nobody configured, and the read-back would pass while proving nothing.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Final

from eawf.kernel.spec.release import (
    Release,
    ReleaseStatus,
    release_key,
    semver_equivalent,
)
from eawf.kernel.spec.release_config import (
    ReleaseArtifactKind,
    ReleaseConfig,
    ReleaseTargetConfig,
)
from eawf.workflow.release.boundaries import PublicationBoundary, durable_boundary
from eawf.workflow.release.lifecycle import ReleaseGuardContext, advance_release
from eawf.workflow.release.observation import (
    FrozenArtifact,
    FrozenManifest,
    FrozenTarget,
)
from eawf.workflow.release.publication_receipt import PublicationReceipt, read_receipts
from eawf.workflow.release.source_host_assets import source_host_assets

logger = logging.getLogger(__name__)

#: Wall budget for the one git query this module runs. Matches the
#: release probes' budget, because it is the same class of local read.
GIT_TIMEOUT_SECONDS: Final[int] = 30

#: Filename suffix that identifies each Python distribution artifact
#: among the files a publish job reported. PyPI legs also upload
#: attestations beside the distributions, so the kinds are matched by
#: what they are rather than by taking everything the job listed.
_ARTIFACT_SUFFIXES: Final[Mapping[ReleaseArtifactKind, str]] = {
    ReleaseArtifactKind.WHEEL: ".whl",
    ReleaseArtifactKind.SDIST: ".tar.gz",
}


class CandidateRefusal(StrEnum):
    """Named reason a manifest freeze or a pin was refused.

    Values:
        PUBLICATION_RECEIPT_MISSING: A declared target left no receipt in
            the directory, so its artifact set is unknown.
        PUBLICATION_RECEIPT_VERSION_MISMATCH: A receipt names a version
            other than the checkpoint being frozen.
        FROZEN_ARTIFACT_MISSING: A declared artifact kind resolves to no
            file the receipt reported.
        FROZEN_ARTIFACT_UNRESOLVED: A declared artifact kind has no rule
            naming it among the reported files.
        TARGET_IDENTITY_UNDECLARED: A declared target configures no
            external registry identity.
        SOURCE_TREE_UNRESOLVED: The pinned source commit has no tree in
            this checkout.
    """

    PUBLICATION_RECEIPT_MISSING = "publication_receipt_missing"
    PUBLICATION_RECEIPT_VERSION_MISMATCH = "publication_receipt_version_mismatch"
    FROZEN_ARTIFACT_MISSING = "frozen_artifact_missing"
    FROZEN_ARTIFACT_UNRESOLVED = "frozen_artifact_unresolved"
    TARGET_IDENTITY_UNDECLARED = "target_identity_undeclared"
    SOURCE_TREE_UNRESOLVED = "source_tree_unresolved"


class CandidateFreezeError(ValueError):
    """The manifest could not be frozen from what the receipts carry.

    Attributes:
        code: The named refusal, quoted by the operator surface.
    """

    def __init__(self, code: CandidateRefusal, message: str) -> None:
        """Store the typed *code* alongside the operator message."""
        super().__init__(f"{code.value}: {message}")
        self.code = code


def _target_filenames(
    kind: ReleaseArtifactKind,
    *,
    reported: Mapping[str, str],
    assets: Mapping[ReleaseArtifactKind, str],
    sole_kind: bool,
) -> tuple[str, ...]:
    """Return the reported filenames *kind* claims.

    Three rules, tried in order, and between them they are total over
    :class:`~eawf.kernel.spec.release_config.ReleaseArtifactKind`. A kind
    the source host attaches has one canonical name, so it is matched
    exactly. A Python distribution is matched by its suffix, which is
    what separates a wheel from the attestation uploaded beside it.
    Anything else takes every reported file, which is only an answer for
    a leg that declares exactly one kind -- a multi-kind leg gets no
    filenames and the caller refuses.

    Args:
        kind: The artifact kind being resolved.
        reported: ``filename -> digest`` as the publish job reported it.
        assets: Canonical source-host filename per kind, for this version.
        sole_kind: Whether the target declares this kind and no other.

    Returns:
        The filenames this kind claims, sorted; empty when none apply.
    """
    canonical = assets.get(kind)
    if canonical is not None:
        return (canonical,) if canonical in reported else ()
    suffix = _ARTIFACT_SUFFIXES.get(kind)
    if suffix is not None:
        return tuple(sorted(name for name in reported if name.endswith(suffix)))
    return tuple(sorted(reported)) if sole_kind else ()


def _frozen_target(
    target: ReleaseTargetConfig,
    receipt: PublicationReceipt,
    *,
    version: str,
) -> FrozenTarget:
    """Return the frozen row *receipt* supports for *target*.

    Args:
        target: The configured leg being frozen.
        receipt: What that leg's publish job reported.
        version: Normalized checkpoint version being frozen.

    Returns:
        The identity and artifact set the target is observed against.

    Raises:
        CandidateFreezeError: With
            :attr:`CandidateRefusal.TARGET_IDENTITY_UNDECLARED` when the
            target configures no registry identity,
            :attr:`CandidateRefusal.FROZEN_ARTIFACT_UNRESOLVED` when a
            declared kind has no naming rule, or
            :attr:`CandidateRefusal.FROZEN_ARTIFACT_MISSING` when a
            declared kind names nothing the receipt reported.
    """
    if target.identity is None:
        raise CandidateFreezeError(
            CandidateRefusal.TARGET_IDENTITY_UNDECLARED,
            f"target {target.target_id!r} configures no registry identity; "
            f"declare one on the checkpoint configuration before freezing a manifest",
        )
    assets = source_host_assets(version)
    sole_kind = len(set(target.artifact_kinds)) == 1
    artifacts: list[FrozenArtifact] = []
    for kind in target.artifact_kinds:
        if not (sole_kind or kind in assets or kind in _ARTIFACT_SUFFIXES):
            raise CandidateFreezeError(
                CandidateRefusal.FROZEN_ARTIFACT_UNRESOLVED,
                f"target {target.target_id!r} declares artifact kind {kind.value!r} "
                f"beside others, and no rule names it among the reported files "
                f"{sorted(receipt.artifact_digests)}",
            )
        names = _target_filenames(
            kind, reported=receipt.artifact_digests, assets=assets, sole_kind=sole_kind
        )
        if not names:
            raise CandidateFreezeError(
                CandidateRefusal.FROZEN_ARTIFACT_MISSING,
                f"target {target.target_id!r} declares artifact kind {kind.value!r}, "
                f"and its receipt reports {sorted(receipt.artifact_digests)}",
            )
        artifacts.extend(
            FrozenArtifact(kind=kind, filename=name, digest=receipt.artifact_digests[name])
            for name in names
        )
    return FrozenTarget(identity=target.identity, artifacts=tuple(artifacts))


def _assert_receipt_version(receipt: PublicationReceipt, *, version: str) -> None:
    """Raise unless *receipt* was written for *version*.

    The npm leg carries the SemVer spelling of the same checkpoint, so
    both spellings are admitted and nothing else is: a receipt directory
    downloaded for the previous tag is the mistake this check exists for.

    Args:
        receipt: The downloaded receipt.
        version: Normalized checkpoint version being frozen.

    Raises:
        CandidateFreezeError: With
            :attr:`CandidateRefusal.PUBLICATION_RECEIPT_VERSION_MISMATCH`.
    """
    accepted = {version, semver_equivalent(version)}
    if receipt.version in accepted:
        return
    raise CandidateFreezeError(
        CandidateRefusal.PUBLICATION_RECEIPT_VERSION_MISMATCH,
        f"receipt for target {receipt.target_id!r} reports version "
        f"{receipt.version!r}, not {sorted(accepted)}",
    )


def freeze_manifest(config: ReleaseConfig, *, receipts_dir: Path) -> FrozenManifest:
    """Return the manifest the receipts in *receipts_dir* freeze.

    Args:
        config: The loaded checkpoint configuration; it names every
            target, its artifact kinds and its registry identity.
        receipts_dir: Directory the tag's publication receipts were
            downloaded into, one ``publication-receipt-<target>.json``
            per leg.

    Returns:
        The frozen manifest, carrying one row per declared target.

    Raises:
        CandidateFreezeError: When a declared target left no receipt, a
            receipt names another version, a target configures no
            identity, or a declared artifact kind names no reported file.
        ValueError: When a present receipt is not valid JSON, does not
            validate, or names a different leg than its filename does.
    """
    target_ids = tuple(target.target_id for target in config.targets)
    found = read_receipts(receipts_dir, target_ids)
    missing = sorted(set(target_ids) - set(found))
    if missing:
        raise CandidateFreezeError(
            CandidateRefusal.PUBLICATION_RECEIPT_MISSING,
            f"checkpoint {config.version} declares targets {sorted(target_ids)} and "
            f"{receipts_dir} carries no receipt for {missing}",
        )
    targets: dict[str, FrozenTarget] = {}
    for target in config.targets:
        receipt = found[target.target_id]
        _assert_receipt_version(receipt, version=config.version)
        targets[target.target_id] = _frozen_target(target, receipt, version=config.version)
    manifest = FrozenManifest(
        release_key=release_key(config.version),
        version=config.version,
        targets=targets,
    )
    logger.info(
        f"freeze_manifest version={config.version!r} targets={sorted(targets)} "
        f"digest={manifest.digest!r}"
    )
    return manifest


def manifest_complete(config: ReleaseConfig, manifest: FrozenManifest) -> bool:
    """Return whether *manifest* pins everything *config* requires.

    Backs the ``manifest_complete`` guard of the ``DRAFT -> CANDIDATE``
    edge. The predicate is every required leg frozen with a non-empty
    artifact set; an optional leg the checkpoint declares may be absent,
    because a target that cannot block a bake cannot block a pin either.

    Args:
        config: The loaded checkpoint configuration.
        manifest: The manifest offered as the pin.

    Returns:
        ``True`` when the manifest freezes this checkpoint's version and
        carries every required target.
    """
    if manifest.version != config.version:
        return False
    required = {target.target_id for target in config.targets if target.required}
    return required <= set(manifest.targets)


def resolve_source_tree(repo_root: Path, source_sha: str) -> str:
    """Return the tree sha of *source_sha* as *repo_root* records it.

    The record pins the commit and its tree, because a commit is a
    pointer into history that a rewrite can re-point while the tree is
    the content itself. Reading the tree from the checkout rather than
    accepting it from the caller is what keeps the two in step.

    Args:
        repo_root: Working copy holding the commit.
        source_sha: The commit the artifacts were built from.

    Returns:
        The 40-character tree sha.

    Raises:
        CandidateFreezeError: With
            :attr:`CandidateRefusal.SOURCE_TREE_UNRESOLVED` when git
            cannot resolve the commit in this checkout.
    """
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", f"{source_sha}^{{tree}}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    tree = result.stdout.strip()
    if result.returncode != 0 or not tree:
        raise CandidateFreezeError(
            CandidateRefusal.SOURCE_TREE_UNRESOLVED,
            f"git cannot resolve the tree of {source_sha!r} in {repo_root}: "
            f"{result.stderr.strip() or 'no such commit'}",
        )
    logger.debug(f"resolve_source_tree source_sha={source_sha!r} tree_sha={tree!r}")
    return tree


@durable_boundary(PublicationBoundary.TRANSITION_APPLY)
def pin_candidate(
    release: Release,
    config: ReleaseConfig,
    manifest: FrozenManifest,
    *,
    source_sha: str,
    source_tree_sha: str,
    manifest_ref: str,
) -> Release:
    """Return the CANDIDATE successor of *release*, pinned to *manifest*.

    Every field the ``candidate`` status requires is written in one
    transition -- the source commit, its tree, the manifest reference and
    the manifest's recomputed digest -- so no half-pinned record can
    exist. Whatever the draft already carried, ``supersedes_release_ref``
    included, is carried forward by the copy.

    Args:
        release: The DRAFT record being pinned.
        config: The checkpoint configuration the completeness predicate
            is computed against.
        manifest: The frozen manifest the candidate pins.
        source_sha: Commit the artifacts were built from.
        source_tree_sha: Tree of that commit.
        manifest_ref: Pointer to the stored manifest artifact.

    Returns:
        The successor record at
        :attr:`~eawf.kernel.spec.release.ReleaseStatus.CANDIDATE`.

    Raises:
        ValueError: When *manifest* was frozen for a different release
            key, or the successor violates a record invariant.
        ReleaseTransitionError: With
            :attr:`~eawf.workflow.release.lifecycle.ReleaseDenialCode.RELEASE_MANIFEST_INCOMPLETE`
            when the manifest does not carry every required target, or
            with
            :attr:`~eawf.workflow.release.lifecycle.ReleaseDenialCode.ILLEGAL_RELEASE_TRANSITION`
            when the record is not a draft.
    """
    if manifest.release_key != release.key:
        raise ValueError(
            f"manifest for {manifest.release_key!r} cannot pin release {release.key!r}"
        )
    pinned = advance_release(
        release,
        ReleaseStatus.CANDIDATE,
        ReleaseGuardContext(manifest_complete=manifest_complete(config, manifest)),
        source_sha=source_sha,
        source_tree_sha=source_tree_sha,
        manifest_ref=manifest_ref,
        manifest_digest=manifest.digest,
    )
    logger.info(
        f"pin_candidate key={pinned.key!r} revision={pinned.revision} "
        f"source_sha={source_sha!r} manifest_digest={pinned.manifest_digest!r}"
    )
    return pinned


__all__ = [
    "GIT_TIMEOUT_SECONDS",
    "CandidateFreezeError",
    "CandidateRefusal",
    "freeze_manifest",
    "manifest_complete",
    "pin_candidate",
    "resolve_source_tree",
]
