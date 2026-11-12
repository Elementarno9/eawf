"""Run one git command while holding the state document lock.

pre-commit stashes the unstaged files of a work tree, checks the tree out from
the index, runs its hooks and then restores the stash. A daemon write to the
state document that lands in that window is overwritten by the restore. A git
hook cannot close the window, because the stash happens before any hook runs,
so the lock is taken here, around the whole git process: every daemon document
session waits until git has exited.

git and its hooks do not inherit the lock: the lock file is opened
non-inheritable, and a second open of it blocks. A hook that opened a document
session itself would wait for this hold and fail with ``LockTimeout``; a lint
hook that only reads files never takes the lock.
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from eawf.kernel.layout import EA_DIRNAME
from eawf.platform.subprocess_detach import no_window_kwargs
from eawf.runtime.daemon.epoch2_root import DOCUMENT_LOCK_NAME, LOCK_LOCATOR
from eawf.runtime.lock import portalock

logger = logging.getLogger(__name__)

#: How long to wait for the document lock before git starts. A daemon session
#: holds it for seconds at most; the margin covers a busy daemon under load.
DOCUMENT_HOLD_TIMEOUT_SECONDS: Final = 120.0


def document_lock_target(repo_root: Path) -> Path:
    """Return the path whose sibling lock file is the state document lock.

    Args:
        repo_root: The main checkout of the repository.

    Returns:
        The lock target the daemon's document sessions lock.
    """
    return repo_root / EA_DIRNAME / LOCK_LOCATOR / DOCUMENT_LOCK_NAME


def run_git_holding_document_lock(repo_root: Path, git_args: Sequence[str]) -> int:
    """Run ``git`` with *git_args* while holding the state document lock.

    The lock is taken before git starts and released only once git has exited.
    git runs in the current working directory with the inherited environment,
    so its arguments mean what they mean on a plain command line. However
    many interrupts arrive, the call still waits for git to exit, which lets
    pre-commit restore its stash under the lock, and then raises one
    interrupt.

    Args:
        repo_root: The main checkout of the repository whose ``.ea`` tree
            holds the lock.
        git_args: The arguments after ``git``, the subcommand first.

    Returns:
        git's exit status, unchanged.

    Raises:
        FileNotFoundError: *repo_root* holds no ``.ea`` tree, or no ``git``
            executable is on the path.
        LockTimeout: The lock stayed held for
            :data:`DOCUMENT_HOLD_TIMEOUT_SECONDS`.
        KeyboardInterrupt: The wait for git was interrupted; git has exited.
    """
    if not (repo_root / EA_DIRNAME).is_dir():
        raise FileNotFoundError(f"{repo_root} holds no {EA_DIRNAME} tree to lock")
    command = ["git", *git_args]
    requested = time.monotonic()
    with portalock.acquire(document_lock_target(repo_root), timeout=DOCUMENT_HOLD_TIMEOUT_SECONDS):
        acquired = time.monotonic()
        process = subprocess.Popen(command, **no_window_kwargs())
        interrupted = False
        try:
            while True:
                try:
                    status = process.wait()
                    break
                except KeyboardInterrupt:
                    # The terminal sends every interrupt to git as well; pre-commit
                    # then restores its stash, which must still happen under the
                    # lock, so the wait goes on however often the operator presses.
                    interrupted = True
            if interrupted:
                raise KeyboardInterrupt
        finally:
            logger.info(
                f"document_hold git={git_args[0] if git_args else '-'} "
                f"returncode={process.returncode} "
                f"wait_ms={(acquired - requested) * 1000:.0f} "
                f"hold_ms={(time.monotonic() - acquired) * 1000:.0f}"
            )
    return status


__all__ = [
    "DOCUMENT_HOLD_TIMEOUT_SECONDS",
    "document_lock_target",
    "run_git_holding_document_lock",
]
