"""``cleanup_worktree`` — tear down a worktree directory + branch.

Refusal contract per the §Phase-5 plan row ("clean teardown" + "Property
test: file-scope claims disjoint"):

- ``record.status == ACTIVE`` AND working tree is dirty AND not
  ``--force`` -> :class:`StateConflict` (``kind="IntegrityViolation"``,
  exit 8).
- ``record.status == CONFLICTED`` AND not ``--force`` ->
  :class:`StateConflict` (``kind="IntegrityViolation"``; "preserve
  evidence; pass --force to discard"). The conflict-preservation
  criterion in AGENTS.md rule 11
  is the contract here: a CONFLICTED state is operator-recoverable
  evidence, not garbage.

On success, the function:

1. Runs ``git worktree remove`` (which prunes both the directory and
   the ``.git/worktrees/<name>`` registry entry).
2. Optionally deletes the per-wave branch (default; ``--keep-branch``
   skips this courtesy step).
3. Transitions ``record.status`` to ``MERGED`` if it was already
   merged-and-cleanup-only, or ``ABANDONED`` if the operator forced
   teardown of an unmerged worktree. ``MERGED`` and ``CONFLICTED+force``
   keep the existing transition semantics.

The function mutates the supplied :class:`State` in place. Caller holds
``portalock(state.json)`` and the worktree-registry lock.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import eawf.runtime.worktree.git as git
from eawf.kernel.state.enums import WorktreeStatus
from eawf.kernel.state.models import State, WorktreeRecord
from eawf.surfaces.cli import errors as cli_errors

logger = logging.getLogger(__name__)


@dataclass
class CleanupResult:
    """Return shape from :func:`cleanup_worktree`.

    Attributes:
        record: The (possibly transitioned) :class:`WorktreeRecord`.
        removed_path: Path that was unlinked.
        branch_deleted: ``True`` iff ``git branch -D`` succeeded.
        branch: Per-wave branch name.
    """

    record: WorktreeRecord
    removed_path: str
    branch_deleted: bool
    branch: str


def _find_record_for_wave(state: State, wave_id: str) -> WorktreeRecord:
    """Return the :class:`WorktreeRecord` for *wave_id*."""
    if state.worktrees is None:
        raise cli_errors.UserError(f"no worktrees recorded for wave {wave_id}", kind="NotFound")
    wave = state.waves.get(wave_id)
    if wave is None:
        raise cli_errors.UserError(f"unknown wave: {wave_id}", kind="NotFound")
    if wave.worktree_id is None:
        raise cli_errors.UserError(f"wave {wave_id} has no worktree id stamped", kind="NotFound")
    record = state.worktrees.get(wave.worktree_id)
    if record is None:
        raise cli_errors.UserError(
            f"wave {wave_id} references worktree {wave.worktree_id!r} which is not in state",
            kind="NotFound",
        )
    return record


def cleanup_worktree(
    state: State,
    *,
    repo_root: Path,
    wave_id: str,
    force: bool = False,
    keep_branch: bool = False,
) -> CleanupResult:
    """Tear down the worktree directory + per-wave branch.

    Raises:
        UserError: The wave or its worktree record is absent
            (``kind="NotFound"``).
        StateConflict: The dirty/CONFLICTED guard refused
            (``kind="IntegrityViolation"``).
    """
    record = _find_record_for_wave(state, wave_id)
    worktree_path = repo_root / record.path

    # ---- 1. Refusal guards ----------------------------------------------
    # Conflicted records preserve evidence; --force overrides.
    if record.status == WorktreeStatus.CONFLICTED and not force:
        raise cli_errors.StateConflict(
            f"worktree {record.id!r} is CONFLICTED; preserve evidence and resolve "
            f"with `eawf worktree merge-back --continue` or pass --force to discard",
            kind="IntegrityViolation",
        )

    # Dirty-tree guard. Skip when status is already MERGED (the
    # post-merge worktree often retains cherry-pick artifacts that are
    # innocuous) or when the path is already gone. Only suppress the
    # ``kind="InstrumentMissing"`` case so a broken git invocation still
    # surfaces: ``kind="IntegrityViolation"`` (rc!=0) and
    # ``kind="LockConflict"`` (timeout) are operator-actionable signals
    # that the cleanup must not swallow.
    if worktree_path.exists() and record.status == WorktreeStatus.ACTIVE and not force:
        try:
            dirty = git.status_porcelain(worktree_path)
        except cli_errors.UserError as exc:
            if exc.kind != "InstrumentMissing":
                raise
            dirty = []
        if dirty:
            raise cli_errors.StateConflict(
                f"worktree {record.id!r} is dirty (status reports {len(dirty)} entries); "
                f"commit/discard changes or pass --force",
                kind="IntegrityViolation",
            )

    # ---- 2. Remove via git worktree remove ------------------------------
    if worktree_path.exists():
        git.worktree_remove(repo_root, path=worktree_path, force=force)

    # ---- 3. Delete branch (default; --keep-branch skips) ----------------
    branch_deleted = False
    if not keep_branch:
        branch_deleted = git.branch_delete(repo_root, name=record.branch)

    # ---- 4. Status transition -------------------------------------------
    now = datetime.now(UTC)
    if record.status == WorktreeStatus.ACTIVE:
        # Forced teardown of an unmerged worktree -> ABANDONED.
        record.status = WorktreeStatus.ABANDONED
    # MERGED / CONFLICTED+force / ABANDONED: leave the existing status.
    state.updated_at = now

    logger.info(
        f"cleanup_worktree wave={wave_id} branch={record.branch} "
        f"path={worktree_path} branch_deleted={branch_deleted}"
    )
    return CleanupResult(
        record=record,
        removed_path=str(worktree_path),
        branch_deleted=branch_deleted,
        branch=record.branch,
    )


__all__ = [
    "CleanupResult",
    "cleanup_worktree",
]
