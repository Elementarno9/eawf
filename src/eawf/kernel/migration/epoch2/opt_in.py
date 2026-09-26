"""Verifying the backup an opted-in repository names before it is cut over.

A disposable canary is admitted on its owner's word that losing it costs
nothing. A live repository cannot say that truthfully, so it is admitted
on a different word: that a backup exists which could put it back. That
claim is cheap to make and cheap to check, so the apply checks it -- the
snapshot the declaration names must still be on disk under this
repository's backup directory, must hold the document, and must digest to
exactly the value the owner pinned when they declared the opt-in.

The check runs at the fence, before the workspace is resolved or a lock is
taken, so an opt-in with no usable backup leaves the tree byte-identical.
"""

from __future__ import annotations

import logging

from eawf.kernel.migration.epoch2.canary import (
    OPT_IN_DECLARATION_FILENAME,
    DisposableTarget,
    OptInDeclaration,
)
from eawf.kernel.migration.epoch2.errors import MigrationBackupUnverifiedError
from eawf.kernel.migration.epoch2.rules import StrictMigrationModel
from eawf.platform.backup import BackupStore, snapshot_digest

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
    "verify_opt_in_backup",
]
