"""Version-control helpers.

The re-exports load on first access, so a caller importing one light
submodule, such as :mod:`eawf.runtime.vcs.branch_name` on the hook path,
does not pay for the configuration models behind the co-author policy.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
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

_EXPORTS: dict[str, str] = {
    "CoauthorConfig": "eawf.runtime.vcs.coauthor",
    "CoauthorIdentity": "eawf.runtime.vcs.coauthor",
    "CoauthorPolicyError": "eawf.runtime.vcs.coauthor",
    "VcsConfig": "eawf.runtime.vcs.coauthor",
    "checkpoint_commit_blocker": "eawf.runtime.vcs.checkpoint",
    "checkpoint_commit_exists": "eawf.runtime.vcs.checkpoint",
    "requires_checkpoint_commit": "eawf.runtime.vcs.checkpoint",
    "resolve_checkpoint_cadence": "eawf.runtime.vcs.checkpoint",
    "resolve_coauthor_trailer": "eawf.runtime.vcs.coauthor",
}


def __getattr__(name: str) -> Any:
    """Load a re-exported name from its submodule on first access.

    Args:
        name: The attribute asked for.

    Returns:
        The re-exported object.

    Raises:
        AttributeError: *name* is not a re-exported name.
    """
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value


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
