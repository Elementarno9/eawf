"""Manual snapshot backup surface — create/list/restore/prune.

CLI dispatch (:mod:`eawf.cli.commands.backup`) delegates all on-disk work to
this package. :mod:`eawf.backup.store` owns the user-scope backup tree
layout + atomic snapshot reads/writes; :mod:`eawf.backup.service` orchestrates
the create/list/restore/prune verbs against a repo's ``.ea/`` artifacts.

The backup tree is keyed by ``repo_sha = sha256(repo-absolute-path)[:12]``
under the user-scope home (``~/.eawf/backups/<repo_sha>/``) so snapshots from
distinct repos never collide and never live inside the (committed) repo tree.
"""

from __future__ import annotations

from eawf.backup.service import (
    BackupError,
    UnknownSnapshotError,
    create_backup,
    list_backups,
    prune_backups,
    restore_backup,
)
from eawf.backup.store import (
    BackupStore,
    Snapshot,
    repo_sha,
)

__all__ = [
    "BackupError",
    "BackupStore",
    "Snapshot",
    "UnknownSnapshotError",
    "create_backup",
    "list_backups",
    "prune_backups",
    "repo_sha",
    "restore_backup",
]
