"""``wave_autoland`` -- bring already-closed waves' commits home in dep order.

This is the *land back-half*: the dispatch front-half
(``wave dispatch`` / ``dispatch-batch``) spawns worktree-isolated
executors on per-wave branches; those executors close their wave inside
the worktree (recording a commit SHA as evidence) but their commits do
not yet live on the parent feature branch. ``wave_autoland`` brings the
commit set home.

It differs from :func:`eawf.runtime.worktree.wave_land.wave_land_batch`
in *which* waves it touches. ``wave_land_batch`` lands *and closes*
CLAIMED / IN_PROGRESS waves (the close happens as part of the land).
``wave_autoland`` lands waves that are *already* CLOSED -- the close
already ran inside the worktree -- so it only replays commits and tears
the worktree down; it never drives :func:`close_wave`.

The flow:

1. Resolve the *landable* set -- CLOSED waves with a worktree branch
   still carrying un-landed commits (worktree record still ACTIVE).
2. Topologically sort by ``Wave.deps`` so a dep lands before its
   dependents; within one dep frontier, order by wave id ascending.
3. Cherry-pick each wave's commits onto the parent branch via
   :func:`eawf.runtime.worktree.merge_back.merge_back` -- never a
   reimplemented cherry-pick.
4. Stop on the first conflict: leave the repo in the conflicted state
   for the operator, and report the failing wave plus the waves that
   were not reached.
5. After a clean land of a wave, tear the worktree down via
   :func:`eawf.runtime.worktree.cleanup.cleanup_worktree` -- mirroring
   the post-land cleanup that :func:`wave_land` performs -- unless
   ``keep_worktree`` is set.

The function mutates the supplied :class:`State` in place. Caller holds
``portalock(state.json)`` and the worktree-registry lock.
"""

from __future__ import annotations

import bisect
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import eawf.runtime.worktree.git as git
from eawf.kernel.state.enums import WaveStatus, WorktreeStatus
from eawf.kernel.state.models import State, Wave
from eawf.runtime.worktree.cleanup import CleanupResult, cleanup_worktree
from eawf.runtime.worktree.merge_back import (
    STRATEGY_CHERRY_PICK,
    MergeBackResult,
    merge_back,
)

logger = logging.getLogger(__name__)


@dataclass
class WaveAutolandRow:
    """One landed wave in a :class:`WaveAutolandResult`.

    Attributes:
        wave_id: The wave whose worktree commits were replayed.
        commits: Parent-branch SHAs produced by the cherry-pick, oldest
            first. Empty when the worktree had nothing beyond the
            merge-base.
        merged_commit: Parent-branch HEAD after this wave landed.
        worktree_cleaned: ``True`` iff the post-land cleanup ran.
    """

    wave_id: str
    commits: list[str]
    merged_commit: str
    worktree_cleaned: bool


@dataclass
class WaveAutolandResult:
    """Return shape from :func:`wave_autoland`.

    Attributes:
        order: The planned land order (dep-sorted wave ids). Populated on
            every call, including ``dry_run``.
        landed: One :class:`WaveAutolandRow` per successfully landed
            wave, in land order.
        failed_wave: First wave that hit a cherry-pick conflict, or
            ``None`` when every wave landed (or ``dry_run`` short-circuited
            before any land).
        error: Conflict detail for *failed_wave*, or ``None``.
        remaining: Landable waves not reached because the land stopped --
            *failed_wave* plus every wave after it in *order*. Empty on a
            clean land.
        dry_run: Echoes the call's ``dry_run`` flag.
    """

    order: list[str]
    landed: list[WaveAutolandRow]
    failed_wave: str | None
    error: str | None
    remaining: list[str]
    dry_run: bool


def _resolve_iter_id(state: State, iter_id: str | None) -> str | None:
    """Return the iter scope for autoland.

    When *iter_id* is given it is used verbatim. Otherwise the current
    iter pointer (``state.current.iter_id``) scopes the land. ``None``
    (no explicit iter and no current pointer) means *every* iter is in
    scope.
    """
    if iter_id is not None:
        return iter_id
    return state.current.iter_id


def _has_unlanded_commits(repo_root: Path, *, base_branch: str, branch: str) -> bool:
    """Return ``True`` iff *branch* carries commits not in *base_branch*.

    A landable wave needs commits to replay; a worktree whose branch is
    already an ancestor of the parent (nothing to pick) is skipped so a
    repeat autoland is a no-op rather than a churn of empty merges.

    Raises:
        UserError: When the range endpoints do not resolve (propagated
            from :func:`git.rev_list`).
    """
    range_spec = f"{base_branch}..{branch}"
    return bool(git.rev_list(repo_root, range_spec=range_spec))


def _landable_waves(state: State, *, repo_root: Path, iter_id: str | None) -> list[str]:
    """Return landable wave ids in dependency order.

    Landable means: status CLOSED, a ``worktree_id`` is stamped, the
    referenced worktree record exists and is still ACTIVE (not already
    MERGED / CONFLICTED / ABANDONED), and the worktree branch carries
    commits the parent branch does not. When *iter_id* is set, only
    waves in that iter qualify.

    The returned list is topologically sorted: a wave's in-set deps land
    before it, and ties within a dep frontier break by wave id ascending
    (stable, deterministic order).
    """
    waves = state.waves
    worktrees = state.worktrees or {}
    candidate_set: set[str] = set()
    for wid, wave in waves.items():
        if wave.status != WaveStatus.CLOSED:
            continue
        if wave.worktree_id is None:
            continue
        if iter_id is not None and wave.iter_id != iter_id:
            continue
        record = worktrees.get(wave.worktree_id)
        if record is None or record.status != WorktreeStatus.ACTIVE:
            continue
        if not _has_unlanded_commits(
            repo_root, base_branch=record.base_branch, branch=record.branch
        ):
            continue
        candidate_set.add(wid)

    return _topo_sort(waves, candidate_set)


def _topo_sort(waves: Mapping[str, Wave], candidate_set: set[str]) -> list[str]:
    """Topo-sort *candidate_set* by ``Wave.deps``; ties by id ascending.

    Deps that point outside *candidate_set* are ignored -- they are
    already landed (or not in scope) and must not block the frontier.
    """
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
                bisect.insort(ready, child)
    # Cycles cannot occur (plan_wave rejects them); append any leftover
    # in id order so the output stays stable even if the invariant breaks.
    leftover = sorted(wid for wid in candidate_set if wid not in ordered)
    ordered.extend(leftover)
    return ordered


def _conflict_detail(wave_id: str, merge_result: MergeBackResult) -> str:
    """Return the operator-facing repair hint for a stopped autoland."""
    files = list(merge_result.conflict_files)
    return (
        f"cherry-pick conflict landing wave {wave_id!r}: "
        f"files={files} conflict_commit={merge_result.conflict_commit}; "
        f"resolve in the parent worktree then run "
        f"`eawf worktree merge-back --wave {wave_id} --continue` "
        f"and re-run `eawf wave autoland`"
    )


def wave_autoland(
    state: State,
    *,
    repo_root: Path,
    iter_id: str | None = None,
    keep_worktree: bool = False,
    dry_run: bool = False,
) -> WaveAutolandResult:
    """Cherry-pick closed waves' worktree commits home in dependency order.

    Args:
        state: Mutated in place. Caller holds ``portalock(state.json)``
            and the worktree-registry lock.
        repo_root: Repository root (the parent worktree's working dir).
        iter_id: When set, scope the land to waves in that iter. When
            ``None``, fall back to ``state.current.iter_id``; when that is
            also ``None``, every iter is in scope.
        keep_worktree: When ``True``, skip the post-land worktree
            teardown for each landed wave.
        dry_run: When ``True``, compute and return the planned land order
            without cherry-picking anything. ``landed`` is empty and the
            repo is untouched.

    Returns:
        A :class:`WaveAutolandResult`. On a clean land ``failed_wave`` is
        ``None`` and ``remaining`` is empty; on a stopped land they name
        the failing wave and the un-landed tail.

    Raises:
        UserError: When a worktree range endpoint does not resolve while
            computing the landable set (``kind="InvalidInput"``).
    """
    scope_iter = _resolve_iter_id(state, iter_id)
    order = _landable_waves(state, repo_root=repo_root, iter_id=scope_iter)

    if dry_run:
        logger.info(f"wave_autoland dry_run=True iter={scope_iter} order={order}")
        return WaveAutolandResult(
            order=order,
            landed=[],
            failed_wave=None,
            error=None,
            remaining=[],
            dry_run=True,
        )

    landed: list[WaveAutolandRow] = []
    for idx, wid in enumerate(order):
        merge_result = merge_back(
            state,
            repo_root=repo_root,
            wave_id=wid,
            strategy=STRATEGY_CHERRY_PICK,
        )
        if merge_result.conflicted:
            # merge_back already marked the record CONFLICTED and left the
            # on-disk cherry-pick mid-flight for the operator to resolve.
            remaining = order[idx:]
            detail = _conflict_detail(wid, merge_result)
            logger.info(f"wave_autoland stopping wave={wid} remaining={remaining}")
            return WaveAutolandResult(
                order=order,
                landed=landed,
                failed_wave=wid,
                error=detail,
                remaining=remaining,
                dry_run=False,
            )

        assert merge_result.merged_commit is not None
        commits = list(merge_result.picked_commits)
        cleanup_result: CleanupResult | None = None
        if not keep_worktree:
            cleanup_result = cleanup_worktree(
                state,
                repo_root=repo_root,
                wave_id=wid,
                force=False,
                keep_branch=False,
            )
        landed.append(
            WaveAutolandRow(
                wave_id=wid,
                commits=commits,
                merged_commit=merge_result.merged_commit,
                worktree_cleaned=cleanup_result is not None,
            )
        )

    logger.info(f"wave_autoland iter={scope_iter} landed={[row.wave_id for row in landed]}")
    return WaveAutolandResult(
        order=order,
        landed=landed,
        failed_wave=None,
        error=None,
        remaining=[],
        dry_run=False,
    )


__all__ = [
    "WaveAutolandResult",
    "WaveAutolandRow",
    "wave_autoland",
]
