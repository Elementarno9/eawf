"""Worktree subsystem (Phase 5 W01).

Public API surface:

- :func:`create_worktree` — materialise a per-wave worktree under
  ``.claude/worktrees/<name>/`` and append a
  :class:`~eawf.state.models.WorktreeRecord`.
- :func:`merge_back` — bring worktree commits onto the parent branch
  via ``cherry_pick`` (default per AGENTS.md rule 11) or
  ``rebase_then_ff``, with conflict preservation across ``--continue``
  / ``--abort``.
- :func:`cleanup_worktree` — tear down the worktree directory + branch,
  refusing-by-default for dirty/CONFLICTED records.
- :func:`list_worktrees` — read-only enumeration of recorded
  worktrees with a git-side cross-check.
- :func:`worktree_registry_lock` — advisory file lock guarding git's
  worktree registry from concurrent ``add``/``remove`` collisions.
- :class:`WorktreeError` — module-level alias of
  :class:`eawf.cli.errors.CliError` for callers that want a single
  catch surface.

The module never opens its own ``portalock(state.json)`` — every
mutator in this package expects to be called inside a
:func:`eawf.cli._mutation.state_transaction`.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from eawf.cli import errors as cli_errors
from eawf.state.models import State, WorktreeRecord
from eawf.worktree import git
from eawf.worktree.cleanup import CleanupResult, cleanup_worktree
from eawf.worktree.create import create_worktree
from eawf.worktree.locks import worktree_registry_lock
from eawf.worktree.merge_back import (
    STRATEGY_CHERRY_PICK,
    STRATEGY_REBASE_THEN_FF,
    MergeBackResult,
    merge_back,
)
from eawf.worktree.wave_land import (
    WaveLandBatchResult,
    WaveLandResult,
    wave_land,
    wave_land_batch,
)

# Module alias so callers can `except WorktreeError` without importing
# the cli.errors module directly. The taxonomy stays canonical (one
# CliError subclass per exit code).
WorktreeError = cli_errors.CliError


@dataclass
class WorktreeListing:
    """Read-only enumeration row for :func:`list_worktrees`.

    Attributes:
        record: The on-disk :class:`WorktreeRecord`.
        git_present: ``True`` iff ``git worktree list --porcelain``
            reports the recorded path. Divergence flags an entry that
            ``eawf doctor`` should investigate.
    """

    record: WorktreeRecord
    git_present: bool


def list_worktrees(
    state: State,
    *,
    repo_root: Path,
    include_terminal: bool = False,
) -> Iterator[WorktreeListing]:
    """Yield :class:`WorktreeListing` rows for every recorded worktree.

    Args:
        state: Validated state — ``state.worktrees`` is read-only.
        repo_root: Used to query ``git worktree list --porcelain`` for
            the cross-check column.
        include_terminal: When ``False`` (default), skip
            :class:`~eawf.state.enums.WorktreeStatus` MERGED / ABANDONED.

    Yields:
        One :class:`WorktreeListing` per row. Order matches insertion
        order in ``state.worktrees`` (Python dict insertion order).
    """
    if state.worktrees is None:
        return
    try:
        git_entries = git.worktree_list(repo_root)
    except cli_errors.CliError:
        # If git is missing / repo is broken, surface git_present=False
        # for every row rather than failing the listing.
        git_entries = []
    git_paths = {entry.get("worktree", "") for entry in git_entries}
    for record in state.worktrees.values():
        if not include_terminal and record.status.value in {"merged", "abandoned"}:
            continue
        yield WorktreeListing(
            record=record,
            git_present=record.path in git_paths,
        )


__all__ = [
    "STRATEGY_CHERRY_PICK",
    "STRATEGY_REBASE_THEN_FF",
    "CleanupResult",
    "MergeBackResult",
    "WaveLandBatchResult",
    "WaveLandResult",
    "WorktreeError",
    "WorktreeListing",
    "cleanup_worktree",
    "create_worktree",
    "list_worktrees",
    "merge_back",
    "wave_land",
    "wave_land_batch",
    "worktree_registry_lock",
]
