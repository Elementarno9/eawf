"""Cwd / path-containment guard shared across runtime sandbox helpers.

A small typed helper pair that answers one question: does *child* resolve
inside *root*? Worktree creation, gate-runner sandbox wrapping, and any
future hardener that must refuse to run "outside the repo" share this
exact predicate, so it lives in one canonical home rather than duplicated
per call site.

Public API:

- :func:`is_path_inside` — boolean predicate (``True`` when contained).
- :func:`assert_cwd_inside` — raises :class:`CwdGuardError` when not
  contained; used at gate-runner boundaries that refuse to shell out
  unless the cwd is inside the repo root.

Both call :meth:`pathlib.Path.resolve` with ``strict=False`` so callers
can validate not-yet-existing paths (a freshly-computed worktree dir is
created moments later). Resolution honours absolute paths and ``..``
segments the same way :func:`pathlib.PurePath.is_relative_to` does.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class CwdGuardError(ValueError):
    """Raised when an operation refuses to run because *cwd* is outside *root*.

    The cwd guard surfaces this at the gate-runner boundary so a
    misconfigured subprocess invocation (e.g. a relative cwd that resolves
    above the repo root) fails fast rather than shelling out into an
    unintended directory.
    """


def is_path_inside(child: Path, *, root: Path) -> bool:
    """Return ``True`` when *child* resolves inside *root*.

    Both paths are resolved with ``strict=False`` so the predicate works
    against not-yet-materialised directories. Symlink traversal follows
    :meth:`pathlib.Path.resolve` semantics; the comparison itself uses
    :meth:`pathlib.PurePath.is_relative_to`.

    Args:
        child: Candidate path that should sit inside *root*.
        root: Containing directory.

    Returns:
        ``True`` when *child* equals *root* or sits under it.
    """
    child_resolved = child.resolve(strict=False)
    root_resolved = root.resolve(strict=False)
    return child_resolved.is_relative_to(root_resolved)


def assert_cwd_inside(cwd: Path, *, root: Path) -> None:
    """Refuse to proceed when *cwd* resolves outside *root*.

    The gate-runner calls this before :func:`subprocess.run` so a hostile
    or misconfigured cwd cannot escape the repo root. The message names
    both resolved paths so the operator can triage without re-resolving
    by hand.

    Args:
        cwd: The proposed working directory.
        root: The repo root *cwd* must sit inside.

    Raises:
        CwdGuardError: When *cwd* resolves outside *root*.
    """
    if is_path_inside(cwd, root=root):
        return
    cwd_resolved = cwd.resolve(strict=False)
    root_resolved = root.resolve(strict=False)
    logger.warning(f"assert_cwd_inside reject cwd={cwd_resolved!s} root={root_resolved!s}")
    raise CwdGuardError(
        f"cwd {cwd_resolved} resolves outside repo root {root_resolved}",
    )


__all__ = [
    "CwdGuardError",
    "assert_cwd_inside",
    "is_path_inside",
]
