"""Resolve the active ``.ea/state.json`` path for CLI handlers.

Precedence, highest first (per ``docs/architecture/cli-surface.md``):

1. ``EA_STATE`` environment variable (absolute or relative file path).
2. ``-w / --workspace`` flag from :class:`eawf.cli.flags.GlobalFlags` — joined
   with the literal ``.ea/state.json`` suffix.
3. Pwd-upward walk: starting at :func:`pathlib.Path.cwd` and ascending through
   each parent, return the first ``<dir>/.ea/state.json`` that exists on disk.

If none of those resolve, raise :class:`FileNotFoundError`. CLI handlers
typically catch the exception and re-raise as a :class:`eawf.cli.errors.NotFound`
to surface the canonical exit code.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def resolve_state_path(workspace: Path | None) -> Path:
    """Return the resolved ``.ea/state.json`` path for the current invocation.

    Args:
        workspace: Optional workspace root from ``-w / --workspace``. When set
            and ``EA_STATE`` is unset, the resolver appends ``.ea/state.json``
            to it without checking the path actually exists. Callers that need
            a hard existence check must perform it themselves.

    Raises:
        FileNotFoundError: When no candidate is found via the three-tier
            precedence chain.
    """
    env = os.environ.get("EA_STATE")
    if env:
        logger.debug(f"resolve_state_path env-hit: {env}")
        return Path(env)
    if workspace is not None:
        candidate = Path(workspace) / ".ea" / "state.json"
        logger.debug(f"resolve_state_path workspace-flag: {candidate}")
        return candidate
    cur = Path.cwd().resolve()
    for directory in [cur, *cur.parents]:
        target = directory / ".ea" / "state.json"
        if target.exists():
            logger.debug(f"resolve_state_path pwd-upward hit: {target}")
            return target
    raise FileNotFoundError(
        "No .ea/state.json found upward from cwd; pass -w or set EA_STATE",
    )
