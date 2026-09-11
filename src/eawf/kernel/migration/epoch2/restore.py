"""The pre-cutover copy of every authority surface, and the verified restore.

The apply pins a digest for each authority surface before it writes
anything. A digest proves a surface moved; it cannot put it back. So the
same read that takes the digest also copies the bytes into a restore
directory inside the fenced tree, and the manifest written afterwards is
the one object a rollback needs: for every declared surface, what it
digested to and whether it existed at all.

Two things the restore deliberately does not do.

It never writes outside the target. The workspace registry is digested
into the restore point because the apply resolves the addressing key
against it, but the registry lives in the operator's home directory --
outside the tree whose owner declared it throwaway -- so it is recorded
and never rewritten.

It never restores part of the set. The whole surface set is verified
against the manifest before the first byte is written, and an absent or
mismatched copy refuses there, while the tree is still exactly as the
interrupted cutover left it. A half-written restore would leave a tree
that is neither the corpus the cutover started from nor the generation it
was building, and an operator can reason about either of those but not
about the third thing.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Annotated, Final, Literal, Self

from pydantic import Field, model_validator

from eawf.kernel.migration.epoch2.canary import TARGET_AUTHORITY_LOCATORS, DisposableTarget
from eawf.kernel.migration.epoch2.errors import (
    MigrationRestoreIncompleteError,
    MigrationRestorePointMissingError,
)
from eawf.kernel.migration.epoch2.generation import GENERATION_ID_PATTERN, atomic_write_json
from eawf.kernel.migration.epoch2.manifest import BackupRecord
from eawf.kernel.migration.epoch2.rules import StrictMigrationModel, rule_digest
from eawf.kernel.migration.epoch2.snapshot import digest_bytes

logger = logging.getLogger(__name__)


#: What the restore point records for a surface that does not exist yet.
#: Distinct from a digest over empty bytes, which would claim the file was
#: there and held nothing.
ABSENT_SURFACE: Final = "absent"

#: The locator the restore point records the workspace registry under. A
#: logical name rather than the file's real path, because the registry
#: lives in the operator's home directory and a manifest never records
#: where on a machine anything was.
REGISTRY_LOCATOR: Final = "registry.json"

#: How a pinned digest is introduced in a backup surface entry.
DIGEST_PREFIX: Final = "sha256:"

#: The schema version every restore manifest carries.
RESTORE_SCHEMA_VERSION: Final[Literal["1"]] = "1"


class SurfaceSnapshot(StrictMigrationModel):
    """One authority surface as it stood before the cutover wrote anything.

    Attributes:
        locator: The surface's tree-relative locator.
        digest: What the surface digested to, or :data:`ABSENT_SURFACE`
            when it did not exist.
        restorable: Whether a restore may write this surface. Only the
            in-tree authority surfaces are: the registry is recorded for
            evidence and never rewritten, because it lives outside the
            tree whose owner declared it disposable.
    """

    locator: Annotated[str, Field(min_length=1, max_length=200)]
    digest: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{64}|absent)$")]
    restorable: bool

    @classmethod
    def parse(cls, entry: str) -> SurfaceSnapshot:
        """Return the surface one backup entry describes.

        Args:
            entry: A ``<locator>@sha256:<hex>`` or ``<locator>@absent``
                entry, as :class:`BackupRecord` encodes them.

        Returns:
            The parsed surface.

        Raises:
            ValueError: The entry carries no ``@``, or the value after it
                is neither the absent sentinel nor a pinned digest. A
                restore that guessed at a malformed entry would write
                bytes nobody pinned.
        """
        locator, separator, value = entry.rpartition("@")
        if not separator:
            raise ValueError(
                f"backup entry {entry!r} carries no '@', so it names no surface digest"
            )
        if value == ABSENT_SURFACE:
            digest = ABSENT_SURFACE
        elif value.startswith(DIGEST_PREFIX):
            digest = value.removeprefix(DIGEST_PREFIX)
        else:
            raise ValueError(
                f"backup entry {entry!r} pins neither {ABSENT_SURFACE!r} nor a "
                f"{DIGEST_PREFIX} digest"
            )
        return cls(
            locator=locator,
            digest=digest,
            restorable=locator in TARGET_AUTHORITY_LOCATORS,
        )

    @property
    def was_present(self) -> bool:
        """Whether the surface existed when the restore point was taken."""
        return self.digest != ABSENT_SURFACE


class RestoreManifest(StrictMigrationModel):
    """What a rollback writes back, and which cutover it belongs to.

    Attributes:
        schema_version: Always ``"1"``.
        taken_at: When the surfaces were read and copied.
        generation_id: The generation the cutover was building. A restore
            point from another cutover is not a restore point for this
            tree, so the rollback checks this before it writes.
        manifest_digest: The cutover manifest the restore point belongs to.
        idempotence_digest: What a re-apply of the same plan must
            reproduce, pinned here so a restore-then-reapply can be
            checked against the run it replaced.
        approval_digest: The plan digest the operator approved.
        surfaces: One row per declared surface, in locator order.
        backup_digest: The digest of the :class:`BackupRecord` the rows
            were parsed from, so the two records cannot drift apart.
        restore_digest: A digest over this manifest's whole content.
    """

    schema_version: Literal["1"]
    taken_at: datetime
    generation_id: Annotated[str, Field(pattern=GENERATION_ID_PATTERN)]
    manifest_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    idempotence_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    approval_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    surfaces: tuple[SurfaceSnapshot, ...]
    backup_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    restore_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def _digest_covers_content(self) -> Self:
        """Recompute the digest so a hand-edited manifest cannot pass.

        Returns:
            The validated manifest.

        Raises:
            ValueError: The digest does not cover the content. A restore
                manifest is the instruction set for overwriting somebody's
                authority files, so it re-derives its own identity at load
                rather than trusting the field it was handed.
        """
        expected = restore_digest_for(
            taken_at=self.taken_at,
            generation_id=self.generation_id,
            manifest_digest=self.manifest_digest,
            idempotence_digest=self.idempotence_digest,
            approval_digest=self.approval_digest,
            surfaces=self.surfaces,
            backup_digest=self.backup_digest,
        )
        if self.restore_digest != expected:
            raise ValueError(
                f"restore digest {self.restore_digest} does not cover this manifest's "
                f"{len(self.surfaces)} surfaces (expected {expected})"
            )
        return self

    def restorable_surfaces(self) -> tuple[SurfaceSnapshot, ...]:
        """Return every surface a restore may write, in locator order."""
        return tuple(surface for surface in self.surfaces if surface.restorable)


def restore_digest_for(
    *,
    taken_at: datetime,
    generation_id: str,
    manifest_digest: str,
    idempotence_digest: str,
    approval_digest: str,
    surfaces: tuple[SurfaceSnapshot, ...],
    backup_digest: str,
) -> str:
    """Return the digest one restore manifest is identified by.

    Args:
        taken_at: When the surfaces were read.
        generation_id: The generation the cutover was building.
        manifest_digest: The cutover manifest the point belongs to.
        idempotence_digest: What a re-apply must reproduce.
        approval_digest: The approved plan digest.
        surfaces: The surface rows, in locator order.
        backup_digest: The digest of the backup record they came from.

    Returns:
        A 64-character lowercase hex digest.
    """
    return rule_digest(
        [
            RESTORE_SCHEMA_VERSION,
            taken_at.isoformat(),
            generation_id,
            manifest_digest,
            idempotence_digest,
            approval_digest,
            backup_digest,
            [surface.model_dump(mode="json") for surface in surfaces],
        ]
    )


def capture_restore_point(
    *,
    target: DisposableTarget,
    backup: BackupRecord,
    generation_id: str,
    manifest_digest: str,
    idempotence_digest: str,
    approval_digest: str,
) -> RestoreManifest:
    """Copy every present in-tree surface and seal what a rollback writes back.

    The digests come from ``backup`` rather than from a second read, so
    there is exactly one set of pinned digests in play and each copy is
    checked against it as it lands.

    Args:
        target: The fence-cleared target tree.
        backup: The restore point's pinned digests.
        generation_id: The generation the cutover is about to build.
        manifest_digest: The manifest that will build it.
        idempotence_digest: What a re-apply of the same plan must
            reproduce.
        approval_digest: The plan digest the operator approved.

    Returns:
        The sealed manifest, already on disk.

    Raises:
        MigrationRestoreIncompleteError: A copy did not digest to the
            value the backup pinned, which means the surface moved between
            the digest and the copy. No manifest is written, so the partial
            copy set is not mistaken for a restore point.
        ValueError: A backup entry is malformed, or it names a surface the
            fence does not declare.
        OSError: A surface could not be read or copied.
    """
    surfaces = tuple(SurfaceSnapshot.parse(entry) for entry in backup.surfaces)
    target.restore_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for surface in surfaces:
        if not (surface.restorable and surface.was_present):
            continue
        copy = target.restore_copy_path(surface.locator)
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(target.authority_path(surface.locator), copy)
        landed = digest_bytes(copy.read_bytes())
        if landed != surface.digest:
            raise MigrationRestoreIncompleteError(
                f"the copy of {surface.locator} digests to {landed[:12]} but the restore "
                f"point pinned {surface.digest[:12]}, so the surface moved while it was "
                "being copied"
            )
        copied += 1
    manifest = RestoreManifest(
        schema_version=RESTORE_SCHEMA_VERSION,
        taken_at=backup.taken_at,
        generation_id=generation_id,
        manifest_digest=manifest_digest,
        idempotence_digest=idempotence_digest,
        approval_digest=approval_digest,
        surfaces=surfaces,
        backup_digest=backup.backup_digest,
        restore_digest=restore_digest_for(
            taken_at=backup.taken_at,
            generation_id=generation_id,
            manifest_digest=manifest_digest,
            idempotence_digest=idempotence_digest,
            approval_digest=approval_digest,
            surfaces=surfaces,
            backup_digest=backup.backup_digest,
        ),
    )
    write_restore_manifest(target=target, manifest=manifest)
    logger.info(
        f"capture_restore_point root={target.root.name} surfaces={len(surfaces)} copied={copied}"
    )
    return manifest


def write_restore_manifest(*, target: DisposableTarget, manifest: RestoreManifest) -> Path:
    """Seal the restore manifest, which is the act that makes the point usable.

    It is written after every copy has landed and been checked, so a tree
    that carries the manifest carries a complete restore point.

    Args:
        target: The fence-cleared target tree.
        manifest: The manifest to write.

    Returns:
        Where it was written.

    Raises:
        OSError: The write or the rename failed.
    """
    atomic_write_json(target.restore_manifest_path, manifest)
    return target.restore_manifest_path


def read_restore_manifest(path: Path) -> RestoreManifest:
    """Return the restore manifest at ``path``.

    Args:
        path: The manifest file.

    Returns:
        The parsed manifest.

    Raises:
        MigrationRestorePointMissingError: The file is absent, unreadable,
            not JSON, or does not satisfy the contract. All four are the
            same answer to the only question asked here -- can this tree be
            put back -- so they carry one code.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise MigrationRestorePointMissingError(
            f"{path.name} is absent or unreadable ({error.__class__.__name__}), so this "
            "tree pinned no restore point a rollback could write back"
        ) from error
    except json.JSONDecodeError as error:
        raise MigrationRestorePointMissingError(
            f"{path.name} is not valid JSON, so it names no surface set: {error}"
        ) from error
    try:
        return RestoreManifest.model_validate(payload)
    except ValueError as error:
        raise MigrationRestorePointMissingError(
            f"{path.name} is not a restore manifest: {error}"
        ) from error


def verify_restore_point(*, target: DisposableTarget, manifest: RestoreManifest) -> tuple[str, ...]:
    """Return every locator a restore would touch, refusing an incomplete point.

    This runs before the first byte is written, which is what makes a
    partial restore impossible rather than merely unlikely.

    Args:
        target: The fence-cleared target tree.
        manifest: The restore point being verified.

    Returns:
        The locators a restore would write or remove, in locator order.

    Raises:
        MigrationRestoreIncompleteError: A copy the manifest names is
            absent, or its bytes no longer digest to the pinned value.
        ValueError: The manifest names a surface the fence does not
            declare.
        OSError: A copy exists but could not be read.
    """
    faults: list[str] = []
    planned: list[str] = []
    for surface in manifest.restorable_surfaces():
        if not surface.was_present:
            if target.authority_path(surface.locator).exists():
                planned.append(surface.locator)
            continue
        copy = target.restore_copy_path(surface.locator)
        if not copy.is_file():
            faults.append(f"{surface.locator}: no copy under the restore point")
            continue
        landed = digest_bytes(copy.read_bytes())
        if landed != surface.digest:
            faults.append(
                f"{surface.locator}: copy digests to {landed[:12]}, pinned {surface.digest[:12]}"
            )
            continue
        planned.append(surface.locator)
    if faults:
        raise MigrationRestoreIncompleteError(
            f"the restore point cannot put {len(faults)} of "
            f"{len(manifest.restorable_surfaces())} surfaces back, so nothing is written "
            f"and the tree is left as the interrupted cutover left it: {'; '.join(faults)}"
        )
    return tuple(planned)


def restore_full_set(*, target: DisposableTarget, manifest: RestoreManifest) -> tuple[str, ...]:
    """Put every declared surface back the way the restore point found it.

    Each surface that existed is rewritten from its copy and each surface
    that did not is removed, so the answer afterwards is the whole set
    rather than the subset that happened to have moved. A surface created
    during the write window by a writer that should not have been running
    is therefore removed rather than left behind.

    Args:
        target: The fence-cleared target tree.
        manifest: The restore point to write back.

    Returns:
        The locators that were written or removed, in locator order.

    Raises:
        MigrationRestoreIncompleteError: The point does not verify before
            the write, or a surface does not digest to its pinned value
            after it.
        ValueError: The manifest names a surface the fence does not
            declare.
        OSError: A surface could not be written or removed.
    """
    planned = verify_restore_point(target=target, manifest=manifest)
    for surface in manifest.restorable_surfaces():
        destination = target.authority_path(surface.locator)
        if surface.was_present:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(target.restore_copy_path(surface.locator), destination)
        else:
            destination.unlink(missing_ok=True)
    _require_surfaces_match(target=target, manifest=manifest)
    logger.info(f"restore_full_set root={target.root.name} locators={len(planned)}")
    return planned


def _require_surfaces_match(*, target: DisposableTarget, manifest: RestoreManifest) -> None:
    """Refuse a finished restore whose surfaces do not digest to the pins.

    Args:
        target: The fence-cleared target tree.
        manifest: The restore point that was written back.

    Raises:
        MigrationRestoreIncompleteError: A surface on disk disagrees with
            the manifest. The pre-write verification makes this
            unreachable through a copy fault, so reaching it means the
            write itself did not land -- which is the one failure a
            rollback must never report as success.
    """
    mismatches: list[str] = []
    for surface in manifest.restorable_surfaces():
        landed = _surface_state(target.authority_path(surface.locator))
        if landed != surface.digest:
            mismatches.append(f"{surface.locator}: {landed[:12]}, pinned {surface.digest[:12]}")
    if mismatches:
        raise MigrationRestoreIncompleteError(
            f"{len(mismatches)} surfaces do not match the restore point after the write: "
            f"{'; '.join(mismatches)}"
        )


def _surface_state(path: Path) -> str:
    """Return a surface's digest, or the absent sentinel when it is gone."""
    return digest_bytes(path.read_bytes()) if path.is_file() else ABSENT_SURFACE


__all__ = [
    "ABSENT_SURFACE",
    "DIGEST_PREFIX",
    "REGISTRY_LOCATOR",
    "RESTORE_SCHEMA_VERSION",
    "RestoreManifest",
    "SurfaceSnapshot",
    "capture_restore_point",
    "read_restore_manifest",
    "restore_digest_for",
    "restore_full_set",
    "verify_restore_point",
    "write_restore_manifest",
]
