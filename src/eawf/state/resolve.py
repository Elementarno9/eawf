"""Pure resolver returning the active ``.ea/state.json`` path *and* the reason.

Same precedence as :func:`eawf.cli.scope.resolve_state_path` (``EA_STATE`` >
``-w/--workspace`` > pwd-upward) but, instead of opaquely returning a path,
also reports *why* the resolver picked that path. The reason string is part of
the public CLI contract emitted by ``eawf state resolve``.

The resolver does **not** raise on a missing pwd-upward state — it returns the
candidate ``cwd / .ea / state.json`` with reason ``"pwd_upward"`` so callers
can distinguish "no state on disk" from "lookup failed". CLI commands that
require an existing state must verify ``path.exists()`` themselves.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

REASON_ENV: str = "env"
REASON_WORKSPACE_FLAG: str = "workspace_flag"
REASON_PWD_UPWARD: str = "pwd_upward"


def resolve_with_reason(
    workspace: Path | None,
    env: os._Environ[str] | None = None,
) -> tuple[Path, str]:
    """Return the resolved state path *and* the reason it was selected.

    Args:
        workspace: Optional workspace root from ``-w/--workspace``. Used only
            when ``EA_STATE`` is unset.
        env: Optional environment mapping. Defaults to :data:`os.environ`.
            Tests inject a custom dict here to avoid global mutation.

    Returns:
        A two-tuple ``(path, reason)`` where ``reason`` is one of:

        - ``"env"`` — ``EA_STATE`` was set; *path* is its value verbatim.
        - ``"workspace_flag"`` — ``EA_STATE`` unset, ``workspace`` provided;
          *path* = ``workspace / .ea / state.json``.
        - ``"pwd_upward"`` — neither override was given; the resolver walked
          upward from :func:`pathlib.Path.cwd`. If no candidate exists on
          disk the resolver still returns ``cwd / .ea / state.json`` so the
          caller can distinguish "no state file" from "lookup error".
    """
    environ = env if env is not None else os.environ
    raw_env = environ.get("EA_STATE")
    if raw_env:
        logger.debug(f"resolve_with_reason env-hit: {raw_env}")
        return Path(raw_env), REASON_ENV
    if workspace is not None:
        candidate = Path(workspace) / ".ea" / "state.json"
        logger.debug(f"resolve_with_reason workspace-flag: {candidate}")
        return candidate, REASON_WORKSPACE_FLAG
    cur = Path.cwd().resolve()
    for directory in [cur, *cur.parents]:
        target = directory / ".ea" / "state.json"
        if target.exists():
            logger.debug(f"resolve_with_reason pwd-upward hit: {target}")
            return target, REASON_PWD_UPWARD
    fallback = cur / ".ea" / "state.json"
    logger.debug(f"resolve_with_reason pwd-upward fallback: {fallback}")
    return fallback, REASON_PWD_UPWARD
