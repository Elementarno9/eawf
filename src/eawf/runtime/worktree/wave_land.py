"""``wave_land`` — wave-centric cherry-pick automation (B027).

This module layers a wave-centric automation on top of the
worktree-centric :func:`eawf.runtime.worktree.merge_back.merge_back`. It is the
wave-lifecycle counterpart to ``eawf worktree merge-back``:

- Look up the wave's worktree.
- Replay the worktree commits onto the parent branch via
  *cherry-pick only* (AGENTS.md rule 11 names cherry-pick as the
  discipline; ``rebase_then_ff`` is intentionally not exposed here).
- Drive the :func:`eawf.workflow.lifecycle.transitions.close_wave` transition with
  the resulting commit SHA as evidence.
- Optionally clean up the worktree directory + branch.

On conflict the function refuses to close the wave and surfaces a
:class:`~eawf.surfaces.cli.errors.StateConflict` (``kind="IntegrityViolation"``)
with a repair hint. The on-disk repo state (``.git/CHERRY_PICK_HEAD``) is
preserved so the operator can resolve and re-run.

The function mutates the supplied :class:`State` in place. Caller holds
``portalock(state.json)`` (via :func:`state_transaction`) and the
worktree-registry lock (when applicable).
"""

from __future__ import annotations

import bisect
import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path

import eawf.runtime.worktree.git as git
from eawf.kernel.state.enums import WaveIntegrationKind, WaveStatus, WorktreeStatus
from eawf.kernel.state.models import State, WaveIntegration
from eawf.kernel.store.paths import store_dir as _store_dir
from eawf.runtime.worktree.cleanup import CleanupResult, cleanup_worktree
from eawf.runtime.worktree.merge_back import (
    STRATEGY_CHERRY_PICK,
    MergeBackResult,
    merge_back,
)
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.scope import resolve_state_path
from eawf.workflow.lifecycle.integration import (
    DependencyBarrierError,
    DependencyEvaluation,
    bind_start_dependencies,
    create_wave_integration,
    digest_wave_contract,
    evaluate_dependency_barriers,
    latest_wave_integration,
    require_land_dependencies,
)
from eawf.workflow.lifecycle.transitions import LifecycleError, close_wave
from eawf.workflow.verify import compute as compute_readiness
from eawf.workflow.verify.models import CloseReadiness

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
        closed: ``True`` iff :func:`close_wave` ran. A landed wave can
            remain open when close-readiness computes ``ready=False``.
        worktree_cleaned: ``True`` iff :func:`cleanup_worktree` ran
            successfully after the close.
        merged_commit: HEAD of the parent branch post-merge — same as the
            last entry of *commits* when *commits* is non-empty; otherwise
            the pre-call HEAD.
        integration_id: Immutable integration-generation id recorded before
            close verification.
        cleanup: The :class:`CleanupResult` from
            :func:`cleanup_worktree`, or ``None`` when cleanup was
            skipped via ``keep_worktree=True``.
    """

    wave_id: str
    commits: list[str]
    outcome: str
    closed: bool
    worktree_cleaned: bool
    merged_commit: str
    integration_id: str | None
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


def _merged_record_result(
    state: State,
    *,
    repo_root: Path,
    wave_id: str,
    candidate_sha: str,
    previous_integration: WaveIntegration | None,
) -> MergeBackResult | None:
    """Reuse an exact prior merge or replay only newer repair commits."""
    wave = state.waves[wave_id]
    if state.worktrees is None or wave.worktree_id is None:
        return None
    record = state.worktrees.get(wave.worktree_id)
    if record is None or record.status != WorktreeStatus.MERGED:
        return None
    if previous_integration is not None and candidate_sha != previous_integration.candidate_sha:
        commits = git.rev_list(
            repo_root,
            range_spec=f"{previous_integration.candidate_sha}..{candidate_sha}",
        )
        picked: list[str] = []
        for sha in commits:
            clean, _detail = git.cherry_pick(repo_root, sha=sha)
            if not clean:
                record.status = WorktreeStatus.CONFLICTED
                record.merged_commit = None
                return MergeBackResult(
                    record=record,
                    strategy=STRATEGY_CHERRY_PICK,
                    picked_commits=picked,
                    target_branch=record.base_branch,
                    merged_commit=None,
                    conflicted=True,
                    conflict_files=[],
                    conflict_commit=sha,
                )
            picked.append(git.head_sha(repo_root))
        merged_commit = git.head_sha(repo_root)
        record.merged_commit = merged_commit
        logger.info(f"wave_land wave={wave_id} replay_repair_commits={picked} record={record.id}")
        return MergeBackResult(
            record=record,
            strategy=STRATEGY_CHERRY_PICK,
            picked_commits=picked,
            target_branch=record.base_branch,
            merged_commit=merged_commit,
            conflicted=False,
            conflict_files=[],
            conflict_commit=None,
        )
    merged_commit = record.merged_commit or git.head_sha(repo_root)
    logger.info(f"wave_land wave={wave_id} reuse_merged_record={record.id}")
    return MergeBackResult(
        record=record,
        strategy=STRATEGY_CHERRY_PICK,
        picked_commits=[],
        target_branch=record.base_branch,
        merged_commit=merged_commit,
        conflicted=False,
        conflict_files=[],
        conflict_commit=None,
    )


def _log_readiness_advisory(wave_id: str, readiness: CloseReadiness) -> None:
    """Log non-passing criterion rows from close-readiness."""
    for view in readiness.criteria:
        if view.status != "pass":
            logger.warning(
                f"close_advisory wave={wave_id!r} criterion={view.id!r} status={view.status!r}"
            )


def _compute_close_readiness(
    state: State,
    *,
    repo_root: Path,
    wave_id: str,
) -> CloseReadiness | None:
    """Compute close-readiness for auto-close gating.

    ``None`` means the readiness store could not be resolved; callers
    keep the legacy non-blocking close behaviour in that case.
    """
    try:
        state_path = resolve_state_path(repo_root)
        readiness = compute_readiness(
            wave_id,
            state=state,
            store_dir=_store_dir(state_path),
            repo_root=repo_root,
        )
    except (FileNotFoundError, KeyError) as exc:
        logger.warning(f"close_advisory wave={wave_id!r} status='skip' err={exc!s}")
        return None
    _log_readiness_advisory(wave_id, readiness)
    return readiness


def wave_land(
    state: State,
    *,
    repo_root: Path,
    wave_id: str,
    outcome: str | None = None,
    keep_worktree: bool = False,
    defer_close: bool = False,
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
        defer_close: When ``True``, stop after recording the immutable
            integration fact. The daemon submits durable close work after the
            integration transaction commits.

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
    try:
        require_land_dependencies(state, wave_id=wave_id)
    except DependencyBarrierError as exc:
        raise cli_errors.ValidationError(str(exc)) from exc

    existing_integration = latest_wave_integration(state, wave_id)
    wave = state.waves[wave_id]
    if state.worktrees is None or wave.worktree_id is None:
        raise cli_errors.UserError(
            f"wave {wave_id!r} has no worktree record to integrate",
            kind="NotFound",
        )
    record = state.worktrees.get(wave.worktree_id)
    if record is None:
        raise cli_errors.UserError(
            f"wave {wave_id!r} references unknown worktree {wave.worktree_id!r}",
            kind="NotFound",
        )
    if git.branch_exists(repo_root, record.branch):
        candidate_sha = git.commit_sha(repo_root, record.branch)
    elif existing_integration is not None and record.status is WorktreeStatus.MERGED:
        candidate_sha = git.commit_sha(repo_root, existing_integration.candidate_sha)
    else:
        raise cli_errors.UserError(
            f"worktree branch {record.branch!r} does not exist",
            kind="NotFound",
        )
    current_head = git.commit_sha(repo_root, "HEAD")
    prior_merge_matches = (
        existing_integration is not None
        and record.status is WorktreeStatus.MERGED
        and record.merged_commit is not None
        and git.commit_sha(repo_root, record.merged_commit) == existing_integration.integrated_sha
        and candidate_sha == existing_integration.candidate_sha
    )
    base_sha = (
        git.commit_sha(repo_root, existing_integration.base_sha)
        if prior_merge_matches and existing_integration is not None
        else current_head
    )

    # A prior ``wave land`` may have landed commits but skipped close
    # because readiness was not ready. Re-running should re-check
    # readiness and close, not cherry-pick the same source branch again.
    merge_result = _merged_record_result(
        state,
        repo_root=repo_root,
        wave_id=wave_id,
        candidate_sha=candidate_sha,
        previous_integration=existing_integration,
    )
    if merge_result is None:
        # merge_back will raise UserError (kind="NotFound") when the
        # worktree record is missing (e.g., wave was never claimed via
        # `worktree create`).
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
    merged_commit = git.commit_sha(repo_root, merge_result.merged_commit)

    integration = create_wave_integration(
        state,
        wave_id=wave_id,
        base_sha=base_sha,
        candidate_sha=candidate_sha,
        integrated_sha=merged_commit,
        tree_sha=git.tree_sha(repo_root, merged_commit),
        diff_digest=git.diff_digest(
            repo_root,
            base_sha=base_sha,
            head_sha=merged_commit,
        ),
        spec_digest=digest_wave_contract(state, wave_id=wave_id),
        kind=WaveIntegrationKind.LAND,
    )

    commits = list(merge_result.picked_commits)
    chosen_outcome = outcome if outcome else _format_default_outcome(len(commits))

    readiness = (
        None
        if defer_close
        else _compute_close_readiness(state, repo_root=repo_root, wave_id=wave_id)
    )
    closed = not defer_close and (readiness is None or readiness.ready)
    if closed:
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
    else:
        logger.info(f"wave_land wave={wave_id} readiness_ready=False closed=False")

    cleanup_result: CleanupResult | None = None
    worktree_cleaned = False
    if (closed or defer_close) and not keep_worktree:
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
        f"closed={closed} cleaned={worktree_cleaned}"
    )
    return WaveLandResult(
        wave_id=wave_id,
        commits=commits,
        outcome=chosen_outcome,
        closed=closed,
        worktree_cleaned=worktree_cleaned,
        merged_commit=merged_commit,
        integration_id=integration.id,
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
        barrier_requirements: Required dependency stages for waves skipped or
            stopped at a configured land barrier.
    """

    landed: list[WaveLandResult]
    failed_wave: str | None
    error: str | None
    skipped: list[str]
    barrier_requirements: dict[str, tuple[str, ...]]


def _required_dependency_stages(evaluation: DependencyEvaluation) -> tuple[str, ...]:
    """Return stable stage labels from one blocked dependency evaluation."""
    stages = [item.rsplit(":", 1)[-1] for item in evaluation.unmet]
    if evaluation.stale:
        stages.append("fresh-integration")
    return tuple(dict.fromkeys(stages))


def wave_land_batch(
    state: State,
    *,
    repo_root: Path,
    iter_id: str | None = None,
    ready_only: bool = False,
    keep_worktree: bool = False,
    defer_close: bool = False,
) -> WaveLandBatchResult:
    """Land every eligible wave in dep order; stop on first failure.

    Args:
        state: Mutated in place. Caller holds the state-side lock.
        repo_root: Repository root.
        iter_id: When set, scope the batch to waves in that iter.
        ready_only: When set, skip waves whose configured land dependency
            barriers are not satisfied.
        keep_worktree: Forwarded to :func:`wave_land`.
        defer_close: Forwarded to :func:`wave_land`. Daemon callers set this
            so the batch records integrations only and submits durable close
            attempts after the integration transaction commits.

    Returns:
        :class:`WaveLandBatchResult`. ``failed_wave`` and ``error`` are
        populated when the batch aborts mid-flight.
    """
    candidates = _candidate_waves_for_batch(state, iter_id)
    skipped: list[str] = []
    barrier_requirements: dict[str, tuple[str, ...]] = {}
    landed: list[WaveLandResult] = []
    for wid in candidates:
        # Land evaluation below names any configured threshold that remains
        # blocked. A relaxed integrated edge may become bindable after its
        # upstream wave lands earlier in this same batch.
        with contextlib.suppress(DependencyBarrierError):
            bind_start_dependencies(state, wave_id=wid)
        evaluation = evaluate_dependency_barriers(state, wave_id=wid, for_land=True)
        if not evaluation.satisfied:
            detail = [*evaluation.unmet, *evaluation.stale]
            barrier_requirements[wid] = _required_dependency_stages(evaluation)
            error = f"wave {wid!r} land barrier blocked: {detail}"
            if ready_only:
                skipped.append(wid)
                logger.info(f"wave_land_batch action=skip wave={wid} error={error}")
                continue
            logger.info(f"wave_land_batch action=stop wave={wid} error={error}")
            return WaveLandBatchResult(
                landed=landed,
                failed_wave=wid,
                error=error,
                skipped=skipped,
                barrier_requirements=barrier_requirements,
            )
        try:
            result = wave_land(
                state,
                repo_root=repo_root,
                wave_id=wid,
                outcome=None,
                keep_worktree=keep_worktree,
                defer_close=defer_close,
            )
        except cli_errors.CliError as exc:
            logger.info(f"wave_land_batch action=stop wave={wid} error={exc!s}")
            return WaveLandBatchResult(
                landed=landed,
                failed_wave=wid,
                error=str(exc),
                skipped=skipped,
                barrier_requirements=barrier_requirements,
            )
        landed.append(result)
        if not defer_close and not result.closed:
            error = "close-readiness not ready; wave left open after landing commits"
            logger.info(f"wave_land_batch action=stop wave={wid} error={error}")
            return WaveLandBatchResult(
                landed=landed,
                failed_wave=wid,
                error=error,
                skipped=skipped,
                barrier_requirements=barrier_requirements,
            )

    return WaveLandBatchResult(
        landed=landed,
        failed_wave=None,
        error=None,
        skipped=skipped,
        barrier_requirements=barrier_requirements,
    )


__all__ = [
    "WaveLandBatchResult",
    "WaveLandResult",
    "wave_land",
    "wave_land_batch",
]
