"""Git reads that anchor a diff and identify a commit across a rebase.

Both helpers are thin, detached, read-only ``git`` probes that degrade
instead of raising: a gate runner falls back to a default diff base, and
a doctor repair treats a missing digest as "identity unknown".

Every probe passes ``stdin=subprocess.DEVNULL`` and the
:func:`~eawf.platform.subprocess_detach.detached_subprocess_kwargs` knobs.
Inside the live TUI's Doctor gather the parent's fd 0 is the controlling
TTY; a child sharing that terminal can provoke an escape reply the App's
stdin reader mis-parses as keypresses, so each child runs in its own
session (POSIX) or windowless console (win32).
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
from typing import TYPE_CHECKING, Final

from eawf.platform.subprocess_detach import detached_subprocess_kwargs

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS: Final[float] = 5.0

#: The diff base returned when the merge-base cannot be read; the same
#: default :func:`eawf.platform.lint._conditional.changed_files` callers
#: feed to ``git diff <base>...HEAD``.
FALLBACK_DIFF_BASE: Final[str] = "origin/main"


def _run_git(args: list[str], *, repo_root: Path | None) -> subprocess.CompletedProcess[str] | None:
    """Run one detached read-only git command; return ``None`` when it cannot start."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(repo_root) if repo_root else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
            **detached_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired, OSError:
        return None


def derive_diff_base(*, repo_root: Path | None = None) -> str:
    """Return the diff base a ``changed`` or ``touched`` gate scope compares against.

    The merge-base of ``HEAD`` and ``main`` scopes the diff to the commits
    unique to the branch. A fresh clone or a checkout without ``main``
    cannot answer, and the gate stays fail-open on
    :data:`FALLBACK_DIFF_BASE` instead.

    Args:
        repo_root: Repository working directory; defaults to the process cwd.

    Returns:
        The merge-base SHA, or :data:`FALLBACK_DIFF_BASE` when git is
        missing, the call fails or times out, or it prints nothing.
    """
    if shutil.which("git") is None:
        logger.debug("derive_diff_base git=not-on-path")
        return FALLBACK_DIFF_BASE
    out = _run_git(["merge-base", "HEAD", "main"], repo_root=repo_root)
    if out is None or out.returncode != 0:
        rc = None if out is None else out.returncode
        logger.debug(f"derive_diff_base status=failed rc={rc}")
        return FALLBACK_DIFF_BASE
    return out.stdout.strip() or FALLBACK_DIFF_BASE


def _normalise_commit_message(message: str) -> str:
    return "\n".join(line.rstrip() for line in message.strip().splitlines())


def commit_identity_digest(commit: str, *, repo_root: Path | None = None) -> str | None:
    """Hash a commit's normalised message and semantic patch, which survive a rebase.

    The ``index`` lines of the patch name blob ids that change when a
    rebase replays the commit onto another base, so they are left out.

    Args:
        commit: Any revision git resolves to one commit.
        repo_root: Repository working directory; defaults to the process cwd.

    Returns:
        ``sha256:<hex>``, or ``None`` when git cannot read the commit.
    """
    message = _run_git(["show", "-s", "--format=%B", commit], repo_root=repo_root)
    patch = _run_git(
        ["show", "--format=", "--no-ext-diff", "--no-renames", "--unified=0", commit],
        repo_root=repo_root,
    )
    if message is None or patch is None or message.returncode != 0 or patch.returncode != 0:
        return None
    semantic_lines = [
        line.rstrip() for line in patch.stdout.splitlines() if not line.startswith("index ")
    ]
    payload = (
        _normalise_commit_message(message.stdout) + "\n\x00\n" + "\n".join(semantic_lines).strip()
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


__all__ = ["FALLBACK_DIFF_BASE", "commit_identity_digest", "derive_diff_base"]
