"""``git`` statusline module — branch + dirty marker via ``git`` subprocess.

Emits ``git:<branch>`` (clean) or ``git:<branch>*`` (dirty). Detached HEAD
renders ``git:HEAD@<sha7>``. When ``git`` is missing or any subprocess
invocation fails, the segment degrades to ``git:-`` with
``status="missing"`` so the orchestrator's contract holds (modules never
crash; missing instruments degrade to a single ``-``).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from eawf.render.statusline import StatuslineSegment

logger = logging.getLogger(__name__)


_GIT_TIMEOUT: float = 2.0
"""Hard cap per git invocation. Statusline must stay <100 ms cold; a 2 s
timeout per call is far above the happy path but bounds a hung daemon.
"""


def _git_cwd_for(state_path: Path | None) -> Path:
    """Return the directory git should operate inside.

    Uses the parent of the resolved ``.ea`` directory when available
    (``state_path.parent.parent``) so ``git`` walks up from the workspace
    root rather than the runtime cwd. Falls back to :func:`pathlib.Path.cwd`
    when no state path was resolved.
    """
    if state_path is None:
        return Path.cwd()
    ea_dir = state_path.parent
    return ea_dir.parent if ea_dir.name == ".ea" else ea_dir


def _run_git(args: list[str], cwd: Path) -> str | None:
    """Return stripped git stdout, or ``None`` on any failure."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.debug(f"statusline_modules.git: {' '.join(args)!r} failed: {exc}")
        return None
    return proc.stdout.strip()


def build(claude_payload: dict[str, Any], state_path: Path | None) -> StatuslineSegment:
    """Return the ``git:<branch>[*]`` segment.

    Args:
        claude_payload: Decoded Claude stdin JSON. Read for ``cwd`` so the
            git call uses the session's working directory when the state
            resolver returns ``None``.
        state_path: Resolved ``.ea/state.json`` path. Used to derive the
            workspace root passed to ``git -C``.

    Returns:
        A :class:`StatuslineSegment` with ``module="git"``. Status is
        ``ok`` for a clean tree, ``warn`` for a dirty tree, ``missing``
        when ``git`` is unavailable.
    """
    cwd_str = claude_payload.get("cwd")
    if isinstance(cwd_str, str) and cwd_str:
        try:
            cwd = Path(cwd_str)
        except TypeError, ValueError:
            cwd = _git_cwd_for(state_path)
    else:
        cwd = _git_cwd_for(state_path)
    if not cwd.exists():
        return StatuslineSegment(module="git", text="git:-", status="missing")
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    if branch is None:
        return StatuslineSegment(module="git", text="git:-", status="missing")
    if branch == "HEAD":
        sha = _run_git(["rev-parse", "--short=7", "HEAD"], cwd)
        label = f"HEAD@{sha}" if sha else "HEAD"
    else:
        label = branch
    porcelain = _run_git(["status", "--porcelain"], cwd)
    if porcelain is None:
        return StatuslineSegment(module="git", text=f"git:{label}", status="ok")
    dirty = bool(porcelain)
    suffix = "*" if dirty else ""
    if dirty:
        return StatuslineSegment(module="git", text=f"git:{label}{suffix}", status="warn")
    return StatuslineSegment(module="git", text=f"git:{label}{suffix}", status="ok")


__all__ = ["build"]
