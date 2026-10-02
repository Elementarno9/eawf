"""Declaring and verifying the backup an opted-in repository is cut over against.

A disposable canary is admitted on its owner's word that losing it costs
nothing. A live repository cannot say that truthfully, so it is admitted
on a different word: that a backup exists which could put it back. That
claim is cheap to make and cheap to check, so the apply checks it -- the
snapshot the declaration names must still be on disk under this
repository's backup directory, must hold the document, and must digest to
exactly the value the owner pinned when they declared the opt-in.

The check runs at the fence, before the workspace is resolved or a lock is
taken, so an opt-in with no usable backup leaves the tree byte-identical.

The declaration is written the same way whether ``eawf init`` bears a new
tree at epoch 2 or an operator opts an existing epoch-1 repository in: the
backup is taken first and the declaration pins what it digested to, so a
declaration never names a snapshot that was not on disk when it was written.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from eawf.kernel.migration.epoch2.canary import (
    CANARY_DECLARATION_FILENAME,
    OPT_IN_DECLARATION_FILENAME,
    DisposableTarget,
    OptInDeclaration,
    declaration_path,
    opt_in_path,
)
from eawf.kernel.migration.epoch2.errors import (
    MigrationAlreadyCutOverError,
    MigrationBackupUnverifiedError,
    MigrationTargetNotDisposableError,
)
from eawf.kernel.migration.epoch2.generation import atomic_write_json
from eawf.kernel.migration.epoch2.rules import StrictMigrationModel
from eawf.kernel.state.io import epoch_marker_present
from eawf.platform.backup import BackupStore, create_backup, snapshot_digest

logger = logging.getLogger(__name__)


#: The artifact a backup must hold to be one a repository could be put
#: back from. A snapshot of the configuration alone restores nothing.
REQUIRED_BACKUP_ARTIFACT = "state.json"


class VerifiedBackup(StrictMigrationModel):
    """The backup an opted-in tree was admitted on.

    Attributes:
        ts: The snapshot's identifier.
        digest: What it digests to, equal to the declared pin.
        artifacts: The artifacts it holds.
    """

    ts: str
    digest: str
    artifacts: tuple[str, ...]


def declare_opt_in(
    state_path: Path, *, declared_by: str, purpose: str, note: str, when: datetime
) -> OptInDeclaration:
    """Back up the tree holding ``state_path`` and opt it into epoch 2 against that backup.

    A second call replaces the declaration with one pinned to a fresh
    backup, so an operator who commits more work before the cutover can pin
    the state they will actually cut over.

    Args:
        state_path: The tree's ``state.json``; its directory is the tree root.
        declared_by: Who opts the repository in.
        purpose: Why the repository is moving to epoch 2, in one line.
        note: The note the pinned backup carries.
        when: The backup's snapshot instant.

    Returns:
        The declaration written to the tree.

    Raises:
        MigrationAlreadyCutOverError: The tree already carries the epoch
            marker, so its pin records the backup the cutover was admitted on.
        MigrationTargetNotDisposableError: The tree already declares itself
            a disposable canary, and a tree may carry only one declaration.
        BackupError: The tree has no ``state.json`` to back up.
        ValueError: ``declared_by`` or ``purpose`` is empty or too long.
        OSError: The backup or the declaration could not be written.
    """
    tree_root = state_path.parent
    if epoch_marker_present(tree_root):
        raise MigrationAlreadyCutOverError(
            f"{tree_root.name} already carries the epoch marker, so it was cut over against the "
            f"backup {OPT_IN_DECLARATION_FILENAME} names and that pin stays as written"
        )
    if declaration_path(tree_root).exists():
        raise MigrationTargetNotDisposableError(
            f"{tree_root.name} already carries {CANARY_DECLARATION_FILENAME}; a tree is either "
            f"throwaway or opted in, so remove that declaration before writing "
            f"{OPT_IN_DECLARATION_FILENAME}"
        )
    snapshot = create_backup(state_path, note=note, when=when)
    declaration = OptInDeclaration(
        opt_in=True,
        declared_by=declared_by,
        purpose=purpose,
        backup_ts=snapshot.ts,
        backup_digest=snapshot_digest(snapshot),
    )
    atomic_write_json(opt_in_path(tree_root), declaration)
    logger.info(f"declare_opt_in root={tree_root.name} backup={snapshot.ts}")
    return declaration


def verify_opt_in_backup(target: DisposableTarget) -> VerifiedBackup | None:
    """Verify the backup an opted-in tree names, or refuse its apply.

    The backup is looked up under the repository that holds the tree --
    the tree's parent directory -- in the same per-repository directory
    ``eawf backup create`` writes to.

    Args:
        target: The fence-cleared target tree.

    Returns:
        The verified backup for an opted-in tree, ``None`` for a disposable
        canary, which is admitted without one.

    Raises:
        MigrationBackupUnverifiedError: The snapshot is absent, holds no
            document, or no longer digests to the declared value.
        OSError: A snapshot artifact could not be read.
    """
    declaration = target.declaration
    if not isinstance(declaration, OptInDeclaration):
        return None
    store = BackupStore(target.root.parent)
    snapshot = store.get_snapshot(declaration.backup_ts)
    if snapshot is None:
        raise MigrationBackupUnverifiedError(
            f"{OPT_IN_DECLARATION_FILENAME} names backup {declaration.backup_ts}, which is "
            "not in this repository's backup directory, so nothing could put the tree back "
            "and the apply refuses to write into it"
        )
    if REQUIRED_BACKUP_ARTIFACT not in snapshot.artifacts:
        raise MigrationBackupUnverifiedError(
            f"backup {snapshot.ts} holds no {REQUIRED_BACKUP_ARTIFACT}, so it could not put "
            "the tree's document back"
        )
    digest = snapshot_digest(snapshot)
    if digest != declaration.backup_digest:
        raise MigrationBackupUnverifiedError(
            f"backup {snapshot.ts} digests to {digest[:12]} but "
            f"{OPT_IN_DECLARATION_FILENAME} pinned {declaration.backup_digest[:12]}, so the "
            "snapshot changed after the opt-in was declared"
        )
    logger.info(f"verify_opt_in_backup root={target.root.name} ts={snapshot.ts}")
    return VerifiedBackup(ts=snapshot.ts, digest=digest, artifacts=snapshot.artifacts)


__all__ = [
    "REQUIRED_BACKUP_ARTIFACT",
    "VerifiedBackup",
    "declare_opt_in",
    "verify_opt_in_backup",
]
