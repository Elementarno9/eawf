"""Version-control helpers."""

from __future__ import annotations

from eawf.vcs.coauthor import (
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
    "resolve_coauthor_trailer",
]
