"""``wave_land`` — wave-centric cherry-pick automation (B027).

This module layers a wave-centric automation on top of the
worktree-centric :func:`eawf.worktree.merge_back.merge_back`. It is the
wave-lifecycle counterpart to ``eawf worktree merge-back``:

- Look up the wave's worktree.
- Replay the worktree commits onto the parent branch via
  *cherry-pick only* (AGENTS.md rule 11 names cherry-pick as the
  discipline; ``rebase_then_ff`` is intentionally not exposed here).
- Drive the :func:`eawf.workflow.lifecycle.transitions.close_wave` transition with
  the resulting commit SHA as evidence.
- Optionally clean up the worktree directory + branch.

On conflict the function refuses to close the wave and surfaces a
:class:`~eawf.cli.errors.StateConflict` (``kind="IntegrityViolation"``)
with a repair hint. The on-disk repo state (``.git/CHERRY_PICK_HEAD``) is
preserved so the operator can resolve and re-run.

The function mutates the supplied :class:`State` in place. Caller holds
``portalock(state.json)`` (via :func:`state_transaction`) and the
worktree-registry lock (when applicable).
"""

from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass
from pathlib import Path

from eawf.cli import errors as cli_errors
from eawf.kernel.state.enums import WaveStatus
from eawf.kernel.state.models import State
from eawf.workflow.lifecycle.transitions import LifecycleError, close_wave
from eawf.worktree.cleanup import CleanupResult, cleanup_worktree
from eawf.worktree.merge_back import (
    STRATEGY_CHERRY_PICK,
    MergeBackResult,
    merge_back,
)

logger = logging.getLogger(__name__)


@dataclass
class WaveLandResult:
    """Return shape from :func:`wave_land`.

    Attributes:
        wave_id: The wave that was landed.
        commits: Ordered list of parent-branch commit SHAs produced by
            the cherry-pick. Empty when the worktree had no commits beyond
            the merge-base (e.g., wave already merged out-of-band).
        outcome: Outcome text stamped on the wave.
        worktree_cleaned: ``True`` iff :func:`cleanup_worktree` ran
            successfully after the close.
        merged_commit: HEAD of the parent branch post-merge — same as the
            last entry of *commits* when *commits* is non-empty; otherwise
            the pre-call HEAD.
        cleanup: The :class:`CleanupResult` from
            :func:`cleanup_worktree`, or ``None`` when cleanup was
            skipped via ``keep_worktree=True``.
    """

    wave_id: str
    commits: list[str]
    outcome: str
    worktree_cleaned: bool
    merged_commit: str
    cleanup: CleanupResult | None


def _format_default_outcome(commit_count: int) -> str:
    """Return the default outcome text used when the caller omits one.

    Plural-agnostic on purpose — "1 commit(s)" reads awkwardly but stays
    machine-parseable for downstream tooling. Spec contract calls for
    ``f"landed {len(shas)} commit(s) via wave land"``.
    """
    return f"landed {commit_count} commit(s) via wave land"


def _check_wave_exists_and_active(state: State, wave_id: str) -> None:
    """Surface canonical errors for the common pre-flight failures.

    The :func:`merge_back` call below already raises :class:`UserError`
    (``kind="NotFound"``) when the wave is missing, but it does so via the
    worktree-record
    lookup ("wave X has no worktree id stamped") which obscures the
    underlying cause. We raise the cleaner shape up-front so the CLI
    envelope reads ``unknown wave`` rather than ``no worktree``.
    """
    wave = state.waves.get(wave_id)
    if wave is None:
        raise cli_errors.UserError(f"unknown wave: {wave_id}", kind="NotFound")
    if wave.status not in {WaveStatus.CLAIMED, WaveStatus.IN_PROGRESS}:
        raise cli_errors.ValidationError(
            f"wave {wave_id!r} is not claimed/in_progress "
            f"(status={wave.status.value!r}); cannot land"
        )


def _refuse_on_conflict(
    wave_id: str,
    merge_result: MergeBackResult,
    record_path: str,
) -> None:
    """Raise :class:`StateConflict` (``kind="IntegrityViolation"``) on conflict.

    Fires when *merge_result* is conflicted.

    The hint mirrors the repair procedure documented in :func:`merge_back`:
    resolve the conflict in the parent worktree, then either re-run
    ``wave land`` (the wave-centric entry point) or invoke
    ``worktree merge-back --continue`` (the lower-level entry point).
    """
    if not merge_result.conflicted:
        return
    files = list(merge_result.conflict_files)
    detail = (
        f"cherry-pick conflict landing wave {wave_id!r}: "
        f"files={files} conflict_commit={merge_result.conflict_commit}; "
        f"re-run after resolving in {record_path} "
        f"then run `eawf wave land {wave_id}` again or "
        f"`eawf worktree merge-back --wave {wave_id} --continue`"
    )
    raise cli_errors.StateConflict(detail, kind="IntegrityViolation")


def wave_land(
    state: State,
    *,
    repo_root: Path,
    wave_id: str,
    outcome: str | None = None,
    keep_worktree: bool = False,
) -> WaveLandResult:
    """Cherry-pick the wave's worktree commits onto the parent branch
    and close the wave.

    Args:
        state: Mutated in place. Caller holds
            ``portalock(state.json)``.
        repo_root: Repository root (the parent worktree's working dir).
        wave_id: Wave whose worktree to land.
        outcome: Outcome text recorded on the wave. When ``None``, a
            default is synthesised from the picked-commit count.
        keep_worktree: When ``True``, skip the post-close cleanup. The
            worktree directory and branch remain in place — useful for
            inspect-then-discard flows.

    Returns:
        A :class:`WaveLandResult` describing the picked commits, the
        outcome stamped, and whether the worktree was torn down.

    Raises:
        UserError: When the wave is unknown or has no worktree
            (``kind="NotFound"``).
        ValidationError: When the wave is not in a closable status.
        StateConflict: When the cherry-pick conflicts
            (``kind="IntegrityViolation"``).
        LifecycleError surfaces propagate from :func:`close_wave`.
    """
    _check_wave_exists_and_active(state, wave_id)

    # merge_back will raise UserError (kind="NotFound") when the worktree
    # record is missing (e.g., wave was never claimed via `worktree create`).
    merge_result = merge_back(
        state,
        repo_root=repo_root,
        wave_id=wave_id,
        strategy=STRATEGY_CHERRY_PICK,
    )

    record_path_value = Path(merge_result.record.path)
    abs_record_path = (
        record_path_value if record_path_value.is_absolute() else repo_root / record_path_value
    )
    _refuse_on_conflict(wave_id, merge_result, str(abs_record_path))

    # Conflict path is guarded above; the success branch must have a
    # non-None merged_commit (merge_back populates HEAD on the no-op
    # path too, so this is always set on the success branch).
    assert merge_result.merged_commit is not None
    merged_commit = merge_result.merged_commit

    commits = list(merge_result.picked_commits)
    chosen_outcome = outcome if outcome else _format_default_outcome(len(commits))

    try:
        close_wave(
            state,
            wave_id=wave_id,
            outcome=chosen_outcome,
        )
    except LifecycleError as exc:
        # Close-time rejection is a validation-side failure — the wave
        # was active when we checked but raced into a terminal state
        # before close_wave ran. Surface the canonical exit code.
        raise cli_errors.ValidationError(str(exc)) from exc

    cleanup_result: CleanupResult | None = None
    worktree_cleaned = False
    if not keep_worktree:
        # The cleanup runs under the same state lock; the worktree-
        # registry lock is held by the caller (see worktree.py for the
        # always-registry-then-state ordering).
        cleanup_result = cleanup_worktree(
            state,
            repo_root=repo_root,
            wave_id=wave_id,
            force=False,
            keep_branch=False,
        )
        worktree_cleaned = True

    logger.info(
        f"wave_land wave={wave_id} commits={commits} outcome={chosen_outcome!r} "
        f"cleaned={worktree_cleaned}"
    )
    return WaveLandResult(
        wave_id=wave_id,
        commits=commits,
        outcome=chosen_outcome,
        worktree_cleaned=worktree_cleaned,
        merged_commit=merged_commit,
        cleanup=cleanup_result,
    )


def _candidate_waves_for_batch(state: State, iter_id: str | None) -> list[str]:
    """Return wave ids eligible for ``wave land-batch`` in dep order.

    Eligibility: status in ``{CLAIMED, IN_PROGRESS}``, has a
    ``worktree_id`` stamped, and (when *iter_id* is set) belongs to the
    named iter. The returned list is in topo (deps-first) order so the
    batch lands prerequisites before their dependents.
    """
    waves = state.waves
    candidate_set: set[str] = set()
    for wid, wave in waves.items():
        if wave.status not in {WaveStatus.CLAIMED, WaveStatus.IN_PROGRESS}:
            continue
        if wave.worktree_id is None:
            continue
        if iter_id is not None and wave.iter_id != iter_id:
            continue
        candidate_set.add(wid)

    # Topo-sort the candidates by their declared dep graph. Deps that
    # point outside the candidate set are ignored (already-closed deps
    # do not block the batch — they are exactly what the operator wants
    # to land on top of).
    in_degree: dict[str, int] = {}
    edges: dict[str, list[str]] = {wid: [] for wid in candidate_set}
    for wid in candidate_set:
        deps_in_set = [d for d in waves[wid].deps if d in candidate_set]
        in_degree[wid] = len(deps_in_set)
        for d in deps_in_set:
            edges[d].append(wid)

    ready = sorted([wid for wid, deg in in_degree.items() if deg == 0])
    ordered: list[str] = []
    while ready:
        nxt = ready.pop(0)
        ordered.append(nxt)
        for child in sorted(edges[nxt]):
            in_degree[child] -= 1
            if in_degree[child] == 0:
                # Maintain id-sorted ready queue for determinism.
                bisect.insort(ready, child)
    # Any nodes left have cycles — should never happen (plan_wave rejects
    # cycles) but append defensively in id order for stable output.
    leftover = sorted(wid for wid in candidate_set if wid not in ordered)
    ordered.extend(leftover)
    return ordered


@dataclass
class WaveLandBatchResult:
    """Return shape from :func:`wave_land_batch`.

    Attributes:
        landed: Ordered :class:`WaveLandResult` records — one per
            successfully landed wave.
        failed_wave: First wave that failed to land, or ``None`` when
            the batch completed cleanly.
        error: ``str(exc)`` for *failed_wave*, or ``None`` on success.
        skipped: Waves that were inspected but did not meet the
            eligibility filter (informational; populated only when
            ``ready_only`` is set).
    """

    landed: list[WaveLandResult]
    failed_wave: str | None
    error: str | None
    skipped: list[str]


def wave_land_batch(
    state: State,
    *,
    repo_root: Path,
    iter_id: str | None = None,
    ready_only: bool = False,
    keep_worktree: bool = False,
) -> WaveLandBatchResult:
    """Land every eligible wave in dep order; stop on first failure.

    Args:
        state: Mutated in place. Caller holds the state-side lock.
        repo_root: Repository root.
        iter_id: When set, scope the batch to waves in that iter.
        ready_only: When set, additionally require all declared deps to
            be ``CLOSED`` before landing. Useful when the operator wants
            to avoid landing waves whose dependencies have not yet
            completed (the unfiltered batch will *still* land them if
            the cherry-pick succeeds — declared deps and pickable
            commits are different concepts).
        keep_worktree: Forwarded to :func:`wave_land`.

    Returns:
        :class:`WaveLandBatchResult`. ``failed_wave`` and ``error`` are
        populated when the batch aborts mid-flight.
    """
    candidates = _candidate_waves_for_batch(state, iter_id)
    skipped: list[str] = []
    if ready_only:
        filtered: list[str] = []
        for wid in candidates:
            wave = state.waves[wid]
            deps_ok = all(
                state.waves[d].status == WaveStatus.CLOSED for d in wave.deps if d in state.waves
            )
            if deps_ok:
                filtered.append(wid)
            else:
                skipped.append(wid)
        candidates = filtered

    landed: list[WaveLandResult] = []
    for wid in candidates:
        try:
            result = wave_land(
                state,
                repo_root=repo_root,
                wave_id=wid,
                outcome=None,
                keep_worktree=keep_worktree,
            )
        except cli_errors.CliError as exc:
            logger.info(f"wave_land_batch stopping wave={wid} error={exc!s}")
            return WaveLandBatchResult(
                landed=landed,
                failed_wave=wid,
                error=str(exc),
                skipped=skipped,
            )
        landed.append(result)

    return WaveLandBatchResult(
        landed=landed,
        failed_wave=None,
        error=None,
        skipped=skipped,
    )


__all__ = [
    "WaveLandBatchResult",
    "WaveLandResult",
    "wave_land",
    "wave_land_batch",
]
