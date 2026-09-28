"""Pure resolver returning the active ``.ea/state.json`` path *and* the reason.

Same precedence as :func:`eawf.surfaces.cli.scope.resolve_state_path` (``EA_STATE`` >
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
from collections.abc import Mapping
from pathlib import Path

logger = logging.getLogger(__name__)

REASON_ENV: str = "env"
REASON_WORKSPACE_FLAG: str = "workspace_flag"
REASON_PWD_UPWARD: str = "pwd_upward"

#: The ledger a gate harness forbids its gate command to reach. Set together
#: with :data:`GATE_SANDBOX_STATE_ENV` only by the daemon's gate runner. Named
#: outside the ``EAWF_`` family on purpose: a gate may run an older checkout
#: whose config layer reads every unreserved ``EAWF_*`` variable as an override.
GATE_LIVE_STATE_ENV: str = "EA_GATE_LIVE_STATE"

#: The throwaway snapshot of :data:`GATE_LIVE_STATE_ENV` that stands in for it.
GATE_SANDBOX_STATE_ENV: str = "EA_GATE_SANDBOX_STATE"


def fence_live_ledger(path: Path, environ: Mapping[str, str]) -> Path:
    """Return the sandbox ledger when *path* is the live ledger a gate is fenced from.

    A gate harness cannot pin every resolution to its sandbox: a gate that is
    a test suite builds its own workspaces and names them through ``-w``,
    ``EA_STATE`` and the working directory, and must reach exactly those, as
    it would outside the harness. Only a resolution that lands on the live
    ledger is swapped for its snapshot. The live ledger is the fenced path
    itself or the ``.ea`` tree that contains it, because a delivery proof
    fences a claim ledger nested under the repository's ``.ea`` while its
    checkout's upward walk reaches the repository ledger.

    Args:
        path: The ledger the precedence chain resolved.
        environ: The environment carrying the fence bindings.

    Returns:
        The sandbox ledger when *path* is fenced, else *path* unchanged.
    """
    live_raw = environ.get(GATE_LIVE_STATE_ENV)
    sandbox_raw = environ.get(GATE_SANDBOX_STATE_ENV)
    if not live_raw or not sandbox_raw:
        return path
    live = Path(live_raw).resolve()
    candidate = path.resolve()
    ledger_dir = candidate.parent
    fenced = candidate == live or (
        ledger_dir.name == ".ea" and live.parent.is_relative_to(ledger_dir)
    )
    if not fenced:
        return path
    logger.debug(f"fence_live_ledger live={str(candidate)!r} sandbox={sandbox_raw!r}")
    return Path(sandbox_raw)


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

        Under a gate harness a path that lands on the fenced live ledger is
        replaced by its sandbox snapshot (:func:`fence_live_ledger`); the
        reason still names the tier that selected it.
    """
    environ = env if env is not None else os.environ
    raw_env = environ.get("EA_STATE")
    if raw_env:
        logger.debug(f"resolve_with_reason source=env path={raw_env}")
        return fence_live_ledger(Path(raw_env), environ), REASON_ENV
    if workspace is not None:
        candidate = Path(workspace) / ".ea" / "state.json"
        logger.debug(f"resolve_with_reason source=workspace-flag path={candidate}")
        return fence_live_ledger(candidate, environ), REASON_WORKSPACE_FLAG
    cur = Path.cwd().resolve()
    for directory in [cur, *cur.parents]:
        target = directory / ".ea" / "state.json"
        if target.exists():
            logger.debug(f"resolve_with_reason source=pwd-upward path={target}")
            return fence_live_ledger(target, environ), REASON_PWD_UPWARD
    fallback = cur / ".ea" / "state.json"
    logger.debug(f"resolve_with_reason source=pwd-upward-fallback path={fallback}")
    return fence_live_ledger(fallback, environ), REASON_PWD_UPWARD
