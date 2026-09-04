"""Checkpoint-commit cadence for coherent scope closes.

``vcs.checkpoint_requires_commit`` decides whether a coherent scope close
(iter or phase) must back its ``--checkpoint`` ref with a commit that
actually exists. The cadence validates the *one* commit a close names and
never demands a per-wave pin: a batched close lands several waves under a
single ``state:`` bookkeeping commit that carries no per-wave proof, which
is also why ``tools/commit_prefix_lint.py`` exempts ``state:`` commits from
the CLAIMED-wave-proof branch.

The cadence stays inert -- it returns no blocker -- whenever it cannot
answer its own question honestly: no repository root in hand, no git work
tree under that root, or no usable ``git`` binary. Refusing a close because
the probe failed would trade a real defect for a tooling outage.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Annotated, Final

from pydantic import Field, TypeAdapter

from eawf.kernel.config.defaults import BUILT_IN_DEFAULTS
from eawf.platform.subprocess_detach import detached_subprocess_kwargs
from eawf.runtime.vcs.coauthor import VcsConfig

logger = logging.getLogger(__name__)

DEFAULT_CHECKPOINT_REQUIRES_COMMIT: Final[bool] = bool(
    BUILT_IN_DEFAULTS["vcs"]["checkpoint_requires_commit"]
)

#: Read-only ``git rev-parse`` probes return in milliseconds on a warm
#: checkout; the headroom absorbs a cold index on a slow file system.
_GIT_TIMEOUT_SECONDS: Final[float] = 10.0

_CADENCE_ADAPTER: Final[TypeAdapter[bool]] = TypeAdapter(Annotated[bool, Field(strict=True)])


def requires_checkpoint_commit(config: VcsConfig) -> bool:
    """Return whether the configured cadence gates a close on its checkpoint commit."""
    return config.checkpoint_requires_commit


def resolve_checkpoint_cadence(repo_root: Path | None) -> bool:
    """Resolve ``vcs.checkpoint_requires_commit`` from the layered config.

    Args:
        repo_root: Repository root supplying the layered configuration, or
            ``None`` when the caller holds no root.

    Returns:
        ``False`` when *repo_root* is ``None`` -- with no repository in hand
        there is no history to answer "does this checkpoint commit exist",
        so the cadence cannot be evaluated and must not refuse. Otherwise
        the resolved leaf, falling back to the built-in default when no
        layer declares it.

    Raises:
        pydantic.ValidationError: When the resolved leaf is not a bool.
    """
    if repo_root is None:
        return False
    from eawf.kernel.config.layered import get_dotted, merge_config

    merged, _sources = merge_config(workspace=repo_root, repo=repo_root)
    try:
        raw = get_dotted(merged, "vcs.checkpoint_requires_commit")
    except KeyError:
        return DEFAULT_CHECKPOINT_REQUIRES_COMMIT
    return _CADENCE_ADAPTER.validate_python(raw)


def _run_git(args: list[str], *, repo_root: Path) -> subprocess.CompletedProcess[str] | None:
    """Run one detached read-only git command; ``None`` when the probe itself fails."""
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
            **detached_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired, FileNotFoundError, OSError:
        return None


def checkpoint_commit_exists(ref: str, *, repo_root: Path) -> bool | None:
    """Return whether *ref* resolves to a commit under *repo_root*.

    Args:
        ref: Any git commit-ish (SHA, prefix, tag, branch).
        repo_root: Directory the probe runs in.

    Returns:
        ``True`` / ``False`` when git answered, and ``None`` when the probe
        could not run at all (no git binary, no work tree, timeout) so the
        caller can stay inert instead of inventing a verdict.
    """
    work_tree = _run_git(["rev-parse", "--is-inside-work-tree"], repo_root=repo_root)
    if work_tree is None or work_tree.returncode != 0:
        return None
    probe = _run_git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], repo_root=repo_root)
    if probe is None:
        return None
    return probe.returncode == 0


def checkpoint_commit_blocker(
    *,
    scope_id: str,
    checkpoint_commit: str | None,
    repo_root: Path | None,
) -> str | None:
    """Return the close-refusal text for *checkpoint_commit*, or ``None`` to proceed.

    Args:
        scope_id: Iter or phase id the close is closing, used in the text.
        checkpoint_commit: The checkpoint ref the close names, or ``None``
            when the close names none.
        repo_root: Repository root supplying both the cadence leaf and the
            git history the ref is verified against.

    Returns:
        ``None`` when the close may proceed: no ref named, cadence off, or
        the ref resolves. Otherwise the operator-facing refusal text.
    """
    if checkpoint_commit is None:
        return None
    if repo_root is None or not resolve_checkpoint_cadence(repo_root):
        return None
    ref = checkpoint_commit.strip()
    if not ref:
        return (
            f"{scope_id} close names a blank checkpoint commit; "
            "vcs.checkpoint_requires_commit needs a real commit ref"
        )
    exists = checkpoint_commit_exists(ref, repo_root=repo_root)
    if exists is None:
        logger.warning(
            f"checkpoint_cadence scope={scope_id} ref={ref} "
            "outcome=unprobeable reason=no_git_work_tree"
        )
        return None
    if not exists:
        return (
            f"{scope_id} checkpoint commit {ref!r} does not exist; "
            "vcs.checkpoint_requires_commit refuses the close until it does"
        )
    return None


__all__ = [
    "DEFAULT_CHECKPOINT_REQUIRES_COMMIT",
    "checkpoint_commit_blocker",
    "checkpoint_commit_exists",
    "requires_checkpoint_commit",
    "resolve_checkpoint_cadence",
]
