"""Archive local wave branch heads under ``refs/eawf/archive/<phase>/<wave>``.

Evidence rows and old ``Wave.commit`` pins cite pre-squash worktree SHAs
that live only on the per-wave branches (``feature/<symbol>-v<X.Y>-pNN-wMM``).
Pruning those branches, or re-pinning the waves to their ``main`` copies,
would leave the cited SHAs unreachable and eventually garbage-collected.
Writing each branch head to an archive ref first keeps them resolvable
without keeping the branches themselves.

The archive never rewrites history: a new ref is created, an existing ref is
advanced only when the old target is an ancestor of the new head, and any
other disagreement refuses the whole batch before a single ref is written.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from eawf.platform.subprocess_detach import detached_subprocess_kwargs
from eawf.workflow.lifecycle.wave_sha import _MAIN_REFS, _run_git

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

ARCHIVE_REF_PREFIX: str = "refs/eawf/archive"
_MISC_ARCHIVE_PREFIX: str = f"{ARCHIVE_REF_PREFIX}/misc"

# The per-wave worktree branch suffix; the long-running phase branch and
# harness branches carry no wave number, so they are never archived here.
_WAVE_BRANCH_RE = re.compile(r"-p(?P<phase>\d{2,})-w(?P<wave>\d{2,})$", re.IGNORECASE)
_FIELD_SEP = "\x1f"
# for-each-ref spells a hex byte as ``%1f``, unlike git log's ``%x1f``.
_FIELD_SEP_PLACEHOLDER = "%1f"
_REF_TIMEOUT_SECONDS: float = 20.0

# Branches a prune (or a misc archive sweep) never touches, regardless of
# archive state: the release branch and the npm/plugin publish branch.
_PROTECTED_BRANCHES = ("main", "plugins-dist")

# The long-running phase branch (``feature/<symbol>-v<X.Y>``, optionally
# phase-suffixed ``-pNN``) carries every cherry-picked wave until the phase
# PR merges, so a prune never deletes one even from another checkout.
_PHASE_BRANCH_RE = re.compile(r"^feature/[^/]+-v\d+(?:\.\d+)+(?:-p\d{2,})?$", re.IGNORECASE)

ArchiveOutcome = Literal["created", "advanced", "unchanged"]


class WaveArchiveError(RuntimeError):
    """Git could not list, read or write the archive refs."""


class WaveArchiveConflictError(WaveArchiveError):
    """An archive ref would have to move to a commit that does not descend from it.

    Attributes:
        conflicts: One ``(archive_ref, current_target, branch, branch_head)``
            tuple per refused ref; ``current_target`` is the other branch's
            head when two branches map to the same archive ref.
    """

    def __init__(self, conflicts: list[tuple[str, str, str, str]]) -> None:
        self.conflicts = conflicts
        detail = "; ".join(
            f"{ref} holds {current[:12]} but {branch} is at {head[:12]}"
            for ref, current, branch, head in conflicts
        )
        super().__init__(f"archive refused, no ref written: {detail}")


@dataclass(frozen=True)
class WaveBranch:
    """One local per-wave branch and the archive ref that preserves its head.

    Attributes:
        branch: Short branch name, e.g. ``feature/zz-v1.0-p28-w01``.
        head: The branch's current 40-hex commit.
        archive_ref: ``refs/eawf/archive/P28/W01`` for that branch.
    """

    branch: str
    head: str
    archive_ref: str


@dataclass(frozen=True)
class ArchiveEntry:
    """One archive ref as :func:`archive_wave_branches` left it.

    Attributes:
        wave_branch: The branch whose head the ref now preserves.
        outcome: ``created`` for a new ref, ``advanced`` for a fast-forward
            of an existing ref, ``unchanged`` when it already held the head.
        previous: The ref's target before the call (``None`` when created).
    """

    wave_branch: WaveBranch
    outcome: ArchiveOutcome
    previous: str | None = None


def archive_ref_for_branch(branch: str) -> str | None:
    """Return the archive ref for a per-wave branch name, or ``None`` if it is not one."""
    match = _WAVE_BRANCH_RE.search(branch)
    if match is None:
        return None
    return f"{ARCHIVE_REF_PREFIX}/P{match['phase']}/W{match['wave']}"


def misc_archive_ref_for_branch(branch: str) -> str:
    """Return the archive ref a non-wave branch is preserved under."""
    return f"{_MISC_ARCHIVE_PREFIX}/{branch}"


def _current_branch(*, repo_root: Path | None) -> str | None:
    """Return the checked-out branch's short name, or ``None`` when detached or unavailable."""
    out = _run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], repo_root=repo_root)
    if out is None or out.returncode != 0:
        return None
    name = out.stdout.strip()
    return name or None


def _refs(pattern: str, *, repo_root: Path | None) -> dict[str, str]:
    """Map every ref under *pattern* to its target; raise when git cannot list them."""
    fmt = f"%(refname){_FIELD_SEP_PLACEHOLDER}%(objectname)"
    out = _run_git(["for-each-ref", f"--format={fmt}", pattern], repo_root=repo_root)
    if out is None or out.returncode != 0:
        detail = out.stderr.strip() if out is not None else "git unavailable"
        raise WaveArchiveError(f"cannot list {pattern}: {detail}")
    refs: dict[str, str] = {}
    for line in out.stdout.splitlines():
        name, _, target = line.partition(_FIELD_SEP)
        if target:
            refs[name] = target
    return refs


def list_wave_branches(*, repo_root: Path | None = None) -> list[WaveBranch]:
    """Return every local per-wave branch with its head and archive ref, sorted by branch.

    Raises:
        WaveArchiveError: git is unavailable or *repo_root* is not a repository.
    """
    branches: list[WaveBranch] = []
    for refname, head in _refs("refs/heads", repo_root=repo_root).items():
        branch = refname.removeprefix("refs/heads/")
        archive_ref = archive_ref_for_branch(branch)
        if archive_ref is not None:
            branches.append(WaveBranch(branch=branch, head=head, archive_ref=archive_ref))
    return sorted(branches, key=lambda b: b.branch)


def list_misc_branches(*, repo_root: Path | None = None) -> list[WaveBranch]:
    """Return every local non-wave branch, minus ``main``/``plugins-dist``/the checked-out one.

    Covers the harness leftovers a wave-only sweep misses: 50+
    ``worktree-agent-<hex>`` branches, one-off ``p33-presquash*`` forks, and
    a stale long-running branch copy. Each maps to
    ``refs/eawf/archive/misc/<branch-name>`` via :func:`misc_archive_ref_for_branch`.

    Raises:
        WaveArchiveError: git is unavailable or *repo_root* is not a repository.
    """
    current = _current_branch(repo_root=repo_root)
    excluded = set(_PROTECTED_BRANCHES)
    if current is not None:
        excluded.add(current)
    branches: list[WaveBranch] = []
    for refname, head in _refs("refs/heads", repo_root=repo_root).items():
        branch = refname.removeprefix("refs/heads/")
        if branch in excluded or archive_ref_for_branch(branch) is not None:
            continue
        branches.append(
            WaveBranch(branch=branch, head=head, archive_ref=misc_archive_ref_for_branch(branch))
        )
    return sorted(branches, key=lambda b: b.branch)


def _is_ancestor(ancestor: str, descendant: str, *, repo_root: Path | None) -> bool:
    out = _run_git(["merge-base", "--is-ancestor", ancestor, descendant], repo_root=repo_root)
    if out is None or out.returncode not in (0, 1):
        raise WaveArchiveError(f"cannot compare {ancestor[:12]} with {descendant[:12]}")
    return out.returncode == 0


def archive_wave_branches(
    *, repo_root: Path | None = None, include_misc: bool = False
) -> list[ArchiveEntry]:
    """Write every local wave branch head to its archive ref, all or nothing.

    Idempotent: a second call reports every ref ``unchanged``. An existing
    ref is advanced only by fast-forward, so the commit it held stays
    reachable from the new target. A ref that would move sideways or
    backwards, or two branches that map to one ref at different heads,
    refuses the whole batch before anything is written.

    Args:
        repo_root: Repository working directory; defaults to the process cwd.
        include_misc: Also archive :func:`list_misc_branches` (non-wave
            harness leftovers) under ``refs/eawf/archive/misc/<branch>``.
            Off by default: misc archiving is a one-time hygiene sweep, not
            steady-state per-wave behaviour, and leaving it opt-in keeps
            every existing ``archive-refs`` caller (``verify-commits
            --repair``'s orphan check, CI, prior callers) working unchanged.

    Returns:
        One :class:`ArchiveEntry` per branch archived, sorted by branch name.

    Raises:
        WaveArchiveConflictError: At least one ref cannot be written safely.
        WaveArchiveError: git failed to list, compare or update refs.
    """
    branches = list_wave_branches(repo_root=repo_root)
    if include_misc:
        branches = sorted(
            [*branches, *list_misc_branches(repo_root=repo_root)], key=lambda b: b.branch
        )
    existing = _refs(ARCHIVE_REF_PREFIX, repo_root=repo_root)
    claimed: dict[str, WaveBranch] = {}
    conflicts: list[tuple[str, str, str, str]] = []
    entries: list[ArchiveEntry] = []
    for wave_branch in branches:
        ref, head = wave_branch.archive_ref, wave_branch.head
        twin = claimed.setdefault(ref, wave_branch)
        if twin.head != head:
            conflicts.append((ref, twin.head, wave_branch.branch, head))
            continue
        if twin is not wave_branch:
            entries.append(
                ArchiveEntry(wave_branch=wave_branch, outcome="unchanged", previous=head)
            )
            continue
        current = existing.get(ref)
        if current is None:
            entries.append(ArchiveEntry(wave_branch=wave_branch, outcome="created"))
        elif current == head:
            entries.append(
                ArchiveEntry(wave_branch=wave_branch, outcome="unchanged", previous=head)
            )
        elif _is_ancestor(current, head, repo_root=repo_root):
            entries.append(
                ArchiveEntry(wave_branch=wave_branch, outcome="advanced", previous=current)
            )
        else:
            conflicts.append((ref, current, wave_branch.branch, head))
    if conflicts:
        raise WaveArchiveConflictError(conflicts)

    # One ``update-ref --stdin`` batch is a single ref transaction, and each
    # line carries the expected old value, so a concurrent writer fails it
    # whole instead of being overwritten.
    commands = [
        f"create {e.wave_branch.archive_ref} {e.wave_branch.head}"
        if e.outcome == "created"
        else f"update {e.wave_branch.archive_ref} {e.wave_branch.head} {e.previous}"
        for e in entries
        if e.outcome != "unchanged"
    ]
    if commands:
        _write_refs(commands, repo_root=repo_root)
    logger.info(
        f"archive_wave_branches branches={len(branches)} written={len(commands)} "
        f"unchanged={len(entries) - len(commands)}"
    )
    return entries


def _run_git_mutation(
    args: list[str], *, repo_root: Path | None, input_text: str | None = None, label: str
) -> None:
    """Run one git command that changes repo state; raise :class:`WaveArchiveError` on failure."""
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(repo_root) if repo_root else None,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=_REF_TIMEOUT_SECONDS,
            check=False,
            **detached_subprocess_kwargs(),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise WaveArchiveError(f"{label} failed: {exc}") from exc
    if out.returncode != 0:
        raise WaveArchiveError(f"{label} failed: {out.stderr.strip()}")


def _write_refs(commands: list[str], *, repo_root: Path | None) -> None:
    _run_git_mutation(
        ["update-ref", "--stdin"],
        repo_root=repo_root,
        input_text="\n".join(commands) + "\n",
        label="archive ref write",
    )


def branches_orphaning(
    commits: Iterable[str], *, repo_root: Path | None = None
) -> list[WaveBranch]:
    """Return the unarchived wave branches that are the only refs holding any of *commits*.

    A commit reachable from ``main``, a release tag or an archive ref is safe
    to drop from a pin; one reachable only from a wave branch whose head is
    not archived would be lost at the next branch prune.

    Args:
        commits: Full commit SHAs a repair would stop citing.
        repo_root: Repository working directory; defaults to the process cwd.

    Returns:
        The at-risk branches, sorted by branch name (empty when none).

    Raises:
        WaveArchiveError: git failed to list refs or walk a branch.
    """
    wanted = set(commits)
    if not wanted:
        return []
    archived = _refs(ARCHIVE_REF_PREFIX, repo_root=repo_root)
    unarchived = [
        b for b in list_wave_branches(repo_root=repo_root) if archived.get(b.archive_ref) != b.head
    ]
    if not unarchived:
        return []
    keepers = [
        *archived,
        *(ref for pattern in _MAIN_REFS for ref in _refs(pattern, repo_root=repo_root)),
        *_refs("refs/tags/v*", repo_root=repo_root),
    ]
    at_risk: list[WaveBranch] = []
    for wave_branch in unarchived:
        out = _run_git(
            ["rev-list", wave_branch.head, "--not", *keepers],
            repo_root=repo_root,
            timeout=_REF_TIMEOUT_SECONDS,
        )
        if out is None or out.returncode != 0:
            raise WaveArchiveError(f"cannot walk {wave_branch.branch}")
        if wanted.intersection(out.stdout.split()):
            at_risk.append(wave_branch)
    return at_risk


# ---- Branch prune ------------------------------------------------------


@dataclass(frozen=True)
class PruneCandidate:
    """One local branch head selected for deletion by :func:`prune_branches`.

    Attributes:
        branch: Short branch name.
        head: Commit the branch currently points at.
        archive_ref: The archive ref that preserves *head* (the reason it
            is safe to delete the branch).
    """

    branch: str
    head: str
    archive_ref: str


@dataclass(frozen=True)
class PruneResult:
    """What one :func:`prune_branches` call deleted, skipped, or refused.

    Attributes:
        deleted: Branches removed (or, on *dry_run*, that would be).
        skipped: ``(branch, reason)`` pairs left alone on purpose.
        pruned_worktrees: Worktree paths whose stale registration was
            removed (or, on *dry_run*, that would be).
        dry_run: Whether this result previews a run rather than having
            performed one.
    """

    deleted: list[PruneCandidate]
    skipped: list[tuple[str, str]]
    pruned_worktrees: list[str]
    dry_run: bool


class WaveBranchMovedError(WaveArchiveError):
    """A selected branch no longer points at the head its archive ref preserves.

    Attributes:
        moved: The ``(branch, expected_head)`` pairs whose tip moved after
            selection; the delete transaction left every branch in place.
    """

    def __init__(self, moved: list[tuple[str, str]]) -> None:
        self.moved = moved
        names = ", ".join(f"{branch} (expected {head[:12]})" for branch, head in moved)
        super().__init__(f"prune refused, no branch deleted: branch tip moved: {names}")


class WaveBranchPruneRefusedError(WaveArchiveError):
    """A candidate branch head has no archive ref (or a mismatched one).

    Attributes:
        unarchived: The ``(branch, head)`` pairs that block the whole batch.
    """

    def __init__(self, unarchived: list[tuple[str, str]]) -> None:
        self.unarchived = unarchived
        names = ", ".join(f"{branch} ({head[:12]})" for branch, head in unarchived)
        super().__init__(
            f"prune refused, no branch deleted: {len(unarchived)} unarchived head(s): {names}"
        )


def _skip_reason(branch: str, *, current: str | None, checked_out: set[str]) -> str | None:
    """Return why *branch* is out of scope for prune, or ``None`` if it is a candidate."""
    if branch == current:
        return "checked out (current branch)"
    if branch in _PROTECTED_BRANCHES:
        return "protected"
    if _PHASE_BRANCH_RE.match(branch):
        return "phase branch"
    if branch in checked_out:
        return "checked out in a worktree"
    return None


def _worktree_entries(*, repo_root: Path | None) -> list[str]:
    out = _run_git(["worktree", "list", "--porcelain"], repo_root=repo_root)
    if out is None or out.returncode != 0:
        raise WaveArchiveError("cannot list worktrees")
    return out.stdout.splitlines()


def _worktree_checked_out_branches(*, repo_root: Path | None) -> set[str]:
    """Return every branch checked out in any worktree, including the current one."""
    return {
        line.removeprefix("branch ").strip().removeprefix("refs/heads/")
        for line in _worktree_entries(repo_root=repo_root)
        if line.startswith("branch ")
    }


def _prunable_worktrees(*, repo_root: Path | None) -> list[str]:
    """Return registered worktree paths whose directory no longer exists on disk."""
    return [
        path
        for path in (
            line.removeprefix("worktree ").strip()
            for line in _worktree_entries(repo_root=repo_root)
            if line.startswith("worktree ")
        )
        if path and not Path(path).exists()
    ]


def _delete_branches_at_expected_heads(
    candidates: list[PruneCandidate], *, repo_root: Path | None
) -> None:
    """Delete every candidate branch in one transaction guarded by its expected head.

    Each ``delete <ref> <old>`` line makes git refuse the whole batch when a
    branch moved after selection, so a commit made on it since archiving is
    never orphaned.

    Raises:
        WaveBranchMovedError: A candidate's tip no longer matches its head.
        WaveArchiveError: git refused the transaction for another reason.
    """
    commands = [f"delete refs/heads/{c.branch} {c.head}" for c in candidates]
    try:
        _run_git_mutation(
            ["update-ref", "--stdin"],
            repo_root=repo_root,
            input_text="\n".join(commands) + "\n",
            label="branch delete",
        )
    except WaveArchiveError:
        heads = _refs("refs/heads", repo_root=repo_root)
        moved = [
            (c.branch, c.head)
            for c in candidates
            if heads.get(f"refs/heads/{c.branch}", c.head) != c.head
        ]
        if moved:
            raise WaveBranchMovedError(moved) from None
        raise


def prune_branches(*, repo_root: Path | None = None, dry_run: bool = False) -> PruneResult:
    """Delete local branches whose head an archive ref already preserves.

    Selection walks every local branch except ``main``, ``plugins-dist``,
    any long-running phase branch (checked out or not), the checked-out
    branch, and any branch checked out in another worktree (those are
    reported in ``skipped``, never deleted). A selected branch's
    archive ref is the wave path (:func:`archive_ref_for_branch`) for a
    ``-pNN-wMM`` branch, else the misc path
    (:func:`misc_archive_ref_for_branch`). If ANY selected branch's archive
    ref is missing or points elsewhere, the whole batch is refused before a
    single branch is deleted -- the same all-or-nothing semantics
    :func:`archive_wave_branches` uses for writes. Also removes worktree
    registrations whose directory no longer exists on disk (the outcome
    ``git worktree prune`` produces). The deletes run as one ref
    transaction that names each branch's selected head, so a branch whose
    tip moves after selection refuses the batch instead of being deleted.

    Args:
        repo_root: Repository working directory; defaults to the process cwd.
        dry_run: Compute the same selection and worktree-prune list without
            deleting or pruning anything.

    Returns:
        A :class:`PruneResult` describing what happened, or -- on
        *dry_run* -- what would happen.

    Raises:
        WaveBranchPruneRefusedError: A selected branch has no matching
            archive ref.
        WaveBranchMovedError: A selected branch moved before the delete.
        WaveArchiveError: git failed to list refs, list worktrees, delete a
            branch, or prune a worktree.
    """
    archived = _refs(ARCHIVE_REF_PREFIX, repo_root=repo_root)
    checked_out = _worktree_checked_out_branches(repo_root=repo_root)
    current = _current_branch(repo_root=repo_root)

    candidates: list[PruneCandidate] = []
    skipped: list[tuple[str, str]] = []
    unarchived: list[tuple[str, str]] = []
    heads = _refs("refs/heads", repo_root=repo_root)
    for refname in sorted(heads):
        branch, head = refname.removeprefix("refs/heads/"), heads[refname]
        reason = _skip_reason(branch, current=current, checked_out=checked_out)
        if reason is not None:
            skipped.append((branch, reason))
            continue
        archive_ref = archive_ref_for_branch(branch) or misc_archive_ref_for_branch(branch)
        if archived.get(archive_ref) != head:
            unarchived.append((branch, head))
            continue
        candidates.append(PruneCandidate(branch=branch, head=head, archive_ref=archive_ref))
    if unarchived:
        raise WaveBranchPruneRefusedError(unarchived)

    prunable = _prunable_worktrees(repo_root=repo_root)
    if dry_run:
        return PruneResult(
            deleted=candidates, skipped=skipped, pruned_worktrees=prunable, dry_run=True
        )

    if candidates:
        _delete_branches_at_expected_heads(candidates, repo_root=repo_root)
    if prunable:
        _run_git_mutation(["worktree", "prune"], repo_root=repo_root, label="worktree prune")
    logger.info(
        f"prune_branches deleted={len(candidates)} skipped={len(skipped)} "
        f"pruned_worktrees={len(prunable)}"
    )
    return PruneResult(
        deleted=candidates, skipped=skipped, pruned_worktrees=prunable, dry_run=False
    )
