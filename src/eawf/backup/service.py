"""Orchestration for the manual snapshot backup verbs.

The service layer composes :class:`eawf.backup.store.BackupStore` primitives
into the four operator verbs — create / list / restore / prune — and raises a
typed :class:`BackupError` for the conditions the CLI maps onto a ``USER_ERROR``
exit. CLI dispatch (:mod:`eawf.cli.commands.backup`) catches these and renders
the canonical error envelope; the service itself never touches Typer.

Restore is itself reversible: :func:`restore_backup` writes a pre-restore copy
of the live ``state.json`` before overwriting it, so a mistaken restore is
recoverable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from eawf.backup.store import BackupStore, Snapshot, format_timestamp

logger = logging.getLogger(__name__)


class BackupError(Exception):
    """Operator-fixable backup failure (maps onto ``USER_ERROR``)."""


class UnknownSnapshotError(BackupError):
    """Requested ``--ts`` does not name an existing snapshot."""


@dataclass(frozen=True)
class RestoreResult:
    """Outcome of a :func:`restore_backup` call.

    Attributes:
        snapshot: The snapshot that was restored from.
        restored: Artifact filenames written back into ``.ea/``.
        pre_restore: Path of the pre-restore safety copy of the live
            ``state.json``, or ``None`` when there was no live state to
            protect.
    """

    snapshot: Snapshot
    restored: tuple[str, ...]
    pre_restore: Path | None


def _repo_root_for(state_path: Path) -> Path:
    """Return the repo root that owns *state_path*.

    The resolver hands back ``<root>/.ea/state.json``; the repo root is two
    parents up. When the path is not under an ``.ea`` directory the parent of
    ``state.json`` is treated as the root (defensive — the standard resolver
    always yields the ``.ea`` shape).
    """
    parent = state_path.parent
    return parent.parent if parent.name == ".ea" else parent


def create_backup(
    state_path: Path,
    *,
    note: str | None = None,
    home: Path | None = None,
    when: datetime | None = None,
) -> Snapshot:
    """Snapshot the repo's ``.ea/`` artifacts into a timestamped dir.

    Args:
        state_path: The resolved ``.ea/state.json`` path. Its repo root
            (two parents up) is the source of the artifacts and the key
            for the per-repo backup directory.
        note: Optional operator note persisted as ``note.txt``.
        home: Optional explicit user-home root (test seam).
        when: Optional snapshot instant (test seam); defaults to now (UTC).

    Returns:
        The written :class:`Snapshot`.

    Raises:
        BackupError: When the repo has no ``.ea/state.json`` to snapshot.
    """
    repo_root = _repo_root_for(state_path)
    if not state_path.is_file():
        raise BackupError(f"no state to back up: {state_path} does not exist")
    store = BackupStore(repo_root, home=home)
    ts = format_timestamp(when if when is not None else datetime.now(tz=UTC))
    return store.write_snapshot(ts, note=note)


def list_backups(
    state_path: Path,
    *,
    home: Path | None = None,
) -> list[Snapshot]:
    """Return the repo's snapshots, most-recent first.

    Args:
        state_path: The resolved ``.ea/state.json`` path — its repo root
            keys the per-repo backup directory.
        home: Optional explicit user-home root (test seam).

    Returns:
        Snapshots sorted by timestamp descending (empty when none exist).
    """
    repo_root = _repo_root_for(state_path)
    return BackupStore(repo_root, home=home).list_snapshots()


def restore_backup(
    state_path: Path,
    *,
    ts: str,
    home: Path | None = None,
    when: datetime | None = None,
) -> RestoreResult:
    """Restore the repo's ``.ea/`` artifacts from snapshot *ts*.

    Writes a pre-restore copy of the live ``state.json`` first so the
    restore is reversible, then copies the snapshot's artifacts back
    byte-for-byte. Only ``state.json`` / ``config.yaml`` / ``profile.yaml``
    are restored (per C10 §5.15.3).

    Args:
        state_path: The resolved ``.ea/state.json`` path — its repo root
            is both the restore target and the per-repo backup key.
        ts: The snapshot timestamp dir-name to restore from (``--ts``).
        home: Optional explicit user-home root (test seam).
        when: Optional pre-restore-copy instant (test seam); defaults to
            now (UTC).

    Returns:
        A :class:`RestoreResult` naming the restored artifacts and the
        pre-restore copy path.

    Raises:
        UnknownSnapshotError: When *ts* does not name an existing snapshot.
    """
    repo_root = _repo_root_for(state_path)
    store = BackupStore(repo_root, home=home)
    snapshot = store.get_snapshot(ts)
    if snapshot is None:
        known = ", ".join(s.ts for s in store.list_snapshots()) or "<none>"
        raise UnknownSnapshotError(f"unknown snapshot timestamp: {ts!r} (known: {known})")
    pre_restore = store.write_pre_restore(when if when is not None else datetime.now(tz=UTC))
    restored = store.restore_snapshot(snapshot)
    return RestoreResult(
        snapshot=snapshot,
        restored=tuple(restored),
        pre_restore=pre_restore,
    )


def prune_backups(
    state_path: Path,
    *,
    keep: int,
    home: Path | None = None,
) -> list[str]:
    """Keep the *keep* most-recent snapshots; delete older ones.

    Args:
        state_path: The resolved ``.ea/state.json`` path — its repo root
            keys the per-repo backup directory.
        keep: Number of most-recent snapshots to retain (``>= 0``).
        home: Optional explicit user-home root (test seam).

    Returns:
        The timestamp names removed (empty when nothing was pruned).

    Raises:
        BackupError: When *keep* is negative.
    """
    if keep < 0:
        raise BackupError(f"invalid keep count: {keep} (must be >= 0)")
    repo_root = _repo_root_for(state_path)
    return BackupStore(repo_root, home=home).prune(keep)
