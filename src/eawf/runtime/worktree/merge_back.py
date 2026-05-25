"""``merge_back`` — bring per-wave worktree commits onto the parent branch.

Two strategies:

- ``cherry_pick`` (default per AGENTS.md rule 11): enumerate
  ``<target>..<wt_branch>`` and replay each commit onto *target*. On
  conflict, stop the loop, mark the :class:`WorktreeRecord` as
  ``CONFLICTED``, and surface the conflict envelope so the operator can
  resolve in the parent worktree and call ``--continue``.
- ``rebase_then_ff``: rebase the worktree branch on *target* (inside
  the worktree) and fast-forward the parent. On conflict, the rebase
  is left mid-flight; ``--continue`` resumes it from the worktree.
  If the parent is not fast-forwardable post-rebase (a concurrent
  commit landed on *target* between the rebase and the merge step),
  raises :class:`~eawf.surfaces.cli.errors.StateConflict`
  (``kind="IntegrityViolation"``) rather than force-updating — operator
  must re-attempt under a fresh lock.

Both strategies preserve evidence on conflict — the on-disk repo state
(``.git/CHERRY_PICK_HEAD`` for cherry-pick, ``rebase-merge`` for rebase)
is intentionally not aborted, so the operator can fix and re-issue.

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


# Strategy literals. Defined as strings (not StrEnum) because we don't
# want to leak a wide enum into state.json — the strategy is per-call,
# never persisted.
STRATEGY_CHERRY_PICK: str = "cherry_pick"
STRATEGY_REBASE_THEN_FF: str = "rebase_then_ff"

_VALID_STRATEGIES: frozenset[str] = frozenset({STRATEGY_CHERRY_PICK, STRATEGY_REBASE_THEN_FF})


@dataclass
class MergeBackResult:
    """Return shape from :func:`merge_back`.

    Attributes:
        record: The (possibly mutated) :class:`WorktreeRecord`.
        strategy: The strategy that was attempted.
        picked_commits: Short SHAs that were successfully cherry-picked
            (or, for rebase, the source range commits replayed). Empty
            when no commit landed (e.g., immediate conflict).
        target_branch: The parent branch the merge targeted.
        merged_commit: New HEAD on *target_branch* on success; ``None``
            on conflict / abort.
        conflicted: ``True`` iff a conflict halted the merge.
        conflict_files: When *conflicted*, the file list git reports as
            requiring resolution. Empty otherwise.
        conflict_commit: When *conflicted*, the sha that triggered it.
    """

    record: WorktreeRecord
    strategy: str
    picked_commits: list[str]
    target_branch: str
    merged_commit: str | None
    conflicted: bool
    conflict_files: list[str]
    conflict_commit: str | None


def _validate_strategy(strategy: str) -> None:
    if strategy not in _VALID_STRATEGIES:
        raise cli_errors.UserError(
            f"unknown merge-back strategy: {strategy!r} "
            f"(expected one of {sorted(_VALID_STRATEGIES)})",
            kind="InvalidInput",
        )


def _find_record_for_wave(state: State, wave_id: str) -> WorktreeRecord:
    """Return the active :class:`WorktreeRecord` for *wave_id*."""
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


def _conflict_files(repo: Path) -> list[str]:
    """Parse ``git status --porcelain`` for files in conflict.

    Status codes ``UU`` / ``AA`` / ``DD`` (and ``U_`` / ``_U`` family)
    mark unresolved merge entries — they are surfaced verbatim. We
    keep the leading two-char status code stripped off and return the
    pathname only.
    """
    files: list[str] = []
    for line in git.status_porcelain(repo):
        # porcelain v1: first two chars = XY status, then space, then path.
        if len(line) < 4:
            continue
        x, y = line[0], line[1]
        # Conflict entries: any of UU, AA, DD, AU, UA, DU, UD.
        if "U" in (x, y) or (x == "A" and y == "A") or (x == "D" and y == "D"):
            files.append(line[3:].strip())
    return files


def _cherry_pick_loop(
    repo_root: Path,
    *,
    record: WorktreeRecord,
    target_branch: str,
) -> MergeBackResult:
    """Replay ``<target>..<wt_branch>`` onto *target_branch*.

    Returns the populated :class:`MergeBackResult`. The function does
    not mutate *record*; the caller is responsible for transitioning
    ``record.status`` based on the result.
    """
    range_spec = f"{target_branch}..{record.branch}"
    commits = git.rev_list(repo_root, range_spec=range_spec)
    picked: list[str] = []
    if not commits:
        # Already in sync. Treat as a clean merge, leaving merged_commit
        # as the current target HEAD.
        head = git.head_sha(repo_root)
        return MergeBackResult(
            record=record,
            strategy=STRATEGY_CHERRY_PICK,
            picked_commits=[],
            target_branch=target_branch,
            merged_commit=head,
            conflicted=False,
            conflict_files=[],
            conflict_commit=None,
        )
    for sha in commits:
        clean, detail = git.cherry_pick(repo_root, sha=sha)
        if clean:
            # Track the short sha of the just-applied commit.
            applied = git.head_sha(repo_root)
            picked.append(applied)
            continue
        files = _conflict_files(repo_root)
        logger.info(f"_cherry_pick_loop conflict sha={sha} files={files} detail={detail!r}")
        return MergeBackResult(
            record=record,
            strategy=STRATEGY_CHERRY_PICK,
            picked_commits=picked,
            target_branch=target_branch,
            merged_commit=None,
            conflicted=True,
            conflict_files=files,
            conflict_commit=sha,
        )
    return MergeBackResult(
        record=record,
        strategy=STRATEGY_CHERRY_PICK,
        picked_commits=picked,
        target_branch=target_branch,
        merged_commit=picked[-1] if picked else git.head_sha(repo_root),
        conflicted=False,
        conflict_files=[],
        conflict_commit=None,
    )


def _rebase_then_ff(
    repo_root: Path,
    *,
    record: WorktreeRecord,
    target_branch: str,
) -> MergeBackResult:
    """Rebase the worktree branch on *target_branch*, then ff-merge."""
    worktree_path = repo_root / record.path
    clean, detail = git.rebase(worktree_path, target=target_branch)
    if not clean:
        files = _conflict_files(worktree_path)
        logger.info(f"_rebase_then_ff conflict files={files} detail={detail!r}")
        return MergeBackResult(
            record=record,
            strategy=STRATEGY_REBASE_THEN_FF,
            picked_commits=[],
            target_branch=target_branch,
            merged_commit=None,
            conflicted=True,
            conflict_files=files,
            conflict_commit=None,
        )
    head = git.merge_ff_only(repo_root, source=record.branch)
    return MergeBackResult(
        record=record,
        strategy=STRATEGY_REBASE_THEN_FF,
        picked_commits=[head],
        target_branch=target_branch,
        merged_commit=head,
        conflicted=False,
        conflict_files=[],
        conflict_commit=None,
    )


def _continue_resume(
    repo_root: Path,
    *,
    record: WorktreeRecord,
    target_branch: str,
) -> MergeBackResult:
    """Resume an in-progress conflict.

    Detects strategy from on-disk evidence:

    - ``CHERRY_PICK_HEAD`` present in *repo_root*'s ``.git`` -> cherry-pick.
    - rebase-merge / rebase-apply present in the worktree's ``.git`` -> rebase.

    Raises:
        StateConflict: with ``kind="IntegrityViolation"`` when the record
            is CONFLICTED but neither evidence is present (operator likely
            aborted manually).
    """
    if git.cherry_pick_in_progress(repo_root):
        clean, _detail = git.cherry_pick_continue(repo_root)
        if not clean:
            files = _conflict_files(repo_root)
            return MergeBackResult(
                record=record,
                strategy=STRATEGY_CHERRY_PICK,
                picked_commits=[],
                target_branch=target_branch,
                merged_commit=None,
                conflicted=True,
                conflict_files=files,
                conflict_commit=None,
            )
        # The "continue" handled the conflicted commit. Re-evaluate
        # the range — `rev-list <target>..<wt_branch>` now reflects
        # whatever commits remain (typically empty if the conflict
        # resolution converged on parent's existing content).
        commits_left = git.rev_list(repo_root, range_spec=f"{target_branch}..{record.branch}")
        if not commits_left:
            head = git.head_sha(repo_root)
            return MergeBackResult(
                record=record,
                strategy=STRATEGY_CHERRY_PICK,
                picked_commits=[head],
                target_branch=target_branch,
                merged_commit=head,
                conflicted=False,
                conflict_files=[],
                conflict_commit=None,
            )
        # Otherwise replay any remaining commits.
        return _cherry_pick_loop(repo_root, record=record, target_branch=target_branch)

    worktree_path = repo_root / record.path
    if git.rebase_in_progress(worktree_path):
        clean, _detail = git.rebase_continue(worktree_path)
        if not clean:
            files = _conflict_files(worktree_path)
            return MergeBackResult(
                record=record,
                strategy=STRATEGY_REBASE_THEN_FF,
                picked_commits=[],
                target_branch=target_branch,
                merged_commit=None,
                conflicted=True,
                conflict_files=files,
                conflict_commit=None,
            )
        head = git.merge_ff_only(repo_root, source=record.branch)
        return MergeBackResult(
            record=record,
            strategy=STRATEGY_REBASE_THEN_FF,
            picked_commits=[head],
            target_branch=target_branch,
            merged_commit=head,
            conflicted=False,
            conflict_files=[],
            conflict_commit=None,
        )

    raise cli_errors.StateConflict(
        "no in-progress cherry-pick or rebase found; record is CONFLICTED but "
        "the on-disk state has been cleared (was --abort run manually?)",
        kind="IntegrityViolation",
    )


def merge_back(
    state: State,
    *,
    repo_root: Path,
    wave_id: str,
    strategy: str = STRATEGY_CHERRY_PICK,
    target: str | None = None,
    continue_: bool = False,
    abort: bool = False,
) -> MergeBackResult:
    """Bring worktree commits onto the parent branch.

    Args:
        state: Mutated in place (record status + merged_commit).
        repo_root: Repository root (the parent worktree's working dir).
        wave_id: Wave whose worktree to merge back.
        strategy: ``cherry_pick`` (default) or ``rebase_then_ff``.
        target: Optional explicit target branch; defaults to
            ``record.base_branch``.
        continue_: Resume a previously-conflicted merge.
        abort: Abort an in-progress merge and mark ABANDONED.

    Returns:
        A :class:`MergeBackResult` describing the outcome.
    """
    if continue_ and abort:
        raise cli_errors.UserError(
            "--continue and --abort are mutually exclusive", kind="InvalidInput"
        )
    _validate_strategy(strategy)
    record = _find_record_for_wave(state, wave_id)
    chosen_target = target or record.base_branch

    now = datetime.now(UTC)

    if abort:
        # Honour whichever evidence is on disk. If neither, the abort is
        # a state-only ABANDONED transition.
        worktree_path = repo_root / record.path
        if git.cherry_pick_in_progress(repo_root):
            git.cherry_pick_abort(repo_root)
        if git.rebase_in_progress(worktree_path):
            git.rebase_abort(worktree_path)
        record.status = WorktreeStatus.ABANDONED
        record.merged_commit = None
        state.updated_at = now
        return MergeBackResult(
            record=record,
            strategy=strategy,
            picked_commits=[],
            target_branch=chosen_target,
            merged_commit=None,
            conflicted=False,
            conflict_files=[],
            conflict_commit=None,
        )

    if continue_:
        if record.status != WorktreeStatus.CONFLICTED:
            raise cli_errors.UserError(
                f"--continue requires record status CONFLICTED; got {record.status.value}",
                kind="InvalidInput",
            )
        result = _continue_resume(repo_root, record=record, target_branch=chosen_target)
    elif strategy == STRATEGY_CHERRY_PICK:
        result = _cherry_pick_loop(repo_root, record=record, target_branch=chosen_target)
    else:
        result = _rebase_then_ff(repo_root, record=record, target_branch=chosen_target)

    if result.conflicted:
        record.status = WorktreeStatus.CONFLICTED
        record.merged_commit = None
    else:
        record.status = WorktreeStatus.MERGED
        record.merged_commit = result.merged_commit
    state.updated_at = now
    return result


__all__ = [
    "STRATEGY_CHERRY_PICK",
    "STRATEGY_REBASE_THEN_FF",
    "MergeBackResult",
    "merge_back",
]
