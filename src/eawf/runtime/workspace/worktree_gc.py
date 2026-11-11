"""Find the linked worktrees of a repository and the ones a garbage collection may reap.

Task and scratch worktrees live under ``<main checkout>/.ea/worktrees/``. A tree
is *landed* when it is clean, holds no ignored file beyond rebuildable caches,
and every commit of its own has an equivalent patch on the integration branch,
so removing it loses nothing; the dry run lists those, the other reap
candidates and their apparent bytes, and prints the exact ``git worktree
remove`` line per landed tree for the operator. Nothing here removes, moves or
writes anything: reaping waits for the registry that records what eawf created.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal

from eawf.platform.subprocess_detach import no_window_kwargs
from eawf.runtime.vcs.branch_name import checked_out_branch

#: Where Task and scratch worktrees live, relative to the main checkout.
WORKTREES_ROOT: Final = Path(".ea") / "worktrees"

#: How long a detached scratch tree may sit idle before it expires.
SCRATCH_TTL: Final = timedelta(hours=24)

#: The branch a Milestone branch lands on when the main checkout names none.
FALLBACK_INTEGRATION: Final = "dev"

#: The last activity of a tree whose directory is gone: none to speak of.
_EPOCH: Final = datetime.fromtimestamp(0, tz=UTC)

#: Each git read is local and small; a stalled mount must not hold the dry run.
_GIT_TIMEOUT_SECONDS: Final = 30.0

GcReason = Literal["landed", "expired", "prunable", "unregistered", "leftover"]


class NotARepositoryError(ValueError):
    """The directory is not the main checkout of a git repository."""


@dataclass(frozen=True)
class WorktreeRow:
    """One linked worktree as git registers it.

    Attributes:
        path: The worktree directory, resolved.
        branch: The checked-out short branch name; ``None`` when detached.
        locked: ``git worktree lock`` holds it.
        prunable: git reports its directory gone.
        dirty: ``git status --porcelain`` lists a change or an untracked file.
        landed: Every commit of its own has an equivalent patch on the
            integration branch; a tree with no own commits is landed.
        last_activity: The later modification time of its index and ``HEAD``.
        size_bytes: Apparent bytes of the files under it, links not followed.
        trailer_seen: An unlanded own commit's ``Task:`` trailer is on the
            integration branch: a hint that its work may have landed squashed.
        ignored_files: Ignored entries a removal would lose, rebuildable
            caches left out.
        ignored_bytes: Their apparent bytes.
    """

    path: Path
    branch: str | None
    locked: bool
    prunable: bool
    dirty: bool
    landed: bool
    last_activity: datetime
    size_bytes: int
    trailer_seen: bool = False
    ignored_files: int = 0
    ignored_bytes: int = 0


@dataclass(frozen=True)
class GcCandidate:
    """An entry under the worktrees root that a garbage collection may reap.

    Attributes:
        path: The entry, resolved.
        reason: Why it may go.
        size_bytes: Its apparent bytes.
        remove_line: The command that removes a landed tree; ``None`` otherwise.
    """

    path: Path
    reason: GcReason
    size_bytes: int
    remove_line: str | None = None


@dataclass(frozen=True)
class KeptTree:
    """A tree under the worktrees root that a garbage collection keeps.

    Attributes:
        path: The tree, resolved.
        flags: Why it stays: ``dirty``, ``unlanded``, ``ignored``, ``locked``
            or ``scratch``, plus the hint ``trailer-landed`` beside ``unlanded``.
        ignored_files: Ignored entries a removal would lose.
        ignored_bytes: Their apparent bytes.
    """

    path: Path
    flags: tuple[str, ...]
    ignored_files: int = 0
    ignored_bytes: int = 0


@dataclass(frozen=True)
class GcPlan:
    """What a garbage collection of one repository would reap and keep.

    Attributes:
        integration: The branch own commits are judged landed on; ``None``
            when the main checkout is detached and no ``dev`` branch exists.
        candidates: Entries that may go, in path order.
        kept: Trees under the root that stay, each with why.
    """

    integration: str | None
    candidates: list[GcCandidate] = field(default_factory=list)
    kept: list[KeptTree] = field(default_factory=list)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a read-only ``git`` command in *cwd* without taking optional locks."""
    return subprocess.run(
        ["git", "--no-optional-locks", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        check=False,
        **no_window_kwargs(),
    )


def apparent_bytes(path: Path) -> int:
    """Return the apparent bytes of *path*, summed over its tree without following links.

    Args:
        path: A file or directory, which need not exist.

    Returns:
        The summed ``st_size`` of every non-directory entry; 0 when *path* is gone.
    """
    try:
        if not path.is_dir(follow_symlinks=False):
            return path.lstat().st_size
    except OSError:
        return 0
    total = 0
    for directory, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(directory, name)).st_size
            except OSError:
                continue
    return total


def integration_branch(main_root: Path) -> str | None:
    """Return the branch a tree's own commits land on.

    The main checkout holds the Milestone branch while a Milestone runs, so
    its branch is the integration branch; a detached main checkout falls back
    to ``dev`` when that branch exists.

    Args:
        main_root: The main checkout of the repository.

    Returns:
        The short branch name, or ``None`` when neither exists.
    """
    branch = checked_out_branch(main_root)
    if branch is not None:
        return branch
    probe = _git(
        main_root, "rev-parse", "--verify", "--quiet", f"refs/heads/{FALLBACK_INTEGRATION}"
    )
    return FALLBACK_INTEGRATION if probe.returncode == 0 else None


def _landed(tree: Path, integration: str | None) -> tuple[bool, bool]:
    """Return whether every own commit of *tree* reached *integration*, and the trailer hint.

    An own commit is landed only when ``git cherry`` marks it ``-``: an
    equivalent patch is on the integration branch since the fork point. A
    ``Task:`` trailer seen there proves nothing about this tree's other
    commits, so it is only reported as a hint.

    Returns:
        ``(landed, trailer_seen)``: ``landed`` is true when every own commit
        has an equivalent patch upstream, or there is no own commit;
        ``trailer_seen`` is true when some unlanded own commit's ``Task:``
        trailer appears on the integration branch.
    """
    if integration is None:
        return False, False
    fork = _git(tree, "merge-base", "HEAD", integration)
    if fork.returncode != 0:
        return False, False
    base = fork.stdout.strip()
    cherry = _git(tree, "cherry", integration, "HEAD", base)
    if cherry.returncode != 0:
        return False, False
    unlanded = [line[2:] for line in cherry.stdout.splitlines() if not line.startswith("- ")]
    if not unlanded:
        return True, False
    trailer = "%(trailers:key=Task,valueonly,separator=%x2C)"
    upstream = _git(tree, "log", f"--format={trailer}", f"{base}..{integration}")
    landed_keys = {key.strip() for line in upstream.stdout.splitlines() for key in line.split(",")}
    landed_keys.discard("")
    own = _git(tree, "log", "--no-walk", f"--format={trailer}", *unlanded)
    own_keys = {key.strip() for line in own.stdout.splitlines() for key in line.split(",")}
    return False, bool(landed_keys & own_keys)


#: Ignored entries a removal may take without loss: rebuildable environments
#: and tool caches, matched by any path component.
REBUILDABLE: Final = frozenset(
    {".venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
)


def _ignored(tree: Path) -> tuple[int, int]:
    """Return how many ignored entries of *tree* a removal would lose, and their bytes.

    ``git worktree remove`` deletes ignored files with the tree, so local work
    that only ``.gitignore`` hides would go too; :data:`REBUILDABLE` entries do
    not count. git lists each path an ignore pattern matches, a matched
    directory as one entry, so a cache nested in an otherwise empty directory
    is still named by its own component.
    """
    listed = _git(tree, "status", "--porcelain", "--ignored=matching", "-z")
    entries = [
        entry[3:]
        for entry in listed.stdout.split("\0")
        if entry.startswith("!! ") and not REBUILDABLE & set(Path(entry[3:]).parts)
    ]
    return len(entries), sum(apparent_bytes(tree / entry) for entry in entries)


def _last_activity(tree: Path) -> datetime:
    """Return the later modification time of *tree*'s index and ``HEAD``."""
    git_dir = _git(tree, "rev-parse", "--absolute-git-dir").stdout.strip()
    stamps = [0.0]
    for name in ("index", "HEAD"):
        try:
            stamps.append((Path(git_dir) / name).stat().st_mtime)
        except OSError:
            continue
    return datetime.fromtimestamp(max(stamps), tz=UTC)


def _porcelain_blocks(main_root: Path) -> list[dict[str, str]]:
    """Return ``git worktree list --porcelain`` as one mapping per tree, main first.

    Raises:
        NotARepositoryError: git cannot list the worktrees of *main_root*.
    """
    listed = _git(main_root, "worktree", "list", "--porcelain")
    if listed.returncode != 0:
        raise NotARepositoryError(f"not a git repository: {main_root}")
    blocks: list[dict[str, str]] = []
    for chunk in listed.stdout.split("\n\n"):
        block: dict[str, str] = {}
        for line in chunk.splitlines():
            key, _, value = line.partition(" ")
            block[key] = value
        if "worktree" in block:
            blocks.append(block)
    return blocks


def scan_worktrees(main_root: Path, *, integration: str | None) -> list[WorktreeRow]:
    """Return every linked worktree of the repository whose main checkout is *main_root*.

    Args:
        main_root: The main checkout of the repository.
        integration: The branch own commits are judged landed on, as
            :func:`integration_branch` names it; ``None`` judges no tree landed.

    Returns:
        One row per linked worktree, the main checkout left out, in git's order.

    Raises:
        NotARepositoryError: *main_root* is not in a git repository.
    """
    rows: list[WorktreeRow] = []
    for block in _porcelain_blocks(main_root)[1:]:
        path = Path(block["worktree"]).resolve()
        branch = block.get("branch", "").removeprefix("refs/heads/") or None
        prunable = "prunable" in block or not path.is_dir()
        if prunable:
            rows.append(
                WorktreeRow(
                    path, branch, "locked" in block, True, False, False, datetime.now(UTC), 0
                )
            )
            continue
        status = _git(path, "status", "--porcelain")
        landed, trailer_seen = _landed(path, integration)
        ignored_files, ignored_bytes = _ignored(path)
        rows.append(
            WorktreeRow(
                path=path,
                branch=branch,
                locked="locked" in block,
                prunable=False,
                dirty=status.returncode != 0 or bool(status.stdout.strip()),
                landed=landed,
                last_activity=_last_activity(path),
                size_bytes=apparent_bytes(path),
                trailer_seen=trailer_seen,
                ignored_files=ignored_files,
                ignored_bytes=ignored_bytes,
            )
        )
    return rows


def _judge(row: WorktreeRow, now: datetime) -> GcReason | tuple[str, ...]:
    """Return the reap reason of a registered tree, or the flags that keep it."""
    if row.prunable:
        return "prunable"
    flags = tuple(
        flag
        for flag, raised in (
            ("dirty", row.dirty),
            ("unlanded", not row.landed),
            ("trailer-landed", row.trailer_seen),
            ("ignored", row.ignored_files > 0),
            ("locked", row.locked),
        )
        if raised
    )
    if flags:
        return flags
    if row.branch is not None:
        return "landed"
    # A detached tree is scratch: it lives out its time to live even when clean.
    return "expired" if now - row.last_activity > SCRATCH_TTL else ("scratch",)


def plan_worktree_gc(main_root: Path, *, now: datetime) -> GcPlan:
    """Return what a garbage collection under ``<main_root>/.ea/worktrees/`` would reap.

    Args:
        main_root: The main checkout of the repository.
        now: The instant scratch expiry is measured against.

    Returns:
        The plan: landed, expired and prunable registered trees, unregistered
        directories and leftover files as candidates; every other tree kept.

    Raises:
        NotARepositoryError: *main_root* is not in a git repository.
    """
    root = (main_root / WORKTREES_ROOT).resolve()
    plan = GcPlan(integration=integration_branch(main_root))
    registered: set[Path] = set()
    for row in scan_worktrees(main_root, integration=plan.integration):
        registered.add(row.path)
        if not row.path.is_relative_to(root):
            continue
        verdict = _judge(row, now)
        if isinstance(verdict, tuple):
            plan.kept.append(KeptTree(row.path, verdict, row.ignored_files, row.ignored_bytes))
            continue
        # The operator pastes the line into a shell, so the path is quoted.
        remove = (
            f"git worktree remove {shlex.quote(str(row.path))}" if verdict == "landed" else None
        )
        plan.candidates.append(GcCandidate(row.path, verdict, row.size_bytes, remove))
    if root.is_dir():
        for entry in sorted(root.iterdir()):
            resolved = entry.resolve()
            if resolved in registered:
                continue
            reason: GcReason = "unregistered" if entry.is_dir() else "leftover"
            plan.candidates.append(GcCandidate(resolved, reason, apparent_bytes(entry)))
    plan.candidates.sort(key=lambda candidate: candidate.path)
    return plan


__all__ = [
    "FALLBACK_INTEGRATION",
    "REBUILDABLE",
    "SCRATCH_TTL",
    "WORKTREES_ROOT",
    "GcCandidate",
    "GcPlan",
    "GcReason",
    "KeptTree",
    "NotARepositoryError",
    "WorktreeRow",
    "apparent_bytes",
    "integration_branch",
    "plan_worktree_gc",
    "scan_worktrees",
]
