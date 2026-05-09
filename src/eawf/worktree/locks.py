"""Worktree-registry advisory lock for concurrent ``git worktree add``.

Why a second lock layer? :func:`eawf.cli._mutation.state_transaction`
already holds ``portalock(state.json)`` for the duration of every
:class:`~eawf.state.models.WorktreeRecord` mutation, so the *state-side*
race is covered. The git-side race is separate: ``git worktree add``
allocates a new entry under ``.git/worktrees/<name>`` and git's internal
lock guards the index but does not serialise *name* allocation across
unrelated invocations. Two callers racing the same target path can both
pass git's check and clobber each other's registry entry.

The lock here is an Eä-managed sibling lock at
``.ea/locks/worktrees.lock`` (sibling-style, gitignored per AGENTS.md
rule 3). It serialises every Eä-mediated ``git worktree add``,
``git worktree remove``, and registry-listing operation. Operators
that bypass the CLI to invoke ``git worktree`` directly are outside
the contract — that path is a foot-gun by design.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from eawf.lock import portalock

logger = logging.getLogger(__name__)


def _registry_lock_path(repo_root: Path) -> Path:
    """Return the canonical lock path under ``<repo_root>/.ea/locks/``.

    The parent directory is created on demand; the lock file itself is
    materialised by :func:`portalock.acquire` and removed on release.
    """
    return repo_root / ".ea" / "locks" / "worktrees.lock"


@contextmanager
def worktree_registry_lock(
    repo_root: Path,
    *,
    timeout: float = 5.0,
) -> Iterator[None]:
    """Acquire the Eä worktree-registry advisory lock.

    The lock is a :func:`eawf.lock.portalock.acquire` on
    ``<repo_root>/.ea/locks/worktrees.lock``. Re-entrant calls in the
    same process will deadlock — ``flock`` is non-recursive — so callers
    must hold the lock at exactly one nesting level per invocation.

    Args:
        repo_root: Repository root (the directory containing ``.ea/``).
        timeout: Seconds to wait before raising :class:`LockTimeout`.

    Raises:
        LockTimeout: When the lock cannot be acquired within *timeout*.
    """
    lock_target = _registry_lock_path(repo_root)
    lock_target.parent.mkdir(parents=True, exist_ok=True)
    logger.debug(f"worktree_registry_lock: acquiring {lock_target}")
    with portalock.acquire(lock_target, timeout=timeout):
        yield
    logger.debug(f"worktree_registry_lock: released {lock_target}")


__all__ = [
    "worktree_registry_lock",
]
