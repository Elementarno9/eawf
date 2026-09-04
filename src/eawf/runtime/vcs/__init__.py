"""Version-control helpers."""

from __future__ import annotations

from eawf.runtime.vcs.checkpoint import (
    checkpoint_commit_blocker,
    checkpoint_commit_exists,
    requires_checkpoint_commit,
    resolve_checkpoint_cadence,
)
from eawf.runtime.vcs.coauthor import (
    CoauthorConfig,
    CoauthorIdentity,
    CoauthorPolicyError,
    VcsConfig,
    resolve_coauthor_trailer,
)

__all__ = [
    "CoauthorConfig",
    "CoauthorIdentity",
    "CoauthorPolicyError",
    "VcsConfig",
    "checkpoint_commit_blocker",
    "checkpoint_commit_exists",
    "requires_checkpoint_commit",
    "resolve_checkpoint_cadence",
    "resolve_coauthor_trailer",
]
