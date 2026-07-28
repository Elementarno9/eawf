"""Detached exact-revision workspaces for durable wave verification."""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_GIT_TIMEOUT_SECONDS = 30.0


class CloseWorkspaceError(RuntimeError):
    """Raised when an exact-revision close workspace cannot be prepared."""


@dataclass(frozen=True)
class CloseWorkspace:
    """Prepared detached worktree and its verified immutable Git facts."""

    attempt_id: str
    path: Path
    commit_sha: str
    tree_sha: str
    created: bool


def _git(
    repo_root: Path,
    *args: str,
    timeout_seconds: float = _GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run one bounded Git command against *repo_root*."""
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise CloseWorkspaceError(
            f"git command timed out after {timeout_seconds:g}s: {args[0]!r}"
        ) from exc
    except OSError as exc:
        raise CloseWorkspaceError(f"git command failed to start: {exc!s}") from exc


def _require_git_ok(result: subprocess.CompletedProcess[str], *, operation: str) -> str:
    """Return stripped stdout or raise with bounded Git detail."""
    if result.returncode == 0:
        return result.stdout.strip()
    detail = (result.stderr or result.stdout).strip()
    if len(detail) > 1000:
        detail = f"{detail[:1000]}…"
    raise CloseWorkspaceError(
        f"{operation} failed (exit={result.returncode}): {detail or 'no output'}"
    )


def resolve_exact_revision(repo_root: Path, commit_ref: str) -> tuple[str, str]:
    """Resolve *commit_ref* to canonical commit and tree SHAs."""
    commit_sha = _require_git_ok(
        _git(repo_root, "rev-parse", "--verify", f"{commit_ref}^{{commit}}"),
        operation="resolve close commit",
    )
    tree_sha = _require_git_ok(
        _git(repo_root, "rev-parse", "--verify", f"{commit_sha}^{{tree}}"),
        operation="resolve close tree",
    )
    return commit_sha, tree_sha


def workspace_path(repo_root: Path, attempt_id: str) -> Path:
    """Return the daemon-owned local worktree path for *attempt_id*."""
    if _ATTEMPT_ID_RE.fullmatch(attempt_id) is None:
        raise ValueError(f"invalid close attempt id: {attempt_id!r}")
    return repo_root / ".ea" / "worktrees" / "close" / attempt_id


def prepare_close_workspace(
    repo_root: Path,
    *,
    attempt_id: str,
    commit_ref: str,
    expected_tree_sha: str | None = None,
) -> CloseWorkspace:
    """Create or validate a detached worktree at the frozen integration SHA.

    Re-entry is idempotent. An existing path is reused only when its ``HEAD``
    and tree match the frozen facts and it is clean; otherwise the attempt is
    refused as stale/corrupt instead of silently verifying another revision.
    """
    commit_sha, tree_sha = resolve_exact_revision(repo_root, commit_ref)
    if expected_tree_sha is not None and tree_sha != expected_tree_sha:
        raise CloseWorkspaceError(
            f"close tree mismatch: expected {expected_tree_sha!r}, resolved {tree_sha!r}"
        )
    path = workspace_path(repo_root, attempt_id)
    created = False
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        _require_git_ok(
            _git(repo_root, "worktree", "add", "--detach", str(path), commit_sha),
            operation="create close worktree",
        )
        created = True
    actual_commit, actual_tree = resolve_exact_revision(path, "HEAD")
    if actual_commit != commit_sha or actual_tree != tree_sha:
        raise CloseWorkspaceError(
            f"close worktree drifted: expected {commit_sha}/{tree_sha}, "
            f"found {actual_commit}/{actual_tree}"
        )
    dirty = _require_git_ok(
        _git(path, "status", "--porcelain=v1", "--untracked-files=all"),
        operation="inspect close worktree",
    )
    if dirty:
        digest = hashlib.sha256(dirty.encode("utf-8")).hexdigest()
        raise CloseWorkspaceError(f"close worktree is dirty (status_digest={digest})")
    logger.info(
        f"prepare_close_workspace attempt={attempt_id!r} commit={commit_sha} "
        f"tree={tree_sha} created={created}"
    )
    return CloseWorkspace(
        attempt_id=attempt_id,
        path=path,
        commit_sha=commit_sha,
        tree_sha=tree_sha,
        created=created,
    )


def cleanup_close_workspace(
    repo_root: Path,
    *,
    attempt_id: str,
) -> bool:
    """Remove one daemon-owned close worktree through Git.

    Returns ``False`` when the path is already absent. No recursive filesystem
    deletion is used; Git validates that the target is one of its worktrees.
    """
    path = workspace_path(repo_root, attempt_id)
    if not path.exists():
        return False
    _require_git_ok(
        _git(repo_root, "worktree", "remove", "--force", str(path)),
        operation="remove close worktree",
    )
    logger.info(f"cleanup_close_workspace attempt={attempt_id!r} removed=True")
    return True


__all__ = [
    "CloseWorkspace",
    "CloseWorkspaceError",
    "cleanup_close_workspace",
    "prepare_close_workspace",
    "resolve_exact_revision",
    "workspace_path",
]
