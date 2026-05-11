"""Thin :mod:`subprocess` wrappers around ``git`` for the worktree subsystem.

Each helper invokes a single ``git`` subcommand with ``capture_output=True``,
maps non-zero exit / timeout / missing-binary into the canonical
:class:`~eawf.cli.errors.CliError` taxonomy, and returns the stripped stdout
to the caller (or ``None`` when the call is a write).

The structure mirrors :func:`eawf.cli.commands.clone_repo._git_clone`:

- :func:`shutil.which` check up-front maps to
  :class:`~eawf.cli.errors.InstrumentMissing` (exit 6).
- :class:`subprocess.TimeoutExpired` maps to
  :class:`~eawf.cli.errors.IntegrityViolation` (exit 8). A timeout is a
  transient hung-git symptom, not a sibling-lock-held condition; mapping
  it to ``LockConflict`` would lie to operators about the failure mode.
- Non-zero exit is dispatched per-helper, with stderr-marker matching
  for the well-known git error strings (``"fatal: not a git repository"``,
  ``"fatal: invalid reference"``, ``"fatal: already a working tree"``).

Default timeout is 30 s for fast subcommands (``branch``, ``status``,
``rev-parse``) and 60 s for ``worktree add``/``cherry-pick``/``rebase``
which can do disk work proportional to repo size.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from eawf.cli import errors as cli_errors

logger = logging.getLogger(__name__)

# Subcommand timeouts (seconds). Tuned for v0.1: a clean checkout of the
# eawf repo runs all of these under 5 s; the headroom absorbs slow file
# systems and cold caches.
_FAST_TIMEOUT: float = 30.0
_SLOW_TIMEOUT: float = 60.0


def _ensure_git() -> None:
    """Raise :class:`InstrumentMissing` if ``git`` is not on PATH."""
    if shutil.which("git") is None:
        raise cli_errors.InstrumentMissing(
            "git executable not found on PATH; install git before using eawf worktree"
        )


def _run(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = _FAST_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    """Invoke *args* via :func:`subprocess.run`, mapping timeout to IntegrityViolation."""
    _ensure_git()
    logger.info(f"_run: invoking {args} cwd={cwd} timeout={timeout}")
    try:
        return subprocess.run(
            args,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise cli_errors.IntegrityViolation(
            f"git command timed out after {timeout}s: {' '.join(args)}"
        ) from exc


def repo_root(start: Path) -> Path:
    """Return the repository root that *start* lies inside.

    Raises :class:`NotFound` if *start* is not in a git working tree.
    """
    res = _run(["git", "-C", str(start), "rev-parse", "--show-toplevel"])
    if res.returncode != 0:
        raise cli_errors.NotFound(
            f"not a git repository at {start}: {(res.stderr or '').strip() or 'unknown'}"
        )
    return Path(res.stdout.strip())


def current_branch(repo: Path) -> str:
    """Return the current HEAD branch of *repo*.

    Raises :class:`InvalidInput` when HEAD is detached (no symbolic ref).
    """
    res = _run(["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"])
    if res.returncode != 0:
        stderr = (res.stderr or "").strip()
        raise cli_errors.InvalidInput(
            f"worktree create requires a non-detached HEAD; got: {stderr or 'unknown'}"
        )
    return res.stdout.strip()


def branch_exists(repo: Path, name: str) -> bool:
    """Return ``True`` iff a local branch named *name* exists in *repo*."""
    res = _run(["git", "-C", str(repo), "branch", "--list", name])
    if res.returncode != 0:
        # `git branch --list` only fails for invalid argv; treat as "no".
        logger.warning(f"branch_exists rc={res.returncode} stderr={res.stderr!r}")
        return False
    # Output is one line per match; non-empty means the branch exists.
    return bool(res.stdout.strip())


def worktree_add(
    repo: Path,
    *,
    branch: str,
    path: Path,
    base: str,
) -> None:
    """Run ``git worktree add -b <branch> <path> <base>``.

    Maps git's well-known error strings to the canonical taxonomy:

    - ``"already a working tree"`` -> :class:`LockConflict` (exit 5).
    - ``"invalid reference"`` -> :class:`InvalidInput` (exit 3).
    - ``"not a git repository"`` -> :class:`NotFound` (exit 2).
    - default -> :class:`IntegrityViolation` (exit 8).
    """
    args = [
        "git",
        "-C",
        str(repo),
        "worktree",
        "add",
        "--quiet",
        "-b",
        branch,
        str(path),
        base,
    ]
    res = _run(args, timeout=_SLOW_TIMEOUT)
    if res.returncode == 0:
        return
    stderr = (res.stderr or "").strip()
    lower = stderr.lower()
    if "already a working tree" in lower or "is already used" in lower:
        raise cli_errors.LockConflict(f"git worktree add: {stderr or 'already in use'}")
    if "invalid reference" in lower or "fatal: not a valid ref" in lower:
        raise cli_errors.InvalidInput(f"git worktree add: {stderr or 'invalid reference'}")
    if "not a git repository" in lower:
        raise cli_errors.NotFound(f"git worktree add: {stderr or 'not a git repository'}")
    raise cli_errors.IntegrityViolation(
        f"git worktree add failed (rc={res.returncode}): {stderr or 'unknown'}"
    )


def worktree_remove(repo: Path, *, path: Path, force: bool = False) -> None:
    """Run ``git worktree remove [--force] <path>``.

    Removes both the working directory and the ``.git/worktrees/<name>``
    registry entry in one invocation.
    """
    args = ["git", "-C", str(repo), "worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(path))
    res = _run(args, timeout=_SLOW_TIMEOUT)
    if res.returncode != 0:
        stderr = (res.stderr or "").strip()
        raise cli_errors.IntegrityViolation(
            f"git worktree remove failed (rc={res.returncode}): {stderr or 'unknown'}"
        )


def worktree_list(repo: Path) -> list[dict[str, str]]:
    """Return ``git worktree list --porcelain`` parsed into one dict per entry.

    Each dict carries the keys git emits for that entry — at minimum
    ``worktree`` (path) and usually ``HEAD`` and ``branch``.
    """
    res = _run(["git", "-C", str(repo), "worktree", "list", "--porcelain"])
    if res.returncode != 0:
        stderr = (res.stderr or "").strip()
        raise cli_errors.IntegrityViolation(
            f"git worktree list failed (rc={res.returncode}): {stderr or 'unknown'}"
        )
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in (res.stdout or "").splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        # Each non-empty line is "<key> <value>" or just a flag like "bare".
        parts = line.split(None, 1)
        key = parts[0]
        value = parts[1] if len(parts) > 1 else ""
        current[key] = value
    if current:
        entries.append(current)
    return entries


def status_porcelain(path: Path) -> list[str]:
    """Return ``git status --porcelain`` lines (one per dirty entry).

    An empty list means the working tree is clean. Any non-empty line
    flags a dirty entry (modified, untracked, conflicted, etc.).
    """
    res = _run(["git", "-C", str(path), "status", "--porcelain"])
    if res.returncode != 0:
        stderr = (res.stderr or "").strip()
        raise cli_errors.IntegrityViolation(
            f"git status failed (rc={res.returncode}): {stderr or 'unknown'}"
        )
    return [line for line in (res.stdout or "").splitlines() if line.strip()]


def rev_list(repo: Path, *, range_spec: str) -> list[str]:
    """Return commits in *range_spec*, oldest first, as short SHAs.

    Used to enumerate commits to cherry-pick: ``range_spec`` is
    ``"<target>..<wt_branch>"`` (i.e., commits in wt_branch not in target).
    """
    res = _run(
        ["git", "-C", str(repo), "rev-list", "--reverse", range_spec],
        timeout=_SLOW_TIMEOUT,
    )
    if res.returncode != 0:
        stderr = (res.stderr or "").strip()
        if "unknown revision" in stderr.lower() or "bad revision" in stderr.lower():
            raise cli_errors.InvalidInput(f"git rev-list: {stderr or range_spec}")
        raise cli_errors.IntegrityViolation(
            f"git rev-list failed (rc={res.returncode}): {stderr or 'unknown'}"
        )
    return [line.strip() for line in (res.stdout or "").splitlines() if line.strip()]


def cherry_pick(repo: Path, *, sha: str) -> tuple[bool, str]:
    """Run ``git cherry-pick <sha>``.

    Returns a tuple ``(clean, detail)``. ``clean`` is ``True`` on
    rc=0; ``False`` indicates a conflict was reported and stopped
    the picker. ``detail`` is the stderr (or stdout if stderr is
    empty) for caller-facing diagnostics.

    Special case: when the picked commit is "now empty" (e.g., target
    already has the same content as the source), git rc=1's with a
    "now empty" message. We auto-skip via ``cherry-pick --skip`` and
    return ``(True, "")`` because the loop's job is done — the parent
    has the equivalent content.

    Hard failures unrelated to a conflict (invalid sha, etc.) raise
    :class:`InvalidInput` / :class:`IntegrityViolation` per the same
    pattern as :func:`worktree_add`.
    """
    res = _run(["git", "-C", str(repo), "cherry-pick", sha], timeout=_SLOW_TIMEOUT)
    if res.returncode == 0:
        return True, ""
    stderr = (res.stderr or "").strip()
    stdout = (res.stdout or "").strip()
    detail = stderr or stdout
    lower = detail.lower()
    if "now empty" in lower or "previous cherry-pick is now empty" in lower:
        skip = _run(
            ["git", "-C", str(repo), "cherry-pick", "--skip"],
            timeout=_SLOW_TIMEOUT,
        )
        if skip.returncode == 0:
            return True, ""
        # If the skip itself failed, fall through to the IntegrityViolation
        # path below — we cannot leave the repo mid-pick.
        detail = ((skip.stderr or "") + (skip.stdout or "")).strip() or detail
    if "after resolving the conflicts" in lower or "could not apply" in lower:
        return False, detail
    if "bad revision" in lower or "unknown revision" in lower:
        raise cli_errors.InvalidInput(f"git cherry-pick: {detail or 'invalid sha'}")
    raise cli_errors.IntegrityViolation(
        f"git cherry-pick failed (rc={res.returncode}): {detail or 'unknown'}"
    )


def cherry_pick_continue(repo: Path) -> tuple[bool, str]:
    """Run ``git cherry-pick --continue``. Same return shape as :func:`cherry_pick`.

    Special case: when the resolution makes the picked commit empty
    (operator's resolution converged on a content git already has), git
    rc=1's with a "now empty" message. We treat that as a successful
    continue by issuing ``--skip`` to advance past the empty commit.
    """
    res = _run(
        ["git", "-C", str(repo), "cherry-pick", "--continue"],
        timeout=_SLOW_TIMEOUT,
    )
    if res.returncode == 0:
        return True, ""
    detail = ((res.stderr or "") + (res.stdout or "")).strip()
    lower = detail.lower()
    if "now empty" in lower or "previous cherry-pick is now empty" in lower:
        # Skip the empty commit; the resolution was idempotent w.r.t. parent.
        skip = _run(
            ["git", "-C", str(repo), "cherry-pick", "--skip"],
            timeout=_SLOW_TIMEOUT,
        )
        if skip.returncode == 0:
            return True, ""
        return False, ((skip.stderr or "") + (skip.stdout or "")).strip()
    return False, detail


def cherry_pick_abort(repo: Path) -> None:
    """Run ``git cherry-pick --abort`` to reset the in-progress state."""
    res = _run(["git", "-C", str(repo), "cherry-pick", "--abort"])
    if res.returncode != 0:
        stderr = (res.stderr or "").strip()
        raise cli_errors.IntegrityViolation(
            f"git cherry-pick --abort failed (rc={res.returncode}): {stderr or 'unknown'}"
        )


def rebase(repo: Path, *, target: str) -> tuple[bool, str]:
    """Run ``git rebase <target>`` from *repo* (a worktree path)."""
    res = _run(["git", "-C", str(repo), "rebase", target], timeout=_SLOW_TIMEOUT)
    if res.returncode == 0:
        return True, ""
    detail = ((res.stderr or "") + (res.stdout or "")).strip()
    lower = detail.lower()
    if "could not apply" in lower or "merge conflict" in lower or "needs merge" in lower:
        return False, detail
    if "unknown revision" in lower or "invalid upstream" in lower:
        raise cli_errors.InvalidInput(f"git rebase: {detail or target}")
    raise cli_errors.IntegrityViolation(
        f"git rebase failed (rc={res.returncode}): {detail or 'unknown'}"
    )


def rebase_continue(repo: Path) -> tuple[bool, str]:
    """Run ``git rebase --continue``. Same return shape as :func:`rebase`."""
    res = _run(["git", "-C", str(repo), "rebase", "--continue"], timeout=_SLOW_TIMEOUT)
    if res.returncode == 0:
        return True, ""
    detail = ((res.stderr or "") + (res.stdout or "")).strip()
    return False, detail


def rebase_abort(repo: Path) -> None:
    """Run ``git rebase --abort``."""
    res = _run(["git", "-C", str(repo), "rebase", "--abort"])
    if res.returncode != 0:
        stderr = (res.stderr or "").strip()
        raise cli_errors.IntegrityViolation(
            f"git rebase --abort failed (rc={res.returncode}): {stderr or 'unknown'}"
        )


def merge_ff_only(repo: Path, *, source: str) -> str:
    """Run ``git merge --ff-only <source>``. Returns the new HEAD sha."""
    res = _run(
        ["git", "-C", str(repo), "merge", "--ff-only", source],
        timeout=_SLOW_TIMEOUT,
    )
    if res.returncode != 0:
        detail = ((res.stderr or "") + (res.stdout or "")).strip()
        if "non-fast-forward" in detail.lower() or "not possible" in detail.lower():
            raise cli_errors.IntegrityViolation(
                f"git merge --ff-only refused (non-fast-forward): {detail or source}"
            )
        raise cli_errors.IntegrityViolation(
            f"git merge --ff-only failed (rc={res.returncode}): {detail or 'unknown'}"
        )
    head = head_sha(repo)
    return head


def head_sha(repo: Path) -> str:
    """Return the short HEAD sha for *repo*."""
    res = _run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"])
    if res.returncode != 0:
        stderr = (res.stderr or "").strip()
        raise cli_errors.IntegrityViolation(
            f"git rev-parse HEAD failed (rc={res.returncode}): {stderr or 'unknown'}"
        )
    return res.stdout.strip()


def branch_delete(repo: Path, *, name: str) -> bool:
    """Run ``git branch -D <name>``. Returns ``True`` on success.

    Failure is non-fatal: the caller treats branch deletion as a
    courtesy, not a contract.
    """
    res = _run(["git", "-C", str(repo), "branch", "-D", name])
    if res.returncode != 0:
        logger.warning(
            f"branch_delete: rc={res.returncode} name={name} stderr={(res.stderr or '').strip()!r}"
        )
        return False
    return True


def cherry_pick_in_progress(repo: Path) -> bool:
    """Return ``True`` iff ``<repo>/.git/CHERRY_PICK_HEAD`` exists.

    git records the in-progress cherry-pick by creating that file. We
    use the presence as a resume-mode signal in :func:`merge_back.continue_`.
    """
    return (repo / ".git" / "CHERRY_PICK_HEAD").exists()


def rebase_in_progress(worktree: Path) -> bool:
    """Return ``True`` iff a rebase is in progress in *worktree*.

    git records an in-progress rebase under ``.git/rebase-merge/`` or
    ``.git/rebase-apply/`` (depending on strategy). Either signals
    "resume needed".
    """
    git_dir = worktree / ".git"
    if git_dir.is_file():
        # git worktree creates a file containing "gitdir: <path>" for sub-worktrees.
        try:
            content = git_dir.read_text(encoding="utf-8").strip()
        except OSError:
            return False
        if content.startswith("gitdir:"):
            if ":" not in content:
                # Defensive: ``startswith("gitdir:")`` already implies a
                # colon, but a malformed file (no second segment) would
                # crash on the unconditional split below.
                return False
            actual = Path(content.split(":", 1)[1].strip())
            if not actual.is_absolute():
                actual = (worktree / actual).resolve()
            return (actual / "rebase-merge").exists() or (actual / "rebase-apply").exists()
    return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()


__all__ = [
    "branch_delete",
    "branch_exists",
    "cherry_pick",
    "cherry_pick_abort",
    "cherry_pick_continue",
    "cherry_pick_in_progress",
    "current_branch",
    "head_sha",
    "merge_ff_only",
    "rebase",
    "rebase_abort",
    "rebase_continue",
    "rebase_in_progress",
    "repo_root",
    "rev_list",
    "status_porcelain",
    "worktree_add",
    "worktree_list",
    "worktree_remove",
]
